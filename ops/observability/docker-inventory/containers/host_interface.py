"""Bounded, auditable migration of the existing ROC Zabbix agent interface.

Operator-only. Never executes at container startup, never touches the host OS.
The normal path is read-only; --apply requires an explicit expected current IP.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

API_URL = os.getenv("ROC_ZABBIX_API_URL", "http://roc-zabbix-web:8080/api_jsonrpc.php")
BACKUP = Path("/var/lib/roc-migration/roc-engine-agent-interface-before.json")
HOST = "Andy Engine - AGT01"
TARGET_DNS = "roc-agent2"


class ZabbixAPI:
    def __init__(self, transport=None):
        self.transport = transport or self._transport
        self.counter = 0
        self.token = ""
        username = os.getenv("ZABBIX_ADMIN_USER")
        password = os.getenv("ZABBIX_ADMIN_PASSWORD")
        if not username or not password:
            raise RuntimeError("operator credential not configured")
        self.token = self.call("user.login", {"username": username, "password": password})

    def _transport(self, body, token):
        headers = {"Content-Type": "application/json-rpc"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = Request(API_URL, data=json.dumps(body).encode(), headers=headers)
        with urlopen(req, timeout=8) as response:
            return json.load(response)

    def call(self, method, params):
        self.counter += 1
        payload = {
            "jsonrpc": "2.0", "method": method, "params": params, "id": self.counter
        }
        result = self.transport(payload, self.token)
        if "error" in result:
            # Do not display private API response fields or credentials.
            raise RuntimeError("Zabbix API rejected operation " + method)
        return result["result"]


def identify(api):
    hosts = api.call("host.get", {"output": ["hostid", "host"], "filter": {"host": [HOST]}})
    if len(hosts) != 1 or hosts[0].get("host") != HOST:
        raise RuntimeError("ambiguous ROC Engine host")
    interfaces = api.call("hostinterface.get", {
        "hostids": [hosts[0]["hostid"]],
        "output": ["interfaceid", "hostid", "type", "main", "useip", "ip", "dns", "port"],
    })
    selected = [x for x in interfaces if str(x.get("type")) == "1" and str(x.get("main")) == "1"]
    if len(selected) != 1:
        raise RuntimeError("ambiguous main Agent interface")
    result = selected[0]
    if str(result.get("hostid")) != str(hosts[0]["hostid"]):
        raise RuntimeError("interface belongs to wrong host")
    if str(result.get("port")) != "10050":
        raise RuntimeError("unexpected agent port")
    return {k: result[k] for k in
            ("interfaceid", "hostid", "type", "main", "useip", "ip", "dns", "port")}


def is_target(entry):
    return str(entry["useip"]) == "0" and entry["dns"] == TARGET_DNS and entry["port"] == "10050"


def apply(api, expected_ip, backup_path=BACKUP):
    before = identify(api)
    if is_target(before):
        return "ALREADY_CONTAINERIZED"
    if not expected_ip or str(before["useip"]) != "1" or before["ip"] != expected_ip:
        raise RuntimeError("current interface differs from operator precondition")
    if backup_path.exists():
        raise RuntimeError("private rollback checkpoint already exists")
    backup_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    document = {
        "recorded_utc": datetime.now(timezone.utc).isoformat(),
        "host": HOST,
        "before": before,
        "target": {"dns": TARGET_DNS, "port": "10050", "useip": "0"},
    }
    with backup_path.open("x", encoding="utf-8") as fd:
        os.chmod(backup_path, 0o600)
        json.dump(document, fd)
    api.call("hostinterface.update", {
        "interfaceid": before["interfaceid"],
        "useip": 0, "ip": "", "dns": TARGET_DNS, "port": "10050",
    })
    after = identify(api)
    if not is_target(after):
        raise RuntimeError("Zabbix interface failed post-update verification")
    return "CUTOVER_PASS"


def rollback(api, backup_path=BACKUP):
    if not backup_path.is_file():
        raise RuntimeError("no private rollback checkpoint")
    document = json.loads(backup_path.read_text())
    if document.get("host") != HOST or "before" not in document:
        raise RuntimeError("invalid rollback checkpoint")
    before = document["before"]
    current = identify(api)
    if not is_target(current) or current["interfaceid"] != before["interfaceid"]:
        raise RuntimeError("unexpected current interface; refuse blind rollback")
    api.call("hostinterface.update", {
        "interfaceid": before["interfaceid"], "useip": before["useip"],
        "ip": before["ip"], "dns": before["dns"], "port": before["port"],
    })
    if identify(api) != before:
        raise RuntimeError("rollback mismatch")
    return "ROLLBACK_PASS"


def main():
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--apply", action="store_true")
    action.add_argument("--rollback", action="store_true")
    parser.add_argument("--expect-ip")
    args = parser.parse_args()
    api = ZabbixAPI()
    if args.check:
        entry = identify(api)
        print("INTERFACE", json.dumps({k: entry[k] for k in ("interfaceid","useip","ip","dns","port")}))
    elif args.apply:
        print(apply(api, args.expect_ip))
    else:
        print(rollback(api))


if __name__ == "__main__":
    main()
