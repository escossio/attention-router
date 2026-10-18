"""Contrato leve do catálogo: prova local sem runtime/DB/Zabbix."""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "ops/observability/failure-catalog/catalog.v0.json"
STATES = {"LIVE_TRIGGER", "PROPOSED", "IN_REPO_TEMPLATE"}
FAMILIES = {
    "application": "1", "runtime": "2", "delivery": "3",
    "storage": "4", "network": "5", "observability": "6",
}


class FailureCatalogV0Tests(unittest.TestCase):
    def test_codes_unique_and_stable(self):
        d = json.loads(CATALOG.read_text(encoding="utf-8"))
        self.assertEqual(d["catalog_version"], "0.1.0-draft")
        seen = set()
        for entry in d["entries"]:
            with self.subTest(code=entry["code"]):
                code = entry["code"]
                self.assertRegex(code, r"^AR-[1-6][0-9]{3}$")
                self.assertEqual(code[3], FAMILIES[entry["family"]])
                self.assertNotIn(code, seen)
                seen.add(code)
                self.assertIn(entry["state"], STATES)
                for field in ("title", "condition", "false_positive_guard"):
                    self.assertIsInstance(entry[field], str)
                    self.assertTrue(entry[field].strip())
                self.assertTrue(entry["source_codes"])
                self.assertTrue(entry["minimum_evidence"])
                self.assertEqual(len(entry["minimum_evidence"]),
                                 len(set(entry["minimum_evidence"])))
                self.assertNotIn("remediation", entry)
                self.assertNotIn("root_cause", entry)

    def test_existing_zabbix_triggers_are_mapped_not_renamed(self):
        d = json.loads(CATALOG.read_text(encoding="utf-8"))
        indexed = {e["code"]: e for e in d["entries"]}
        self.assertIn("ANDY_WA_AUTO_REPLY_STALLED",
                      indexed["AR-1001"]["source_codes"])
        self.assertIn("ANDY_WA_REPLY_OBSERVABILITY_LOST",
                      indexed["AR-6001"]["source_codes"])
        self.assertEqual(indexed["AR-1001"]["state"], "LIVE_TRIGGER")
        self.assertEqual(indexed["AR-6001"]["state"], "LIVE_TRIGGER")

    def test_all_references_are_classes_not_dynamic_event_ids(self):
        d = json.loads(CATALOG.read_text(encoding="utf-8"))
        for entry in d["entries"]:
            for source in entry["source_codes"]:
                self.assertRegex(source, r"^[A-Z][A-Z0-9_]+$")
                self.assertNotIn("ZBX_TRIGGER:", source)

if __name__ == "__main__":
    unittest.main()
