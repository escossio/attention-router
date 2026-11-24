"""Read-only hybrid retrieval; calibrated textual support never certifies a fact."""

import json
from pathlib import Path
import time
from attention_router.application import conversation_vectors as v
from attention_router.application.conversation_embedding import get_provider
from attention_router.application.conversation_relevance import (
    order_candidates,
    evidence_reason,
    ambiguous,
    POLICY_VERSION,
)


def load_policy():
    policy = json.loads(Path(__file__).with_name("conversation_relevance_policy.json").read_text())
    if policy["policy_version"] != POLICY_VERSION:
        raise ValueError("RELEVANCE_POLICY_VERSION_MISMATCH")
    return policy


class QueryProvider:
    def __init__(self, provider, query, vector):
        self.base = provider
        self.query = query
        self.vector = vector
        self.model = provider.model
        self.revision = provider.revision
        self.dimensions = provider.dimensions

    def encode(self, texts, *, query=False):
        if query and texts == [self.query]:
            return [self.vector]
        return self.base.encode(texts, query=query)

    def count(self, value):
        return self.base.count(value)

    def fragments(self, value):
        return self.base.fragments(value)


def _pool(
    s,
    provider,
    vector,
    *,
    tenant_id,
    source_account,
    conversation_id=None,
    participant=None,
    direction=None,
    message_type=None,
    start_at=None,
    end_at=None,
    include_versions=False,
    pool_limit=5001,
    **unused,
):
    versions = (
        ", g.generation AS segment_generation, g.updated_at AS segment_updated_at"
        ", g.policy_version AS segment_policy, e.created_at AS embedding_created_at"
        ", e.embedding::text AS indexed_embedding"
        if include_versions else ""
    )
    return [
        dict(r)
        for r in v.sql(
            s,
            f"""SELECT g.id,g.content_text,g.conversation_id,g.content_hash,g.source_hash,g.start_at,g.end_at,
      1-(e.embedding <=> CAST(:vector AS vector)) AS similarity{versions}
      FROM conversation_vector_segments g JOIN conversation_embeddings e ON e.segment_id=g.id AND e.tenant_id=g.tenant_id AND e.source_account=g.source_account AND e.content_hash=g.content_hash
      WHERE g.tenant_id=:tenant AND g.source_account=:account AND g.state='READY' AND e.model=:model AND e.model_revision=:revision
      AND (CAST(:conversation AS varchar) IS NULL OR g.conversation_id=:conversation)
      AND EXISTS(SELECT 1 FROM conversation_vector_memberships sm JOIN conversation_messages m ON m.id=sm.message_id
        WHERE sm.segment_id=g.id AND m.tenant_id=:tenant AND m.source_account=:account AND m.searchable AND m.sensitivity_class<>'SECRET'
        AND (CAST(:participant AS varchar) IS NULL OR m.external_sender_key=:participant OR m.sender_actor_id=:participant)
        AND (CAST(:direction AS varchar) IS NULL OR m.direction=:direction)
        AND (CAST(:kind AS varchar) IS NULL OR m.message_type=:kind)
        AND (CAST(:start AS timestamptz) IS NULL OR m.sent_at>=:start)
        AND (CAST(:end AS timestamptz) IS NULL OR m.sent_at<=:end))
      ORDER BY g.id LIMIT :pool_limit""",
            tenant=tenant_id,
            account=source_account,
            conversation=conversation_id,
            participant=participant,
            direction=direction,
            kind=message_type,
            start=start_at,
            end=end_at,
            model=provider.model,
            revision=provider.revision,
            vector=v.vector_literal(vector),
            pool_limit=pool_limit,
        ).mappings()
    ]


def _context(session, result, tenant, account, provider, versions):
    rows = v.sources(session, tenant, account, result["conversation_id"], require_current_receipts=True)
    positions = {m["id"]: i for i, m in enumerate(rows)}
    selected = {m["id"] for m in result["messages"]}
    present = [positions[mid] for mid in selected if mid in positions]
    if len(present) != len(selected):
        return None
    if any(v._source_version(rows[positions[mid]]) != versions[mid] for mid in selected):
        return None
    additions = []
    for index, boundary, side in [
        (min(present) - 1, min(present), "PREVIOUS"),
        (max(present) + 1, max(present), "NEXT"),
    ]:
        if not 0 <= index < len(rows):
            continue
        neighbor, current = rows[index], rows[boundary]
        if (
            neighbor["id"] in selected
            or abs((neighbor["sent_at"] - current["sent_at"]).total_seconds()) > 300
        ):
            continue
        explicit = (
            current.get("reply_to_message_id") == neighbor["id"]
            or neighbor.get("reply_to_message_id") == current["id"]
        )
        dependent = (side == "PREVIOUS" and len(current["index_text"].split()) <= 4) or (
            side == "NEXT"
            and "?" in current["index_text"]
            and len(neighbor["index_text"].split()) <= 4
        )
        if not (explicit or dependent):
            continue
        fields = (
            "id",
            "source_message_id",
            "conversation_id",
            "source",
            "source_account",
            "sent_at",
            "direction",
            "from_me",
            "sender_actor_id",
            "external_sender_key",
            "reply_to_message_id",
            "message_type",
            "transcription_id",
        )
        fragments = provider.fragments(neighbor["index_text"])
        lo, hi = fragments[-1] if side == "PREVIOUS" else fragments[0]
        versions[neighbor["id"]] = v._source_version(neighbor)
        additions.append(
            {k: neighbor.get(k) for k in fields}
            | {
                "text": neighbor["index_text"][lo:hi],
                "char_start": lo,
                "char_end": hi,
                "context_truncated": lo > 0 or hi < len(neighbor["index_text"]),
                "role": "EXPLICIT_REPLY_CONTEXT"
                if explicit
                else "CHRONOLOGICAL_CONTEXT_NOT_CONFIRMED_REPLY",
                "context_only": True,
            }
        )
    result["messages"] = sorted(
        result["messages"] + additions,
        key=lambda m: (m["sent_at"], m["id"], m.get("char_start", 0)),
    )
    result["context_expansion"] = {
        "added_messages": len(additions),
        "max_neighbors": 2,
        "max_gap_seconds": 300,
        "max_tokens_per_neighbor": 350,
        "relation_inferred": False,
    }
    return result


def search(
    session,
    *,
    query,
    tenant_id,
    source_account,
    limit=10,
    search_mode="hybrid",
    provider=None,
    policy=None,
    **filters,
):
    started = time.monotonic()
    v.scope(session, tenant_id, source_account)
    if not v.authorized(session, tenant_id):
        raise PermissionError("VECTOR_SCOPE_REVOKED")
    if (
        search_mode not in ("hybrid", "vector", "lexical")
        or not query.strip()
        or len(query) > 4000
        or not 1 <= limit <= 50
    ):
        raise ValueError("VECTOR_QUERY_INVALID")
    if filters.get("direction") not in (None, "INBOUND", "OUTBOUND") or filters.get(
        "message_type"
    ) not in (None, "TEXT", "VOICE"):
        raise ValueError("VECTOR_FILTER_INVALID")
    start, end = filters.get("start_at"), filters.get("end_at")
    if any(x is not None and x.tzinfo is None for x in (start, end)) or (
        start and end and start > end
    ):
        raise ValueError("VECTOR_TIME_RANGE_INVALID")
    policy = policy or load_policy()
    provider = provider or get_provider()
    reasons = {}

    def response(results, state):
        return {
            "strategy": POLICY_VERSION
            if search_mode == "hybrid"
            else "PASSIVE_" + search_mode.upper() + "_DIAGNOSTIC",
            "state": state,
            "results": results,
            "rejection_reasons": reasons,
            "policy_version": POLICY_VERSION,
            "latency_ms": round((time.monotonic() - started) * 1000, 2),
            "evidence_contract": "TEXTUAL_SUPPORT_NOT_ENTAILMENT_OR_TRUTH_PROBABILITY",
            "execution_authority": False,
        }

    if search_mode == "hybrid" and ambiguous(
        query,
        conversation_id=filters.get("conversation_id"),
        participant=filters.get("participant"),
    ):
        reasons["QUERY_AMBIGUOUS"] = 1
        return response([], "INSUFFICIENT_EVIDENCE")
    vector = provider.encode([query], query=True)[0]
    candidates = _pool(
        session, provider, vector, tenant_id=tenant_id, source_account=source_account, **filters
    )
    if len(candidates) > 5000:
        reasons["SCOPE_REQUIRES_NARROWER_FILTERS"] = 1
        return response([], "INSUFFICIENT_EVIDENCE")
    ranked = order_candidates(candidates, query, strategy=search_mode, config=policy)
    kept = []
    for row in ranked:
        reason = evidence_reason(row, policy) if search_mode == "hybrid" else None
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
        else:
            kept.append(row)
    # Validate the original source/hash again after candidate scoring. Invalidated evidence is never served.
    selected = kept[:50]
    if not selected:
        if not reasons:
            reasons["NO_ELIGIBLE_EVIDENCE"] = 1
        return response([], "INSUFFICIENT_EVIDENCE")
    versions = {}
    raw = v.search(
        session,
        tenant_id=tenant_id,
        source_account=source_account,
        query=query,
        limit=50,
        provider=QueryProvider(provider, query, vector),
        _candidate_ids=[r["id"] for r in selected],
        _source_versions=versions,
        **filters,
    )
    valid = {r["segment_id"]: r for r in raw["results"]}
    results = []
    for row in selected:
        if row["id"] not in valid:
            reasons["SOURCE_CHANGED_OR_REVOKED"] = reasons.get("SOURCE_CHANGED_OR_REVOKED", 0) + 1
            continue
        result = valid[row["id"]]
        result.update(
            relevance_score=row["relevance_score"],
            relevance_score_semantics="WEIGHTED_RECIPROCAL_RANK_NOT_PROBABILITY",
            lexical_score=row["lexical_score"],
            query_coverage=row["coverage"],
            selection_reasons=[
                "SOURCE_HASH_REVALIDATED",
                "EXPLICIT_SCOPE_MATCH",
                "CALIBRATED_TEXTUAL_SUPPORT",
            ]
            if search_mode == "hybrid"
            else ["SOURCE_HASH_REVALIDATED", "DIAGNOSTIC_NEIGHBOR_NOT_SUFFICIENCY"],
            relevance_policy=POLICY_VERSION,
        )
        contextual = _context(session, result, tenant_id, source_account, provider, versions[row["id"]])
        if contextual is None:
            reasons["SOURCE_CHANGED_OR_REVOKED"] = reasons.get("SOURCE_CHANGED_OR_REVOKED", 0) + 1
            continue
        results.append(contextual)
        if len(results) >= limit:
            break
    fresh = v.revalidated_results(session, tenant_id, source_account, results, versions)
    if len(fresh) != len(results):
        reasons["SOURCE_CHANGED_OR_REVOKED"] = (
            reasons.get("SOURCE_CHANGED_OR_REVOKED", 0) + len(results) - len(fresh)
        )
    results = fresh
    return response(
        results,
        "EVIDENCE_FOUND"
        if results and search_mode == "hybrid"
        else "NEIGHBORS_ONLY"
        if results
        else "INSUFFICIENT_EVIDENCE",
    )
