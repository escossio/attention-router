"""Opt-in staged conversation retrieval for the governed test reader."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from time import monotonic
from types import SimpleNamespace

from sqlalchemy import inspect

from attention_router.application import conversation_evidence as evidence
from attention_router.application import conversation_vectors as vectors
from attention_router.application.client_context_staged import StagedClientContextAuthority
from attention_router.application.context_compiler import compile_evidence_candidates
from attention_router.application.context_index_fence import fence_index
from attention_router.application.conversation_segments import POLICY as SEGMENT_POLICY
from attention_router.application.platform.artifacts import context_receipt_refusal
from attention_router.application.staged_read import (
    DetachedEvidence, SourceVersion, StagedRead, StagedReadRejected,
)
from attention_router.observability.private_context import Decision, record
from attention_router.infrastructure.artifact_models import ArtifactReceiptRow
from attention_router.infrastructure.models import ConversationMessageRow


_MESSAGE_FIELDS = (
    "id", "tenant_id", "source_account", "conversation_id", "source", "source_message_id",
    "sender_actor_id", "external_sender_key", "from_me", "sent_at", "reply_to_message_id",
    "index_text", "source_fingerprint", "context_revision", "transcription_id",
)


def _encode(value):
    def timestamp(item):
        if isinstance(item, datetime):
            return item.isoformat()
        raise TypeError("STAGED_CONVERSATION_FIELD_INVALID")

    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, allow_nan=False,
        separators=(",", ":"), default=timestamp,
    ).encode()


def _hash_row(row):
    return sha256(_encode({
        prop.key: getattr(row, prop.key) for prop in inspect(type(row)).column_attrs
    })).hexdigest()


def _collect(context, resource_id, prepared, filters, policy, captured_at):
    tenant = context.arguments["tenant_id"]
    account = context.arguments["source_account"]
    session = context.session
    vectors.scope(session, tenant, account)
    if context._source_allowed is None or not vectors.authorized(session, tenant):
        raise StagedReadRejected("STAGED_CONVERSATION_SCOPE_REVOKED")

    rows = evidence._pool(
        session, prepared, prepared.vector, tenant_id=tenant, source_account=account,
        include_versions=True, pool_limit=evidence.MAX_SEGMENTS + 1, **filters,
    )
    if len(rows) > evidence.MAX_SEGMENTS:
        raise StagedReadRejected("CONVERSATION_EVIDENCE_NARROWER_SCOPE_REQUIRED")
    memberships = evidence._memberships(session, rows, tenant, account)
    if any(len(members) > 12 for members in memberships.values()):
        raise StagedReadRejected("STAGED_CONVERSATION_MEMBERSHIP_INVALID")
    authorized, receipts, locked = evidence._read_sources(
        context, memberships, {key: value for key, value in filters.items() if key != "conversation_id"},
        reuse_locked_sources=True, capture_versions=True,
    )
    candidate_refs = {member[1] for members in memberships.values() for member in members}
    record(Decision.CANDIDATES, len(candidate_refs))
    record(Decision.DISCARDED, len(candidate_refs.difference(locked.get(ConversationMessageRow, {}))))
    detached_messages = {}
    sources = {}
    for conversation, messages in authorized.items():
        detached_messages[conversation] = []
        for message in messages:
            receipt = receipts[message["metadata"]["artifact_receipt_id"]]
            mid = message["id"]
            if mid in sources:
                raise StagedReadRejected("STAGED_CONVERSATION_SOURCE_DUPLICATE")
            projected = {key: message[key] for key in _MESSAGE_FIELDS}
            projected["metadata"] = {"artifact_receipt_id": receipt.id}
            detached_messages[conversation].append(projected)
            sources[mid] = SourceVersion(
                mid, message["context_revision"], receipt.id, receipt.context_revision,
                message["source_fingerprint"],
            )

    detached_rows = []
    for row in rows:
        # The candidate pool can contain text outside the consented source set.
        # Fingerprint it for equality and reconstruct only from permitted messages.
        detached_rows.append({
            key: value for key, value in row.items() if key not in ("content_text", "indexed_embedding")
        } | {
            "indexed_text_sha256": sha256(row["content_text"].encode()).hexdigest(),
            "embedding_sha256": sha256(row["indexed_embedding"].encode()).hexdigest(),
        })

    provider = prepared.base
    if (provider.model, provider.revision, provider.dimensions) != (
        prepared.model, prepared.revision, prepared.dimensions,
    ):
        raise StagedReadRejected("STAGED_CONVERSATION_MODEL_CHANGED")

    snapshot = DetachedEvidence(_encode({
        "scope": context.scope,
        "tenant": tenant,
        "source_account": account,
        "resource_id": resource_id,
        "start": context.arguments["start"],
        "captured_at": captured_at,
        "embedding": [prepared.model, prepared.revision, prepared.dimensions],
        "query_vector_sha256": sha256(vectors.vector_literal(prepared.vector).encode()).hexdigest(),
        "filters": dict(filters),
        "segment_policy": SEGMENT_POLICY,
        "ranking_policy": policy,
        "rows": detached_rows,
        "memberships": memberships,
        "messages": detached_messages,
        "dependencies": {
            model.__tablename__: {key: _hash_row(row) for key, row in instances.items()}
            for model, instances in locked.items()
        },
    }), tuple(sources[key] for key in sorted(sources)))
    return snapshot, locked


def _check_expiry(context, snapshot, locked, now):
    """Repeat only time-dependent gates immediately before handing off the body."""
    wall_time = datetime.now(UTC)
    for source in snapshot.sources:
        message = locked[ConversationMessageRow][source.source_ref]
        receipt = locked[ArtifactReceiptRow][source.receipt_ref]
        layers = ("embedding", "transcription") if message.message_type == "VOICE" else ("embedding",)
        if (
            not context._source_allowed(
                message, owner_id=context.arguments["owner_human_identity_id"], now=now,
                required_layers=layers,
            )
            or context_receipt_refusal(receipt.metadata_json, now=now) is not None
            or context_receipt_refusal(receipt.metadata_json, now=wall_time) is not None
        ):
            raise StagedReadRejected("STAGED_CONVERSATION_SUPPORT_EXPIRED")


def _compile(snapshot, query, provider, *, item_budget, token_budget):
    payload = json.loads(snapshot.content)
    for messages in payload["messages"].values():
        for message in messages:
            message["sent_at"] = datetime.fromisoformat(message["sent_at"])
    memberships = {
        key: [tuple(member) for member in members] for key, members in payload["memberships"].items()
    }
    receipts = {
        source.receipt_ref: SimpleNamespace(id=source.receipt_ref, context_revision=source.receipt_revision)
        for source in snapshot.sources
    }
    candidates = evidence._rank_evidence(
        payload["rows"], memberships, payload["messages"], receipts, provider, query, payload["ranking_policy"],
    )
    return compile_evidence_candidates(
        payload["tenant"], query, candidates, item_budget=item_budget, token_budget=token_budget,
        now=datetime.fromisoformat(payload["captured_at"]),
    )


class StagedConversationEvidenceReader:
    """Explicit wiring for the test-only ClientContextReader.

    reserve_query(scope) must return True after charging/reserving one encode.
    The PostgreSQL fence protects the complete pool predicate, including
    insertions and membership moves.
    The StagedRead callback is the actual disclosure boundary.
    """

    def __init__(
        self, original, *, reserve_query: Callable,
        provider=None, conversation_id=None, participant=None, direction=None,
        message_type=None, start_at=None, end_at=None, item_budget=10, token_budget=4000,
        pg_consent=None, owner_scope_registry=None, render_packet=None,
    ):
        if not callable(reserve_query):
            raise ValueError("STAGED_CONVERSATION_GATES_REQUIRED")
        evidence._validate_filters(direction, message_type, start_at, end_at)
        self._render_packet = render_packet
        self._original = original
        self._pg_consent = pg_consent
        self._owner_scope_registry = owner_scope_registry
        self._reserve_query = reserve_query
        self._provider = provider
        self._filters = dict(conversation_id=conversation_id, participant=participant,
                             direction=direction, message_type=message_type, start_at=start_at, end_at=end_at)
        self._item_budget = item_budget
        self._token_budget = token_budget

    def query_and_deliver(self, *, bearer, scope_ref, question, deliver):
        if not callable(deliver):
            raise ValueError("STAGED_DELIVERY_CALLBACK_REQUIRED")
        if (
            not bearer or not scope_ref or not isinstance(question, str)
            or not question.strip() or len(question) > 1000
        ):
            raise StagedReadRejected("STAGED_QUERY_INVALID")

        def capture(context, _epoch, _question, resource_id):
            fence_index(context)
            snapshot, _ = _collect(
                context, resource_id, prepared, self._filters, policy, context.arguments["now"],
            )
            return snapshot

        def verify(context, _epoch, snapshot, resource_id):
            fence_index(context)
            current, locked = _collect(
                context, resource_id, prepared, self._filters, evidence.load_policy(),
                json.loads(snapshot.content)["captured_at"],
            )
            if current != snapshot:
                raise StagedReadRejected("STAGED_CONVERSATION_SUPPORT_CHANGED")
            return lambda: _check_expiry(context, current, locked, self._original._clock())

        def compute(snapshot, query):
            packet = _compile(
                snapshot, query, prepared, item_budget=self._item_budget, token_budget=self._token_budget,
            )
            record(Decision.COMPILED, len(packet.items))
            return _encode(self._render_packet(packet) if self._render_packet else packet.internal_payload())

        authority = StagedClientContextAuthority(
            self._original, capture_fn=capture, verify_fn=verify,
            pg_consent=self._pg_consent, owner_scope_registry=self._owner_scope_registry,
        )
        scope = authority.authorize_preparation(bearer=bearer, scope_ref=scope_ref)
        if self._reserve_query(scope) is not True:
            raise StagedReadRejected("STAGED_CONVERSATION_COST_DENIED")
        record(Decision.ADMITTED)
        started = monotonic()
        prepared = evidence.prepare_evidence_query(question, provider=self._provider)
        prepared.vector = tuple(prepared.vector)
        vectors.vector_literal(prepared.vector)
        if monotonic() - started > 15:
            raise StagedReadRejected("STAGED_CONVERSATION_ENCODE_EXPIRED")
        policy = evidence.load_policy()
        reader = StagedRead(authority, compute, max_body_bytes=16_384)
        return reader.query_and_deliver(bearer=bearer, scope_ref=scope_ref, question=question, deliver=deliver)
