import pytest
from pydantic import ValidationError

from attention_router.adapters.inbound import NormalizedInboundEvent
from attention_router.adapters.synthetic_inbound import SyntheticInboundAdapter


def valid_payload():
    return {
        "schema_version": "synthetic-1",
        "synthetic_event_id": "evt-1",
        "event_type": "message",
        "contact_id": "contact_mae",
        "contact_name": "Mãe Sintética",
        "relationship_category": "family_core",
        "text": "Fixture sintética.",
        "metadata": {"extra": "ok"},
        "scenario_id": "SCN-PE-001",
        "scenario_run_id": "scenario-run-1",
        "stimulus_id": "stimulus-1",
    }


def test_synthetic_adapter_valid_event():
    event = SyntheticInboundAdapter().normalize(valid_payload())
    assert event.schema_version == "1"
    assert event.source == "synthetic"
    assert event.external_event_id == "evt-1"
    assert event.lineage_classification == "SYNTHETIC"
    assert event.scenario_run_id == "scenario-run-1"
    assert event.payload_hash


def test_synthetic_adapter_requires_structural_scenario_lineage():
    payload = valid_payload()
    payload.pop("scenario_run_id")
    with pytest.raises(ValidationError):
        SyntheticInboundAdapter().normalize(payload)


def test_synthetic_adapter_missing_required_field():
    payload = valid_payload()
    payload.pop("text")
    with pytest.raises(ValidationError):
        SyntheticInboundAdapter().normalize(payload)


def test_synthetic_adapter_invalid_enum():
    payload = valid_payload()
    payload["event_type"] = "fax"
    with pytest.raises(ValidationError):
        SyntheticInboundAdapter().normalize(payload)


def test_normalized_schema_version_incompatible():
    with pytest.raises(ValidationError):
        NormalizedInboundEvent.model_validate(
            {
                "schema_version": "2",
                "source": "synthetic",
                "external_event_id": "evt",
                "event_type": "message",
                "actor_id": "actor",
                "actor_display_name": "Actor",
                "actor_category": "unknown",
                "content": "Fixture",
            }
        )


def test_metadata_additional_is_allowed_and_hash_is_canonical():
    a = SyntheticInboundAdapter().normalize(valid_payload())
    payload = valid_payload()
    payload["metadata"] = {"b": 2, "a": 1}
    b = SyntheticInboundAdapter().normalize(payload)
    payload2 = valid_payload()
    payload2["metadata"] = {"a": 1, "b": 2}
    c = SyntheticInboundAdapter().normalize(payload2)
    assert a.metadata["extra"] == "ok"
    assert b.payload_hash == c.payload_hash


def test_payload_malformed_extra_field():
    payload = valid_payload()
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        SyntheticInboundAdapter().normalize(payload)
