"""PG principals and explicit source authorization in the isolated pilot."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import os
import subprocess
import sys
from secrets import token_hex
from threading import Event

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from attention_router.application.context_consent import LabConsent, RETENTION_LAYERS, SourceConsent
from attention_router.application.context_preactivation import ContextPreactivation, require_context_database
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import ConversationMessageRow, TimelineEventRow
import test_postgres_context_epoch as epoch_fixtures
import test_postgres_context_transactions as context_fixtures
from test_cognitive_observation import _attachment


pytestmark = pytest.mark.postgres
world = context_fixtures.world
boundary = context_fixtures.boundary
anchor = epoch_fixtures.anchor


@pytest.fixture
def enroll_role(Session):
    roles = []

    def create(*, current=False, historical=False, source_withdrawal=False):
        role = f"fixture_context_{token_hex(6)}"
        password = token_hex(24)
        with Session.begin() as admin:
            admin.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'"))
            admin.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            if current:
                admin.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {role}"))
                admin.execute(text(f"""
                    GRANT UPDATE(id) ON actor_bindings, client_tenant_memberships, conversation_messages,
                    inbound_events, conversation_threads, canonical_events, artifact_receipts,
                    artifacts, resources, semantic_episodes, voice_transcriptions TO {role}
                """))
                admin.execute(text(f"""
                    GRANT INSERT, UPDATE ON timeline_events, semantic_episodes, semantic_episode_memberships,
                    candidate_insights, candidate_insight_evidence, audit_events TO {role}
                """))
                admin.execute(text(f"GRANT UPDATE(metadata) ON actor_bindings TO {role}"))
            if source_withdrawal:
                admin.execute(text(f"""
                    GRANT UPDATE(metadata, searchable) ON conversation_messages TO {role};
                    GRANT INSERT(id, kind, status, payload, created_at),
                        UPDATE(status, processed_at, payload) ON queue TO {role};
                    GRANT DELETE ON conversation_embeddings TO {role};
                    GRANT UPDATE(state, content_text, updated_at) ON conversation_vector_segments TO {role};
                """))
            if historical:
                # A separate projection with a fixed fixture tenant and only the
                # fields needed to review revoked candidate history.
                admin.execute(text(f"""
                    CREATE VIEW fixture_context_history WITH (security_barrier=true) AS
                    SELECT id, 'EVIDENCE_REVOKED' AS evidence_status FROM conversation_messages
                    WHERE tenant_id='{DEFAULT_TENANT_ID}'
                      AND metadata->>'context_evidence_withdrawn'='true'
                """))
                admin.execute(text(f"GRANT SELECT ON fixture_context_history TO {role}"))
        url = Session.kw["bind"].url.set(username=role, password=password)
        engine = create_engine(url, hide_parameters=True)
        roles.append((role, engine))
        return role, sessionmaker(engine, expire_on_commit=False)

    yield create
    for role, engine in roles:
        engine.dispose()
        with Session.begin() as admin:
            admin.execute(text(f"DROP OWNED BY {role}"))
            admin.execute(text(f"DROP ROLE {role}"))
    with Session.begin() as admin:
        admin.execute(text("DROP VIEW IF EXISTS fixture_context_history"))


def test_v01_v02_v03_v06_v07_v08_v09_v10_roles_and_schema(Session, world, enroll_role):
    current, Current = enroll_role(current=True)
    _, Old = enroll_role()
    _, History = enroll_role(historical=True)

    with Current() as session:
        require_context_database(session, reader_role=current)
        assert boundary(session).graph()[1].nodes
    with Session.begin() as session:
        validity = boundary(session)
        for message_id in world["ids"]:
            validity.revoke_message(message_id)

    with Old() as session:
        with pytest.raises(ValueError, match="CONTEXT_DATABASE_ROLE_MISMATCH"):
            require_context_database(session, reader_role=current)
        with pytest.raises(DBAPIError) as refusal:
            session.scalar(select(TimelineEventRow.id))
        assert refusal.value.orig.sqlstate == "42501"
    with History() as session:
        historical = session.execute(text("SELECT evidence_status FROM fixture_context_history")).scalars().all()
        assert historical and all(status == "EVIDENCE_REVOKED" for status in historical)
        with pytest.raises(ValueError, match="CONTEXT_DATABASE_ROLE_MISMATCH"):
            require_context_database(session, reader_role=current)
        with pytest.raises(DBAPIError) as refusal:
            session.scalar(select(TimelineEventRow.id))
        assert refusal.value.orig.sqlstate == "42501"

    # Simulate a restored older schema marker in the disposable database.
    with Session.begin() as admin:
        installed = admin.scalar(text("SELECT version_num FROM alembic_version"))
        admin.execute(text("UPDATE alembic_version SET version_num='0068_google_contacts_cache'"))
    try:
        with Current() as session:
            with pytest.raises(ValueError, match="CONTEXT_DATABASE_SCHEMA_MISMATCH"):
                require_context_database(session, reader_role=current)
    finally:
        with Session.begin() as admin:
            admin.execute(text("UPDATE alembic_version SET version_num=:installed"), {"installed": installed})


def test_v07_new_reader_denies_real_0068_schema(Session, enroll_role, pg_url):
    role, Reader = enroll_role(current=True)
    environment = {**os.environ, "DATABASE_URL": pg_url}

    def migrate(*arguments):
        subprocess.run([sys.executable, "-m", "alembic", *arguments], env=environment, check=True)

    migrate("downgrade", "0068_google_contacts_cache")
    try:
        with Reader() as session:
            with pytest.raises(ValueError, match="CONTEXT_DATABASE_SCHEMA_MISMATCH"):
                require_context_database(session, reader_role=role)
    finally:
        migrate("upgrade", "head")


@pytest.fixture
def consent_pilot(Session, world, anchor, enroll_role, tmp_path):
    role, sessions = enroll_role(current=True)
    with Session.begin() as session:
        scope = dict(boundary(session).arguments)
        extra, _, _ = _attachment(session, "preactivation-fixture", 3)
        cross_account, _, _ = _attachment(session, "preactivation-other-account", 3, account="another-fixture")
        source = session.get(ConversationMessageRow, extra)
        grants = (SourceConsent(
            decision_ref="fixture-owner-decision",
            tenant_id=scope["tenant_id"], owner_id=scope["owner_human_identity_id"],
            source_account=scope["source_account"], conversation_id=source.conversation_id,
            message_id=source.id, receipt_id=source.metadata_json["artifact_receipt_id"],
            purpose="prospective_context", starts_at=source.sent_at,
            expires_at=scope["now"] + timedelta(hours=2), sensitivity="NORMAL",
            retention=tuple((layer, scope["now"] + timedelta(hours=1)) for layer in sorted(RETENTION_LAYERS)),
        ),)
    path = tmp_path / "consent.sqlite"
    authority = LabConsent(path, grants)
    authority.initialize()
    epoch = anchor[0]

    observed_now = scope.pop("now")

    def pilot(authority=authority, *, when=observed_now):
        return ContextPreactivation(sessions, role=role, epoch=epoch, consent=authority, clock=lambda: when)

    return pilot, authority, grants, scope, extra, cross_account


def test_c01_c04_c05_c08_c09_c10_c11_c12_source_consent(Session, world, consent_pilot):
    client, authority, grants, scope, message_id, other_account = consent_pilot
    with pytest.raises(ValueError, match="CONTEXT_CONSENT_SOURCE_DENIED"):
        client().observe((world["ids"][0],), **scope)
    with pytest.raises(ValueError, match="CONTEXT_CONSENT_SOURCE_DENIED"):
        client().observe((other_account,), **scope)
    result = client().observe((message_id,), **scope)
    assert result.linked == result.new_links == 1
    assert result.new_episodes == result.new_candidates == 0
    token, packet = client().compile("fixture", **scope)
    assert packet.items and all(item.source_type != "SEMANTIC_EPISODE" for item in packet.items)
    assert client().consume(token, packet, **scope).items
    expired = dict(grants[0].retention)["source"]
    assert not client(when=expired).consume(token, packet, **scope).items
    with pytest.raises(ValueError, match="CONTEXT_PREACTIVATION_CALLER_CLOCK_DENIED"):
        client().consume(token, packet, now=grants[0].starts_at, **scope)

    assert authority.revoke(message_id)
    assert not authority.revoke(message_id)
    restarted = client(LabConsent(authority.path, grants))
    with pytest.raises(ValueError, match="CONTEXT_CONSENT_REVISION_STALE"):
        restarted.consume(token, packet, **scope)
    with pytest.raises(ValueError, match="CONTEXT_CONSENT_SOURCE_DENIED"):
        restarted.observe((message_id,), **scope)
    assert not restarted.compile("fixture", **scope)[1].items


def test_c02_c03_purpose_and_exact_source(Session, consent_pilot):
    _, _, grants, scope, message_id, other_account = consent_pilot
    grant, = grants
    with pytest.raises(ValueError, match="CONTEXT_CONSENT_INVALID"):
        replace(grant, purpose="archive_only")
    with Session() as session:
        message = session.get(ConversationMessageRow, message_id)
        observed_now = grant.expires_at - timedelta(hours=2)
        expired = replace(grant, expires_at=observed_now)
        assert not expired.permits(message, owner_id=grant.owner_id, now=observed_now)
        assert not replace(grant, source_account="another-fixture").permits(
            message, owner_id=grant.owner_id, now=observed_now,
        )
        assert not grant.permits(
            session.get(ConversationMessageRow, other_account),
            owner_id=grant.owner_id, now=observed_now,
        )


@pytest.mark.parametrize("operation", ["observe", "compile"])
def test_c06_c07_revocation_serializes_with_operation(consent_pilot, monkeypatch, operation):
    from attention_router.application.context_validity import ContextValidity

    client, authority, _, scope, message_id, _ = consent_pilot
    entering, resume, withdrawing, withdrawn = Event(), Event(), Event(), Event()
    original = getattr(ContextValidity, operation)

    def pause(*args, **kwargs):
        entering.set()
        assert resume.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(ContextValidity, operation, pause)

    def revoke():
        withdrawing.set()
        assert authority.revoke(message_id)
        withdrawn.set()

    def run():
        if operation == "observe":
            return client().observe((message_id,), **scope)
        return client().compile("fixture", **scope)

    with ThreadPoolExecutor(max_workers=2) as pool:
        action = pool.submit(run)
        assert entering.wait(5)
        withdrawal = pool.submit(revoke)
        assert withdrawing.wait(5)
        assert not withdrawn.wait(0.15)
        resume.set()
        action.result(timeout=10)
        withdrawal.result(timeout=10)
    assert withdrawn.is_set()
    assert not client().compile("fixture", **scope)[1].items
