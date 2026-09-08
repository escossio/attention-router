from datetime import timedelta

from sqlalchemy import select

from attention_router.adapters.inbound import NormalizedInboundEvent
from attention_router.application import services
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import ActorBindingRow, AuditEventRow, InteractionRow, OutboxMessageRow
from attention_router.infrastructure.repository import upsert_actor_binding
from scripts.seed_actor_bindings import seed_actor_bindings


def latest_attention_audit(session, interaction_id):
    return session.scalars(
        select(AuditEventRow)
        .where(
            AuditEventRow.interaction_id == interaction_id,
            AuditEventRow.event_type == "attention_logic_decision_v1",
        )
        .order_by(AuditEventRow.created_at.desc())
    ).first()


def create_message(session, actor_id="synthetic_unknown", text="Mensagem sintética."):
    result = services.receive_normalized_inbound_event(
        session,
        NormalizedInboundEvent(
            source="test",
            external_event_id=f"evt-{actor_id}-{new_id()}",
            event_type="message",
            actor_id=actor_id,
            actor_display_name="Ator Sintético",
            actor_category="synthetic",
            channel="synthetic",
            content=text,
        ),
    )
    session.commit()
    return result


def bind_actor(session, actor_id, actor_key, display_name, priority="normal", test_allowed=True):
    return upsert_actor_binding(
        session,
        source="test",
        external_actor_id=actor_id,
        actor_key=actor_key,
        actor_category="known",
        display_name=display_name,
        metadata={
            "actor_alias": actor_key,
            "display_name": display_name,
            "relationship": "fixture",
            "role": "synthetic_contact",
            "priority": priority,
            "test_allowed": test_allowed,
            "policy_id": "attention_default_v1",
        },
    )


def test_unknown_actor_single_message(session):
    result = create_message(session)
    audit = latest_attention_audit(session, result["id"])
    assert audit.payload["attention_level"] == "normal"
    assert audit.payload["signals"]["message_count_recent"] == 1
    assert audit.payload["reason_codes"] == ["NORMAL_SINGLE_MESSAGE"]


def test_known_normal_priority_actor(session):
    bind_actor(session, "normal-ext", "actor_normal_fixture", "Contato Normal Sintético")
    result = create_message(session, "normal-ext")
    audit = latest_attention_audit(session, result["id"])
    assert audit.payload["display_name"] == "Contato Normal Sintético"
    assert audit.payload["signals"]["actor_priority"] == "normal"
    assert audit.payload["attention_level"] == "normal"


def test_data_driven_seed_known_actor_and_idempotent(session):
    entries = [
        {
            "source": "test",
            "external_actor_id": "seed-known-ext",
            "actor_key": "actor_seed_known",
            "actor_category": "known",
            "display_name": "Contato Data Driven",
            "metadata": {
                "actor_alias": "contato_data_driven",
                "display_name": "Contato Data Driven",
                "relationship": "fixture",
                "role": "contact",
                "priority": "normal",
                "test_allowed": True,
                "policy_id": "attention_default_v1",
            },
            "is_active": True,
        }
    ]
    seed_actor_bindings(session, entries)
    seed_actor_bindings(session, entries)
    rows = session.scalars(select(ActorBindingRow).where(ActorBindingRow.external_actor_id == "seed-known-ext")).all()
    assert len(rows) == 1
    result = create_message(session, "seed-known-ext")
    audit = latest_attention_audit(session, result["id"])
    assert audit.payload["display_name"] == "Contato Data Driven"
    assert audit.payload["signals"]["actor_priority"] == "normal"


def test_high_priority_synthetic_morgan_isolated(session):
    bind_actor(session, "morgan-ext", "actor_sr_morgan_fixture", "Sr. Morgan Example", "high")
    result = create_message(session, "morgan-ext")
    audit = latest_attention_audit(session, result["id"])
    assert audit.payload["attention_level"] == "medium"
    assert audit.payload["reason_codes"] == ["KNOWN_HIGH_PRIORITY_ACTOR"]


def test_metadata_change_modifies_decision_without_engine_code_change(session):
    bind_actor(session, "metadata-ext", "actor_metadata_fixture", "Contato Metadata", "normal")
    first = create_message(session, "metadata-ext")
    first_audit = latest_attention_audit(session, first["id"])
    assert first_audit.payload["attention_level"] == "normal"
    bind_actor(session, "metadata-ext", "actor_metadata_fixture", "Contato Metadata", "high")
    second = create_message(session, "metadata-ext")
    second_audit = latest_attention_audit(session, second["id"])
    assert second_audit.payload["signals"]["actor_priority"] == "high"
    assert "KNOWN_HIGH_PRIORITY_ACTOR" in second_audit.payload["reason_codes"]


def test_high_priority_synthetic_morgan_rapid_repeat(session):
    bind_actor(session, "morgan-repeat-ext", "actor_sr_morgan_repeat_fixture", "Sr. Morgan Example", "high")
    create_message(session, "morgan-repeat-ext", "Primeira mensagem sintética.")
    result = create_message(session, "morgan-repeat-ext", "Segunda mensagem sintética?")
    audit = latest_attention_audit(session, result["id"])
    assert audit.payload["attention_level"] == "high"
    assert "KNOWN_HIGH_PRIORITY_ACTOR" in audit.payload["reason_codes"]
    assert "RAPID_REPEAT" in audit.payload["reason_codes"]


def test_urgency_keyword_with_repeat(session):
    create_message(session, "urgent-ext", "Primeira mensagem sintética.")
    result = create_message(session, "urgent-ext", "Isso e urgente?")
    audit = latest_attention_audit(session, result["id"])
    assert audit.payload["attention_level"] == "high"
    assert "URGENCY_KEYWORD" in audit.payload["reason_codes"]


def test_expired_window_does_not_count_as_repeat(session):
    first = create_message(session, "expired-ext", "Mensagem antiga sintética.")
    row = session.get(InteractionRow, first["id"])
    row.created_at = now_utc() - timedelta(seconds=services.settings.attention_recent_window_seconds + 30)
    row.updated_at = row.created_at
    session.commit()
    result = create_message(session, "expired-ext", "Mensagem nova sintética.")
    audit = latest_attention_audit(session, result["id"])
    assert audit.payload["signals"]["message_count_recent"] == 1
    assert audit.payload["signals"]["is_repeated_contact"] is False
    assert audit.payload["reason_codes"] == ["NORMAL_SINGLE_MESSAGE"]


def test_attention_policy_version_is_correct(session):
    result = create_message(session, "version-ext")
    audit = latest_attention_audit(session, result["id"])
    assert audit.payload["policy_version"] == "attention_logic_v1:1"


def test_no_wwebjs_outbound_created_by_attention_logic(session):
    result = create_message(session, "no-wwebjs-ext", "Mensagem sintética.")
    rows = session.scalars(
        select(OutboxMessageRow).where(
            OutboxMessageRow.interaction_id == result["id"],
            OutboxMessageRow.destination == "wwebjs",
        )
    ).all()
    audit = latest_attention_audit(session, result["id"])
    assert rows == []
    assert audit.payload["auto_action_enabled"] is False


def test_test_not_allowed_actor_still_gets_dry_run_decision(session):
    bind_actor(session, "taylor-synthetic-ext", "actor_taylor_synthetic", "Taylor", "normal", test_allowed=False)
    result = create_message(session, "taylor-synthetic-ext", "Mensagem sintética de contato protegido.")
    audit = latest_attention_audit(session, result["id"])
    assert result["contact"]["display_name"] == "Taylor"
    assert audit.payload["display_name"] == "Taylor"
    assert audit.payload["attention_level"] == "normal"
    assert audit.payload["auto_action_enabled"] is False


def test_taylor_is_not_live_fixture(session):
    bindings = session.scalars(select(ActorBindingRow).where(ActorBindingRow.display_name == "Taylor")).all()
    assert bindings == []


def test_no_human_name_hardcoded_in_attention_logic():
    from pathlib import Path

    source = Path("attention_router/domain/attention_logic.py").read_text(encoding="utf-8")
    assert "Morgan" not in source
    assert "Taylor" not in source
    assert "Example" not in source
