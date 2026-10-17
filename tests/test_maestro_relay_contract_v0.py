"""No DB, HTTP, auth provider, secrets, or external network calls."""
import json
import unittest
from datetime import timezone

from attention_router.maestro.relay_contract import (
    RelayContractRejected,
    TrustedRelayBinding,
    validate_relay_envelope,
)

BIND = TrustedRelayBinding("synthetic-tenant", "zabbix", "relay-stage-1")


def sample():
    return {
        "contract_version": "occurrence-relay-v0-draft",
        "occurrence_number": 1,
        "source_system": "zabbix",
        "tenant_id": "synthetic-tenant",
        "signal": {
            "schema_version": "1",
            "event_id": "problem:1234",
            "occurred_at": "2026-10-17T12:00:00Z",
            "asset_ref": "zabbix:host:2001",
            "signal_code": "ZBX_TRIGGER:333",
            "status": "PROBLEM",
            "severity": 4,
        },
    }


def encode(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def check(envelope, *, binding=BIND, key="zabbix:1"):
    return validate_relay_envelope(
        encode(envelope), authenticated_binding=binding, idempotency_header=key
    )


class RelayEnvelopeContractTests(unittest.TestCase):
    def test_accepts_provenance_and_extracts_scoped_receipt_identity(self):
        validated = check(sample())
        self.assertEqual(validated.receipt_identity, ("synthetic-tenant", "zabbix", "relay-stage-1", 1))
        self.assertEqual(validated.idempotency_key, "zabbix:1")
        self.assertEqual(validated.status, "PROBLEM")
        self.assertEqual(validated.occurred_at.tzinfo, timezone.utc)
        self.assertEqual(len(validated.digest), 64)

    def test_canonical_digest_does_not_depend_on_json_whitespace(self):
        payload = sample()
        compact = check(payload)
        spaced = validate_relay_envelope(
            json.dumps(payload, indent=2).encode(), authenticated_binding=BIND,
            idempotency_header="zabbix:1",
        )
        self.assertEqual(compact.digest, spaced.digest)

    def test_recovery_preserves_parent_identity_without_closing_case(self):
        envelope = sample()
        envelope["occurrence_number"] = 2
        envelope["signal"].update(event_id="recovery:1235", status="RECOVERY",
                                  problem_event_id="problem:1234")
        record = check(envelope, key="zabbix:2")
        self.assertEqual(record.signal["problem_event_id"], "problem:1234")
        self.assertEqual(record.status, "RECOVERY")

    def test_rejects_untrusted_tenant_and_source(self):
        for field, value in (("tenant_id", "other-tenant"), ("source_system", "other-detector")):
            envelope = sample()
            envelope[field] = value
            with self.subTest(field=field), self.assertRaises(RelayContractRejected):
                check(envelope)

    def test_scoped_instance_identity_changes_between_installations(self):
        result_a = check(sample())
        result_b = check(sample(), binding=TrustedRelayBinding("synthetic-tenant", "zabbix", "relay-stage-2"))
        self.assertNotEqual(result_a.receipt_identity, result_b.receipt_identity)

    def test_rejects_mismatched_key_and_oversized_number(self):
        for key in ("zabbix:01", "zabbix:2", "other:1", ""):
            with self.subTest(key=key), self.assertRaises(RelayContractRejected):
                check(sample(), key=key)
        envelope = sample()
        envelope["occurrence_number"] = 2**64
        with self.assertRaises(RelayContractRejected):
            check(envelope, key="zabbix:" + str(2**64))

    def test_rejects_duplicate_json_keys(self):
        raw = encode(sample()).decode().replace(
            '"tenant_id":"synthetic-tenant",',
            '"tenant_id":"synthetic-tenant","tenant_id":"synthetic-tenant",',
        )
        with self.assertRaises(RelayContractRejected):
            validate_relay_envelope(
                raw.encode(), authenticated_binding=BIND, idempotency_header="zabbix:1"
            )

    def test_rejects_wrong_payload_types_unknown_fields_and_missing_parent(self):
        specimens = []
        data = sample()
        data["occurrence_number"] = True
        specimens.append(data)
        data = sample()
        data["signal"]["severity"] = True
        specimens.append(data)
        data = sample()
        data["signal"]["unknown_field"] = "test"
        specimens.append(data)
        data = sample()
        data["signal"]["status"] = "RECOVERY"
        specimens.append(data)
        data = sample()
        data["signal"]["occurred_at"] = "2026-10-17T12:00:00"
        specimens.append(data)
        data = sample()
        data["contract_version"] = "2"
        specimens.append(data)
        for item in specimens:
            with self.subTest(item=item), self.assertRaises(RelayContractRejected):
                check(item)

    def test_rejects_missing_auth_binding_and_excessive_body(self):
        with self.assertRaises(RelayContractRejected):
            validate_relay_envelope(
                encode(sample()), authenticated_binding=None, idempotency_header="zabbix:1"
            )
        with self.assertRaises(RelayContractRejected):
            validate_relay_envelope(
                b" " * 16385, authenticated_binding=BIND, idempotency_header="zabbix:1"
            )

    def test_rejects_nonfinite_number_and_deep_json(self):
        bad = encode(sample()).decode().replace('"severity":4', '"severity":NaN')
        with self.assertRaises(RelayContractRejected):
            validate_relay_envelope(
                bad.encode(), authenticated_binding=BIND, idempotency_header="zabbix:1"
            )
        data = sample()
        data["signal"]["summary"] = ["nested"]  # not a string
        with self.assertRaises(RelayContractRejected):
            check(data)

    def test_rejects_envelope_that_looks_like_canonical_inbound_event(self):
        data = sample()
        data["contract_type"] = "inbound_event"
        with self.assertRaises(RelayContractRejected):
            check(data)


if __name__ == "__main__":
    unittest.main()
