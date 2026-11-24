import pytest
from sqlalchemy import select

import test_postgres_context_transactions as transactions

from attention_router.infrastructure.models import ActorBindingRow
from attention_router.infrastructure.repository import upsert_actor_binding
from attention_router.core.tenancy import DEFAULT_TENANT_ID

world = transactions.world
boundary = transactions.boundary

pytestmark = pytest.mark.postgres


def update(session, metadata):
    return upsert_actor_binding(
        session, "wwebjs", "fixture-owner-external", "fixture-owner", "owner",
        metadata=metadata, tenant_id=DEFAULT_TENANT_ID,
    )


@pytest.mark.parametrize("metadata", [None, {}, {"description": "fixture"}])
def test_legacy_update_preserves_ledger(Session, world, metadata):
    with Session() as writer:
        token = boundary(writer).revoke_message(world["ids"][0])
        writer.commit()
    with Session() as writer:
        update(writer, metadata)
        writer.commit()
    with Session() as reader:
        assert boundary(reader).token() == token
        assert reader.scalar(select(ActorBindingRow).where(
            ActorBindingRow.id == "fixture-owner",
        )).binding_metadata["context_generations"]


@pytest.mark.parametrize("metadata", [{"context_generations": {}}, {"context_generations": {"forged": 999}}])
def test_reserved_field_rejected(Session, world, metadata):
    with Session() as session:
        with pytest.raises(ValueError, match="RESERVED_FIELD"):
            update(session, metadata)


def test_stale_writer_and_compiled_snapshot(Session, world):
    with Session() as stale:
        stale.get(ActorBindingRow, "fixture-owner")
        with Session() as writer:
            valid = boundary(writer)
            token, packet = valid.compile("fixture")
            valid.revoke_message(world["ids"][0])
            writer.commit()
        update(stale, {"description": "new"})
        stale.commit()
        valid = boundary(stale)
        assert valid.token().generation > token.generation
        with pytest.raises(ValueError, match="STALE_GENERATION"):
            valid.consume_compiled(token, packet)


def race(Session, first, second):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from time import monotonic
    from sqlalchemy import text

    pids = {}
    ready = Event()
    errors = []
    with Session() as holder:
        holder.execute(text("SET LOCAL lock_timeout='8s'"))
        pids["holder"] = holder.scalar(text("select pg_backend_pid()"))
        first(holder)

        def contend():
            with Session() as session:
                session.execute(text("SET LOCAL lock_timeout='8s'"))
                pids["other"] = session.scalar(text("select pg_backend_pid()"))
                ready.set()
                try:
                    second(session)
                    session.commit()
                except Exception as error:
                    session.rollback()
                    errors.append(error)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(contend)
            assert ready.wait(5)
            assert pids["holder"] != pids["other"]
            with holder.get_bind().connect() as probe:
                deadline = monotonic() + 5
                while monotonic() < deadline:
                    if probe.scalar(text("select :holder = ANY(pg_blocking_pids(:other))"), pids):
                        print(f"pg_blocking_pids proof holder={pids['holder']} contender={pids['other']}")
                        break
                else:
                    pytest.fail("No PostgreSQL lock wait observed")
            holder.commit()
            future.result(timeout=12)
    return errors


@pytest.mark.parametrize("reverse", [False, True])
def test_legacy_writer_race(Session, world, reverse):
    def revoke(session):
        boundary(session).revoke_message(world["ids"][0])
    def legacy(session):
        update(session, {"description": "fixture"})
    errors = race(Session, legacy if reverse else revoke, revoke if reverse else legacy)
    assert not errors
    with Session() as session:
        assert boundary(session).token().generation == 1


@pytest.mark.parametrize("reverse", [False, True])
def test_identity_writer_race(Session, world, reverse):
    from attention_router.infrastructure.repository import set_actor_binding_active
    with Session() as session:
        boundary(session)._advance("FIXTURE_ENROLLMENT")
        session.commit()
    def deactivate(session):
        set_actor_binding_active(session, "fixture-owner", False)
    def read(session):
        boundary(session).compile("fixture")
    errors = race(Session, deactivate if reverse else read, read if reverse else deactivate)
    assert bool(errors) == reverse
    with Session() as session:
        with pytest.raises(ValueError, match="OWNER_UNRESOLVED"):
            boundary(session)


@pytest.mark.parametrize("reverse", [False, True])
def test_canonical_artifact_admission_race(Session, world, reverse):
    from test_cognitive_observation import _attachment
    from attention_router.infrastructure.candidate_insight_models import CandidateInsightRow

    with Session() as session:
        created = [_attachment(session, f"dedicated-{i}", i, content_sha256="b" * 64) for i in (1, 2, 3)]
        session.commit()
        valid = boundary(session)
        valid.observe([item[0] for item in created])
        artifact_id = created[0][2].artifact.id
        candidate = session.scalar(select(CandidateInsightRow).where(
            CandidateInsightRow.proposed_value["target_id"].as_string() == created[0][2].artifact.resource_id,
        ))
        candidate_id = candidate.id
        session.commit()
    def revoke(session):
        boundary(session).revoke_artifact(artifact_id)
    def admit(session):
        boundary(session).admit(candidate_id, "fixture:decision")
    errors = race(Session, revoke if reverse else admit, admit if reverse else revoke)
    assert bool(errors) == reverse
    with Session() as session:
        from attention_router.application.candidate_insights import candidate_insight_source_is_current
        candidate = session.get(CandidateInsightRow, candidate_id)
        assert not candidate_insight_source_is_current(session, candidate)
        if not reverse:
            assert candidate.state == "ADMITTED"
            assert candidate.decision_ref == "fixture:decision"


def test_multiscope_artifact_fails_closed(Session, world):
    from attention_router.infrastructure.artifact_models import ArtifactRow
    with Session() as session:
        artifact_id = session.scalar(select(ArtifactRow.id))
        with pytest.raises(ValueError, match="MULTISCOPE_UNPROVEN"):
            boundary(session).revoke_artifact(artifact_id)


def test_transcription_does_not_restore_withdrawn_message(Session, world):
    from datetime import timedelta
    from test_postgres_conversation_live_capture import receipt
    from test_cognitive_observation import START, ACCOUNT
    from attention_router.application.conversation_live_capture import capture_live_receipt, capture_voice_transcription
    from attention_router.infrastructure.models import VoiceTranscriptionRow, ConversationMessageRow
    from attention_router.domain.models import new_id

    with Session() as session:
        event = receipt("ledger-voice", voice=True)
        event.received_at = START + timedelta(hours=1)
        event.payload = {
            **event.payload, "occurred_at": event.received_at.isoformat(),
            "metadata": {**event.payload["metadata"], "source_account": ACCOUNT},
        }
        session.add(event)
        session.flush()
        message_id = capture_live_receipt(session, event).message_id
        session.commit()
        valid = boundary(session)
        token = valid.revoke_message(message_id)
        session.commit()
        transcript = VoiceTranscriptionRow(
            id=new_id(), tenant_id=DEFAULT_TENANT_ID, inbound_event_id=event.id,
            status="READY", transcript_text="fixture voice transcript",
            provider="synthetic", model="synthetic", attempt_count=1,
            created_at=event.received_at, updated_at=event.received_at,
            completed_at=event.received_at,
        )
        session.add(transcript)
        session.flush()
        assert capture_voice_transcription(session, event, transcript).status == "TRANSCRIPTION_ATTACHED"
        session.commit()
        message = session.get(ConversationMessageRow, message_id)
        assert message.text == "fixture voice transcript"
        assert not message.searchable
        current = boundary(session).token()
        assert current.generation > token.generation
        session.commit()
        capture_voice_transcription(session, event, transcript)
        session.commit()
        assert boundary(session).token() == current


def test_account_generation_isolation(Session, world):
    from datetime import timedelta
    from attention_router.application.context_validity import ContextValidity
    from test_cognitive_observation import START
    with Session() as session:
        other = ContextValidity(
            session, tenant_id=DEFAULT_TENANT_ID, binding_id="fixture-owner",
            owner_human_identity_id="fixture-human-owner", source_account="another-fixture-account",
            start=START, now=START + timedelta(days=4),
        )
        token = other.token()
        session.commit()
        boundary(session).revoke_message(world["ids"][0])
        session.commit()
        other = ContextValidity(
            session, tenant_id=DEFAULT_TENANT_ID, binding_id="fixture-owner",
            owner_human_identity_id="fixture-human-owner", source_account="another-fixture-account",
            start=START, now=START + timedelta(days=4),
        )
        assert other.token() == token
        assert token.scope != boundary(session).token().scope


def test_identity_change_advances_but_description_does_not(Session, world):
    with Session() as session:
        token = boundary(session).revoke_message(world["ids"][0])
        session.commit()
        update(session, {"description": "changed"})
        session.commit()
        assert boundary(session).token() == token
        session.commit()
        upsert_actor_binding(
            session, "wwebjs", "fixture-owner-external", "corrected-owner", "owner",
            tenant_id=DEFAULT_TENANT_ID,
        )
        session.commit()
        assert boundary(session).token().generation == token.generation + 1


def test_concurrent_account_scopes(Session, world):
    from datetime import timedelta
    from test_cognitive_observation import START
    from attention_router.application.context_validity import ContextValidity
    from attention_router.infrastructure.models import ConversationMessageRow

    def other_boundary(session):
        return ContextValidity(
            session, tenant_id=DEFAULT_TENANT_ID, binding_id="fixture-owner",
            owner_human_identity_id="fixture-human-owner", source_account="another-fixture-account",
            start=START, now=START + timedelta(days=4),
        )
    with Session() as session:
        other_id = session.scalar(select(ConversationMessageRow.id).where(
            ConversationMessageRow.source_account == "another-fixture-account",
        ))
    errors = race(
        Session, lambda s: boundary(s).revoke_message(world["ids"][0]),
        lambda s: other_boundary(s).revoke_message(other_id),
    )
    assert not errors
    with Session() as session:
        first = boundary(session).token()
        session.commit()
        second = other_boundary(session).token()
        assert first.scope != second.scope
        assert first.generation == second.generation == 1


def test_no_authority_or_external_effect_records(Session, world):
    from sqlalchemy import func
    from attention_router.infrastructure import models as m
    with Session() as session:
        valid = boundary(session)
        valid.revoke_message(world["ids"][0])
        token, packet = valid.compile("fixture")
        valid.consume_compiled(token, packet)
        session.commit()
        for model in (
            m.OutboxMessageRow, m.InteractionRow, m.ExecutionIntentRow, m.AgentExecutionIntentRow,
            m.CapabilityGrantRow, m.HumanExecutionAuthorizationRow,
            m.HumanApprovalDecisionEvidenceRow, m.FactRow, m.RelationshipRow, m.MemoryClaimRow,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0


@pytest.mark.parametrize("reverse", [False, True])
def test_transcription_withdrawal_race(Session, world, reverse):
    from datetime import timedelta
    from test_postgres_conversation_live_capture import receipt
    from test_cognitive_observation import START, ACCOUNT
    from attention_router.application.conversation_live_capture import capture_live_receipt, capture_voice_transcription
    from attention_router.infrastructure.models import VoiceTranscriptionRow, ConversationMessageRow, InboundEventRow
    from attention_router.domain.models import new_id

    with Session() as session:
        event = receipt("concurrent-voice", voice=True)
        event.received_at = START + timedelta(hours=1)
        event.payload = {
            **event.payload, "occurred_at": event.received_at.isoformat(),
            "metadata": {**event.payload["metadata"], "source_account": ACCOUNT},
        }
        session.add(event)
        session.flush()
        message_id = capture_live_receipt(session, event).message_id
        transcript = VoiceTranscriptionRow(
            id=new_id(), tenant_id=DEFAULT_TENANT_ID, inbound_event_id=event.id,
            status="READY", transcript_text="fixture concurrent transcript",
            provider="synthetic", model="synthetic", attempt_count=1,
            created_at=event.received_at, updated_at=event.received_at,
        )
        session.add(transcript)
        session.commit()
        event_id, transcript_id = event.id, transcript.id
        boundary(session)._advance("FIXTURE_ENROLLMENT")
        session.commit()
    def transcribe(session):
        capture_voice_transcription(
            session, session.get(InboundEventRow, event_id),
            session.get(VoiceTranscriptionRow, transcript_id),
        )
    def withdraw(session):
        boundary(session).revoke_message(message_id)
    assert not race(Session, withdraw if reverse else transcribe, transcribe if reverse else withdraw)
    with Session() as session:
        message = session.get(ConversationMessageRow, message_id)
        assert not message.searchable
        assert message.metadata_json["context_evidence_withdrawn"]
        assert message.text == "fixture concurrent transcript"
        assert boundary(session).token().generation == 3


@pytest.mark.parametrize("correction_first", [False, True])
def test_new_candidate_and_sender_correction_serialize(Session, world, correction_first):
    from attention_router.application.candidate_insights import (
        CandidateEvidenceInput,
        candidate_insight_source_is_current,
        propose_candidate_insight,
    )
    from attention_router.infrastructure.candidate_insight_models import CandidateInsightRow
    from attention_router.infrastructure.models import TimelineEventRow
    from attention_router.infrastructure.repository import set_actor_binding_active

    with Session() as session:
        source_id = session.scalar(select(TimelineEventRow.id).where(
            TimelineEventRow.tenant_id == DEFAULT_TENANT_ID,
            TimelineEventRow.event_ref["conversation_message_id"].as_string() == world["ids"][0],
        ))
    assert source_id is not None

    def correct_sender(session):
        set_actor_binding_active(session, "fixture-peer-binding", False, tenant_id=DEFAULT_TENANT_ID)

    def build_candidate(session):
        context = boundary(session)
        _, graph = context.graph()
        if not any(node.source_type == "TIMELINE_EVENT" and node.source_id == source_id for node in graph.nodes):
            raise ValueError("FIXTURE_PROPOSAL_SOURCE_UNAVAILABLE")
        source = session.get(TimelineEventRow, source_id)
        _, created = propose_candidate_insight(
            session, tenant_id=source.tenant_id, insight_type="STATE_PROPOSAL",
            subject_type="RESOURCE", subject_id=source.resource_id,
            predicate="fixture_sender_dependent_state", proposed_value={"state": "OBSERVED"},
            source_engine="RULE", confidence=0.7,
            evidence=[CandidateEvidenceInput("TIMELINE_EVENT", source.id, source.canonical_event_id, 0.7)],
            now=context.arguments["now"],
        )
        assert created
        context._advance("FIXTURE_PROPOSAL")

    first, second = (correct_sender, build_candidate) if correction_first else (build_candidate, correct_sender)
    errors = race(Session, first, second)
    with Session() as check:
        candidate = check.scalar(select(CandidateInsightRow).where(
            CandidateInsightRow.predicate == "fixture_sender_dependent_state",
        ))
        if correction_first:
            assert len(errors) == 1 and str(errors[0]) == "FIXTURE_PROPOSAL_SOURCE_UNAVAILABLE"
            assert candidate is None
        else:
            assert not errors and candidate is not None
            assert not candidate_insight_source_is_current(check, candidate, now=boundary(check).arguments["now"])
