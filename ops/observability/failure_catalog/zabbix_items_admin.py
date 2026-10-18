"""Zabbix ROC AR-1001 operator gate: snapshot first, create disabled items later.

Never deployed as a service. Default mode = offline validation only.
No live API credentials, tokens, tenant IDs, host IDs or private IPs in Git.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import urllib.request


HOST_LABEL = "Andy Engine - AGT01"
OLD_KEYS = (
    "andy.reply.probe_ok",
    "andy.reply.functional_stalled",
    "andy.reply.direct_6h",
    "andy.reply.no_safe_6h",
    "andy.reply.blueprint_6h",
    "andy.reply.other_failed_6h",
    "andy.reply.latest_failure_epoch",
    "andy.reply.last_agent_done_epoch",
)
TRIGGER_NAMES = (
    "ANDY_WA_AUTO_REPLY_STALLED",
    "ANDY_WA_REPLY_OBSERVABILITY_LOST",
)


def _config(manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "CANDIDATE_NOT_PROVISIONED"
    assert manifest["item_policy"]["zabbix_item_type"] == 2
    assert manifest["item_policy"]["zabbix_value_type"] == 3
    items = manifest["items"]
    keys = [x["key"] for x in items]
    if len(items) != 7 or len(set(keys)) != 7:
        raise ValueError("AR1001_MANIFEST_INVALID")
    if set(keys) & set(OLD_KEYS):
        raise ValueError("AR1001_OVERLAPPING_EXISTING_ITEMS")
    for entry in items:
        if not re.fullmatch(r"andy\.reply\.[a-z0-9_]+", entry["key"]):
            raise ValueError("AR1001_UNSAFE_ZABBIX_KEY")
    return manifest


class ZabbixAPI:
    def __init__(self, url: str, *, opener=urllib.request.urlopen) -> None:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("AR1001_INVALID_API_URL")
        self.url = url
        self.token: str | None = None
        self.opener = opener

    def call(self, method: str, params: dict | list) -> object:
        if method not in {
            "user.login", "apiinfo.version", "host.get", "item.get",
            "trigger.get", "problem.get", "item.create", "item.update",
        }:
            raise ValueError("AR1001_API_METHOD_DENIED")
        headers = {"Content-Type": "application/json-rpc"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        data = json.dumps({
            "jsonrpc": "2.0", "method": method, "params": params, "id": 1,
        }).encode()
        req = urllib.request.Request(self.url, data=data, headers=headers)
        try:
            with self.opener(req, timeout=12) as response:
                value = json.load(response)
        except Exception:
            raise RuntimeError("AR1001_API_UNAVAILABLE_" + method.replace(".", "_")) from None
        if "error" in value:
            err = value["error"]
            code = err.get("code")
            raise RuntimeError(
                "AR1001_API_REJECTED_" + method.replace(".", "_")
                + "_CODE_" + str(code if isinstance(code, int) else "UNKNOWN")
            )
        if "result" not in value:
            raise RuntimeError("AR1001_API_MALFORMED_" + method.replace(".", "_"))
        return value["result"]

    def login(self, settings: dict, credentials: dict) -> None:
        token = settings.get("api_token")
        if not token:
            token = self.call("user.login", {
                "username": credentials["username"], "password": credentials["password"],
            })
        if not isinstance(token, str) or len(token) < 12:
            raise RuntimeError("AR1001_AUTH_INVALID")
        self.token = token


def inspect(api: ZabbixAPI, manifest: dict) -> dict:
    matched = api.call("host.get", {
        "output": ["hostid", "host", "name", "status"],
        "search": {"name": HOST_LABEL},
        "limit": 30,
    })
    hosts = [x for x in matched if x.get("name") == HOST_LABEL]
    if len(hosts) != 1:
        raise RuntimeError("AR1001_HOST_NOT_UNIQUE")
    host = hosts[0]
    hostid = host["hostid"]
    items = api.call("item.get", {
        "output": ["itemid", "key_", "hostid", "name", "type", "value_type",
                   "status", "history", "trends", "delay"],
        "hostids": [hostid], "filter": {
            "key_": list(OLD_KEYS) + [i["key"] for i in manifest["items"]]
        },
    })
    existing = [x for x in items if x["key_"] in OLD_KEYS]
    if len(existing) != 8 or {x["key_"] for x in existing} != set(OLD_KEYS):
        raise RuntimeError("AR1001_LEGACY_ITEMS_MISMATCH")
    if any(int(x["status"]) != 0 or int(x["type"]) != 2 for x in existing):
        raise RuntimeError("AR1001_LEGACY_ITEM_DRIFT")
    try:
        triggers = api.call("trigger.get", {
            "output": ["triggerid", "description", "status", "value", "priority", "expression"],
            "hostids": [hostid],
            "search": {"description": "ANDY_WA_"},
            "selectTags": ["tag", "value"],
            "limit": 30,
        })
    except RuntimeError:
        raise
    active = [t for t in triggers if any(t["description"].startswith(n) for n in TRIGGER_NAMES)]
    if len(active) != 2 or {
        next(n for n in TRIGGER_NAMES if t["description"].startswith(n)) for t in active
    } != set(TRIGGER_NAMES):
        raise RuntimeError("AR1001_TRIGGERS_MISMATCH")
    return {
        "host": host,
        "legacy_items": sorted(existing, key=lambda x: x["key_"]),
        "candidate_items": sorted([x for x in items if x["key_"] not in OLD_KEYS],
                                  key=lambda x: x["key_"]),
        "triggers": sorted(active, key=lambda x: x["description"]),
    }


def snapshot(root: Path, inventory: dict, manifest: dict) -> tuple[Path, str]:
    if root.exists():
        raise RuntimeError("AR1001_BACKUP_PATH_EXISTS")
    root.mkdir(mode=0o700, parents=False)
    os.chmod(root, 0o700)
    data = {
        "snapshot_version": "0.1.0-draft",
        "manifest_sha256": hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
        "inventory": inventory,
    }
    output = root / "ar1001-zabbix-before.json"
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    bytes_out = json.dumps(data, ensure_ascii=False, sort_keys=True,
                           indent=2).encode()
    with os.fdopen(fd, "wb") as stream:
        stream.write(bytes_out)
        stream.flush()
        os.fsync(stream.fileno())
    digest = hashlib.sha256(bytes_out).hexdigest()
    return output, digest


def candidate_items(inventory: dict, manifest: dict) -> list[dict]:
    if inventory["candidate_items"]:
        raise RuntimeError("AR1001_PARTIAL_OR_ALREADY_PROVISIONED")
    host = inventory["host"]
    items = []
    for entry in manifest["items"]:
        items.append({
            "hostid": host["hostid"],
            "name": entry["label"],
            "key_": entry["key"],
            "type": 2,
            "value_type": 3,
            "status": 1,  # create DISABLED, no unbacked green or queued collector
            "history": "7d",
            "trends": "30d",
            "tags": [{"tag": "fault_code", "value": "AR-1001"}],
            "description": entry["semantic"][:255],
        })
    return items


def planned_status_updates(inventory: dict, manifest: dict, checkpoint: dict,
                           *, enable: bool) -> list[dict]:
    items = inventory["candidate_items"]
    expected_keys = {entry["key"] for entry in manifest["items"]}
    checkpoint_ids = set(checkpoint.get("candidate_itemids", []))
    if (len(items) != 7 or {item["key_"] for item in items} != expected_keys or
            {item["itemid"] for item in items} != checkpoint_ids or
            len(checkpoint_ids) != 7):
        raise RuntimeError("AR1001_CANDIDATE_CHECKPOINT_MISMATCH")
    if any(int(item["type"]) != 2 or int(item["value_type"]) != 3
           for item in items):
        raise RuntimeError("AR1001_CANDIDATE_TYPE_DRIFT")
    if enable and any(int(item["status"]) != 1 for item in items):
        raise RuntimeError("AR1001_CANDIDATE_NOT_ALL_DISABLED")
    target = 0 if enable else 1
    return [{"itemid": item["itemid"], "status": target}
            for item in sorted(items, key=lambda item: item["key_"])]


def main(argv=None, *, api_factory=ZabbixAPI) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--operator-settings", type=Path)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--installed-probe", type=Path)
    parser.add_argument("--expected-probe-sha256")
    parser.add_argument("--mode", choices=("plan", "snapshot", "create-disabled",
                                           "enable", "disable"),
                        default="plan")
    args = parser.parse_args(argv)
    manifest = _config(args.manifest)
    if args.mode == "plan":
        print("AR1001_OFFLINE_PLAN items=7 create_status=DISABLED "
              "legacy_preserved=true transport_unchanged=true")
        return 0
    if not args.operator_settings or not args.backup_dir:
        raise RuntimeError("AR1001_OPERATOR_ARGS_REQUIRED")
    settings = json.loads(args.operator_settings.read_text(encoding="utf-8"))
    credentials_path = Path(settings["admin_credentials_file"])
    credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
    api = api_factory(settings["api_url"])
    api.login(settings, credentials)
    inventory = inspect(api, manifest)
    if args.mode == "snapshot":
        _, digest = snapshot(args.backup_dir, inventory, manifest)
        print("AR1001_SNAPSHOT_CONFIRMED", "items", len(inventory["legacy_items"]),
              "triggers", len(inventory["triggers"]), "sha256", digest)
        return 0
    # Create only disabled items after independent operator backup/review.
    proof_file = args.backup_dir / "ar1001-zabbix-before.json"
    if not proof_file.is_file():
        raise RuntimeError("AR1001_PRIOR_BACKUP_REQUIRED")
    proof = json.loads(proof_file.read_text(encoding="utf-8"))
    expected_digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    if proof.get("manifest_sha256") != expected_digest:
        raise RuntimeError("AR1001_MANIFEST_CHANGED_SINCE_BACKUP")
    if args.mode != "disable" and proof.get("inventory", {}).get("legacy_items") != inventory["legacy_items"]:
        raise RuntimeError("AR1001_LEGACY_DRIFT_SINCE_BACKUP")
    if args.mode != "disable" and proof.get("inventory", {}).get("triggers") != inventory["triggers"]:
        raise RuntimeError("AR1001_TRIGGER_DRIFT_SINCE_BACKUP")
    if args.mode in ("enable", "disable"):
        checkpoint_file = args.backup_dir / "ar1001-created-disabled.private.json"
        if not checkpoint_file.is_file():
            raise RuntimeError("AR1001_CREATED_CHECKPOINT_REQUIRED")
        checkpoint = json.loads(checkpoint_file.read_text(encoding="utf-8"))
        if args.mode == "enable":
            if not args.installed_probe or not args.expected_probe_sha256:
                raise RuntimeError("AR1001_INSTALLED_PROBE_PROOF_REQUIRED")
            if hashlib.sha256(args.installed_probe.read_bytes()).hexdigest() != args.expected_probe_sha256:
                raise RuntimeError("AR1001_INSTALLED_PROBE_HASH_MISMATCH")
        updates = planned_status_updates(inventory, manifest, checkpoint,
                                         enable=args.mode == "enable")
        updated = api.call("item.update", updates)
        if (not isinstance(updated, dict) or
                set(updated.get("itemids", [])) != {item["itemid"] for item in updates}):
            raise RuntimeError("AR1001_STATUS_UPDATE_PARTIAL_RESULT")
        after = inspect(api, manifest)
        if ({item["itemid"] for item in after["candidate_items"]} !=
                {item["itemid"] for item in updates} or
                any(int(item["status"]) != updates[0]["status"]
                    for item in after["candidate_items"])):
            raise RuntimeError("AR1001_STATUS_UPDATE_NOT_CONFIRMED")
        print("AR1001_ITEM_STATUS_CONFIRMED", args.mode, "count", len(updates))
        return 0
    new_items = candidate_items(inventory, manifest)
    created = api.call("item.create", new_items)
    if (not isinstance(created, dict) or
        len(created.get("itemids", [])) != len(new_items)):
        raise RuntimeError("AR1001_CREATE_PARTIAL_RESULT")
    print("AR1001_DISABLED_ITEMS_CREATED count", len(new_items))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, KeyError, ValueError, RuntimeError) as exc:
        # Never print API error details / secrets / private payloads.
        code = str(exc)
        if not code.startswith("AR1001_"):
            code = "AR1001_OPERATOR_FAILED"
        print(code, file=sys.stderr)
        raise SystemExit(2)
