"""Database integrity and authorization continuity for the contextual pilot."""
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

import test_postgres_context_transactions as transactions
from attention_router.application.candidate_insights import candidate_insight_source_is_current
from attention_router.infrastructure.candidate_insight_models import CandidateInsightRow
from test_cognitive_observation import START

world = transactions.world
boundary = transactions.boundary
pytestmark = pytest.mark.postgres


def test_legacy_sql_cannot_erase_generation(Session, world):
    with Session.begin() as session:
        token = boundary(session).revoke_message(world["ids"][0])
    with Session() as session:
        with pytest.raises(DBAPIError, match="CONTEXT_GENERATION_REGRESSION"):
            session.execute(text("UPDATE actor_bindings SET metadata='{}' WHERE id='fixture-owner'"))
        session.rollback()
        assert boundary(session).token() == token


def test_context_journal_rejects_row_deletion(Session, world):
    with Session.begin() as session:
        boundary(session).revoke_message(world["ids"][0])
    with Session() as session:
        with pytest.raises(DBAPIError, match="CONTEXT_JOURNAL_IMMUTABLE"):
            session.execute(text("DELETE FROM audit_events WHERE origin='context_validity'"))


def test_membership_reactivation_does_not_restore_prior_evidence(Session, world):
    with Session.begin() as session:
        token = boundary(session).token()
    for status in ("REVOKED", "ACTIVE"):
        with Session.begin() as session:
            session.execute(text("UPDATE client_tenant_memberships SET status=:status"), {"status": status})
    with Session() as session:
        current = boundary(session)
        candidate = session.scalar(select(CandidateInsightRow))
        assert not candidate_insight_source_is_current(session, candidate, now=START + timedelta(days=4))
        with pytest.raises(ValueError, match="STALE_GENERATION"):
            current.validate(token)
        assert current.observe(world["ids"]).new_candidates == 0


@pytest.mark.parametrize("action", ["observe", "admit", "compile", "consume"])
@pytest.mark.parametrize("revocation_first", [False, True])
def test_membership_revocation_race(Session, world, action, revocation_first):
    from test_postgres_context_generation_integrity import race
    with Session.begin() as session:
        token, packet = boundary(session).compile("fixture")

    def owner_operation(session):
        validity = boundary(session)
        if action == "observe":
            validity.observe(world["ids"])
        elif action == "admit":
            validity.admit(world["candidate"], "fixture:owner-decision")
        elif action == "compile":
            validity.compile("fixture")
        else:
            validity.consume_compiled(token, packet)

    def revoke(session):
        session.execute(text("UPDATE client_tenant_memberships SET status='REVOKED'"))

    errors = race(
        Session, revoke if revocation_first else owner_operation,
        owner_operation if revocation_first else revoke,
    )
    assert bool(errors) == revocation_first
    if revocation_first:
        assert str(errors[0]) in {"BOOTSTRAP_OWNER_MEMBERSHIP_REQUIRED", "OBSERVATION_CANARY_OWNER_UNRESOLVED"}
    with Session() as session:
        assert not candidate_insight_source_is_current(
            session, session.get(CandidateInsightRow, world["candidate"]), now=START + timedelta(days=4),
        )


def test_direct_binding_identity_roundtrip_invalidates_token(Session, world):
    with Session.begin() as session:
        token = boundary(session).token()
    for key in ("changed-owner", "fixture-owner"):
        with Session.begin() as session:
            session.execute(text("UPDATE actor_bindings SET actor_key=:key WHERE id='fixture-owner'"), {"key": key})
    with Session() as session:
        validity = boundary(session)
        assert validity.token().generation == token.generation
        with pytest.raises(ValueError, match="STALE_GENERATION"):
            validity.validate(token)
        assert validity.observe(world["ids"]).new_candidates == 0


def test_shared_receipts_withdraw_independently(Session, world):
    from attention_router.application.context_validity import ContextValidity
    from attention_router.infrastructure.artifact_models import ArtifactRow, ArtifactReceiptRow
    from attention_router.infrastructure.models import ConversationMessageRow
    from test_cognitive_observation import _attachment

    account = "another-fixture-account"
    with Session.begin() as session:
        other_id, _, linked = _attachment(session, "second-account-proof", 3, account=account)
        other_receipt_id = linked.receipt.id
        artifact_id = linked.artifact.id
        current_receipt_ids = session.scalars(select(ArtifactReceiptRow.id).where(
            ArtifactReceiptRow.artifact_id == artifact_id,
            ArtifactReceiptRow.source_account == "fixture-account",
        )).all()

    def other(session):
        return ContextValidity(
            session, tenant_id=transactions.DEFAULT_TENANT_ID, binding_id="fixture-owner",
            owner_human_identity_id="fixture-human-owner", source_account=account,
            start=START, now=START + timedelta(days=4),
        )

    with Session.begin() as session:
        validity = other(session)
        assert validity.observe((other_id,)).new_links == 1
        other_token, other_graph = validity.graph()
        assert len([node for node in other_graph.nodes if node.source_type == "TIMELINE_EVENT"]) == 1
    with Session.begin() as session:
        validity = boundary(session)
        for receipt_id in current_receipt_ids:
            validity.revoke_receipt(receipt_id)
        assert not validity.graph()[1].nodes
        with pytest.raises(ValueError, match="RECEIPT_SCOPE"):
            validity.revoke_receipt(other_receipt_id)
    with Session.begin() as session:
        validity = other(session)
        validity.validate(other_token)
        assert validity.graph()[1].nodes
        assert session.get(ConversationMessageRow, other_id).searchable
        assert session.get(ArtifactRow, artifact_id).status == "AVAILABLE"
        validity.revoke_receipt(other_receipt_id)
        assert not validity.graph()[1].nodes
        assert validity.observe((other_id,)).linked == 0
        assert session.get(ArtifactRow, artifact_id).status == "AVAILABLE"


def test_receipt_deadline_and_replay(Session, world):
    from attention_router.infrastructure.models import ConversationMessageRow
    with Session.begin() as session:
        message = session.get(ConversationMessageRow, world["ids"][0])
        receipt_id = message.metadata_json["artifact_receipt_id"]
        validity = boundary(session)
        future = validity.arguments["now"] + timedelta(hours=1)
        token = validity.revoke_receipt(receipt_id, expires_at=future)
        assert validity.graph()[1].nodes
    with Session.begin() as session:
        previous = boundary(session)
        validity = type(previous)(session, **{**previous.arguments, "now": future})
        candidate = session.get(CandidateInsightRow, world["candidate"])
        assert not candidate_insight_source_is_current(session, candidate, now=future)
        assert validity.observe((world["ids"][0],)).linked == 0
        with pytest.raises(ValueError, match="DEADLINE_EXTENSION"):
            validity.revoke_receipt(receipt_id, expires_at=future + timedelta(hours=1))
        expired = validity.revoke_receipt(receipt_id, expires_at=future)
        assert expired.generation > token.generation
        assert validity.revoke_receipt(receipt_id) == expired


@pytest.mark.parametrize("withdrawal_first", [False, True])
def test_direct_artifact_update_serializes_against_observation(Session, world, withdrawal_first):
    from test_postgres_context_generation_integrity import race
    with Session() as session:
        artifact_id = session.scalar(text("SELECT id FROM artifacts WHERE tenant_id=:tenant"), {
            "tenant": transactions.DEFAULT_TENANT_ID,
        })
    results = []

    def observe(session):
        results.append(boundary(session).observe(world["ids"]))

    def withdraw(session):
        session.execute(text("UPDATE artifacts SET status='DELETED' WHERE id=:id"), {"id": artifact_id})

    assert not race(Session, withdraw if withdrawal_first else observe, observe if withdrawal_first else withdraw)
    assert results[0].linked == (0 if withdrawal_first else 3)
    with Session.begin() as session:
        assert not boundary(session).graph()[1].nodes


def test_restoring_database_snapshot_requires_external_epoch(Session, world, pg_url):
    """A full database clone taken before withdrawal has the former authority."""
    from uuid import uuid4
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import sessionmaker

    with Session.begin() as session:
        token = boundary(session).token()
    fixture_engine = Session.kw["bind"]
    fixture_engine.dispose()
    base = make_url(pg_url)
    assert base.database.startswith("attention_router_test_")
    clone = f"attention_router_test_{uuid4().hex[:10]}"
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT", hide_parameters=True)
    with admin.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{clone}" TEMPLATE "{base.database}"')
    restored = create_engine(base.set(database=clone), hide_parameters=True)
    try:
        with Session.begin() as session:
            boundary(session).revoke_message(world["ids"][0])
        with Session() as session:
            with pytest.raises(ValueError, match="STALE_GENERATION"):
                boundary(session).validate(token)
        with sessionmaker(restored).begin() as session:
            # Demonstrate the limit. The expected restore fence is external.
            boundary(session).validate(token)
    finally:
        restored.dispose()
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{clone}"')
        admin.dispose()


def test_concurrent_shared_artifact_registration(Session, world):
    from attention_router.application.platform.artifacts import ArtifactReceiptInput, register_artifact_receipt
    from attention_router.infrastructure.artifact_models import ArtifactRow, ArtifactReceiptRow
    from test_postgres_context_generation_integrity import race

    def register(account):
        def operation(session):
            register_artifact_receipt(session, ArtifactReceiptInput(
                tenant_id=transactions.DEFAULT_TENANT_ID, content_sha256="d" * 64,
                artifact_kind="IMAGE", mime_type="image/png", size_bytes=4,
                storage_provider="fixture", storage_reference="fixture",
                source_channel="channel.whatsapp", external_receipt_id="fixture-receipt",
                source_account=account, received_at=START,
            ))
        return operation

    assert not race(Session, register("fixture-account"), register("another-fixture-account"))
    with Session() as session:
        artifact_id = session.scalar(select(ArtifactRow.id).where(ArtifactRow.content_sha256 == "d" * 64))
        accounts = set(session.scalars(select(ArtifactReceiptRow.source_account).where(
            ArtifactReceiptRow.artifact_id == artifact_id,
        )))
        assert accounts == {"fixture-account", "another-fixture-account"}


def test_receipt_withdrawal_crash_rolls_back_source_generation_and_history(Session, world):
    from attention_router.infrastructure.artifact_models import ArtifactReceiptRow
    from attention_router.infrastructure.models import AuditEventRow, ConversationMessageRow

    with Session() as victim:
        pid = victim.scalar(text("select pg_backend_pid()"))
        message = victim.get(ConversationMessageRow, world["ids"][0])
        receipt_id = message.metadata_json["artifact_receipt_id"]
        validity = boundary(victim)
        original = validity.token()
        validity.revoke_receipt(receipt_id)
        with victim.get_bind().connect().execution_options(isolation_level="AUTOCOMMIT") as control:
            assert control.scalar(text("select pg_backend_pid()")) != pid
            assert control.scalar(text("select pg_terminate_backend(:pid)"), {"pid": pid})
        with pytest.raises(DBAPIError):
            victim.commit()
        victim.rollback()
    with Session.begin() as session:
        validity = boundary(session)
        assert validity.token() == original
        assert not session.get(ArtifactReceiptRow, receipt_id).metadata_json.get("context_evidence_withdrawn")
        assert session.get(ConversationMessageRow, world["ids"][0]).searchable
        committed = validity.revoke_receipt(receipt_id)
    with Session.begin() as session:
        validity = boundary(session)
        assert validity.revoke_receipt(receipt_id) == committed
        rows = session.scalars(select(AuditEventRow).where(
            AuditEventRow.event_type == "CONTEXT_VALIDITY_CHANGED",
        )).all()
        assert [row.payload["generation"] for row in rows] == [committed.generation]


@pytest.mark.parametrize("attempt", ["admit", "restore"])
def test_already_revoked_candidate_rejects_legacy_writes(Session, world, attempt):
    with Session.begin() as session:
        boundary(session).revoke_message(world["ids"][0])
    statement = {
        "admit": "UPDATE candidate_insights SET state='ADMITTED' WHERE id=:id",
        "restore": "UPDATE candidate_insights SET evidence_status='CURRENT' WHERE id=:id",
    }[attempt]
    with Session() as session:
        with pytest.raises(DBAPIError, match="CONTEXT_DERIVATIVE_ALREADY_REVOKED"):
            session.execute(text(statement), {"id": world["candidate"]})


def test_boundaries_require_an_authorized_pilot_environment(Session, world, monkeypatch):
    from attention_router.config import settings
    monkeypatch.setattr(settings, "app_env", "production")
    with Session() as session:
        with pytest.raises(ValueError, match="CONSENT_AUTHORITY_UNAVAILABLE"):
            boundary(session)
        assert not candidate_insight_source_is_current(
            session, session.get(CandidateInsightRow, world["candidate"]), now=START + timedelta(days=4),
        )


def test_scoped_compilation_query_cost(Session, world, record_property):
    from time import perf_counter
    from sqlalchemy import event
    from test_cognitive_observation import _attachment

    for added in (0, 7, 15):
        with Session.begin() as session:
            extra = [_attachment(session, f"cost-{added}-{i}", 3)[0] for i in range(added)]
        if extra:
            with Session.begin() as session:
                boundary(session).observe(tuple(extra))
        statements = 0

        def counted(*args):
            nonlocal statements
            statements += 1

        with Session() as session:
            engine = session.get_bind()
            event.listen(engine, "before_cursor_execute", counted)
            start = perf_counter()
            try:
                validity = boundary(session)
                _, graph = validity.graph()
                elapsed_ms = (perf_counter() - start) * 1000
            finally:
                event.remove(engine, "before_cursor_execute", counted)
            event_count = sum(node.source_type == "TIMELINE_EVENT" for node in graph.nodes)
        assert 0 < event_count <= 200
        assert 0 < statements < 3000
        record_property(f"pilot_{event_count}_events_queries", statements)
        print(f"pilot_events={event_count} sql_statements={statements} lock_and_projection_ms={elapsed_ms:.2f}")


def test_artifact_status_roundtrip_cannot_reanimate_prior_derivation(Session, world):
    # Neither status write uses the pilot, and there is no reconciliation
    # between the two commits.
    for status in ("DELETED", "AVAILABLE"):
        with Session.begin() as session:
            session.execute(text("UPDATE artifacts SET status=:status"), {"status": status})
    with Session.begin() as session:
        validity = boundary(session)
        assert not validity.graph()[1].nodes
        assert validity.observe(world["ids"]).linked == 0
        candidate = session.get(CandidateInsightRow, world["candidate"])
        assert candidate.evidence_status == "EVIDENCE_REVOKED"
        assert candidate.state == "PROPOSED"


@pytest.mark.parametrize("changed_source", ["message", "receipt"])
def test_source_correction_roundtrip_cannot_restore_old_support(Session, world, changed_source):
    with Session() as session:
        message_id = world["ids"][0]
        receipt_id = session.scalar(text(
            "SELECT metadata->>'artifact_receipt_id' FROM conversation_messages WHERE id=:id"
        ), {"id": message_id})
        assert receipt_id
    if changed_source == "message":
        statement = "UPDATE conversation_messages SET searchable=:value WHERE id=:id"
        row_id, values = message_id, (False, True)
    else:
        statement = "UPDATE artifact_receipts SET source_account=:value WHERE id=:id"
        row_id, values = receipt_id, ("corrected-fixture-account", "fixture-account")
    for value in values:
        with Session.begin() as session:
            session.execute(text(statement), {"id": row_id, "value": value})
    with Session.begin() as session:
        validity = boundary(session)
        candidate = session.get(CandidateInsightRow, world["candidate"])
        assert not candidate_insight_source_is_current(session, candidate, now=START + timedelta(days=4))
        assert validity.observe(world["ids"]).new_candidates == 1
        assert candidate.evidence_status == "EVIDENCE_REVOKED"


def test_legacy_description_update_preserves_source_revision(Session, world):
    with Session.begin() as session:
        session.execute(text("""
            UPDATE artifact_receipts
            SET metadata=metadata::jsonb || '{"fixture_description":"updated"}'::jsonb
        """))
    with Session() as session:
        validity = boundary(session)
        assert validity.graph()[1].nodes
        assert candidate_insight_source_is_current(
            session, session.get(CandidateInsightRow, world["candidate"]), now=START + timedelta(days=4),
        )


@pytest.mark.parametrize("source", ["message", "receipt"])
def test_withdrawal_rechecks_account_after_waiting_for_source_writer(Session, world, source):
    from test_postgres_context_generation_integrity import race
    from attention_router.infrastructure.artifact_models import ArtifactReceiptRow
    from attention_router.infrastructure.models import ConversationMessageRow

    message_id = world["ids"][0]
    with Session() as session:
        message = session.get(ConversationMessageRow, message_id)
        receipt_id = message.metadata_json["artifact_receipt_id"]
        token = boundary(session).token()

    model, source_id = (
        (ConversationMessageRow, message_id) if source == "message"
        else (ArtifactReceiptRow, receipt_id)
    )

    def change_account(session):
        row = session.get(model, source_id)
        row.source_account = "corrected-fixture-account"
        session.flush()

    def withdraw(session):
        validity = boundary(session)
        if source == "message":
            validity.revoke_message(source_id)
        else:
            validity.revoke_receipt(source_id)

    errors = race(Session, change_account, withdraw)
    assert len(errors) == 1
    assert "SCOPE" in str(errors[0])
    with Session() as session:
        row = session.get(model, source_id)
        assert row.source_account == "corrected-fixture-account"
        assert not row.metadata_json.get("context_evidence_withdrawn")
        assert boundary(session).token() == token


def test_ungoverned_candidate_consumer_reloads_cached_source(Session, world):
    from attention_router.infrastructure.models import ConversationMessageRow

    stamp = START + timedelta(days=4)
    with Session() as reader:
        candidate = reader.get(CandidateInsightRow, world["candidate"])
        message = reader.get(ConversationMessageRow, world["ids"][0])
        assert candidate_insight_source_is_current(reader, candidate, now=stamp)
        reader.commit()
        with Session.begin() as writer:
            source = writer.get(ConversationMessageRow, message.id)
            source.searchable = False
        assert not candidate_insight_source_is_current(reader, candidate, now=stamp)


def test_artifact_withdrawal_waits_for_receipt_account_correction(Session, world):
    from test_postgres_context_generation_integrity import race
    from attention_router.infrastructure.artifact_models import ArtifactReceiptRow, ArtifactRow
    from attention_router.infrastructure.models import ConversationMessageRow

    with Session() as session:
        message = session.get(ConversationMessageRow, world["ids"][0])
        receipt_id = message.metadata_json["artifact_receipt_id"]
        artifact_id = message.metadata_json["artifact_id"]
        token = boundary(session).token()

    def change_account(session):
        receipt = session.get(ArtifactReceiptRow, receipt_id)
        receipt.source_account = "corrected-fixture-account"
        session.flush()

    errors = race(Session, change_account, lambda session: boundary(session).revoke_artifact(artifact_id))
    assert len(errors) == 1
    assert "ARTIFACT_WITHDRAWAL_MULTISCOPE_UNPROVEN" in str(errors[0])
    with Session() as session:
        assert session.get(ArtifactRow, artifact_id).status == "AVAILABLE"
        assert boundary(session).token() == token


def test_observed_receipt_expiration_advances_generation_once(Session, world):
    from attention_router.infrastructure.models import AuditEventRow, ConversationMessageRow

    message_id = world["ids"][0]
    with Session.begin() as session:
        message = session.get(ConversationMessageRow, message_id)
        validity = boundary(session)
        deadline = validity.arguments["now"] + timedelta(hours=1)
        scheduled = validity.revoke_receipt(
            message.metadata_json["artifact_receipt_id"], expires_at=deadline,
        )

    with Session.begin() as session:
        previous = boundary(session)
        validity = type(previous)(session, **{**previous.arguments, "now": deadline})
        assert validity.observe((message_id,)).linked == 0
        reconciled = validity.token()
        assert reconciled.generation == scheduled.generation + 1
        with pytest.raises(ValueError, match="STALE_GENERATION"):
            validity.validate(scheduled)
        assert validity.observe((message_id,)).linked == 0
        assert validity.token() == reconciled
        journal = session.scalars(select(AuditEventRow).where(
            AuditEventRow.origin == "context_validity",
        )).all()
        revisions = [row for row in journal if row.payload.get("reason") == "CONTEXT_SUPPORT_REVISED"]
        assert len(revisions) == 1
        assert revisions[0].payload["generation"] == reconciled.generation


def test_reconciliation_preserves_correlated_account_isolation(Session, monkeypatch):
    from test_cognitive_revocation_contract import test_reconciliation_cannot_tombstone_another_accounts_event

    with Session.begin() as session:
        test_reconciliation_cannot_tombstone_another_accounts_event(session, monkeypatch)


def test_recorded_observation_scope_cannot_change(Session, world):
    from attention_router.infrastructure.models import TimelineEventRow

    with Session() as session:
        event = session.scalars(select(TimelineEventRow).where(
            TimelineEventRow.event_type == "ATTACHMENT_OBSERVED",
        )).first()
        assert event.metadata_json["observation_scope"]
        event.metadata_json = {**event.metadata_json, "observation_scope": "a" * 64}
        with pytest.raises(DBAPIError, match="CONTEXT_OBSERVATION_SCOPE_IMMUTABLE"):
            session.flush()
