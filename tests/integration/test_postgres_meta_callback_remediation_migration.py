from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from attention_router.infrastructure.repository import seed_policies
from attention_router.platform.meta_callback_reconciliation import (
    admit_meta_callback_evidence,
    reconcile_due_meta_callback_outcomes,
    recover_quarantined_meta_reconciliation,
)
from tests.integration.test_postgres_meta_callback_reconciliation_v2 import (
    _accepted_attempt,
    _status,
)


pytestmark = pytest.mark.postgres
ROOT = Path(__file__).resolve().parents[2]


def _alembic(url: str, revision: str, *, check: bool = True):
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=ROOT,
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


def test_0027_to_head_blocks_all_inflight_shapes_atomically_then_upgrades(pg_url):
    database = f"attention_router_migration_{uuid4().hex[:10]}"
    base = pg_url.rsplit("/", 1)[0]
    admin = create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT", future=True)
    url = f"{base}/{database}"
    with admin.connect() as connection:
        connection.execute(text(f'create database "{database}"'))
    try:
        _alembic(url, "0027_frozen_retirement")
        engine = create_engine(url, future=True)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO tenants VALUES "
                    "('migration-tenant','migration-tenant','Migration','ACTIVE',now(),now())"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO interactions "
                    "(id,event_type,contact_id,contact_name,relationship_category,"
                    "active_context,inbound_text,state,created_at,updated_at,tenant_id) "
                    "VALUES ('migration-interaction','message','redacted','Redacted',"
                    "'UNKNOWN','{}','x','OPEN',now(),now(),'migration-tenant')"
                )
            )
            shapes = (
                ("valid", '{"provider_message_id":"provider-valid","retry_policy":"NONE"}'),
                ("incomplete", '{"retry_policy":"NONE"}'),
                ("ambiguous-a", '{"provider_message_id":"provider-shared"}'),
                ("ambiguous-b", '{"provider_message_id":"provider-shared"}'),
            )
            for suffix, payload in shapes:
                connection.execute(
                    text(
                        "INSERT INTO outbox_messages "
                        "(id,interaction_id,action_type,destination,payload,status,created_at,"
                        "available_at,attempt_count,idempotency_key) VALUES "
                        "(:id,'migration-interaction','production_conversation_reply',"
                        "'meta_whatsapp_cloud',CAST(:payload AS jsonb),'AWAITING_DELIVERY',"
                        "now(),now(),1,:key)"
                    ),
                    {
                        "id": f"migration-outbox-{suffix}",
                        "payload": payload,
                        "key": f"migration-key-{suffix}",
                    },
                )
        failed = _alembic(url, "head", check=False)
        assert failed.returncode != 0
        assert "requires zero in-flight accepted Meta delivery attempts" in failed.stderr
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0027_frozen_retirement"
            )
            assert connection.scalar(
                text("SELECT to_regclass('public.meta_delivery_reconciliations')")
            ) is None
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM outbox_messages"))
        _alembic(url, "head")
        _alembic(url, "head")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0035_whatsapp_voice_media"
            )
            indexes = set(
                connection.scalars(
                    text(
                        "SELECT indexname FROM pg_indexes WHERE tablename IN "
                        "('meta_delivery_reconciliations','meta_callback_inbox',"
                        "'meta_callback_evidence')"
                    )
                )
            )
            assert {
                "ix_meta_delivery_reconciliation_due",
                "ix_meta_callback_inbox_due",
                "ix_meta_callback_inbox_provider_state",
                "uq_meta_delivery_reconciliation_provider",
            } <= indexes
        engine.dispose()
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'drop database if exists "{database}" with (force)'))
        admin.dispose()


def test_historical_ambiguous_evidence_is_contained_redacted_and_requeued(pg_url):
    database = f"attention_router_ambiguity_{uuid4().hex[:10]}"
    base = pg_url.rsplit("/", 1)[0]
    admin = create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT", future=True)
    url = f"{base}/{database}"
    with admin.connect() as connection:
        connection.execute(text(f'create database "{database}"'))
    try:
        _alembic(url, "head")
        engine = create_engine(url, future=True)
        Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        with Session.begin() as session:
            seed_policies(session)
        attempt = _accepted_attempt(Session, suffix=uuid4().hex)
        admission = admit_meta_callback_evidence(
            Session,
            status_event=_status(attempt, "failed", 1_788_220_800),
            received_at=attempt.accepted_at,
        )
        assert admission.evidence_persisted is True

        status_audit_id = f"historical-status-{uuid4().hex}"
        with engine.begin() as connection:
            evidence = connection.execute(
                text(
                    "SELECT id, inbox_id FROM meta_callback_evidence "
                    "WHERE reconciliation_id=:reconciliation_id"
                ),
                {"reconciliation_id": attempt.reconciliation_id},
            ).mappings().one()
            inbox_id = evidence["inbox_id"]
            graph = connection.execute(
                text(
                    "SELECT r.tenant_id, o.interaction_id, o.correlation_id, "
                    "o.causation_id FROM meta_delivery_reconciliations r "
                    "JOIN outbox_messages o ON o.id=r.outbox_message_id "
                    "WHERE r.id=:reconciliation_id"
                ),
                {"reconciliation_id": attempt.reconciliation_id},
            ).mappings().one()
            before_counts = connection.execute(
                text(
                    "SELECT (SELECT count(*) FROM outbox_messages), "
                    "(SELECT count(*) FROM audit_events WHERE event_type IN "
                    "('production_conversation_reply_delivered', "
                    "'production_conversation_reply_failed'))"
                )
            ).one()
            connection.execute(
                text(
                    "ALTER TABLE meta_callback_evidence DISABLE TRIGGER "
                    "meta_callback_evidence_immutable"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE meta_callback_inbox DISABLE TRIGGER "
                    "meta_callback_inbox_lifecycle"
                )
            )
            connection.execute(
                text(
                    "UPDATE meta_callback_evidence SET inbox_id=NULL WHERE id=:id"
                ),
                {"id": evidence["id"]},
            )
            connection.execute(
                text(
                    "UPDATE meta_callback_inbox SET provider_status='delivered', "
                    "state='PENDING', reconciliation_id=NULL, correlated_at=NULL, "
                    "quarantined_at=NULL, last_error_code=NULL, claim_token=NULL, "
                    "claimed_by=NULL, claimed_at=NULL, last_requeue_audit_id=NULL "
                    "WHERE id=:id"
                ),
                {"id": inbox_id},
            )
            connection.execute(
                text(
                    "ALTER TABLE meta_callback_evidence ENABLE TRIGGER "
                    "meta_callback_evidence_immutable"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE meta_callback_inbox ENABLE TRIGGER "
                    "meta_callback_inbox_lifecycle"
                )
            )
            connection.execute(
                text(
                    "UPDATE audit_events SET payload=jsonb_build_object("
                    "'outbox_id', CAST(:outbox_id AS text), "
                    "'provider_message_id', CAST(:provider_id AS text)) "
                    "WHERE event_type='production_meta_api_accepted' "
                    "AND payload->>'outbox_id'=:outbox_id"
                ),
                {
                    "outbox_id": attempt.outbox_id,
                    "provider_id": attempt.provider_message_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO audit_events "
                    "(id, tenant_id, interaction_id, event_type, correlation_id, "
                    "causation_id, origin, payload, created_at) VALUES "
                    "(:id, :tenant_id, :interaction_id, 'meta_status_received', "
                    ":correlation_id, :causation_id, 'ingress', CAST(:payload AS jsonb), "
                    ":created_at)"
                ),
                {
                    "id": status_audit_id,
                    **graph,
                    "payload": json.dumps(
                        {
                            "id": attempt.provider_message_id,
                            "status": "failed",
                            "timestamp": "1788220800",
                            "errors": [],
                        }
                    ),
                    "created_at": attempt.accepted_at,
                },
            )

        migration_spec = importlib.util.spec_from_file_location(
            "meta_callback_release_gate_migration",
            ROOT / "alembic/versions/0030_meta_callback_release_gate_remediation.py",
        )
        assert migration_spec is not None and migration_spec.loader is not None
        migration = importlib.util.module_from_spec(migration_spec)
        migration_spec.loader.exec_module(migration)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE FUNCTION fail_rg01a_redaction() RETURNS trigger "
                    "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION "
                    "'synthetic redaction failure'; END $$"
                )
            )
            connection.execute(
                text(
                    "CREATE TRIGGER fail_rg01a_redaction BEFORE UPDATE ON audit_events "
                    "FOR EACH ROW EXECUTE FUNCTION fail_rg01a_redaction()"
                )
            )
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                migration._backfill_historical_evidence(connection)
                migration._redact_historical_status_and_shadow_audits(connection)
                migration._redact_historical_audits(connection)
        with engine.begin() as connection:
            assert connection.execute(
                text(
                    "SELECT operational_state, quarantine_reason "
                    "FROM meta_delivery_reconciliations WHERE id=:id"
                ),
                {"id": attempt.reconciliation_id},
            ).one() == ("ACTIVE", None)
            assert connection.scalar(
                text("SELECT state FROM meta_callback_inbox WHERE id=:id"),
                {"id": inbox_id},
            ) == "PENDING"
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM audit_events "
                    "WHERE payload::text LIKE '%' || :provider_id || '%'"
                ),
                {"provider_id": attempt.provider_message_id},
            ) == 2
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM audit_events WHERE event_type="
                    "'production_meta_historical_evidence_quarantined'"
                )
            ) == 0
            connection.execute(text("DROP TRIGGER fail_rg01a_redaction ON audit_events"))
            connection.execute(text("DROP FUNCTION fail_rg01a_redaction()"))
        with engine.begin() as connection:
            migration._backfill_historical_evidence(connection)
            migration._redact_historical_status_and_shadow_audits(connection)
            migration._redact_historical_audits(connection)

        with engine.connect() as connection:
            reconciliation = connection.execute(
                text(
                    "SELECT state, operational_state, quarantine_reason "
                    "FROM meta_delivery_reconciliations WHERE id=:id"
                ),
                {"id": attempt.reconciliation_id},
            ).one()
            inbox = connection.execute(
                text(
                    "SELECT state, last_error_code FROM meta_callback_inbox "
                    "WHERE id=:id"
                ),
                {"id": inbox_id},
            ).one()
            assert reconciliation == (
                "PENDING",
                "QUARANTINED",
                "HISTORICAL_EVIDENCE_INBOX_AMBIGUOUS",
            )
            assert inbox == (
                "QUARANTINED",
                "PRODUCTION_META_HISTORICAL_EVIDENCE_INBOX_AMBIGUOUS",
            )
            assert connection.scalar(
                text("SELECT inbox_id FROM meta_callback_evidence WHERE id=:id"),
                {"id": evidence["id"]},
            ) is None
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM audit_events "
                    "WHERE payload::text LIKE '%' || :provider_id || '%'"
                ),
                {"provider_id": attempt.provider_message_id},
            ) == 0
            assert connection.scalar(
                text(
                    "SELECT payload->>'provider_message_id_hash' FROM audit_events "
                    "WHERE event_type='production_meta_api_accepted' "
                    "AND payload->>'outbox_id'=:outbox_id"
                ),
                {"outbox_id": attempt.outbox_id},
            ) == hashlib.sha256(attempt.provider_message_id.encode()).hexdigest()
            assert connection.scalar(
                text(
                    "SELECT provider_message_id FROM meta_delivery_reconciliations "
                    "WHERE id=:id"
                ),
                {"id": attempt.reconciliation_id},
            ) == attempt.provider_message_id
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM audit_events WHERE event_type="
                    "'production_meta_historical_evidence_quarantined' "
                    "AND payload->>'evidence_id'=:evidence_id "
                    "AND NOT payload::text LIKE '%' || :provider_id || '%'"
                ),
                {
                    "evidence_id": evidence["id"],
                    "provider_id": attempt.provider_message_id,
                },
            ) == 1

        sweep = reconcile_due_meta_callback_outcomes(
            Session,
            worker_id="migration-quarantine-test",
            now=datetime.now(UTC),
            limit=100,
        )
        assert sweep.selected == 0
        with pytest.raises(DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE meta_delivery_reconciliations SET "
                        "operational_state='ACTIVE', quarantined_at=NULL, "
                        "quarantine_reason=NULL WHERE id=:id"
                    ),
                    {"id": attempt.reconciliation_id},
                )
        with Session.begin() as session:
            assert recover_quarantined_meta_reconciliation(
                session,
                reconciliation_id=attempt.reconciliation_id,
                operator_id="synthetic-migration-reviewer",
                reason="REVIEWED_SAFE_RETRY",
            ) is True
        with Session.begin() as session:
            assert recover_quarantined_meta_reconciliation(
                session,
                reconciliation_id=attempt.reconciliation_id,
                operator_id="synthetic-migration-reviewer",
                reason="REVIEWED_SAFE_RETRY",
            ) is False
        with engine.connect() as connection:
            after_counts = connection.execute(
                text(
                    "SELECT (SELECT count(*) FROM outbox_messages), "
                    "(SELECT count(*) FROM audit_events WHERE event_type IN "
                    "('production_conversation_reply_delivered', "
                    "'production_conversation_reply_failed'))"
                )
            ).one()
            assert after_counts == before_counts
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM audit_events WHERE event_type="
                    "'production_meta_reconciliation_requeued' "
                    "AND payload->>'reconciliation_id'=:id "
                    "AND NOT payload::text LIKE '%' || :provider_id || '%'"
                ),
                {
                    "id": attempt.reconciliation_id,
                    "provider_id": attempt.provider_message_id,
                },
            ) == 1
        engine.dispose()
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'drop database if exists "{database}" with (force)'))
        admin.dispose()
