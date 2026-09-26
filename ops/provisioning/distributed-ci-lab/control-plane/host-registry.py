#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import pathlib
import re
import sqlite3
import sys

SCHEMA_VERSION = 1
HOST_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
STATES = {"UNKNOWN", "READY", "DEGRADED", "UNAVAILABLE"}
ENDPOINT_STATES = {"VALID", "STALE", "CONFLICT", "UNREACHABLE"}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def connect(path: pathlib.Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    return db
def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS registry_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS hosts (
            host_id TEXT PRIMARY KEY,
            canonical_name TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL,
            access_method TEXT NOT NULL,
            access_target TEXT NOT NULL,
            expected_identity TEXT,
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS host_capabilities (
            host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE CASCADE,
            capability TEXT NOT NULL,
            PRIMARY KEY(host_id, capability)
        );
        CREATE TABLE IF NOT EXISTS host_dependencies (
            host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE CASCADE,
            dependency_host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE RESTRICT,
            dependency_kind TEXT NOT NULL DEFAULT 'access',
            PRIMARY KEY(host_id, dependency_host_id, dependency_kind),
            CHECK(host_id <> dependency_host_id)
        );
        CREATE TABLE IF NOT EXISTS host_endpoints (
            endpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
            host_id TEXT NOT NULL REFERENCES hosts(host_id) ON DELETE CASCADE,
            address_family TEXT NOT NULL,
            address TEXT NOT NULL,
            source TEXT NOT NULL,
            state TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            expires_at TEXT,
            UNIQUE(host_id, address_family, address, source)
        );
        CREATE TABLE IF NOT EXISTS host_health (
            host_id TEXT PRIMARY KEY REFERENCES hosts(host_id) ON DELETE CASCADE,
            state TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            observed_at TEXT NOT NULL
        );
        """
    )
    row = db.execute(
        "SELECT value FROM registry_meta WHERE key='schema_version'"
    ).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO registry_meta(key,value) VALUES('schema_version',?)",
            (str(SCHEMA_VERSION),),
        )
    elif int(row["value"]) != SCHEMA_VERSION:
        raise SystemExit("HOST_REGISTRY_SCHEMA_MISMATCH")
    db.commit()


def validate_host_id(value: str) -> str:
    if not HOST_ID.fullmatch(value):
        raise SystemExit(f"invalid host id: {value}")
    return value
def upsert_host(db: sqlite3.Connection, args: argparse.Namespace) -> None:
    host_id = validate_host_id(args.host_id)
    stamp = now_iso()
    db.execute(
        """
        INSERT INTO hosts(
            host_id, canonical_name, role, access_method, access_target,
            expected_identity, enabled, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(host_id) DO UPDATE SET
            canonical_name=excluded.canonical_name,
            role=excluded.role,
            access_method=excluded.access_method,
            access_target=excluded.access_target,
            expected_identity=excluded.expected_identity,
            enabled=excluded.enabled,
            updated_at=excluded.updated_at
        """,
        (
            host_id,
            args.canonical_name,
            args.role,
            args.access_method,
            args.access_target,
            args.expected_identity,
            1 if args.enabled else 0,
            stamp,
            stamp,
        ),
    )
    db.execute(
        """
        INSERT INTO host_health(host_id,state,reason,observed_at)
        VALUES(?,?,?,?)
        ON CONFLICT(host_id) DO NOTHING
        """,
        (host_id, "UNKNOWN", "not yet probed", stamp),
    )
    db.commit()
def set_capability(db: sqlite3.Connection, args: argparse.Namespace) -> None:
    db.execute(
        "INSERT OR IGNORE INTO host_capabilities(host_id,capability) VALUES(?,?)",
        (validate_host_id(args.host_id), args.capability),
    )
    db.commit()


def add_dependency(db: sqlite3.Connection, args: argparse.Namespace) -> None:
    host_id = validate_host_id(args.host_id)
    dependency = validate_host_id(args.depends_on)
    db.execute(
        """
        INSERT OR IGNORE INTO host_dependencies(
            host_id,dependency_host_id,dependency_kind
        ) VALUES(?,?,?)
        """,
        (host_id, dependency, args.kind),
    )
    db.commit()


def set_health(db: sqlite3.Connection, args: argparse.Namespace) -> None:
    if args.state not in STATES:
        raise SystemExit(f"invalid health state: {args.state}")
    db.execute(
        """
        INSERT INTO host_health(host_id,state,reason,observed_at)
        VALUES(?,?,?,?)
        ON CONFLICT(host_id) DO UPDATE SET
            state=excluded.state,
            reason=excluded.reason,
            observed_at=excluded.observed_at
        """,
        (validate_host_id(args.host_id), args.state, args.reason, now_iso()),
    )
    db.commit()
def observe_endpoint(db: sqlite3.Connection, args: argparse.Namespace) -> None:
    family = args.family.lower()
    if family in {"ipv4", "ipv6"}:
        parsed = ipaddress.ip_address(args.address)
        if (family == "ipv4" and parsed.version != 4) or (
            family == "ipv6" and parsed.version != 6
        ):
            raise SystemExit("address family mismatch")
    elif family != "dns":
        raise SystemExit("family must be ipv4, ipv6, or dns")
    if args.state not in ENDPOINT_STATES:
        raise SystemExit(f"invalid endpoint state: {args.state}")

    stamp = dt.datetime.now(dt.timezone.utc)
    expires = (
        stamp + dt.timedelta(seconds=args.ttl_seconds)
        if args.ttl_seconds is not None
        else None
    )
    db.execute(
        """
        INSERT INTO host_endpoints(
            host_id,address_family,address,source,state,observed_at,expires_at
        ) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(host_id,address_family,address,source) DO UPDATE SET
            state=excluded.state,
            observed_at=excluded.observed_at,
            expires_at=excluded.expires_at
        """,
        (
            validate_host_id(args.host_id),
            family,
            args.address,
            args.source,
            args.state,
            stamp.isoformat(),
            expires.isoformat() if expires else None,
        ),
    )
    db.commit()
def list_workers(db: sqlite3.Connection, capability: str) -> None:
    rows = db.execute(
        """
        SELECT h.host_id
        FROM hosts h
        JOIN host_capabilities c ON c.host_id=h.host_id
        WHERE h.enabled=1 AND c.capability=?
        ORDER BY h.host_id
        """,
        (capability,),
    ).fetchall()
    for row in rows:
        print(row["host_id"])


def field(db: sqlite3.Connection, host_id: str, name: str) -> None:
    allowed = {
        "canonical_name",
        "role",
        "access_method",
        "access_target",
        "expected_identity",
        "enabled",
    }
    if name not in allowed:
        raise SystemExit(f"unsupported field: {name}")
    row = db.execute(
        f"SELECT {name} AS value FROM hosts WHERE host_id=?",
        (validate_host_id(host_id),),
    ).fetchone()
    if row is None:
        raise SystemExit("HOST_NOT_FOUND")
    value = row["value"]
    if value is not None:
        print(value)
def dependencies(db: sqlite3.Connection, host_id: str) -> None:
    rows = db.execute(
        """
        SELECT dependency_host_id
        FROM host_dependencies
        WHERE host_id=?
        ORDER BY dependency_host_id
        """,
        (validate_host_id(host_id),),
    ).fetchall()
    for row in rows:
        print(row["dependency_host_id"])


def snapshot(db: sqlite3.Connection, capability: str | None) -> None:
    where = ""
    params: tuple[object, ...] = ()
    if capability:
        where = (
            "WHERE EXISTS (SELECT 1 FROM host_capabilities c "
            "WHERE c.host_id=h.host_id AND c.capability=?)"
        )
        params = (capability,)
    hosts = []
    for row in db.execute(
        f"""
        SELECT h.*, hh.state AS health_state, hh.reason AS health_reason,
               hh.observed_at AS health_observed_at
        FROM hosts h
        LEFT JOIN host_health hh ON hh.host_id=h.host_id
        {where}
        ORDER BY h.host_id
        """,
        params,
    ):
        host = dict(row)
        host["enabled"] = bool(host["enabled"])
        host["capabilities"] = [
            r["capability"]
            for r in db.execute(
                "SELECT capability FROM host_capabilities WHERE host_id=? ORDER BY capability",
                (row["host_id"],),
            )
        ]
        host["dependencies"] = [
            dict(r)
            for r in db.execute(
                """
                SELECT dependency_host_id,dependency_kind
                FROM host_dependencies WHERE host_id=?
                ORDER BY dependency_host_id,dependency_kind
                """,
                (row["host_id"],),
            )
        ]
        host["endpoints"] = [
            dict(r)
            for r in db.execute(
                """
                SELECT address_family,address,source,state,observed_at,expires_at
                FROM host_endpoints WHERE host_id=?
                ORDER BY address_family,address,source
                """,
                (row["host_id"],),
            )
        ]
        hosts.append(host)
    print(json.dumps({"schema_version": SCHEMA_VERSION, "hosts": hosts}, indent=2))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    up = sub.add_parser("upsert-host")
    up.add_argument("--host-id", required=True)
    up.add_argument("--canonical-name", required=True)
    up.add_argument("--role", required=True)
    up.add_argument("--access-method", default="ssh")
    up.add_argument("--access-target", required=True)
    up.add_argument("--expected-identity")
    up.add_argument("--enabled", action=argparse.BooleanOptionalAction, default=True)

    cap = sub.add_parser("set-capability")
    cap.add_argument("--host-id", required=True)
    cap.add_argument("--capability", required=True)

    dep = sub.add_parser("add-dependency")
    dep.add_argument("--host-id", required=True)
    dep.add_argument("--depends-on", required=True)
    dep.add_argument("--kind", default="access")

    health = sub.add_parser("set-health")
    health.add_argument("--host-id", required=True)
    health.add_argument("--state", required=True)
    health.add_argument("--reason", default="")
    endpoint = sub.add_parser("observe-endpoint")
    endpoint.add_argument("--host-id", required=True)
    endpoint.add_argument("--family", required=True)
    endpoint.add_argument("--address", required=True)
    endpoint.add_argument("--source", required=True)
    endpoint.add_argument("--state", default="VALID")
    endpoint.add_argument("--ttl-seconds", type=int)

    workers = sub.add_parser("list-workers")
    workers.add_argument("--capability", required=True)

    get = sub.add_parser("field")
    get.add_argument("--host-id", required=True)
    get.add_argument("--name", required=True)

    deps = sub.add_parser("dependencies")
    deps.add_argument("--host-id", required=True)

    snap = sub.add_parser("snapshot")
    snap.add_argument("--capability")
    return p


def main() -> None:
    args = parser().parse_args()
    db_path = pathlib.Path(args.db)
    with connect(db_path) as db:
        ensure_schema(db)
        if args.command == "init":
            return
        if args.command == "upsert-host":
            upsert_host(db, args)
        elif args.command == "set-capability":
            set_capability(db, args)
        elif args.command == "add-dependency":
            add_dependency(db, args)
        elif args.command == "set-health":
            set_health(db, args)
        elif args.command == "observe-endpoint":
            observe_endpoint(db, args)
        elif args.command == "list-workers":
            list_workers(db, args.capability)
        elif args.command == "field":
            field(db, args.host_id, args.name)
        elif args.command == "dependencies":
            dependencies(db, args.host_id)
        elif args.command == "snapshot":
            snapshot(db, args.capability)
        else:
            raise AssertionError(args.command)


if __name__ == "__main__":
    main()
