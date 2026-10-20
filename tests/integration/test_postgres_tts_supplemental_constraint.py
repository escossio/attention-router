"""Outbox cardinality against the migrated PostgreSQL schema (disposable CI DB)."""

from pathlib import Path
import runpy

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from attention_router.application.voice_tts import process_tts_derivations
from attention_router.config import settings
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import OutboxMessageRow, TTSDerivationRow
from tests.test_first_contact_agent import enabled, enqueue, event, visitor_decision, send, confirm
from tests.test_whatsapp_voice_flow import (
    FakeTTS,
    test_supplemental_commit_error_isolated_without_text_replay as commit_error_case,
)


pytestmark = pytest.mark.postgres


def _copy(row, *, key, action=None, parent=None):
    return OutboxMessageRow(
        id=new_id(), interaction_id=row.interaction_id,
        action_type=action or row.action_type, destination=row.destination,
        payload=dict(row.payload), status="PENDING", created_at=now_utc(),
        available_at=now_utc(), attempt_count=0, idempotency_key=key,
        correlation_id=row.correlation_id, causation_id=row.causation_id,
        execution_intent_id=row.execution_intent_id,
        supplemental_parent_outbox_id=parent,
    )


def _pair(db, monkeypatch, tmp_path):
    enabled(db, monkeypatch)
    monkeypatch.setattr(settings, "tts_enabled", True)
    monkeypatch.setattr(settings, "tts_auto_send", True)
    monkeypatch.setattr(settings, "tts_supplemental_enabled", True)
    monkeypatch.setattr(settings, "whatsapp_media_root", str(tmp_path))
    primary = enqueue(db, visitor_decision(db, event()))
    send(db, monkeypatch, primary)
    assert confirm(db, primary, ack=2)
    assert process_tts_derivations(db, "tts", client=FakeTTS()) == 1
    voice = db.scalar(select(OutboxMessageRow).where(
        OutboxMessageRow.supplemental_parent_outbox_id == primary.id))
    assert voice is not None
    return primary, voice


def test_postgres_one_primary_one_supplemental_and_no_expanded_effects(Session, monkeypatch, tmp_path):
    with Session() as db:
        primary, voice = _pair(db, monkeypatch, tmp_path)
        assert voice.execution_intent_id == primary.execution_intent_id
        assert voice.idempotency_key == f"execution:{primary.execution_intent_id}:audio"
        for extra in (
            _copy(primary, key=f"other-primary:{new_id()}"),
            _copy(voice, key=voice.idempotency_key, parent=primary.id),
            _copy(voice, key=f"execution:{primary.execution_intent_id}:audio2", parent=primary.id),
            _copy(voice, key=f"execution:{primary.execution_intent_id}:audio", action="agent_execution_text", parent=primary.id),
            _copy(voice, key=f"execution:{primary.execution_intent_id}:audio", parent=new_id()),
        ):
            with pytest.raises(IntegrityError), db.begin_nested():
                db.add(extra)
                db.flush()
        with pytest.raises(IntegrityError), db.begin_nested():
            db.delete(voice)
            db.flush()
            db.add(_copy(voice, key=voice.idempotency_key, parent=new_id()))
            db.flush()
        assert len(db.scalars(select(OutboxMessageRow)).all()) == 2
        columns = {row["name"] for row in db.execute(text(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_name='outbox_messages'"
        )).mappings()}
        assert "supplemental_parent_outbox_id" in columns
        schema = inspect(db.bind)
        indexes = {item["name"] for item in schema.get_indexes("outbox_messages")}
        checks = {item["name"] for item in schema.get_check_constraints("outbox_messages")}
        assert {"uq_outbox_primary_intent", "uq_outbox_supplemental_intent"} <= indexes
        assert "ck_outbox_supplemental_voice" in checks
        assert "uq_outbox_messages_execution_intent" not in {
            item["name"] for item in schema.get_unique_constraints("outbox_messages")
        }
        migration = runpy.run_path(str(
            Path(__file__).parents[2] / "alembic/versions/0061_tts_supplemental_outbox_constraint.py"
        ))
        monkeypatch.setattr(migration["op"], "get_bind", db.connection)
        with pytest.raises(RuntimeError, match="REQUIRES_EXPLICIT_RECONCILIATION"):
            migration["downgrade"]()


def test_postgres_reclaimed_derivation_does_not_replay_text(Session, monkeypatch, tmp_path):
    with Session() as db:
        primary, voice = _pair(db, monkeypatch, tmp_path)
        # A second claim cannot create another effect even after a process crash.
        derivation = db.scalar(select(TTSDerivationRow))
        derivation.status = "PENDING"
        db.commit()
        assert process_tts_derivations(db, "tts-restart", client=FakeTTS()) == 1
        assert len(db.scalars(select(OutboxMessageRow)).all()) == 2
        assert db.get(OutboxMessageRow, primary.id).status == "DONE"
        assert db.get(OutboxMessageRow, voice.id).idempotency_key.endswith(":audio")


def test_postgres_commit_exception_does_not_crash_derivation(Session, monkeypatch, tmp_path):
    with Session() as db:
        commit_error_case(db, monkeypatch, tmp_path)
