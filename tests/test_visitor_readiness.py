"""Offline, synthetic-only AR-1002 checks."""
import unittest

from attention_router.observability.visitor_readiness import REQUIRED, classify


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.good = dict.fromkeys(REQUIRED, True)

    def test_all_verified(self):
        result = classify(self.good, collector_ok=True, snapshot_consistent=True)
        self.assertEqual(result["state"], "READY")
        self.assertFalse(result["whatsapp_delivery_proven"])

    def test_each_broken_gate(self):
        for key in REQUIRED:
            with self.subTest(key=key):
                values = self.good | {key: False}
                result = classify(values, collector_ok=True, snapshot_consistent=True)
                self.assertEqual(result["state"], "BROKEN")
                self.assertEqual(result["failed_checks"], [key])

    def test_missing_and_null_are_not_ready(self):
        for values in ({}, self.good | {"default_configured": None}):
            with self.subTest(values=len(values)):
                self.assertEqual(classify(values, collector_ok=True, snapshot_consistent=True)["state"], "DEGRADED")

    def test_collection_failure_overrides_any_violation(self):
        self.assertEqual(classify(self.good | {"default_configured": False},
                                  collector_ok=False, snapshot_consistent=True)["state"], "UNKNOWN")

    def test_inconsistent_snapshot_not_broken(self):
        self.assertEqual(classify(self.good | {"version_checksum_valid": False},
                                  collector_ok=True, snapshot_consistent=False)["state"], "UNKNOWN")

    def test_maintenance_is_not_healthy(self):
        self.assertEqual(classify(self.good, collector_ok=True, snapshot_consistent=True,
                                  maintenance=True)["state"], "UNKNOWN")

    def test_reject_unexpected_or_invalid_data(self):
        with self.assertRaises(ValueError):
            classify(self.good | {"contact_id": True}, collector_ok=True, snapshot_consistent=True)
        with self.assertRaises(ValueError):
            classify(self.good | {"default_configured": 1}, collector_ok=True, snapshot_consistent=True)


if __name__ == "__main__":
    unittest.main()
