#!/usr/bin/env python3
"""Maintain exact ROC Zabbix Server allowlist, using the existing read-only Docker view.

Deliberately never authorizes a subnet. No Docker mutation or application secrets.
This tool requires an explicit --apply and is not installed by this source file.
"""
from __future__ import annotations

import argparse
import http.client
import ipaddress
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile

SOCKET = "/run/andy-zabbix-docker/docker.sock"
CONFIG = "/etc/zabbix/zabbix_agent2.conf"
SERVER = "/usr/sbin/zabbix_agent2"
PROJECT = "attention-router-roc-e2e"
SERVICE = "roc-zabbix-server"
NETWORK = "andy-roc-edge"
TRUSTED_EDGE = ipaddress.ip_network("192.168.16.0/20")
LINE = re.compile(r"^Server=([^\r\n]+)$", re.MULTILINE)


class DockerView(http.client.HTTPConnection):
    def __init__(self, path=SOCKET):
        super().__init__("localhost", timeout=5)
        self.socket_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(self.socket_path)


def view(path, socket_path=SOCKET):
    import json
    connection = DockerView(socket_path)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError("read-only Docker view unavailable")
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Docker view exceeded response budget")
        return json.loads(raw)
    finally:
        connection.close()


def allowed_origin(containers, inspect):
    """Only the sole running Compose service on the expected edge is trusted."""
    matches = [
        obj for obj in containers
        if obj.get("Labels", {}).get("com.docker.compose.project") == PROJECT
        and obj.get("Labels", {}).get("com.docker.compose.service") == SERVICE
    ]
    if len(matches) != 1:
        raise RuntimeError("server identity absent or ambiguous")
    listing = matches[0]
    if listing.get("State") != "running":
        raise RuntimeError("server identity not running")
    if inspect.get("Id") != listing.get("Id") or inspect.get("State", {}).get("Status") != "running":
        raise RuntimeError("server instance changed during validation")
    if inspect.get("Config", {}).get("Labels", {}).get("com.docker.compose.project") != PROJECT:
        raise RuntimeError("server project mismatch")
    if inspect.get("Config", {}).get("Labels", {}).get("com.docker.compose.service") != SERVICE:
        raise RuntimeError("server service mismatch")
    networks = inspect.get("NetworkSettings", {}).get("Networks") or {}
    edge = networks.get(NETWORK) or {}
    address = ipaddress.ip_address(edge.get("IPAddress") or "")
    if address.version != 4 or address not in TRUSTED_EDGE:
        raise RuntimeError("ROC edge source outside verified network")
    if not edge.get("Gateway"):
        raise RuntimeError("ROC edge route missing")
    # A second default gateway would make source address selection ambiguous.
    if any(value.get("Gateway") for name, value in networks.items() if name != NETWORK):
        raise RuntimeError("ambiguous server source route")
    return str(address)


def current_origin(text):
    lines = LINE.findall(text)
    if len(lines) != 1:
        raise RuntimeError("expected exactly one Server directive")
    tokens = [x.strip() for x in lines[0].split(",")]
    if tokens[0] != "127.0.0.1" or not 1 <= len(tokens) <= 2:
        raise RuntimeError("unknown Server allowlist ownership")
    for raw in tokens[1:]:
        addr = ipaddress.ip_address(raw)
        if addr.version != 4 or addr not in TRUSTED_EDGE:
            raise RuntimeError("unmanaged Server allowlist entry")
    return lines[0]


def proposed_config(text, address):
    previous = current_origin(text)
    target = "127.0.0.1" + ("," + str(ipaddress.ip_address(address)) if address else "")
    if target == previous:
        return text, False
    updated = LINE.sub(lambda _match: "Server=" + target, text, count=1)
    return updated, True


def atomic_write(config, content):
    config = Path(config)
    st = config.stat()
    fd, temporary = tempfile.mkstemp(prefix=".roc-origin-", dir=config.parent)
    try:
        os.fchmod(fd, st.st_mode & 0o777)
        os.fchown(fd, st.st_uid, st.st_gid)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, config)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def check(*cmd):
    subprocess.run(list(cmd), check=True, timeout=20, capture_output=True)


def reconcile(config=CONFIG, socket_path=SOCKET, apply=False):
    try:
        containers = view("/containers/json?all=1", socket_path)
        identity = [
            entry for entry in containers if entry.get("Labels", {}).get("com.docker.compose.project") == PROJECT
            and entry.get("Labels", {}).get("com.docker.compose.service") == SERVICE
        ]
        if len(identity) != 1:
            raise RuntimeError("ROC Server identity missing or ambiguous")
        item_id = identity[0]["Id"]
        if not re.fullmatch(r"[a-f0-9]{64}", item_id):
            raise RuntimeError("invalid server container ID")
        inspect = view("/containers/" + item_id + "/json", socket_path)
        source = allowed_origin(containers, inspect)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        # No trusted identity must never authorize the IP of a different container.
        # Fail closed to loopback only, and let the timer retry later.
        print("ROC_AGENT2_SOURCE_UNVERIFIED",type(exc).__name__)
        source = None

    original = Path(config).read_text(encoding="utf-8")
    updated, changed = proposed_config(original, source)
    if not changed:
        return ("IN_SYNC" if source else "LOOPBACK_ONLY", source)
    if not apply:
        return ("DRIFT" if source else "FAIL_CLOSED_REQUIRED", source)
    atomic_write(config, updated)
    try:
        check(SERVER, "-T", "-c", config)
        check("systemctl", "restart", "zabbix-agent2")
        check("systemctl", "is-active", "--quiet", "zabbix-agent2")
        if source:
            # Reject a container recreation racing with the Agent2 restart.
            confirmed = view("/containers/" + item_id + "/json", socket_path)
            if allowed_origin(containers, confirmed) != source:
                raise RuntimeError("ROC server source changed during reconciliation")
    except Exception:
        # Never restore the previously authorized IP: it may now belong to another
        # container after ROC recreation. Roll back to a restricted loopback policy.
        atomic_write(config, LINE.sub("Server=127.0.0.1", original, count=1))
        check(SERVER, "-T", "-c", config)
        check("systemctl", "restart", "zabbix-agent2")
        raise
    return ("UPDATED" if source else "LOCKED_DOWN", source)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="Apply only verified single-IP reconciliation")
    args = p.parse_args()
    state, source = reconcile(apply=args.apply)
    print(f"ROC_AGENT2_ORIGIN={state} SOURCE={source or 'NONE'}")


if __name__ == "__main__":
    main()
