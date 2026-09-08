from datetime import UTC, datetime

from attention_router.application.execution import transport_status_from_payload


NOW = datetime(2026, 8, 27, 12, tzinfo=UTC)


def test_transport_status_normalizes_production_and_synthetic_payloads():
    for role in ("production", "synthetic"):
        result = transport_status_from_payload(
            {"ready": True, "client_state": "CONNECTED", "enabled": True},
            transport_id=f"{role}-transport", role=role, observed_at=NOW,
        )
        assert result.ready is True
        assert result.role == role
        assert result.fresh_until > NOW


def test_transport_status_fails_closed_for_disconnected_or_malformed_payload():
    disconnected = transport_status_from_payload(
        {"ready": False, "client_state": "DISCONNECTED"},
        transport_id="synthetic", role="synthetic", observed_at=NOW,
    )
    malformed = transport_status_from_payload(
        [], transport_id="production", role="production", observed_at=NOW,
    )
    assert disconnected.ready is False
    assert malformed.reason_code == "TRANSPORT_STATUS_MALFORMED"
    assert malformed.fresh_until == NOW
