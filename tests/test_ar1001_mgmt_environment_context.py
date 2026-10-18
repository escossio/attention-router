"""AR-1001 environment context is non-causal; Zabbix general sources only."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "ops/provisioning/andy-ops-panel/ar1001_observability.py"
spec = importlib.util.spec_from_file_location("ar1001_context_candidate", PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

NOW = 1792299000


def core_rows() -> str:
    rows = [f"M|{key}|{NOW-60}|{int(key.endswith(('probe_ok', 'functional_stalled')))}"
            for key in module.EXPECTED_KEYS]
    return "\n".join(rows + [
        "T|ANDY_WA_AUTO_REPLY_STALLED|1",
        "T|ANDY_WA_REPLY_OBSERVABILITY_LOST|0",
    ]) + "\n"


def context_rows(*, age=30, reachable=8, tempo_logs=0) -> str:
    values = {
        "andy.summary.domains": 8,
        "andy.summary.reachable": reachable,
        "andy.summary.complete": 8,
        "andy.summary.incomplete": 0,
        "andy.tempo.query_ok": 1,
        "andy.tempo.log_probe_ok": tempo_logs,
    }
    return "".join(f"C|{key}|{NOW-age}|{value}\n" for key, value in values.items())


class Ar1001EnvironmentContextTests(unittest.TestCase):
    def test_valid_context_does_not_upgrade_unlinked_incident_facets(self):
        p = module.project(core_rows() + context_rows(), now=NOW)
        self.assertEqual(p["status"], "OBSERVED_FAILURE")
        c = p["general_context"]
        self.assertEqual(c["scope"], "INFRASTRUCTURE_GENERAL_NOT_INCIDENT_CORRELATED")
        self.assertIs(c["incident_correlated"], False)
        self.assertEqual(len(c["sensors"]), 6)
        statuses = {x["source"]: x["state"] for x in c["assessments"]}
        self.assertEqual(statuses["NETWORK_OSI_AGGREGATE"], "OBSERVED_HEALTHY")
        self.assertEqual(statuses["TEMPO_QUERY_INTERFACE"], "OBSERVED_HEALTHY")
        self.assertEqual(statuses["TEMPO_LOG_INSPECTION"], "OBSERVED_FAILURE")
        facets = {f["dimension"]: f["state"] for f in p["facets"]}
        for name in ("network", "data_link", "physical", "transport", "tracing"):
            self.assertEqual(facets[name], "NOT_OBSERVED")

    def test_network_degradation_only_changes_environment_assessment(self):
        p = module.project(core_rows() + context_rows(reachable=6), now=NOW)
        assessments = {a["source"]: a for a in p["general_context"]["assessments"]}
        self.assertEqual(assessments["NETWORK_OSI_AGGREGATE"]["state"], "OBSERVED_FAILURE")
        self.assertEqual(p["status"], "OBSERVED_FAILURE")
        self.assertEqual(next(f for f in p["facets"] if f["dimension"] == "network")["state"],
                         "NOT_OBSERVED")

    def test_general_source_stale_and_absent_do_not_hide_primary_incident(self):
        for extra in (context_rows(age=1000), ""):
            with self.subTest(extra=bool(extra)):
                p = module.project(core_rows() + extra, now=NOW)
                self.assertEqual(p["status"], "OBSERVED_FAILURE")
                self.assertTrue(all(a["state"] == "UNKNOWN"
                                    for a in p["general_context"]["assessments"]))

    def test_duplicate_or_unknown_context_is_not_trusted(self):
        for extra in (context_rows() + context_rows(),
                      context_rows() + f"C|andy.secret.value|{NOW-30}|999\n",
                      context_rows().replace("C|andy.summary.domains|", "C|andy.summary.domains|bogus|")):
            with self.subTest(fragment=extra[-42:]):
                p = module.project(core_rows() + extra, now=NOW)
                self.assertEqual(p["status"], "OBSERVED_FAILURE")
                self.assertEqual(p["general_context"]["sensors"], {})
                self.assertTrue(all(a["state"] == "UNKNOWN"
                                    for a in p["general_context"]["assessments"]))

    def test_invalid_counts_are_not_marked_healthy(self):
        broken = context_rows(reachable=9)
        p = module.project(core_rows() + broken, now=NOW)
        a = next(x for x in p["general_context"]["assessments"]
                 if x["source"] == "NETWORK_OSI_AGGREGATE")
        self.assertEqual(a["state"], "UNKNOWN")
        self.assertEqual(a["reason_code"], "OSI_GENERAL_COUNTS_INCONSISTENT")

    def test_context_query_is_read_only_with_fixed_allowed_keys(self):
        self.assertIn("BEGIN TRANSACTION READ ONLY", module.SQL)
        self.assertIn("statement_timeout = '2s'", module.SQL)
        self.assertIn("FROM history_uint", module.SQL)
        self.assertEqual(len(module.GENERAL_SENSOR_KEYS), 7)
        self.assertEqual(module.SQL.count("SELECT 'C|'"), 1)
        self.assertNotIn("UPDATE ", module.SQL)
        self.assertNotIn("DELETE ", module.SQL)
        self.assertNotIn("INSERT ", module.SQL)
        self.assertNotIn("actor_id", module.SQL)


if __name__ == "__main__":
    unittest.main()
