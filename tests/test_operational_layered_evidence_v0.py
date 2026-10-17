"""Valida artefatos V0 de evidências; não acessa Zabbix/AGT/DB."""
import json
import unittest
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
DIR = ROOT / "ops/observability/failure-catalog"


def load(name):
    return json.loads((DIR / name).read_text(encoding="utf-8"))


class LayeredEvidenceV0Tests(unittest.TestCase):
    def test_layers_are_unique_and_linked_to_known_sources(self):
        dimensions = load("layered-dimensions.v0.json")["dimensions"]
        sources = load("evidence-sources.v0.json")["sources"]
        source_ids = [s["source_id"] for s in sources]
        self.assertEqual(len(source_ids), len(set(source_ids)))
        ids = [d["dimension"] for d in dimensions]
        self.assertEqual(len(ids), len(set(ids)))
        for dimension in dimensions:
            self.assertTrue(set(dimension["sources"]).issubset(source_ids))
            self.assertTrue(all(type(layer) is int and 1 <= layer <= 7
                                for layer in dimension["osi_ref"]))
            self.assertTrue(dimension["caution"])
        self.assertIn("manual_packet_evidence", source_ids)
        self.assertEqual(
            next(s for s in sources if s["source_id"] == "manual_packet_evidence")["mode"],
            "on_demand",
        )

    def test_example_has_semantically_sound_states(self):
        example = load("examples/synthetic-ar-1001.v0.json")
        schema = load("layered-evidence.v0.schema.json")
        source_ids = {s["source_id"] for s in load("evidence-sources.v0.json")["sources"]}
        dimensions = {d["dimension"] for d in load("layered-dimensions.v0.json")["dimensions"]}
        catalog = {e["code"] for e in load("catalog.v0.json")["entries"]}
        states = set(schema["properties"]["facets"]["items"]["properties"]["state"]["enum"])
        bases = set(schema["properties"]["facets"]["items"]["properties"]["basis"]["enum"])
        self.assertEqual(example["schema_version"], "0.1.0-draft")
        self.assertIn(example["fault_code"], catalog)
        self.assertTrue(example["source_event_ref"].startswith("synthetic:"))
        self.assertLessEqual(
            datetime.fromisoformat(example["window"]["from"].replace("Z", "+00:00")),
            datetime.fromisoformat(example["window"]["to"].replace("Z", "+00:00")),
        )
        seen = set()
        for facet in example["facets"]:
            key = facet["dimension"]
            self.assertIn(key, dimensions)
            self.assertNotIn(key, seen)
            seen.add(key)
            self.assertIn(facet["state"], states)
            self.assertIn(facet["basis"], bases)
            self.assertTrue(set(facet["source_ids"]).issubset(source_ids))
            self.assertLessEqual(len(facet["evidence_refs"]), 6)
            if facet["state"] in {"OBSERVED_HEALTHY", "OBSERVED_FAILURE",
                                  "NO_RELATED_ANOMALY"}:
                self.assertNotEqual(facet["basis"], "NONE")
                self.assertTrue(facet["source_ids"])
                self.assertTrue(facet["evidence_refs"])
                self.assertTrue(facet["measured_at"])
                self.assertIn("sample_age_seconds", facet)
            if facet["state"] == "NOT_OBSERVED":
                self.assertEqual(facet["basis"], "NONE")
                self.assertEqual(facet["source_ids"], [])
                self.assertEqual(facet["evidence_refs"], [])
        self.assertEqual(
            next(f for f in example["facets"] if f["dimension"] == "tracing")["state"],
            "NOT_OBSERVED",
        )

    def test_catalog_and_zabbix_plan_preserve_native_items(self):
        plan = load("zabbix-plan.ar-1001.v0.json")
        self.assertEqual(plan["status"], "PLANNING_ONLY_NOT_PROVISIONED")
        self.assertEqual(plan["first_case"], "AR-1001")
        items = {i["key"] for i in plan["existing_items"]}
        self.assertIn("andy.reply.functional_stalled", items)
        self.assertIn("andy.reply.probe_ok", items)
        existing = items
        proposed = {i["key"] for i in plan["proposed_observations"]}
        self.assertFalse(existing & proposed)
        self.assertTrue(all(i["available"] == "TO_VERIFY"
                            for i in plan["proposed_observations"]))
        self.assertTrue(all(t["scope"] == "NEW_EVENTS_AFTER_EXPLICIT_ROLLOUT"
                            for t in plan["planned_event_tags"]))


if __name__ == "__main__":
    unittest.main()
