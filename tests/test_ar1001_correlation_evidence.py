"""Synthetic-only tests for AR-1001 trace-correlation evidence levels."""
from datetime import datetime, timedelta, timezone
import json
import unittest
from ops.observability.failure_catalog.ar1001_correlation_evidence import classify

ROOT = datetime(2026, 10, 18, 7, 0, tzinfo=timezone.utc)
ENTRY = "11111111-1111-4111-8111-111111111111"
AUDIT = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"


def witness(**overrides):
    data = {
        "inbound_source": "wwebjs",
        "inbound_interaction_id": "synthetic-interaction-A",
        "audit_interaction_id": "synthetic-interaction-A",
        "same_tenant": True,
        "failure_event_type": "behavior.unavailable",
        "inbound_correlation_id": ENTRY,
        "audit_correlation_id": AUDIT,
        "fetched_span_correlation_id": ENTRY,
        "fetched_span_attribute_name": "roc.correlation_id",
        "fetched_span_name": "attention.message",
        "inbound_at": ROOT,
        "audit_at": ROOT + timedelta(minutes=4),
        "span_at": ROOT + timedelta(seconds=15),
        "tempo_fetch_succeeded": True,
    }
    data.update(overrides)
    return classify(**data)


class CorrelationEvidenceTests(unittest.TestCase):
    def test_same_interaction_input_not_same_failed_operation(self):
        r = witness()
        self.assertEqual(r["level"], "SAME_INTERACTION_INPUT")
        self.assertFalse(r["audit_correlation_matched"])
        self.assertFalse(r["zabbix_event_member_proven"])
        self.assertFalse(r["causal_origin_proven"])
        self.assertEqual(r["span_names"], ["attention.message"])

    def test_explicit_audit_span_can_prove_audit_correlated_operation(self):
        r = witness(fetched_span_correlation_id=AUDIT, span_at=ROOT + timedelta(minutes=4),
                    fetched_span_name="roc.DECISION")
        self.assertEqual(r["level"], "SAME_AUDIT_CORRELATION")
        self.assertTrue(r["audit_correlation_matched"])
        self.assertFalse(r["failure_stage_proven"])
        self.assertFalse(r["causal_origin_proven"])
        self.assertFalse(r["zabbix_event_member_proven"])

    def test_non_matching_ids_and_timing_are_not_proof(self):
        for change in (
            {"fetched_span_correlation_id": OTHER},
            {"fetched_span_correlation_id": AUDIT, "span_at": ROOT + timedelta(hours=2)},
            {"fetched_span_correlation_id": ENTRY, "span_at": ROOT + timedelta(hours=2)},
            {"fetched_span_attribute_name": "http.status_code"},
            {"fetched_span_name": "unknown.operation"},
            {"fetched_span_correlation_id": None},
        ):
            with self.subTest(change=str(change)):
                self.assertEqual(witness(**change)["level"], "NOT_PROVEN")

    def test_nearest_timestamps_never_establish_link(self):
        r = witness(fetched_span_correlation_id=OTHER,
                    inbound_at=ROOT,
                    audit_at=ROOT,
                    span_at=ROOT)
        self.assertEqual(r["level"], "NOT_PROVEN")

    def test_wrong_interaction_tenant_or_event_is_rejected(self):
        for change in (
            {"inbound_source": "email"},
            {"same_tenant": False},
            {"audit_interaction_id": "another-interaction"},
            {"failure_event_type": "heartbeat"},
            {"inbound_interaction_id": None},
        ):
            with self.subTest(change=str(change)):
                self.assertEqual(witness(**change)["level"], "REJECTED_SCOPE")

    def test_missing_trace_or_untrusted_identifiers_fail_closed(self):
        self.assertEqual(witness(tempo_fetch_succeeded=False)["level"], "UNKNOWN")
        self.assertEqual(witness(audit_correlation_id="not-a-uuid")["level"], "UNKNOWN")
        self.assertEqual(witness(inbound_at=ROOT.replace(tzinfo=None))["level"], "UNKNOWN")
        self.assertEqual(witness(audit_at=ROOT-timedelta(hours=1))["level"], "UNKNOWN")

    def test_output_never_discloses_private_identifiers(self):
        r = witness(inbound_interaction_id="synthetic-private-interaction",
                    audit_interaction_id="synthetic-private-interaction")
        visible = json.dumps(r)
        for secret in (ENTRY, AUDIT, OTHER, "synthetic-private-interaction"):
            self.assertNotIn(secret, visible)
        self.assertEqual(r["scope"],
                         "ONE_INTERACTION_WITH_FAILURE_AUDIT_NOT_ZABBIX_PROBLEM_CAUSE")

if __name__ == "__main__":
    unittest.main()
