"""Internal candidate retrieval for an authenticated, consented context read."""

from collections import defaultdict
from hashlib import sha256

from sqlalchemy import select

from attention_router.application import conversation_vectors as vectors
from attention_router.application.cognitive_observation import (
    lock_conversation_dependencies, lock_conversation_sources,
)
from attention_router.application.context_compiler import ConversationExcerpt, RankedEvidenceCandidate
from attention_router.application.conversation_embedding import get_provider
from attention_router.application.conversation_hybrid import QueryProvider, _pool, load_policy
from attention_router.application.conversation_relevance import evidence_reason, order_candidates
from attention_router.application.conversation_segments import segment_messages
from attention_router.application.platform.artifacts import context_receipt_refusal
from attention_router.application.context_canary_gate import canary_configured
from attention_router.config import settings  # noqa: F401 - test compatibility
from attention_router.infrastructure.artifact_models import ArtifactReceiptRow
from attention_router.infrastructure.models import InboundEventRow, VoiceTranscriptionRow


# Bound row locks and reconstruction in the experimental READ COMMITTED path.
MAX_SEGMENTS = 200


def _memberships(session, rows, tenant, account):
    result = defaultdict(list)
    for row in vectors.sql(
        session,
        """SELECT segment_id, conversation_id, message_id, ordinal, char_start, char_end, role
        FROM conversation_vector_memberships
        WHERE tenant_id=:tenant AND source_account=:account AND segment_id=ANY(:ids)
        ORDER BY segment_id, ordinal""",
        tenant=tenant, account=account, ids=[row["id"] for row in rows],
    ).mappings():
        result[row["segment_id"]].append((
            row["conversation_id"], row["message_id"], row["ordinal"],
            row["char_start"], row["char_end"], row["role"],
        ))
    return dict(result)


def _eligible(message, *, now, start, participant, direction, message_type, start_at, end_at):
    return (
        start <= message["sent_at"] <= now
        and (participant is None or participant in (message["external_sender_key"], message["sender_actor_id"]))
        and (direction is None or direction == message["direction"])
        and (message_type is None or message_type == message["message_type"])
        and (start_at is None or start_at <= message["sent_at"])
        and (end_at is None or message["sent_at"] <= end_at)
    )


def _read_sources(context, memberships, filters, *, reuse_locked_sources=False, capture_versions=False):
    tenant = context.arguments["tenant_id"]
    account = context.arguments["source_account"]
    now = context.arguments["now"]
    refs = {member[1] for members in memberships.values() for member in members}
    if reuse_locked_sources:
        messages = context.lock_permitted_source_rows(refs, required_layers=("embedding",))
        permitted = {message.id for message in messages}
        locked = lock_conversation_dependencies(context.session, tenant, messages)
    else:
        permitted = context._permitted_sources(refs, required_layers=("embedding",))
        locked = lock_conversation_sources(context.session, tenant, permitted)
    receipts = locked.get(ArtifactReceiptRow, {})
    inbound = locked.get(InboundEventRow, {})
    transcriptions = set()
    if inbound:
        column = VoiceTranscriptionRow if capture_versions else VoiceTranscriptionRow.id
        transcription_rows = context.session.scalars(
            select(column)
            .where(VoiceTranscriptionRow.tenant_id == tenant, VoiceTranscriptionRow.inbound_event_id.in_(inbound))
            .order_by(VoiceTranscriptionRow.id).with_for_update(read=True)
            .execution_options(populate_existing=True)
        ).all()
        if capture_versions:
            locked[VoiceTranscriptionRow] = {row.id: row for row in transcription_rows}
            transcriptions = locked[VoiceTranscriptionRow].keys()
        else:
            transcriptions = set(transcription_rows)
    conversations = sorted({member[0] for members in memberships.values() for member in members})
    authorized = defaultdict(list)
    for message in vectors.sources_for_conversations(
        context.session, tenant, account, conversations, require_current_receipts=True, message_ids=permitted,
    ):
        if not _eligible(message, now=now, start=context.arguments["start"], **filters):
            continue
        metadata = message["metadata"] or {}
        receipt = receipts.get(metadata.get("artifact_receipt_id"))
        if receipt is None or context_receipt_refusal(receipt.metadata_json, now=now) is not None:
            continue
        if message["message_type"] == "VOICE":
            original = inbound.get(metadata.get("source_inbound_event_id"))
            if (
                original is None or original.source != message["source"]
                or original.external_event_id != message["source_message_id"]
                or (original.payload.get("metadata") or {}).get("source_account") != account
                or message["transcription_id"] not in transcriptions
                or not context._permitted_sources((message["id"],), required_layers=("transcription",))
            ):
                continue
        authorized[message["conversation_id"]].append(message)
    return authorized, receipts, locked


def _excerpts(plan, receipts):
    excerpts = []
    refs = {member["message"]["id"] for member in plan["members"]}
    for member in plan["members"]:
        message = member["message"]
        receipt = receipts[message["metadata"]["artifact_receipt_id"]]
        excerpts.append(ConversationExcerpt(
            source_ref=message["id"],
            conversation_ref=message["conversation_id"],
            observed_at=message["sent_at"],
            text=message["index_text"][member["start"]:member["end"]],
            char_start=member["start"],
            char_end=member["end"],
            source_fingerprint=message["source_fingerprint"],
            source_revision=message["context_revision"],
            receipt_ref=receipt.id,
            receipt_revision=receipt.context_revision,
            transcription_ref=message["transcription_id"],
            reply_to_ref=message["reply_to_message_id"] if message["reply_to_message_id"] in refs else None,
            role=member["role"],
        ))
    return tuple(excerpts)


def prepare_evidence_query(query: str, *, provider=None) -> QueryProvider:
    """Encode only the question for the isolated caller, before its read transaction."""
    if not canary_configured():
        raise ValueError("CONVERSATION_EVIDENCE_PILOT_ONLY")
    if not query.strip() or len(query) > 4000:
        raise ValueError("VECTOR_QUERY_INVALID")
    provider = provider or get_provider()
    return QueryProvider(provider, query, provider.encode([query], query=True)[0])


def _validate_filters(direction, message_type, start_at, end_at):
    if (
        direction not in (None, "INBOUND", "OUTBOUND") or message_type not in (None, "TEXT", "VOICE")
        or any(stamp is not None and stamp.utcoffset() is None for stamp in (start_at, end_at))
        or (start_at is not None and end_at is not None and start_at > end_at)
    ):
        raise ValueError("VECTOR_FILTER_INVALID")


def retrieve_evidence_candidates(
    context,
    epoch,
    query: str,
    *,
    provider=None,
    reuse_locked_sources=False,
    prepared_features=None,
    conversation_id=None,
    participant=None,
    direction=None,
    message_type=None,
    start_at=None,
    end_at=None,
) -> tuple[RankedEvidenceCandidate, ...]:
    """Call inside the authenticated reader's live consent/authority transaction."""
    if not canary_configured() or context._source_allowed is None:
        raise ValueError("CONVERSATION_EVIDENCE_PILOT_ONLY")
    if not query.strip() or len(query) > 4000:
        raise ValueError("VECTOR_QUERY_INVALID")
    _validate_filters(direction, message_type, start_at, end_at)

    token = epoch.token(context)
    tenant = context.arguments["tenant_id"]
    account = context.arguments["source_account"]
    session = context.session
    vectors.scope(session, tenant, account)
    if not vectors.authorized(session, tenant):
        raise PermissionError("VECTOR_SCOPE_REVOKED")
    filters = dict(participant=participant, direction=direction, message_type=message_type,
                   start_at=start_at, end_at=end_at)
    provider = provider or get_provider()
    vector = provider.encode([query], query=True)[0]
    rows = _pool(session, provider, vector, tenant_id=tenant, source_account=account,
                 conversation_id=conversation_id, **filters)
    if len(rows) > MAX_SEGMENTS:
        raise ValueError("CONVERSATION_EVIDENCE_NARROWER_SCOPE_REQUIRED")

    memberships = _memberships(session, rows, tenant, account)
    authorized, receipts, _ = _read_sources(
        context, memberships, filters, reuse_locked_sources=reuse_locked_sources,
    )
    candidates = _rank_evidence(
        rows, memberships, authorized, receipts, provider, query, load_policy(),
        prepared_features=prepared_features,
    )
    latest = _pool(session, provider, vector, tenant_id=tenant, source_account=account,
                   conversation_id=conversation_id, **filters)
    if latest != rows or _memberships(session, rows, tenant, account) != memberships:
        raise ValueError("CONVERSATION_EVIDENCE_INDEX_CHANGED")
    if token != epoch.token(context) or not vectors.authorized(session, tenant):
        raise PermissionError("VECTOR_SCOPE_REVOKED")
    return candidates


def _content_matches(plan, row):
    if "content_text" in row:
        return plan["text"] == row["content_text"]
    return sha256(plan["text"].encode()).hexdigest() == row["indexed_text_sha256"]


def _rank_evidence(
    rows, memberships, authorized, receipts, provider, query, policy, *, prepared_features=None,
):
    plans = {
        plan["id"]: plan for messages in authorized.values() for plan in segment_messages(messages, provider)
    }
    eligible = []
    for row in rows:
        plan = plans.get(row["id"])
        if plan is None or any(plan[key] != row[key] for key in ("source_hash", "content_hash")):
            continue
        expected = [
            (member["message"]["conversation_id"], member["message"]["id"], ordinal,
             member["start"], member["end"], member["role"])
            for ordinal, member in enumerate(plan["members"])
        ]
        if expected != memberships.get(row["id"]) or not _content_matches(plan, row):
            continue
        eligible.append({**row, "content_text": plan["text"]})

    ranked = order_candidates(eligible, query, strategy="hybrid", config=policy, prepared=prepared_features)
    return tuple(RankedEvidenceCandidate(
        segment_ref=row["id"], relevance_score=row["relevance_score"],
        source_hash=row["source_hash"], content_hash=row["content_hash"],
        excerpts=_excerpts(plans[row["id"]], receipts), single_excerpt_heuristic=evidence_reason(row, policy),
    ) for row in ranked)
