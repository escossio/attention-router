"""Read-only laboratory adapter for bounded Portuguese travel statements.

This is test support, with no application registration. The language rules are
adapted from the offline parser of the cognitive lab at f30779a.
"""

from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import re
import sys

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from andy_travel_grammar import interpret as interpret

from attention_router.application.candidate_insights import candidate_insight_source_is_current
from attention_router.application.cognitive_observation import _locked_observation_sources, observation_event_is_current
from attention_router.application.context_consent import LabConsent, SourceConsent
from attention_router.application.context_epoch import ContextEpoch
from attention_router.application.context_preactivation import require_context_database
from attention_router.application.context_validity import ContextValidity
from attention_router.config import settings
from attention_router.infrastructure.candidate_insight_models import CandidateInsightEvidenceRow, CandidateInsightRow
from attention_router.infrastructure.context_epoch_lab import LabEpochAuthority
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    ConversationMessageRow, InboundEventRow, TimelineEventRow, VoiceTranscriptionRow,
)
from attention_router.infrastructure.semantic_episode_models import SemanticEpisodeMembershipRow, SemanticEpisodeRow


AUTHORITY = "fixture-and-lab"
ENVIRONMENT = "synthetic-cognitive-lab"
PREDICATE = "travel_date"
QUERY = "attachment observed"


def summarize(question, current, history):
    if question == "histórico":
        return {"state": "HISTORY", "evidence": history}
    if question not in {"Para que dia ficou a viagem?", "Em que data ficou a viagem?"} or not current:
        return {"state": "INSUFFICIENT_EVIDENCE", "evidence": []}
    latest_proposal = next((row for row in current if row["action"] != "CONFIRMS"), None)
    confirmations = [
        row for row in current if row["action"] == "CONFIRMS"
        and (latest_proposal is None or row["observed_at"] >= latest_proposal["observed_at"])
    ]
    if len({row["date"] for row in confirmations}) > 1:
        return {"state": "CONFLICTING_EVIDENCE", "evidence": confirmations}
    if confirmations:
        return {"state": "CONFIRMATION_SUPPORTED", "date": confirmations[0]["date"], "evidence": confirmations}
    return {"state": "PROPOSAL_ONLY", "date": latest_proposal["date"], "evidence": [latest_proposal]}


def _batch_support(context, candidates, visible):
    session = context.session
    tenant_id = context.arguments["tenant_id"]
    supports = defaultdict(list)
    refs = set()
    if candidates:
        rows = session.scalars(select(CandidateInsightEvidenceRow).where(
            CandidateInsightEvidenceRow.tenant_id == tenant_id,
            CandidateInsightEvidenceRow.candidate_id.in_([item.id for item in candidates]),
            CandidateInsightEvidenceRow.evidence_role == "SUPPORT",
        ).order_by(CandidateInsightEvidenceRow.candidate_id,
                   CandidateInsightEvidenceRow.observed_at, CandidateInsightEvidenceRow.source_ref)
          .with_for_update(read=True).execution_options(populate_existing=True)).all()
        for row in rows:
            supports[row.candidate_id].append(row)
            if row.evidence_type == "TIMELINE_EVENT":
                refs.add(row.source_ref)
    events = {}
    if refs:
        rows = session.scalars(select(TimelineEventRow).where(
            TimelineEventRow.tenant_id == tenant_id, TimelineEventRow.id.in_(refs),
        ).order_by(TimelineEventRow.id).with_for_update(read=True).execution_options(populate_existing=True)).all()
        events = {row.id: row for row in rows}
    permitted = context._permitted_sources(row.event_ref.get("conversation_message_id") for row in events.values())
    eligible = [
        row for row in events.values()
        if row.id in visible and row.event_type == "ATTACHMENT_OBSERVED"
        and row.event_ref.get("owner_binding_id") == context.binding.id
        and row.event_ref.get("conversation_message_id") in permitted
    ]
    source_rows = _locked_observation_sources(session, tenant_id, eligible)
    current_events = {
        row.id for row in eligible if observation_event_is_current(
            session, row, binding_id=context.binding.id, source_account=context.arguments["source_account"],
            start=context.arguments["start"], now=context.arguments["now"], lock_sources=True,
            _source_rows=source_rows,
        )
    }
    return supports, events, permitted, current_events, source_rows.get(ConversationMessageRow, {})


def recover(context, epoch, question, resource_id, *, predicate=PREDICATE, present=summarize, qualify=None, batch=False):
    """Use only the currently compiled, consented support set for conclusions."""
    token, packet = epoch.compile(context, resource_id if qualify is not None else QUERY)
    if packet.lexical_fallback_used:
        raise RuntimeError("COGNITIVE_LAB_UNSCOPED_FALLBACK")
    visible = {item.source_id for item in packet.items if item.source_type == "TIMELINE_EVENT"}
    session = context.session
    tenant_id = context.arguments["tenant_id"]
    query = select(CandidateInsightRow).where(
        CandidateInsightRow.tenant_id == tenant_id,
        CandidateInsightRow.subject_type == "RESOURCE",
        CandidateInsightRow.subject_id == resource_id,
        CandidateInsightRow.predicate == predicate,
    ).order_by(CandidateInsightRow.created_at.desc(), CandidateInsightRow.id)
    if batch:
        query = query.where(CandidateInsightRow.provenance["context_scope"].as_string() == context.scope
                            ).with_for_update(read=True).execution_options(populate_existing=True)
    rows = session.scalars(query).all()
    matching = [
        candidate for candidate in rows
        if candidate.provenance.get("context_scope") == context.scope
    ]
    current = []
    review = []
    history = []
    now = context.arguments["now"]
    if batch:
        supports, events_by_id, permitted, current_events, messages = _batch_support(context, matching, visible)
        captions = {}
    for candidate in matching:
        sources = []
        primary = None
        if batch:
            support = supports[candidate.id]
            events = [events_by_id.get(item.source_ref) for item in support]
        else:
            support = session.scalars(select(CandidateInsightEvidenceRow).where(
                CandidateInsightEvidenceRow.tenant_id == tenant_id,
                CandidateInsightEvidenceRow.candidate_id == candidate.id,
                CandidateInsightEvidenceRow.evidence_role == "SUPPORT",
            ).order_by(CandidateInsightEvidenceRow.observed_at, CandidateInsightEvidenceRow.source_ref)).all()
            events = [session.get(TimelineEventRow, item.source_ref) for item in support]
            permitted = context._permitted_sources(
                event.event_ref.get("conversation_message_id") for event in events if event is not None
            )
        for item, event in zip(support, events, strict=True):
            if (
                item.evidence_type != "TIMELINE_EVENT" or event is None or event.tenant_id != tenant_id
                or event.event_ref.get("owner_binding_id") != context.binding.id
                or event.event_ref.get("conversation_message_id") not in permitted
            ):
                break
            message_id = event.event_ref["conversation_message_id"]
            message = (messages.get(message_id) if batch else None) or session.get(ConversationMessageRow, message_id)
            if message is None:
                break
            if batch and event.id not in captions:
                captions[event.id] = source_text(session, event, message)
            caption = captions[event.id] if batch else source_text(session, event, message)
            caption_is_current = (
                message.text and caption == message.text
                and stable_hash(message.text) == item.provenance.get("caption_fingerprint")
            )
            if qualify is not None and event.id == candidate.provenance.get("primary_event") and caption_is_current:
                primary = (message.id, message.text, item.provenance.get("span"))
            sources.append({
                "original_id": message.source_message_id,
                "conversation_id": message.conversation_id,
                "message_id": message.id,
                "timeline_event_id": event.id,
                "canonical_event_id": event.canonical_event_id,
                "receipt_id": event.event_ref["artifact_receipt_id"],
                "inbound_event_id": event.event_ref["inbound_event_id"],
                "resource_id": event.resource_id,
                "source_account": message.source_account,
                "provenance": event.provenance,
                "message_type": message.message_type,
                "transcription_status": (message.metadata_json or {}).get("transcription_status"),
                "actor_key": event.actor_id,
                "occurred_at": event.occurred_at.isoformat(),
                "sensitivity": message.sensitivity_class,
                "current": event.id in visible and bool(caption_is_current),
            })
        if not support or len(sources) != len(support):
            continue
        record = {
            **candidate.proposed_value,
            "candidate_id": candidate.id,
            "confidence": candidate.confidence,
            "state": candidate.state,
            "observed_at": max(source["occurred_at"] for source in sources) if sources else None,
            "interpreted_at": candidate.created_at.isoformat(),
            "semantic_key": candidate.semantic_key,
            "evidence_fingerprint": candidate.provenance.get("evidence_fingerprint"),
            "evidence_status": candidate.evidence_status,
            "decision_ref": candidate.decision_ref,
            "sources": sources,
        }
        history.append(record)
        pending = qualify is not None and candidate.state == "NEEDS_REVIEW"
        if (
            (candidate.state in {"PROPOSED", "ADMITTED"} or pending)
            and all(source["current"] for source in sources)
            and (
                candidate.evidence_status != "EVIDENCE_REVOKED"
                and all(item.source_ref in current_events for item in support)
                if batch else candidate_insight_source_is_current(session, candidate, now=now)
            )
            and candidate.state == record["state"]
        ):
            if qualify is not None and (
                len(sources) != 1 or primary is None or candidate.contradiction_refs
                or (candidate.valid_from is not None and now < candidate.valid_from)
                or (candidate.valid_until is not None and now >= candidate.valid_until)
                or not qualify(record, *primary)
            ):
                continue
            (review if pending else current).append(record)

    current.sort(key=lambda row: (row["observed_at"], row["candidate_id"]), reverse=True)
    episode_ids = {row["episode_id"] for row in history}
    episodes = []
    for episode_id in sorted(episode_ids):
        episode = session.get(SemanticEpisodeRow, episode_id)
        memberships = session.scalars(select(SemanticEpisodeMembershipRow).where(
            SemanticEpisodeMembershipRow.episode_id == episode_id,
            SemanticEpisodeMembershipRow.tenant_id == tenant_id,
        ).order_by(SemanticEpisodeMembershipRow.observed_at, SemanticEpisodeMembershipRow.id)).all()
        member_events = [session.get(TimelineEventRow, member.member_ref) for member in memberships]
        permitted = context._permitted_sources(
            event.event_ref.get("conversation_message_id") for event in member_events if event is not None
        )
        allowed = [
            event.id for event in member_events
            if event is not None and event.tenant_id == tenant_id
            and event.event_ref.get("owner_binding_id") == context.binding.id
            and event.event_ref.get("conversation_message_id") in permitted
        ]
        complete = len(allowed) == len(memberships)
        episodes.append({
            "episode_id": episode_id,
            "state": episode.state if complete else None,
            "support": episode.evidence_status if complete else None,
            "confidence": episode.confidence if complete else None,
            "revision": episode.provenance.get("technical_evidence_reconciliation") if complete else None,
            "memberships": [
                {"timeline_event_id": member.member_ref, "current": member.member_ref in visible}
                for member in memberships if member.member_ref in allowed
            ],
        })
    token_data = asdict(token)
    token_data["generation"]["authority_revision"] = list(token.generation.authority_revision)
    answer = present(question, current, history)
    if qualify is not None:
        answer["review"] = review
    return {
        "answer": answer, "history": history,
        "episodes": episodes, "token": token_data, "retrieval": packet.retrieval_method,
        "visible_events": sorted(visible),
    }


def source_text(session, event, message):
    inbound = session.get(InboundEventRow, event.event_ref["inbound_event_id"])
    if (
        inbound is None or inbound.tenant_id != message.tenant_id or inbound.source != message.source
        or inbound.external_event_id != message.source_message_id
    ):
        return None
    if message.message_type != "VOICE":
        return inbound.payload.get("content")
    if (message.metadata_json or {}).get("transcription_status") != "READY":
        return None
    transcript = session.scalar(select(VoiceTranscriptionRow).where(
        VoiceTranscriptionRow.tenant_id == inbound.tenant_id,
        VoiceTranscriptionRow.inbound_event_id == inbound.id, VoiceTranscriptionRow.status == "READY",
    ).with_for_update(read=True).execution_options(populate_existing=True))
    if transcript is None:
        return None
    return (transcript.transcript_text or "").strip()


def reopen():
    """Child process reconstructs the read using persisted PostgreSQL and authorities."""
    fixture = json.load(sys.stdin)
    url = make_url(fixture["url"])
    if not (
        settings.app_env == "test" and url.drivername == "postgresql+psycopg"
        and url.host in {"127.0.0.1", "localhost", "postgres"} and not url.query
        and re.fullmatch(r"attention_router_test_[0-9a-f]{10}", url.database or "")
        and re.fullmatch(r"fixture_context_[0-9a-f]{12}", url.username or "")
    ):
        raise ValueError("COGNITIVE_LAB_DISPOSABLE_DATABASE_REQUIRED")
    engine = create_engine(url, hide_parameters=True)
    try:
        scope = fixture["scope"]
        for field in ("start", "now"):
            scope[field] = datetime.fromisoformat(scope[field])
        settings.cognitive_runtime_canary_tenant_id = scope["tenant_id"]
        settings.persistent_memory_canary_binding_id = scope["binding_id"]
        settings.conversation_capture_enabled = True
        settings.conversation_capture_start_at = scope["start"]
        settings.conversation_capture_tenant_id = scope["tenant_id"]
        grants = []
        for value in fixture["grants"]:
            value["starts_at"] = datetime.fromisoformat(value["starts_at"])
            value["expires_at"] = datetime.fromisoformat(value["expires_at"])
            value["retention"] = tuple((layer, datetime.fromisoformat(end)) for layer, end in value["retention"])
            grants.append(SourceConsent(**value))
        consent = LabConsent(Path(fixture["consent"]), tuple(grants))
        key = bytes.fromhex(fixture["secret"])
        authority = LabEpochAuthority(Path(fixture["authority"]), authority_id=AUTHORITY, key=key)
        epoch = ContextEpoch(authority, authority_id=AUTHORITY, environment=ENVIRONMENT, key=key)
        sessions = sessionmaker(engine)
        if fixture.get("view") == "client-query":
            from andy_client_context import ask_child

            print(json.dumps(ask_child(sessions, url.username, consent, epoch, scope, fixture)))
            return
        with consent.current() as (_, permits), sessions.begin() as session:
            require_context_database(session, reader_role=url.username)
            context = ContextValidity(session, source_allowed=permits, **scope)
            if fixture.get("view") == "semantic-v1":
                from andy_semantic_scenario import recover_semantic

                result = recover_semantic(context, epoch, fixture["question"], fixture["resource_id"])
            else:
                result = recover(context, epoch, fixture["question"], fixture["resource_id"])
        print(json.dumps(result, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    reopen()
