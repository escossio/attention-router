#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import os
from pathlib import Path
import re
import tempfile

REQUIRED_KEYS = frozenset(
    {
        "NATIVE_OTEL_EDGE_BIND_IP",
        "NATIVE_OTEL_EDGE_BIND_PORT",
        "NATIVE_OTEL_TRANSPORT_SOURCE_IP",
        "NATIVE_OTEL_INGRESS_SOURCE_IP",
        "NATIVE_OTEL_WORKER_SOURCE_IP",
        "NATIVE_OTEL_COLLECTOR_LOOPBACK_PORT",
    }
)

TOKEN_MAP = {
    "@BIND_IP@": "NATIVE_OTEL_EDGE_BIND_IP",
    "@BIND_PORT@": "NATIVE_OTEL_EDGE_BIND_PORT",
    "@TRANSPORT_SOURCE_IP@": "NATIVE_OTEL_TRANSPORT_SOURCE_IP",
    "@INGRESS_SOURCE_IP@": "NATIVE_OTEL_INGRESS_SOURCE_IP",
    "@WORKER_SOURCE_IP@": "NATIVE_OTEL_WORKER_SOURCE_IP",
    "@COLLECTOR_LOOPBACK_PORT@": "NATIVE_OTEL_COLLECTOR_LOOPBACK_PORT",
}
UNRESOLVED_TOKEN = re.compile(r"@[A-Z0-9_]+@")


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{lineno}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key not in REQUIRED_KEYS:
            raise ValueError(f"{path}:{lineno}: unexpected key {key!r}")
        if key in values:
            raise ValueError(f"{path}:{lineno}: duplicate key {key!r}")
        if not value:
            raise ValueError(f"{path}:{lineno}: empty value for {key}")
        values[key] = value

    missing = sorted(REQUIRED_KEYS - values.keys())
    if missing:
        raise ValueError(f"{path}: missing keys: {', '.join(missing)}")
    return values


def _ipv4(value: str, key: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValueError(f"{key}: invalid IP address") from exc
    if address.version != 4 or address.is_unspecified or address.is_multicast:
        raise ValueError(f"{key}: expected a usable IPv4 address")
    return str(address)


def _port(value: str, key: str) -> str:
    try:
        port = int(value, 10)
    except ValueError as exc:
        raise ValueError(f"{key}: invalid TCP port") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{key}: TCP port outside 1..65535")
    return str(port)


def validated_values(values: dict[str, str]) -> dict[str, str]:
    if set(values) != REQUIRED_KEYS:
        missing = sorted(REQUIRED_KEYS - values.keys())
        extra = sorted(values.keys() - REQUIRED_KEYS)
        raise ValueError(f"invalid key set; missing={missing}, extra={extra}")

    result = {
        "NATIVE_OTEL_EDGE_BIND_IP": _ipv4(
            values["NATIVE_OTEL_EDGE_BIND_IP"],
            "NATIVE_OTEL_EDGE_BIND_IP",
        ),
        "NATIVE_OTEL_EDGE_BIND_PORT": _port(
            values["NATIVE_OTEL_EDGE_BIND_PORT"],
            "NATIVE_OTEL_EDGE_BIND_PORT",
        ),
        "NATIVE_OTEL_TRANSPORT_SOURCE_IP": _ipv4(
            values["NATIVE_OTEL_TRANSPORT_SOURCE_IP"],
            "NATIVE_OTEL_TRANSPORT_SOURCE_IP",
        ),
        "NATIVE_OTEL_INGRESS_SOURCE_IP": _ipv4(
            values["NATIVE_OTEL_INGRESS_SOURCE_IP"],
            "NATIVE_OTEL_INGRESS_SOURCE_IP",
        ),
        "NATIVE_OTEL_WORKER_SOURCE_IP": _ipv4(
            values["NATIVE_OTEL_WORKER_SOURCE_IP"],
            "NATIVE_OTEL_WORKER_SOURCE_IP",
        ),
        "NATIVE_OTEL_COLLECTOR_LOOPBACK_PORT": _port(
            values["NATIVE_OTEL_COLLECTOR_LOOPBACK_PORT"],
            "NATIVE_OTEL_COLLECTOR_LOOPBACK_PORT",
        ),
    }
    source_addresses = {
        result["NATIVE_OTEL_TRANSPORT_SOURCE_IP"],
        result["NATIVE_OTEL_INGRESS_SOURCE_IP"],
        result["NATIVE_OTEL_WORKER_SOURCE_IP"],
    }
    if len(source_addresses) != 3:
        raise ValueError("Transport, Ingress and Worker source addresses must be distinct")
    return result


def render_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    validated = validated_values(values)
    for token, key in TOKEN_MAP.items():
        rendered = rendered.replace(token, validated[key])
    unresolved = sorted(set(UNRESOLVED_TOKEN.findall(rendered)))
    if unresolved:
        raise ValueError(f"unresolved template tokens: {', '.join(unresolved)}")
    return rendered


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    rendered = render_template(
        args.template.read_text(encoding="utf-8"),
        read_env(args.env_file),
    )
    if args.output is None:
        print(rendered, end="")
    else:
        write_atomic(args.output, rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
