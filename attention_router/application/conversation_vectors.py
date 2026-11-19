"""Passive vector indexing/search. Does not call cognition, decision, ingress or transport."""

import json
import math
import time
from sqlalchemy import text
from attention_router.application.conversation_segments import digest, segment_messages, POLICY
from attention_router.application.conversation_embedding import DIMENSIONS, get_provider
from attention_router.application.memory import _redact

KIND = "conversation_vector_v1"


def sql(session, statement, **params):
    return session.execute(text(statement), params)


def scope(session, tenant, account):
    if not tenant or not account:
        raise ValueError("VECTOR_SCOPE_REQUIRED")
    sql(
        session,
        "SELECT set_config('andy.vector_tenant',:t,true),set_config('andy.vector_account',:a,true)",
        t=tenant,
        a=account,
    )


def authorized(session, tenant):
    return bool(
        sql(
            session,
            "SELECT count(DISTINCT human_identity_id)=1 FROM client_tenant_memberships WHERE tenant_id=:t AND role='OWNER' AND status='ACTIVE'",
            t=tenant,
        ).scalar()
    )


def stat(session, name, amount=1):
    sql(
        session,
        """INSERT INTO conversation_vector_stats(name,value) VALUES(:n,:v)
      ON CONFLICT(name) DO UPDATE SET value=conversation_vector_stats.value+EXCLUDED.value,updated_at=now()""",
        n=name,
        v=amount,
    )


def sources(session, tenant, account, conversation):
    scope(session, tenant, account)
    if not authorized(session, tenant):
        return []
    rows = (
        sql(
            session,
            """SELECT m.*,v.id AS transcription_id,v.status AS stt_status,v.transcript_text
      FROM conversation_messages m JOIN conversation_threads t ON t.id=m.conversation_id AND t.tenant_id=m.tenant_id AND t.source_account=m.source_account
      LEFT JOIN voice_transcriptions v ON v.inbound_event_id=m.metadata->>'source_inbound_event_id' AND v.tenant_id=m.tenant_id
      WHERE m.tenant_id=:t AND m.source_account=:a AND m.conversation_id=:c AND m.source='wwebjs'
      AND m.searchable AND m.sensitivity_class<>'SECRET' AND m.message_type IN ('TEXT','VOICE')
      ORDER BY m.sent_at,m.source_message_id,m.id""",
            t=tenant,
            a=account,
            c=conversation,
        )
        .mappings()
        .all()
    )
    result = []
    for original in rows:
        r = dict(original)
        meta = r["metadata"] or {}
        if (
            meta.get("deleted") is True
            or meta.get("revoked") is True
            or meta.get("content_state") in ("DELETED", "REVOKED")
        ):
            continue
        if r["message_type"] == "VOICE":
            if r["stt_status"] != "READY":
                continue
            content = r["transcript_text"] or ""
        else:
            content = r["text"] or ""
        content, sensitivity = _redact(content.strip())
        if not content or sensitivity == "SECRET":
            continue
        r["index_text"] = content
        r["source_fingerprint"] = digest(
            json.dumps(
                [
                    content,
                    r["sent_at"].isoformat(),
                    r["reply_to_message_id"],
                    r["from_me"],
                    r["direction"],
                    r["external_sender_key"],
                    r["sender_actor_id"],
                ],
                ensure_ascii=False,
            )
        )
        result.append(r)
    return result


def vector_literal(value):
    if len(value) != DIMENSIONS or not all(math.isfinite(v) for v in value):
        raise ValueError("VECTOR_INVALID")
    return "[" + ",".join(format(float(v), ".9g") for v in value) + "]"


def cycle(session_factory, provider=None, batch_size=4):
    started = time.monotonic()
    with session_factory() as s:
        sql(s, "SET LOCAL lock_timeout='100ms'")
        job = (
            sql(
                s,
                """SELECT * FROM queue WHERE kind=:k AND
          (status='PENDING' OR (status='PROCESSING' AND coalesce((payload->>'lease_until')::double precision,0)<:now))
          AND coalesce((payload->>'next_at')::double precision,0)<=:now ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED""",
                k=KIND,
                now=time.time(),
            )
            .mappings()
            .first()
        )
        if not job:
            return 0
        payload = dict(job["payload"])
        generation = payload["generation"]
        payload.update(lease_until=time.time() + 180, attempts=payload.get("attempts", 0) + 1)
        sql(
            s,
            "UPDATE queue SET status='PROCESSING',payload=CAST(:p AS jsonb) WHERE id=:id",
            p=json.dumps(payload),
            id=job["id"],
        )
        s.commit()
    try:
        provider = provider or get_provider()
        if provider.dimensions != DIMENSIONS:
            raise ValueError("EMBEDDING_DIMENSION_UNSUPPORTED")
        tenant, account, conversation = (
            payload[k] for k in ("tenant_id", "source_account", "conversation_id")
        )
        with session_factory() as s:
            sql(s, "SET LOCAL lock_timeout='100ms'")
            rows = sources(s, tenant, account, conversation)
            plans = segment_messages(rows, provider)
            # Generation lock follows source reads; a concurrently committed mutation wins.
            current = sql(
                s, "SELECT payload FROM queue WHERE id=:id FOR UPDATE", id=job["id"]
            ).scalar()
            if current["generation"] != generation:
                s.rollback()
                return 0
            ids = [p["id"] for p in plans]
            sql(
                s,
                "DELETE FROM conversation_vector_segments WHERE tenant_id=:t AND source_account=:a AND conversation_id=:c AND NOT(id=ANY(:ids))",
                t=tenant,
                a=account,
                c=conversation,
                ids=ids,
            )
            for p in plans:
                old = (
                    sql(
                        s,
                        "SELECT content_hash,state FROM conversation_vector_segments WHERE id=:id",
                        id=p["id"],
                    )
                    .mappings()
                    .first()
                )
                exists = sql(
                    s,
                    "SELECT 1 FROM conversation_embeddings WHERE segment_id=:id AND model=:m AND model_revision=:r AND content_hash=:h",
                    id=p["id"],
                    m=provider.model,
                    r=provider.revision,
                    h=p["content_hash"],
                ).scalar()
                if old and old["content_hash"] != p["content_hash"]:
                    stat(s, "reindexations")
                    sql(s, "DELETE FROM conversation_embeddings WHERE segment_id=:id", id=p["id"])
                    exists = None
                if not old:
                    stat(s, "segments_created")
                sql(
                    s,
                    """INSERT INTO conversation_vector_segments
                  (id,tenant_id,source_account,conversation_id,policy_version,content_text,content_hash,source_hash,state,generation,start_at,end_at)
                  VALUES(:id,:t,:a,:c,:policy,:txt,:h,:sh,:state,:g,:start,:end)
                  ON CONFLICT(id) DO UPDATE SET content_text=EXCLUDED.content_text,content_hash=EXCLUDED.content_hash,
                  source_hash=EXCLUDED.source_hash,state=EXCLUDED.state,generation=EXCLUDED.generation,start_at=EXCLUDED.start_at,end_at=EXCLUDED.end_at,updated_at=now(),error_code=NULL""",
                    id=p["id"],
                    t=tenant,
                    a=account,
                    c=conversation,
                    policy=POLICY,
                    txt=p["text"],
                    h=p["content_hash"],
                    sh=p["source_hash"],
                    state="READY" if exists else "PENDING",
                    g=generation,
                    start=p["start_at"],
                    end=p["end_at"],
                )
                sql(
                    s,
                    "DELETE FROM conversation_vector_memberships WHERE segment_id=:id",
                    id=p["id"],
                )
                for ordinal, f in enumerate(p["members"]):
                    sql(
                        s,
                        """INSERT INTO conversation_vector_memberships VALUES(:id,:t,:a,:c,:message,:ordinal,:start,:end,:role)""",
                        id=p["id"],
                        t=tenant,
                        a=account,
                        c=conversation,
                        message=f["message"]["id"],
                        ordinal=ordinal,
                        start=f["start"],
                        end=f["end"],
                        role=f["role"],
                    )
            pending = (
                sql(
                    s,
                    "SELECT id,content_text,content_hash FROM conversation_vector_segments WHERE tenant_id=:t AND source_account=:a AND conversation_id=:c AND state='PENDING' ORDER BY start_at,id LIMIT :n",
                    t=tenant,
                    a=account,
                    c=conversation,
                    n=max(1, min(8, batch_size)),
                )
                .mappings()
                .all()
            )
            s.commit()
        # Inference holds no transaction/row lock and uses a single CPU inference thread.
        vectors = provider.encode([p["content_text"] for p in pending]) if pending else []
        with session_factory() as s:
            sql(s, "SET LOCAL lock_timeout='100ms'")
            scope(s, tenant, account)
            current = sql(
                s, "SELECT payload FROM queue WHERE id=:id FOR UPDATE", id=job["id"]
            ).scalar()
            if current["generation"] != generation:
                sql(s, "UPDATE queue SET status='PENDING' WHERE id=:id", id=job["id"])
                s.commit()
                return 0
            for p, vec in zip(pending, vectors, strict=True):
                sql(
                    s,
                    """INSERT INTO conversation_embeddings(segment_id,tenant_id,source_account,model,model_revision,dimensions,content_hash,embedding)
                  VALUES(:id,:t,:a,:m,:r,384,:h,CAST(:v AS vector)) ON CONFLICT(segment_id,model,model_revision) DO UPDATE
                  SET embedding=EXCLUDED.embedding,content_hash=EXCLUDED.content_hash,created_at=now()""",
                    id=p["id"],
                    t=tenant,
                    a=account,
                    m=provider.model,
                    r=provider.revision,
                    h=p["content_hash"],
                    v=vector_literal(vec),
                )
                sql(
                    s,
                    "UPDATE conversation_vector_segments SET state='READY',attempts=attempts+1,updated_at=now() WHERE id=:id",
                    id=p["id"],
                )
                stat(s, "embeddings_generated")
            left = sql(
                s,
                "SELECT count(*) FROM conversation_vector_segments WHERE tenant_id=:t AND source_account=:a AND conversation_id=:c AND state='PENDING'",
                t=tenant,
                a=account,
                c=conversation,
            ).scalar()
            payload.update(
                lease_until=0, attempts=0, next_at=time.time() + 5 if left else 0, error=None
            )
            sql(
                s,
                "UPDATE queue SET status=:state,payload=CAST(:p AS jsonb),processed_at=CASE WHEN :done THEN now() ELSE NULL END WHERE id=:id",
                state="PENDING" if left else "DONE",
                p=json.dumps(payload),
                done=not left,
                id=job["id"],
            )
            stat(s, "index_cycles")
            stat(s, "index_seconds", time.monotonic() - started)
            s.commit()
        return len(pending)
    except Exception as exc:
        with session_factory() as s:
            sql(s, "SET LOCAL lock_timeout='100ms'")
            current = sql(
                s, "SELECT payload FROM queue WHERE id=:id FOR UPDATE", id=job["id"]
            ).scalar()
            if current["generation"] == generation:
                # Exception messages may contain content, URLs or credentials: retain only type.
                payload.update(
                    error=type(exc).__name__,
                    lease_until=0,
                    next_at=time.time() + min(1800, 30 * 2 ** min(payload["attempts"], 6)),
                )
                sql(
                    s,
                    "UPDATE queue SET status=:state,payload=CAST(:p AS jsonb) WHERE id=:id",
                    state="FAILED" if payload["attempts"] >= 5 else "PENDING",
                    p=json.dumps(payload),
                    id=job["id"],
                )
                if payload["attempts"] >= 5:
                    scope(s, payload["tenant_id"], payload["source_account"])
                    sql(
                        s,
                        "UPDATE conversation_vector_segments SET state='FAILED',error_code=:error WHERE conversation_id=:c AND state='PENDING'",
                        error=type(exc).__name__,
                        c=payload["conversation_id"],
                    )
                stat(s, "failures")
                s.commit()
        raise


def search(
    session,
    *,
    tenant_id,
    source_account,
    query,
    conversation_id=None,
    participant=None,
    direction=None,
    message_type=None,
    start_at=None,
    end_at=None,
    limit=10,
    provider=None,
    _candidate_ids=None,
):
    started = time.monotonic()
    scope(session, tenant_id, source_account)
    if not authorized(session, tenant_id):
        raise PermissionError("VECTOR_SCOPE_REVOKED")
    if direction not in (None, "INBOUND", "OUTBOUND") or not query.strip() or len(query) > 4000:
        raise ValueError("VECTOR_QUERY_INVALID")
    if message_type not in (None, "TEXT", "VOICE") or any(
        x is not None and x.tzinfo is None for x in (start_at, end_at)
    ):
        raise ValueError("VECTOR_FILTER_INVALID")
    if start_at and end_at and start_at > end_at:
        raise ValueError("VECTOR_TIME_RANGE_INVALID")
    provider = provider or get_provider()
    vector = provider.encode([query], query=True)[0]
    params = dict(
        t=tenant_id,
        a=source_account,
        c=conversation_id,
        candidate_ids=_candidate_ids,
        p=participant,
        d=direction,
        kind=message_type,
        start=start_at,
        end=end_at,
        n=max(1, min(limit, 50)),
        v=vector_literal(vector),
        model=provider.model,
        revision=provider.revision,
    )
    candidates = (
        sql(
            session,
            """SELECT s.*,1-(e.embedding <=> CAST(:v AS vector)) AS similarity FROM conversation_vector_segments s
      JOIN conversation_embeddings e ON e.segment_id=s.id AND e.tenant_id=s.tenant_id AND e.source_account=s.source_account AND e.content_hash=s.content_hash
      WHERE s.tenant_id=:t AND s.source_account=:a AND s.state='READY' AND e.model=:model AND e.model_revision=:revision
      AND (CAST(:candidate_ids AS varchar[]) IS NULL OR s.id=ANY(:candidate_ids))
      AND (CAST(:c AS varchar) IS NULL OR s.conversation_id=:c)
      AND EXISTS(SELECT 1 FROM conversation_vector_memberships sm JOIN conversation_messages m ON m.id=sm.message_id
        WHERE sm.segment_id=s.id AND m.tenant_id=:t AND m.source_account=:a AND m.searchable AND m.sensitivity_class<>'SECRET'
        AND (CAST(:p AS varchar) IS NULL OR m.external_sender_key=:p OR m.sender_actor_id=:p)
        AND (CAST(:d AS varchar) IS NULL OR m.direction=:d)
        AND (CAST(:kind AS varchar) IS NULL OR m.message_type=:kind)
        AND (CAST(:start AS timestamptz) IS NULL OR m.sent_at>=:start) AND (CAST(:end AS timestamptz) IS NULL OR m.sent_at<=:end))
      ORDER BY e.embedding <=> CAST(:v AS vector),s.id LIMIT :n""",
            **params,
        )
        .mappings()
        .all()
    )
    results = []
    cache = {}
    for c in candidates:
        cid = c["conversation_id"]
        if cid not in cache:
            live = sources(session, tenant_id, source_account, cid)
            cache[cid] = (
                {r["id"]: r for r in live},
                {p["id"]: p for p in segment_messages(live, provider)},
            )
        live, plans = cache[cid]
        plan = plans.get(c["id"])
        if (
            not plan
            or plan["source_hash"] != c["source_hash"]
            or plan["content_hash"] != c["content_hash"]
        ):
            continue
        members = []
        for f in plan["members"]:
            m = live[f["message"]["id"]]
            members.append(
                {
                    k: m.get(k)
                    for k in (
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
                }
                | {
                    "text": m["index_text"],
                    "char_start": f["start"],
                    "char_end": f["end"],
                    "role": f["role"],
                }
            )
        results.append(
            {
                "segment_id": c["id"],
                "conversation_id": cid,
                "text": c["content_text"],
                "start_at": c["start_at"],
                "end_at": c["end_at"],
                "similarity": float(c["similarity"]),
                "score_semantics": "COSINE_SIMILARITY_NOT_TRUTH_PROBABILITY",
                "model": provider.model,
                "model_revision": provider.revision,
                "content_hash": c["content_hash"],
                "source_hash": c["source_hash"],
                "messages": members,
                "trust": "UNTRUSTED_RETRIEVED_CONTENT_NO_EXECUTION_AUTHORITY",
            }
        )
    # Read-only endpoint: query timing is returned, not persisted with query text.
    return {
        "strategy": "PASSIVE_VECTOR_V1",
        "results": results,
        "latency_ms": round((time.monotonic() - started) * 1000, 2),
    }
