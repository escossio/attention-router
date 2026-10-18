"""Guards para SQL de progresso AR-1001; sem executar DB real."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
QUERY = ROOT / "ops/observability/failure-catalog/queries/ar-1001-readonly-progress.v0.sql"
EXPECTED = (
    "direct_event_count",
    "direct_processed_count",
    "decisions_total",
    "decisions_insufficient_dry_run",
    "outbox_created_count",
    "grace_released_count",
    "grace_canceled_count",
)


class Ar1001ReadOnlyProbeV0Tests(unittest.TestCase):
    def test_query_must_remain_bounded_read_only(self):
        query = QUERY.read_text(encoding="utf-8")
        uncommented = "\n".join(line.split("--", 1)[0] for line in query.splitlines())
        self.assertEqual(len(re.findall(r"\bBEGIN TRANSACTION READ ONLY\b", uncommented, re.I)), 1)
        self.assertRegex(uncommented, r"(?i)\bSET LOCAL statement_timeout\s*=\s*'3s'")
        self.assertEqual(len(re.findall(r"\bCOMMIT\s*;", uncommented, re.I)), 1)
        for verb in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "DROP",
                     "ALTER", "CREATE", "COPY", "EXECUTE", "DO", "GRANT",
                     "REVOKE", "pg_sleep", "pg_read_file"):
            with self.subTest(verb=verb):
                self.assertNotRegex(uncommented, r"\b" + verb + r"\b")
        self.assertIn("now() - interval '6 hours'", uncommented)
        self.assertNotIn("SELECT *", uncommented.upper())
        self.assertEqual(len(re.findall(r"\bSELECT\b", uncommented, re.I)) > 0, True)

    def test_aliases_and_count_types_are_explicit(self):
        query = QUERY.read_text(encoding="utf-8")
        for alias in EXPECTED:
            self.assertRegex(query, r"\bAS\s+" + alias + r"\b")
        self.assertNotIn("payload::text", query)
        self.assertNotIn("proposed_response", query)
        self.assertIn("source='wwebjs'", query)
        self.assertIn("processed_at IS NOT NULL", query)
        self.assertIn("decision_type='INSUFFICIENT_CONTEXT'", query)
        self.assertIn("status='DRY_RUN'", query)

    def test_no_new_runtime_or_admission_flag_in_query(self):
        query = QUERY.read_text(encoding="utf-8")
        self.assertNotIn("MAESTRO", query.upper())
        self.assertNotIn("zabbix_sender", query)
        self.assertNotIn("https://", query)
        self.assertNotIn("http://", query)


if __name__ == "__main__":
    unittest.main()
