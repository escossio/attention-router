"""Mock-only validation of the AR-1001 official Zabbix API operator gate."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest

from ops.observability.failure_catalog.zabbix_items_admin import (
    HOST_LABEL, OLD_KEYS, _config, candidate_items, inspect, main,
    planned_status_updates, snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "ops/observability/failure-catalog/zabbix-items.ar-1001.v0.json"


def mock_inventory(*, existing_keys=()):
    old = [{"itemid": str(100 + i), "key_": key, "hostid": "demo-host",
            "name": key, "type": "2", "status": "0"}
           for i, key in enumerate(OLD_KEYS)]
    new = [{"itemid": str(200+i), "key_": k, "hostid": "demo-host",
            "name": k, "type": "2", "value_type": "3", "status": "1"}
           for i, k in enumerate(existing_keys)]
    return {"host": {"hostid": "demo-host", "host": "synthetic", "name": HOST_LABEL},
            "legacy_items": sorted(old, key=lambda x: x["key_"]),
            "candidate_items": new,
            "triggers": [
                {"triggerid": "1", "description": "ANDY_WA_AUTO_REPLY_STALLED: test", "value": "1"},
                {"triggerid": "2", "description": "ANDY_WA_REPLY_OBSERVABILITY_LOST: test", "value": "0"},
            ]}


class FakeAPI:
    def __init__(self, inventory=None):
        self.inventory = inventory or mock_inventory()
        self.calls = []
        self.token = None

    def login(self, *_):
        self.token = "synthetic"

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "host.get":
            return [self.inventory["host"]]
        if method == "item.get":
            return self.inventory["legacy_items"] + self.inventory["candidate_items"]
        if method == "trigger.get":
            return self.inventory["triggers"]
        if method == "item.create":
            return {"itemids": [str(i) for i in range(len(params))]}
        if method == "item.update":
            by_id = {item["itemid"]: item for item in self.inventory["candidate_items"]}
            for update in params:
                by_id[update["itemid"]]["status"] = str(update["status"])
            return {"itemids": [update["itemid"] for update in params]}
        raise AssertionError("unexpected API " + method)


class Ar1001ZabbixOperatorTests(unittest.TestCase):
    def test_offline_plan_does_not_create(self):
        manifest = _config(MANIFEST)
        self.assertEqual(len(manifest["items"]), 7)
        self.assertEqual(main(["--manifest", str(MANIFEST)]), 0)

    def test_snapshot_contains_only_exact_host_items_and_triggers(self):
        api = FakeAPI()
        inv = inspect(api, _config(MANIFEST))
        self.assertEqual(len(inv["legacy_items"]), 8)
        self.assertEqual(len(inv["triggers"]), 2)
        self.assertEqual([x[0] for x in api.calls],
                         ["host.get", "item.get", "trigger.get"])
        with tempfile.TemporaryDirectory() as dirpath:
            dest = Path(dirpath) / "backup"
            out, digest = snapshot(dest, inv, _config(MANIFEST))
            self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
            self.assertEqual(hashlib.sha256(out.read_bytes()).hexdigest(), digest)
            self.assertEqual(json.loads(out.read_text())["inventory"], inv)
            with self.assertRaises(RuntimeError):
                snapshot(dest, inv, _config(MANIFEST))

    def test_new_items_all_disabled_and_trapper(self):
        inv = mock_inventory()
        items = candidate_items(inv, _config(MANIFEST))
        self.assertEqual(len(items), 7)
        self.assertTrue(all(x["type"] == 2 and x["value_type"] == 3 for x in items))
        self.assertTrue(all(x["status"] == 1 and x["hostid"] == "demo-host" for x in items))
        self.assertTrue(all(x["tags"] == [{"tag": "fault_code", "value": "AR-1001"}] for x in items))

    def test_existing_new_items_blocks_creation(self):
        inv = mock_inventory(existing_keys=["andy.reply.direct_event_6h"])
        with self.assertRaisesRegex(RuntimeError, "PARTIAL"):
            candidate_items(inv, _config(MANIFEST))

    def test_unrecognized_source_does_not_enter_snapshot(self):
        inv = mock_inventory()
        inv["triggers"] = [{"description": "UNRELATED_TRAP", "triggerid": "x"}]
        with self.assertRaisesRegex(RuntimeError, "TRIGGERS_MISMATCH"):
            inspect(FakeAPI(inv), _config(MANIFEST))

    def test_live_mode_rejects_missing_private_backup_before_create(self):
        with tempfile.TemporaryDirectory() as dirpath:
            base = Path(dirpath)
            credentials = base / "creds.json"
            credentials.write_text(json.dumps({"username": "synthetic", "password": "fake-not-real"}))
            settings = base / "settings.json"
            settings.write_text(json.dumps({
                "api_url": "https://synthetic.example.test/api_jsonrpc.php",
                "admin_credentials_file": str(credentials),
            }))
            fake = FakeAPI()
            def factory(_):
                return fake
            with self.assertRaisesRegex(RuntimeError, "PRIOR_BACKUP_REQUIRED"):
                main(["--manifest", str(MANIFEST), "--mode", "create-disabled",
                      "--operator-settings", str(settings),
                      "--backup-dir", str(base / "missing")], api_factory=factory)
            self.assertNotIn("item.create", [method for method, _ in fake.calls])

    def test_mocked_lifecycle_creates_disabled_items_only_after_snapshot(self):
        with tempfile.TemporaryDirectory() as dirpath:
            base = Path(dirpath)
            credentials = base / "credentials.json"
            credentials.write_text(json.dumps({"username": "synthetic", "password": "mock-only"}))
            settings = base / "settings.json"
            settings.write_text(json.dumps({
                "api_url": "https://synthetic.example.test/api_jsonrpc.php",
                "admin_credentials_file": str(credentials),
            }))
            api = FakeAPI()
            def factory(_):
                return api
            args = ["--manifest", str(MANIFEST), "--operator-settings", str(settings),
                    "--backup-dir", str(base / "backup")]
            self.assertEqual(main(args + ["--mode", "snapshot"], api_factory=factory), 0)
            self.assertNotIn("item.create", [method for method, _ in api.calls])
            self.assertEqual(main(args + ["--mode", "create-disabled"], api_factory=factory), 0)
            creates = [data for method, data in api.calls if method == "item.create"]
            self.assertEqual(len(creates), 1)
            self.assertEqual(len(creates[0]), 7)
            self.assertTrue(all(v["status"] == 1 for v in creates[0]))
            self.assertTrue(all(v["type"] == 2 for v in creates[0]))

    def test_manifest_changes_fail_closed(self):
        with tempfile.TemporaryDirectory() as dirpath:
            path = Path(dirpath) / "manifest.json"
            data = json.loads(MANIFEST.read_text())
            data["items"] = data["items"][:6]
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                _config(path)

    def test_enable_and_disable_touch_only_checkpointed_new_items(self):
        manifest = _config(MANIFEST)
        keys = [item["key"] for item in manifest["items"]]
        with tempfile.TemporaryDirectory() as dirpath:
            base = Path(dirpath)
            credentials = base / "credentials.json"
            credentials.write_text(json.dumps({"username": "synthetic", "password": "mock-only"}))
            settings = base / "settings.json"
            settings.write_text(json.dumps({
                "api_url": "https://synthetic.example.test/api_jsonrpc.php",
                "admin_credentials_file": str(credentials),
            }))
            api = FakeAPI()
            def factory(_):
                return api
            args = ["--manifest", str(MANIFEST), "--operator-settings", str(settings),
                    "--backup-dir", str(base / "backup")]
            self.assertEqual(main(args + ["--mode", "snapshot"], api_factory=factory), 0)
            api.inventory["candidate_items"] = mock_inventory(existing_keys=keys)["candidate_items"]
            checkpoint = base / "backup/ar1001-created-disabled.private.json"
            checkpoint.write_text(json.dumps({
                "candidate_itemids": [item["itemid"]
                                      for item in api.inventory["candidate_items"]],
            }))
            probe = base / "reply_health.py"
            probe.write_text("synthetic probe")
            digest = hashlib.sha256(probe.read_bytes()).hexdigest()
            self.assertEqual(main(args + ["--mode", "enable", "--installed-probe",
                                          str(probe), "--expected-probe-sha256", digest],
                                  api_factory=factory), 0)
            self.assertTrue(all(item["status"] == "0" for item in api.inventory["candidate_items"]))
            self.assertEqual(main(args + ["--mode", "disable"], api_factory=factory), 0)
            self.assertTrue(all(item["status"] == "1" for item in api.inventory["candidate_items"]))
            with self.assertRaisesRegex(RuntimeError, "HASH_MISMATCH"):
                main(args + ["--mode", "enable", "--installed-probe", str(probe),
                             "--expected-probe-sha256", "0" * 64], api_factory=factory)
            self.assertEqual(len([call for call in api.calls if call[0] == "item.update"]), 2)

    def test_status_update_rejects_uncheckpointed_item(self):
        manifest = _config(MANIFEST)
        inv = mock_inventory(existing_keys=[item["key"] for item in manifest["items"]])
        with self.assertRaisesRegex(RuntimeError, "CHECKPOINT_MISMATCH"):
            planned_status_updates(inv, manifest, {"candidate_itemids": ["wrong"]},
                                   enable=True)


if __name__ == "__main__":
    unittest.main()
