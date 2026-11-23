"""Bind a semantic proposal to existing V2A/D/E/F engines in the test lab."""
from dataclasses import asdict, dataclass
from datetime import timedelta

from sqlalchemy import select

from andy_evidence_support import EvidenceTarget, assess_evidence, matches_projection, read_statement
from andy_lab import recover, source_text
from andy_semantic_contract import SourceDocument, source_document, validate_proposal
from andy_semantic_grounding import MeetingConfirmationQuery, TargetInsufficiency
from attention_router.application.candidate_insights import CandidateEvidenceInput, propose_candidate_insight
from attention_router.application.semantic_episodes import assign_timeline_event_to_episode
from attention_router.infrastructure.candidate_insight_models import CandidateInsightRow
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import ConversationMessageRow, TimelineEventRow


PREDICATE = "lab_semantic_statement_v1"


@dataclass(frozen=True)
class BoundDocument:
    document: SourceDocument
    event: TimelineEventRow


def bind_documents(context, message_ids, anchors):
    """Resolve eligible sources from the governed graph, with row locks."""
    _, graph = context.graph()
    event_ids = [item.source_id for item in graph.nodes if item.source_type == "TIMELINE_EVENT"]
    session = context.session
    events = session.scalars(select(TimelineEventRow).where(
        TimelineEventRow.tenant_id == context.arguments["tenant_id"],
        TimelineEventRow.id.in_(event_ids),
        TimelineEventRow.event_ref["conversation_message_id"].as_string().in_(message_ids),
    )).all()
    docs = {}
    for event in events:
        message = session.get(ConversationMessageRow, event.event_ref["conversation_message_id"])
        text = source_text(session, event, message)
        if event.actor_id and event.resource_id and text and text == message.text:
            docs[message.id] = BoundDocument(
                source_document(message.id, text, event.occurred_at, anchors.get(message.id)), event,
            )
    if docs.keys() != set(message_ids):
        raise ValueError("SEMANTIC_SOURCE_UNAVAILABLE")
    return tuple(docs[message_id] for message_id in message_ids)


def persist_semantic(
    context, raw, documents, *, model_manifest=None, commercial_origin=None,
    commercial_checkpoint=None, target_insufficiency: TargetInsufficiency | None = None,
    target: EvidenceTarget | None = None,
):
    if model_manifest is not None and commercial_origin is not None:
        raise ValueError("SEMANTIC_MULTIPLE_MODEL_ORIGINS")
    if (commercial_origin is None) != (commercial_checkpoint is None):
        raise ValueError("SEMANTIC_COMMERCIAL_CHECKPOINT_REQUIRED")
    primary, *related = documents
    outcome = validate_proposal(raw, primary.document, tuple(item.document for item in related))
    if target_insufficiency is not None:
        target_insufficiency.verify(primary.document)
        return None
    if outcome is None:
        return None
    interpretation, value = outcome
    if target is not None:
        document = primary.document
        assessment = assess_evidence(target, tuple(item.document for item in documents))
        if (
            assessment.state not in {"OBSERVED_PROPOSAL", "QUALIFIED_ONLY"}
            or any(span.source_id not in assessment.support
                   for span in (interpretation.evidence, *interpretation.corroboration))
            or interpretation.evidence.quote != document.text
            or not matches_projection(read_statement(document.text, document.anchors), value)
        ):
            return None
    session = context.session
    event = primary.event
    previous_rejection = session.scalar(select(CandidateInsightRow.id).where(
        CandidateInsightRow.tenant_id == event.tenant_id,
        CandidateInsightRow.predicate == PREDICATE,
        CandidateInsightRow.state == "REJECTED",
        CandidateInsightRow.provenance["context_scope"].as_string() == context.scope,
        CandidateInsightRow.provenance["primary_event"].as_string() == event.id,
    ).order_by(CandidateInsightRow.id).limit(1))
    if previous_rejection is not None:
        return previous_rejection

    stamp = context.arguments["now"]
    scope_key = stable_hash({"binding_id": context.binding.id, "source_account": context.arguments["source_account"]})
    assignment = assign_timeline_event_to_episode(
        session, tenant_id=event.tenant_id, event_id=event.id,
        episode_type=f"ATTACHMENT_OBSERVED:{scope_key[:32]}", scope_type="RESOURCE",
        max_inactivity=timedelta(days=6), confidence=0.5, now=stamp,
    )
    by_source = {item.document.id: item for item in documents}
    # Give corroborating observed inputs a native V2D membership as well, so
    # source withdrawal reconciles V2E dependents of either resource.
    evidence = []
    new_membership = assignment.created_membership
    for span in (interpretation.evidence, *interpretation.corroboration):
        item = by_source[span.source_id]
        if item is not primary:
            linked = assign_timeline_event_to_episode(
                session, tenant_id=event.tenant_id, event_id=item.event.id,
                episode_type=f"ATTACHMENT_OBSERVED:{scope_key[:32]}", scope_type="RESOURCE",
                max_inactivity=timedelta(days=6), confidence=0.5, now=stamp,
            )
            new_membership |= linked.created_membership
        evidence.append(CandidateEvidenceInput(
            "TIMELINE_EVENT", item.event.id, item.event.canonical_event_id, 0.5,
            provenance={
                "span": [span.start, span.end], "caption_fingerprint": stable_hash(item.document.text),
            },
        ))
    source_engine, adapter = ("RULE", "explicit_synthetic_submission")
    model_provenance = {}
    if model_manifest is not None:
        source_engine, adapter = "LLM", "local_completion"
        model_provenance = {"local_model": asdict(model_manifest)}
    if commercial_origin is not None:
        source_engine, adapter = "LLM", "commercial_responses"
        model_provenance = {
            "commercial_model": asdict(commercial_origin),
            "commercial_checkpoint": {
                "generation": commercial_checkpoint.generation.generation,
                "epoch": commercial_checkpoint.epoch,
                "epoch_revision": commercial_checkpoint.revision,
            },
        }
    candidate, created = propose_candidate_insight(
        session, tenant_id=event.tenant_id, insight_type="STATE_PROPOSAL",
        subject_type="RESOURCE", subject_id=event.resource_id, predicate=PREDICATE,
        proposed_value={**value, "episode_id": assignment.episode.id}, source_engine=source_engine,
        confidence=0.5, evidence=evidence, now=stamp,
        provenance={
            "schema_version": interpretation.version, "adapter": adapter,
            "context_scope": context.scope, "primary_event": event.id, **model_provenance,
        },
    )
    if created or new_membership:
        context._advance("LAB_STRUCTURED_PROPOSAL", primary.document.id)
    return candidate.id


def describe(question, current, history):
    if question == "history":
        return {"state": "HISTORY", "evidence": history}
    return describe_matches([row for row in current if row["predicate"] == question])


def describe_matches(matches):
    applicable = [row for row in matches if row["mode"] in {"observation", "cancellation"}]
    claims = {(row["mode"], row["polarity"], row["object"].casefold(), row.get("date")) for row in applicable}
    if len(claims) > 1:
        return {"state": "CONFLICT", "evidence": applicable}
    if applicable:
        return {"state": "OBSERVED_PROPOSAL", "evidence": applicable}
    if matches:
        return {"state": "QUALIFIED_ONLY", "evidence": matches}
    return {"state": "INSUFFICIENT_EVIDENCE", "evidence": []}


def recover_semantic(context, epoch, question, resource_id, *, batch=True):
    if isinstance(question, dict):
        intent = MeetingConfirmationQuery.model_validate(question)
        return recover(
            context, epoch, question, resource_id, predicate=PREDICATE,
            present=lambda _question, current, _history: describe_matches(current), qualify=intent.matches, batch=batch,
        )
    return recover(context, epoch, question, resource_id, predicate=PREDICATE, present=describe)
