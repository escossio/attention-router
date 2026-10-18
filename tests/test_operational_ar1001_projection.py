"""Regression tests for a single-cycle AR-1001 enriched Zabbix publication."""
import unittest
from ops.observability.failure_catalog.ar1001_metric_projection import (
    LEGACY_FIELDS, ENRICHED_KEYS, parse_integrated_psql_row,
)


class IntegratedAr1001ProjectionTests(unittest.TestCase):
    def test_legacy_fields_are_first_seven_and_new_ones_are_separate(self):
        legacy, metrics = parse_integrated_psql_row(
            "6|5|5|0|1792000000|1792000050|1792000008|7|6|6|5|1|4|1\n"
        )
        self.assertEqual(tuple(legacy), LEGACY_FIELDS)
        self.assertEqual(tuple(metrics), ENRICHED_KEYS)
        self.assertEqual(legacy["direct_6h"], 6)
        self.assertEqual(legacy["latest_failure_ts"], 1792000000)
        self.assertEqual(metrics["andy.reply.direct_event_6h"], 7)
        self.assertEqual(metrics["andy.reply.direct_processed_6h"], 6)
        self.assertEqual(metrics["andy.reply.decisions_dry_run_6h"], 5)
        self.assertEqual(metrics["andy.reply.outbox_new_6h"], 1)

    def test_zero_is_measured_zero_only_when_the_query_completed(self):
        legacy, metrics = parse_integrated_psql_row("0|0|0|0|0|0|0|0|0|0|0|0|0|0\n")
        self.assertTrue(all(value == 0 for value in legacy.values()))
        self.assertTrue(all(value == 0 for value in metrics.values()))
        for invalid in ("", "\n", "0|0|0", "0|" * 14, "ERROR:\n"):
            with self.subTest(invalid=invalid[:12]):
                with self.assertRaises(ValueError):
                    parse_integrated_psql_row(invalid)

    def test_no_ambiguous_or_leaky_values(self):
        baseline = ["6", "5", "5", "0", "1792000000", "1792000050",
                    "1792000008", "7", "6", "6", "5", "1", "4", "1"]
        for pos, value in [
            (0, "-1"), (0, "1.5"), (0, "01a"), (0, "１２"),
            (0, "99999999999999999999999999"), (7, "0"), (9, "4"),
        ]:
            data = baseline.copy()
            data[pos] = value
            with self.subTest(pos=pos, value=value):
                with self.assertRaises(ValueError) as cm:
                    parse_integrated_psql_row("|".join(data))
                if len(value) > 10:
                    self.assertNotIn(value, str(cm.exception))
        with self.assertRaises(ValueError):
            parse_integrated_psql_row("|".join(baseline)+"\n"+"|".join(baseline))

    def test_item_manifest_agrees_with_parser_and_zabbix_plan(self):
        import json
        from pathlib import Path

        base = Path(__file__).resolve().parents[1] / "ops/observability/failure-catalog"
        manifest = json.loads((base / "zabbix-items.ar-1001.v0.json").read_text())
        plan = json.loads((base / "zabbix-plan.ar-1001.v0.json").read_text())
        self.assertEqual(manifest["status"], "CANDIDATE_NOT_PROVISIONED")
        self.assertEqual([entry["key"] for entry in manifest["items"]], list(ENRICHED_KEYS))
        self.assertEqual(manifest["item_policy"]["zabbix_item_type"], 2)
        self.assertEqual(manifest["item_policy"]["zabbix_value_type"], 3)
        planned = {entry["key"] for entry in plan["proposed_observations"]}
        self.assertTrue(set(ENRICHED_KEYS).issubset(planned))
        self.assertEqual(manifest["input_sql"], plan["integrated_single_cycle_sql"])

    def test_integrated_query_preserves_legacy_first_seven_columns(self):
        from pathlib import Path
        path = (Path(__file__).resolve().parents[1] /
                "ops/observability/failure-catalog/queries/ar-1001-integrated-readonly.v0.sql")
        sql = path.read_text(encoding="utf-8")
        self.assertIn("BEGIN TRANSACTION READ ONLY", sql)
        self.assertIn("SET LOCAL statement_timeout = '3s'", sql)
        for key in ("legacy_direct_6h", "legacy_no_safe_6h", "legacy_blueprint_6h",
                    "legacy_other_failed_6h", "legacy_latest_failure_epoch",
                    "legacy_last_agent_done_epoch", "legacy_latest_direct_epoch"):
            self.assertIn(" AS " + key, sql)
        self.assertEqual(len(ENRICHED_KEYS), 7)

    def test_metadata_only_no_secret_or_provider_dependencies(self):
        self.assertFalse(any("token" in k or "actor" in k or "body" in k for k in ENRICHED_KEYS))
        self.assertEqual(len(LEGACY_FIELDS) + len(ENRICHED_KEYS), 14)


if __name__ == "__main__":
    unittest.main()
