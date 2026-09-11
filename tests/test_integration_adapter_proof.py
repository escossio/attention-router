from datetime import datetime, timezone

import pytest

from attention_router.application.platform.integrations import (
    artifact_contract_to_input,
    inbound_contract_to_envelope,
)
from attention_router.contracts.integration import InboundIntegrationEvent
from attention_router.core.events import EventOrigin
from attention_router.integrations.channel_adapters import (
    AdapterNormalizationError,
    EmailNormalizedAdapter,
    WhatsAppNormalizedAdapter,
)


TENANT = "00000000-0000-4000-8000-000000000001"
NOW = datetime(2026, 9, 11, 18, 0, tzinfo=timezone.utc)


def _whatsapp_payload() -> dict:
    return {
        "schema_version": "1",
        "source": "wwebjs",
        "external_event_id": "wamid.synthetic.adapter-proof",
        "event_type": "message",
        "occurred_at": "2026-09-11T17:59:00Z",
        "external_actor_id": "5500000000029@c.us",
        "channel": "whatsapp",
        "message_type": "chat",
        "content": "Relatório recebido.",
        "has_media": False,
        "metadata": {
            "provider": "wwebjs",
            "message_type": "chat",
            "from_me": False,
            "has_media": False,
        },
        "identity": {
            "source_message_id": "wamid.synthetic.adapter-proof",
            "canonical_message_id": "wamid.synthetic.adapter-proof",
            "idempotency_key": "wwebjs:wamid.synthetic.adapter-proof",
            "identity_hash": "synthetic-hash",
        },
    }


def _email_payload() -> dict:
    return {
        "message_id": "mail.synthetic.adapter-proof",
        "thread_id": "thread.synthetic.reports",
        "from": {
            "address": "employee@example.invalid",
            "name": "Synthetic Employee",
        },
        "to": ["owner@example.invalid"],
        "subject": "Relatório mensal",
        "body_ref": "opaque/email/body/ref",
        "sent_at": "2026-09-11T17:58:00Z",
        "attachments": [
            {
                "attachment_id": "attachment-1",
                "content_sha256": "a" * 64,
                "artifact_kind": "spreadsheet",
                "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "size_bytes": 2048,
                "storage_provider": "s3",
                "storage_reference": "tenant/opaque/object/ref",
                "original_filename": "synthetic-report.xlsx",
            }
        ],
    }


def test_whatsapp_and_email_emit_the_same_neutral_event_contract():
    whatsapp = WhatsAppNormalizedAdapter().normalize(
        _whatsapp_payload(),
        tenant_id=TENANT,
        instance_id="wwebjs-local",
        account_id="owner-whatsapp",
        received_at=NOW,
    )
    email = EmailNormalizedAdapter().normalize(
        _email_payload(),
        tenant_id=TENANT,
        instance_id="mail-provider-a",
        account_id="owner-email",
        received_at=NOW,
    )

    assert isinstance(whatsapp.event, InboundIntegrationEvent)
    assert isinstance(email.event, InboundIntegrationEvent)
    assert whatsapp.event.tenant_id == email.event.tenant_id == TENANT
    assert whatsapp.event.contract_type == email.event.contract_type == "inbound_event"
    assert whatsapp.event.schema_version == email.event.schema_version == "1"
    assert whatsapp.event.source.name == "channel.whatsapp"
    assert email.event.source.name == "channel.email"

    wa_envelope = inbound_contract_to_envelope(
        whatsapp.event, resolved_actor_id="actor-whatsapp"
    )
    mail_envelope = inbound_contract_to_envelope(
        email.event, resolved_actor_id="actor-email"
    )
    assert wa_envelope.origin == mail_envelope.origin == EventOrigin.EXTERNAL_INBOUND
    assert wa_envelope.tenant_id == mail_envelope.tenant_id == TENANT


def test_provider_payload_cannot_choose_or_override_tenant():
    whatsapp_raw = {**_whatsapp_payload(), "tenant_id": "attacker-tenant"}
    email_raw = {**_email_payload(), "tenant_id": "attacker-tenant"}

    with pytest.raises(AdapterNormalizationError, match="UNTRUSTED_TENANT_FIELD_FORBIDDEN"):
        WhatsAppNormalizedAdapter().normalize(
            whatsapp_raw,
            tenant_id=TENANT,
            instance_id="wwebjs-local",
            received_at=NOW,
        )
    with pytest.raises(AdapterNormalizationError, match="UNTRUSTED_TENANT_FIELD_FORBIDDEN"):
        EmailNormalizedAdapter().normalize(
            email_raw,
            tenant_id=TENANT,
            instance_id="mail-provider-a",
            received_at=NOW,
        )


def test_email_attachment_uses_the_existing_artifact_plane_contract():
    output = EmailNormalizedAdapter().normalize(
        _email_payload(),
        tenant_id=TENANT,
        instance_id="mail-provider-a",
        account_id="owner-email",
        received_at=NOW,
    )

    assert output.event.metadata_sanitized["attachment_count"] == 1
    assert len(output.artifact_receipts) == 1
    receipt = output.artifact_receipts[0]
    assert receipt.source.name == "channel.email"
    assert receipt.artifact_kind == "SPREADSHEET"
    assert receipt.original_filename == "synthetic-report.xlsx"

    artifact_input = artifact_contract_to_input(
        receipt, resolved_sender_actor_id="actor-employee"
    )
    assert artifact_input.tenant_id == TENANT
    assert artifact_input.source_channel == "channel.email"
    assert artifact_input.sender_actor_id == "actor-employee"
    assert artifact_input.storage_reference == "tenant/opaque/object/ref"


def test_external_provider_identity_stays_outside_canonical_actor_until_resolution():
    output = EmailNormalizedAdapter().normalize(
        _email_payload(),
        tenant_id=TENANT,
        instance_id="mail-provider-a",
        received_at=NOW,
    )

    unresolved = inbound_contract_to_envelope(output.event)
    resolved = inbound_contract_to_envelope(
        output.event, resolved_actor_id="actor-canonical-123"
    )

    assert unresolved.actor_id is None
    assert resolved.actor_id == "actor-canonical-123"
    assert "employee@example.invalid" not in str(resolved.metadata_sanitized)
    assert "thread.synthetic.reports" not in str(resolved.metadata_sanitized)


def test_current_wwebjs_normalized_shape_is_accepted_without_engine_branching():
    output = WhatsAppNormalizedAdapter().normalize(
        _whatsapp_payload(),
        tenant_id=TENANT,
        instance_id="wwebjs-local",
        received_at=NOW,
    )

    assert output.event.external_event_id == "wamid.synthetic.adapter-proof"
    assert output.event.actor.external_actor_id == "5500000000029@c.us"
    assert output.event.idempotency_key == "wwebjs:wamid.synthetic.adapter-proof"
    assert output.event.metadata_sanitized == {
        "message_type": "chat",
        "has_media": False,
        "from_me": False,
        "body_present": True,
    }
