"""Non-blocking, private actor memory and conversation archive services.

This module deliberately has no dependency on the decision, review, execution,
or outbound services.  Archive ingestion is therefore safe for historical data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.domain.models import new_id, now_utc
from attention_router.core.tenancy import DEFAULT_TENANT_ID, TenantScopeError
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    ConversationMessageRow,
    ConversationParticipantRow,
    ConversationThreadRow,
    MemoryActorRow,
    MemoryCandidateRow,
    MemoryClaimRow,
    MemoryEvidenceRow,
    MemoryExtractionRunRow,
    MemoryIngestionJobRow,
)
from attention_router.platform.lineage import (
    EventLineage,
    LineageClassification,
    require_organic_write,
)

SECRET_RE = re.compile(r"(?i)\b(password|senha|otp|c[oó]digo(?: de)? (?:2fa|verifica[cç][aã]o)?|api key|token|chave privada|cvv|recovery code)\b.{0,40}")
NAME_RE = re.compile(r"(?i)\b(?:eu me chamo|meu nome [eé])\s+([\wÀ-ÿ'-]+)")
PREFERRED_NAME_RE = re.compile(r"(?i)\b(?:pode me chamar(?: de)?|prefiro que me chame de|me chama(?: de)?|me chame de)\s+(?:dr\.?\s+)?([\wÀ-ÿ'-]+)")
COMPANY_RE = re.compile(r"(?i)\b(?:trabalho na|trabalho no|sou da|estou na)\s+([\wÀ-ÿ&.'-]+(?:\s+[\wÀ-ÿ&.'-]+){0,3})")
ROLE_RE = re.compile(r"(?i)\b(?:sou|trabalho como|atuo como)\s+(?:um |uma )?([\wÀ-ÿ -]{3,60})")
SISTER_RE = re.compile(r"(?i)\bminha irmã\s+([\wÀ-ÿ'-]+)")


class ArchiveSearchProvider(Protocol):
    def search(self, session: Session, query: str, limit: int = 20) -> list[dict[str, Any]]: ...


class HistoryAdapter(Protocol):
    def list_chats(self) -> list[dict[str, Any]]: ...

    def fetch_messages(self, chat_key: str, limit: int, cursor: str | None = None) -> dict[str, Any]: ...


@dataclass(frozen=True)
class HistoryBackfillResult:
    dry_run: bool
    resume_cursor: dict[str, Any] | None
    metrics: dict[str, Any]


class HistoryBackfillService:
    """Batch, resumable archive import. It has no live-ingress or outbound dependency."""

    def __init__(self, session: Session, adapter: HistoryAdapter):
        self.session = session
        self.adapter = adapter

    def run(
        self,
        chat_keys: list[str] | None = None,
        *,
        dry_run: bool = True,
        resume_cursor: dict[str, Any] | None = None,
        page_size: int = 50,
        max_messages_per_chat: int = 100,
    ) -> HistoryBackfillResult:
        chats = self.adapter.list_chats()
        selected = [chat for chat in chats if not chat_keys or chat.get("external_thread_key") in chat_keys]
        metrics: dict[str, Any] = {
            "total_chats_discovered": len(chats),
            "direct_chats": sum(chat.get("thread_type") == "DIRECT" for chat in selected),
            "group_chats": sum(chat.get("thread_type") == "GROUP" for chat in selected),
            "total_messages_discovered": 0,
            "total_messages_archived": 0,
            "messages_skipped": 0,
            "secret_redactions": 0,
            "memory_candidates": 0,
            "memories_promoted": 0,
            "archive_only_candidates": 0,
            "blocked_memory_candidates": 0,
            "low_confidence_candidates": 0,
            "errors": 0,
        }
        start_chat = int((resume_cursor or {}).get("chat_index", 0))
        next_cursor: dict[str, Any] | None = None
        for chat_index, chat in enumerate(selected[start_chat:], start=start_chat):
            chat_key = chat["external_thread_key"]
            cursor = (resume_cursor or {}).get("message_cursor") if chat_index == start_chat else None
            fetched = 0
            while fetched < max_messages_per_chat:
                page = self.adapter.fetch_messages(chat_key, min(page_size, max_messages_per_chat - fetched), cursor)
                messages = page.get("messages", [])
                metrics["total_messages_discovered"] += len(messages)
                for payload in messages:
                    fetched += 1
                    if dry_run:
                        metrics["total_messages_archived"] += 1
                        if payload.get("sensitivity_class") == "SECRET" or _redact(payload.get("text"))[1] == "SECRET":
                            metrics["secret_redactions"] += 1
                        continue
                    item = ArchivedMessageInput(
                        source=payload.get("source", "whatsapp"),
                        source_account=payload.get("source_account", "default"),
                        thread_key=chat_key,
                        thread_type=chat.get("thread_type", "DIRECT"),
                        source_message_id=payload["source_message_id"],
                        sender_key=payload.get("external_sender_key"),
                        sender_display_name=payload.get("sender_display_name"),
                        sent_at=payload["sent_at"],
                        text=payload.get("text"),
                        message_type=payload.get("type", "UNSUPPORTED"),
                        from_me=bool(payload.get("from_me", False)),
                        direction="OUTBOUND" if payload.get("from_me") else "INBOUND",
                        title=chat.get("title"),
                        metadata=payload.get("metadata", {}),
                    )
                    row, created = archive_message(self.session, item)
                    if not created:
                        metrics["messages_skipped"] += 1
                        continue
                    metrics["total_messages_archived"] += 1
                    if row.sensitivity_class == "SECRET":
                        metrics["secret_redactions"] += 1
                    claims = ingest_message(self.session, row.id, mode="BACKFILL")
                    metrics["memories_promoted"] += len(claims)
                    candidates = self.session.scalars(select(MemoryCandidateRow).where(MemoryCandidateRow.message_id == row.id)).all()
                    metrics["memory_candidates"] += len(candidates)
                    metrics["archive_only_candidates"] += sum(candidate.eligibility == "ARCHIVE_ONLY" for candidate in candidates)
                    metrics["blocked_memory_candidates"] += sum(candidate.eligibility == "BLOCK" for candidate in candidates)
                    metrics["low_confidence_candidates"] += sum(candidate.eligibility == "LOW_CONFIDENCE" for candidate in candidates)
                next_page = page.get("next_cursor")
                if not messages or not next_page or fetched >= max_messages_per_chat:
                    break
                cursor = next_page
            if page.get("next_cursor") and fetched >= max_messages_per_chat:
                next_cursor = {"chat_index": chat_index, "message_cursor": page["next_cursor"]}
                break
            resume_cursor = None
        if not next_cursor and start_chat + len(selected[start_chat:]) < len(selected):
            next_cursor = {"chat_index": start_chat + len(selected[start_chat:]), "message_cursor": None}
        return HistoryBackfillResult(dry_run=dry_run, resume_cursor=next_cursor, metrics=metrics)


@dataclass(frozen=True)
class ArchivedMessageInput:
    source: str
    source_account: str
    thread_key: str
    thread_type: str
    source_message_id: str
    sender_key: str | None
    sender_display_name: str | None
    sent_at: datetime
    text: str | None = None
    message_type: str = "TEXT"
    from_me: bool = False
    direction: str = "INBOUND"
    title: str | None = None
    metadata: dict[str, Any] | None = None
    tenant_id: str = DEFAULT_TENANT_ID
    lineage_classification: str = LineageClassification.HISTORICAL_UNKNOWN.value
    scenario_id: str | None = None
    scenario_run_id: str | None = None
    scenario_step_run_id: str | None = None
    stimulus_id: str | None = None


def _normalize(text: str | None) -> str | None:
    return " ".join((text or "").casefold().split()) or None


def _redact(text: str | None) -> tuple[str | None, str]:
    if not text:
        return text, "NORMAL"
    if SECRET_RE.search(text):
        return "[REDACTED_SECRET]", "SECRET"
    return text, "NORMAL"


def _actor(
    session: Session,
    actor_key: str | None,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> str | None:
    if not actor_key:
        return None
    row = session.scalar(select(MemoryActorRow).where(
        MemoryActorRow.tenant_id == tenant_id,
        MemoryActorRow.actor_key == actor_key,
    ))
    if row:
        return row.id
    row = MemoryActorRow(
        id=new_id(), tenant_id=tenant_id, actor_key=actor_key, metadata_json={},
        created_at=now_utc(), updated_at=now_utc(),
    )
    session.add(row)
    session.flush()
    return row.id


def archive_message(session: Session, item: ArchivedMessageInput) -> tuple[ConversationMessageRow, bool]:
    try:
        lineage_classification = LineageClassification(item.lineage_classification)
    except ValueError as exc:
        raise ValueError("ARCHIVE_LINEAGE_CLASSIFICATION_INVALID") from exc
    scenario_values = (
        item.scenario_id,
        item.scenario_run_id,
        item.scenario_step_run_id,
        item.stimulus_id,
    )
    if lineage_classification is LineageClassification.SYNTHETIC:
        if not item.scenario_id or not item.scenario_run_id:
            raise ValueError("SYNTHETIC_SCENARIO_LINEAGE_REQUIRED")
    elif any(scenario_values):
        raise ValueError("NON_SYNTHETIC_SCENARIO_LINEAGE_FORBIDDEN")
    sent_at = item.sent_at.astimezone(timezone.utc).replace(tzinfo=None) if item.sent_at.tzinfo else item.sent_at
    existing = session.scalar(
        select(ConversationMessageRow).where(
            ConversationMessageRow.tenant_id == item.tenant_id,
            ConversationMessageRow.source == item.source,
            ConversationMessageRow.source_account == item.source_account,
            ConversationMessageRow.source_message_id == item.source_message_id,
        )
    )
    if existing:
        return existing, False
    now = now_utc()
    thread = session.scalar(select(ConversationThreadRow).where(
        ConversationThreadRow.tenant_id == item.tenant_id,
        ConversationThreadRow.source == item.source,
        ConversationThreadRow.source_account == item.source_account,
        ConversationThreadRow.external_thread_key == item.thread_key,
    ))
    if not thread:
        thread = ConversationThreadRow(id=new_id(), tenant_id=item.tenant_id, source=item.source, source_account=item.source_account,
            external_thread_key=item.thread_key, thread_type=item.thread_type, title=item.title,
            first_message_at=sent_at, last_message_at=sent_at, created_at=now, updated_at=now)
        session.add(thread)
        session.flush()
    else:
        thread.first_message_at = min(thread.first_message_at or sent_at, sent_at)
        thread.last_message_at = max(thread.last_message_at or sent_at, sent_at)
        thread.updated_at = now
    sender_id = _actor(session, item.sender_key, item.tenant_id)
    if item.sender_key:
        participant = session.scalar(select(ConversationParticipantRow).where(
            ConversationParticipantRow.conversation_id == thread.id,
            ConversationParticipantRow.external_participant_key == item.sender_key,
        ))
        if not participant:
            session.add(ConversationParticipantRow(id=new_id(), conversation_id=thread.id, actor_id=sender_id,
                external_participant_key=item.sender_key, observed_display_name=item.sender_display_name,
                participant_role="MEMBER", first_seen_at=sent_at, last_seen_at=sent_at))
        else:
            participant.last_seen_at = max(participant.last_seen_at or sent_at, sent_at)
    safe_text, sensitivity = _redact(item.text)
    metadata = dict(item.metadata or {})
    metadata["platform_lineage"] = {
        "classification": lineage_classification.value,
        "scenario_id": item.scenario_id,
        "scenario_run_id": item.scenario_run_id,
        "scenario_step_run_id": item.scenario_step_run_id,
        "stimulus_id": item.stimulus_id,
    }
    row = ConversationMessageRow(id=new_id(), tenant_id=item.tenant_id,
        conversation_id=thread.id, source=item.source,
        source_account=item.source_account, source_message_id=item.source_message_id, sender_actor_id=sender_id,
        external_sender_key=item.sender_key, direction=item.direction, from_me=item.from_me,
        sent_at=sent_at, message_type=item.message_type, text=safe_text, normalized_text=_normalize(safe_text),
        content_hash=stable_hash(safe_text or item.metadata or {}), sensitivity_class=sensitivity,
        searchable=sensitivity != "SECRET", metadata_json=metadata, imported_at=now, created_at=now)
    session.add(row)
    session.flush()
    return row, True


def evaluate_eligibility(text: str, sensitivity: str, confidence: float) -> tuple[str, str]:
    if sensitivity == "SECRET":
        return "BLOCK", "credential_or_secret_detected"
    if confidence < 0.55:
        return "LOW_CONFIDENCE", "insufficient_explicit_evidence"
    if re.search(r"(?i)\b(hoje|agora|sono|trânsito|pizza|10 minutos|chego)\b", text):
        return "ARCHIVE_ONLY", "transient_statement"
    return "PROMOTE", "stable_or_explicit_personal_fact"


def _message_lineage_classification(
    message: ConversationMessageRow,
) -> LineageClassification:
    metadata = message.metadata_json or {}
    raw = (metadata.get("platform_lineage") or {}).get(
        "classification", LineageClassification.HISTORICAL_UNKNOWN.value
    )
    try:
        return LineageClassification(raw)
    except ValueError as exc:
        raise ValueError("ARCHIVED_MESSAGE_LINEAGE_INVALID") from exc


def _message_lineage(message: ConversationMessageRow) -> EventLineage:
    metadata = (message.metadata_json or {}).get("platform_lineage") or {}
    return EventLineage(
        tenant_id=message.tenant_id,
        actor_id=message.sender_actor_id or message.external_sender_key or "UNRESOLVED_ACTOR",
        classification=_message_lineage_classification(message),
        scenario_id=metadata.get("scenario_id"),
        scenario_run_id=metadata.get("scenario_run_id"),
        stimulus_id=metadata.get("stimulus_id"),
    )


def extract_candidates(session: Session, message: ConversationMessageRow, run: MemoryExtractionRunRow) -> list[MemoryCandidateRow]:
    text = message.text or ""
    if not text or not message.searchable:
        return []
    candidates: list[tuple[str, dict[str, Any], float, str]] = []
    sender = {"actor_id": message.sender_actor_id}
    if match := NAME_RE.search(text):
        candidates.append(("identity.self_reported_name", {"text": match.group(1)}, .97, "SELF_REPORTED"))
    if match := PREFERRED_NAME_RE.search(text):
        candidates.append(("identity.preferred_name", {"text": match.group(1)}, .99, "SELF_REPORTED"))
    if match := COMPANY_RE.search(text):
        candidates.append(("professional.works_at", {"text": match.group(1).strip(" .,;:")}, .86, "SELF_REPORTED"))
    if match := SISTER_RE.search(text):
        candidates.append(("relationship.sister_of", {"unresolved_name": match.group(1), "actor": sender}, .72, "DIRECT_MESSAGE_STATEMENT"))
    if not candidates and re.search(r"(?i)\b(?:maria|ana|joão|joao)\s+(?:trabalha|é|e)\b", text):
        candidates.append(("professional.works_at", {"third_party_text": text}, .58, "THIRD_PARTY_STATEMENT"))
    rows = []
    for predicate, obj, confidence, quality in candidates:
        eligibility, reason = evaluate_eligibility(text, message.sensitivity_class, confidence)
        lineage_classification = _message_lineage_classification(message)
        if lineage_classification is not LineageClassification.ORGANIC:
            eligibility = "BLOCK"
            reason = (
                "synthetic_lineage_organic_promotion_denied"
                if lineage_classification is LineageClassification.SYNTHETIC
                else "unknown_lineage_organic_promotion_denied"
            )
        obj["source_quality"] = quality
        existing = session.scalars(select(MemoryCandidateRow).where(
            MemoryCandidateRow.message_id == message.id,
            MemoryCandidateRow.predicate == predicate,
        )).all()
        if any(item.object == obj for item in existing):
            rows.append(next(item for item in existing if item.object == obj))
            continue
        row = MemoryCandidateRow(id=new_id(), message_id=message.id, extraction_run_id=run.id,
            subject=sender, predicate=predicate, object=obj, confidence=confidence,
            sensitivity_class=message.sensitivity_class, eligibility=eligibility,
            eligibility_reason=reason, extraction_method="deterministic_v0", created_at=now_utc())
        session.add(row)
        rows.append(row)
    return rows


def _promote(session: Session, candidate: MemoryCandidateRow, message: ConversationMessageRow, run: MemoryExtractionRunRow) -> MemoryClaimRow:
    require_organic_write(_message_lineage(message), writer="memory")
    obj = candidate.object
    object_text = obj.get("text")
    current = session.scalar(select(MemoryClaimRow).where(
        MemoryClaimRow.subject_actor_id == candidate.subject.get("actor_id"),
        MemoryClaimRow.predicate == candidate.predicate,
        MemoryClaimRow.status == "ACTIVE",
    ).order_by(MemoryClaimRow.updated_at.desc()))
    existing_claims = session.scalars(select(MemoryClaimRow).where(
        MemoryClaimRow.subject_actor_id == candidate.subject.get("actor_id"),
        MemoryClaimRow.predicate == candidate.predicate,
        MemoryClaimRow.object_text == object_text,
    )).all()
    for existing_claim in existing_claims:
        if session.scalar(select(MemoryEvidenceRow).where(
            MemoryEvidenceRow.claim_id == existing_claim.id,
            MemoryEvidenceRow.conversation_message_id == message.id,
        )):
            return existing_claim
    claim = MemoryClaimRow(id=new_id(), subject_actor_id=candidate.subject.get("actor_id"), predicate=candidate.predicate,
        object_type="TEXT", object_text=object_text, object_json=obj, context={}, confidence=candidate.confidence,
        sensitivity_class=candidate.sensitivity_class, source_quality=candidate.object.get("source_quality", "") or "SELF_REPORTED",
        valid_from=message.sent_at, status="ACTIVE", staleness_class="STABLE", first_observed_at=message.sent_at,
        last_observed_at=message.sent_at, created_at=now_utc(), updated_at=now_utc())
    if current and current.object_text != object_text:
        current.status = "SUPERSEDED"
        current.valid_until = message.sent_at
        claim.supersedes_claim_id = current.id
    elif current and current.object_text == object_text:
        existing_evidence = session.scalar(select(MemoryEvidenceRow).where(
            MemoryEvidenceRow.claim_id == current.id,
            MemoryEvidenceRow.conversation_message_id == message.id,
        ))
        if existing_evidence is None:
            session.add(MemoryEvidenceRow(id=new_id(), claim_id=current.id, conversation_message_id=message.id,
                extraction_run_id=run.id, evidence_type="MESSAGE_STATEMENT", confidence=candidate.confidence, created_at=now_utc()))
        return current
    session.add(claim)
    session.flush()
    session.add(MemoryEvidenceRow(id=new_id(), claim_id=claim.id, conversation_message_id=message.id,
        extraction_run_id=run.id, evidence_type="MESSAGE_STATEMENT", confidence=candidate.confidence, created_at=now_utc()))
    return claim


def ingest_message(session: Session, message_id: str, mode: str = "INCREMENTAL") -> list[MemoryClaimRow]:
    job = session.scalar(select(MemoryIngestionJobRow).where(MemoryIngestionJobRow.source_message_id == message_id))
    if job and job.status == "DONE":
        return []
    message = session.get(ConversationMessageRow, message_id)
    if not message:
        raise KeyError("archive message not found")
    thread = session.get(ConversationThreadRow, message.conversation_id)
    if thread is None:
        raise KeyError("archive thread not found")
    if not job:
        job = MemoryIngestionJobRow(id=new_id(), source_message_id=message_id, mode=mode, status="PROCESSING", attempt_count=0, created_at=now_utc())
        session.add(job)
    job.status = "PROCESSING"
    job.attempt_count += 1
    job.started_at = now_utc()
    run = MemoryExtractionRunRow(
        id=new_id(), tenant_id=thread.tenant_id, mode=mode, provider="deterministic_v0",
        status="PROCESSING", message_count=1, created_at=now_utc(),
    )
    session.add(run)
    session.flush()
    try:
        candidates = extract_candidates(session, message, run)
        claims = [_promote(session, c, message, run) for c in candidates if c.eligibility == "PROMOTE"]
        run.status = "DONE"
        run.completed_at = now_utc()
        job.status = "DONE"
        job.completed_at = now_utc()
        return claims
    except Exception as exc:
        run.status = "ERROR"
        run.error = str(exc)[:500]
        job.status = "ERROR"
        job.last_error = str(exc)[:500]
        raise


def search_archive(
    session: Session,
    query: str,
    limit: int = 20,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> list[dict[str, Any]]:
    terms = [term for term in query.casefold().split() if term]
    rows = session.scalars(
        select(ConversationMessageRow)
        .join(ConversationThreadRow, ConversationThreadRow.id == ConversationMessageRow.conversation_id)
        .where(
            ConversationThreadRow.tenant_id == tenant_id,
            ConversationMessageRow.searchable.is_(True),
        )
        .order_by(ConversationMessageRow.sent_at.desc())
        .limit(500)
    ).all()
    result = []
    for row in rows:
        haystack = (row.normalized_text or "")
        score = sum(1 for term in terms if term in haystack) / max(len(terms), 1)
        if score == 0 and terms:
            score = max((SequenceMatcher(None, term, haystack).ratio() for term in terms), default=0) * .35
        if score >= .25:
            result.append({"source_type": "ARCHIVE", "message_id": row.id, "conversation_id": row.conversation_id, "actor_id": row.sender_actor_id, "text": row.text, "observed_at": row.sent_at, "score": round(score, 3)})
    return sorted(result, key=lambda item: item["score"], reverse=True)[:limit]


def search_memory(
    session: Session,
    query: str,
    limit: int = 20,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> list[dict[str, Any]]:
    needle = query.casefold()
    rows = session.scalars(
        select(MemoryClaimRow)
        .join(MemoryActorRow, MemoryActorRow.id == MemoryClaimRow.subject_actor_id)
        .where(MemoryActorRow.tenant_id == tenant_id)
        .order_by(MemoryClaimRow.updated_at.desc())
    ).all()
    return [{"source_type": "MEMORY", "claim_id": r.id, "actor_id": r.subject_actor_id, "predicate": r.predicate, "value": r.object_text, "status": r.status, "confidence": r.confidence} for r in rows if needle in f"{r.predicate} {r.object_text or ''}".casefold()][:limit]


def get_memory_evidence(
    session: Session,
    claim_id: str,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> list[dict[str, Any]]:
    claim = session.scalar(
        select(MemoryClaimRow)
        .join(MemoryActorRow, MemoryActorRow.id == MemoryClaimRow.subject_actor_id)
        .where(MemoryClaimRow.id == claim_id, MemoryActorRow.tenant_id == tenant_id)
    )
    if claim is None:
        raise TenantScopeError("MEMORY_CLAIM_NOT_IN_TENANT")
    rows = session.scalars(select(MemoryEvidenceRow).where(MemoryEvidenceRow.claim_id == claim_id)).all()
    return [{"claim_id": r.claim_id, "message_id": r.conversation_message_id, "extraction_run_id": r.extraction_run_id, "evidence_type": r.evidence_type, "confidence": r.confidence, "created_at": r.created_at} for r in rows]


def memory_context(
    session: Session,
    actor_id: str,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> dict[str, Any]:
    actor = session.get(MemoryActorRow, actor_id)
    if actor is not None and actor.tenant_id != tenant_id:
        raise TenantScopeError("TENANT_SCOPE_MISMATCH:memory_actor")
    rows = session.scalars(select(MemoryClaimRow).where(MemoryClaimRow.subject_actor_id == actor_id, MemoryClaimRow.status == "ACTIVE")).all()
    preferred = next((r.object_text for r in rows if r.predicate == "identity.preferred_name"), None)
    self_reported = next((r.object_text for r in rows if r.predicate == "identity.self_reported_name"), None)
    return {"preferred_name": preferred, "self_reported_name": self_reported, "known_company": next((r.object_text for r in rows if r.predicate == "professional.works_at"), None), "relevant_current_facts": [{"predicate": r.predicate, "value": r.object_text} for r in rows[:20] if r.sensitivity_class != "SECRET"]}


def archive_incremental_message(session: Session, *, source: str, source_account: str, thread_key: str,
                                thread_type: str, source_message_id: str, actor_key: str,
                                display_name: str | None, text: str, sent_at: datetime,
                                from_me: bool = False, metadata: dict[str, Any] | None = None,
                                tenant_id: str = DEFAULT_TENANT_ID,
                                lineage_classification: str = LineageClassification.HISTORICAL_UNKNOWN.value,
                                scenario_id: str | None = None,
                                scenario_run_id: str | None = None,
                                scenario_step_run_id: str | None = None,
                                stimulus_id: str | None = None) -> tuple[ConversationMessageRow, bool]:
    return archive_message(session, ArchivedMessageInput(
        source=source, source_account=source_account, thread_key=thread_key, thread_type=thread_type,
        source_message_id=source_message_id, sender_key=actor_key, sender_display_name=display_name,
        sent_at=sent_at, text=text, from_me=from_me, direction="OUTBOUND" if from_me else "INBOUND",
        metadata=metadata or {}, tenant_id=tenant_id,
        lineage_classification=lineage_classification,
        scenario_id=scenario_id,
        scenario_run_id=scenario_run_id,
        scenario_step_run_id=scenario_step_run_id,
        stimulus_id=stimulus_id,
    ))


def enqueue_memory_ingestion(session: Session, message_id: str, mode: str = "INCREMENTAL") -> MemoryIngestionJobRow:
    job = session.scalar(select(MemoryIngestionJobRow).where(MemoryIngestionJobRow.source_message_id == message_id))
    if job:
        return job
    job = MemoryIngestionJobRow(id=new_id(), source_message_id=message_id, mode=mode, status="PENDING",
                                attempt_count=0, created_at=now_utc())
    session.add(job)
    session.flush()
    return job


def process_memory_ingestion_jobs(session: Session, limit: int = 10) -> int:
    jobs = session.scalars(select(MemoryIngestionJobRow).where(
        MemoryIngestionJobRow.status.in_(["PENDING", "ERROR"])
    ).order_by(MemoryIngestionJobRow.created_at).limit(limit)).all()
    processed = 0
    for job in jobs:
        try:
            ingest_message(session, job.source_message_id, mode=job.mode)
            processed += 1
        except Exception as exc:
            job.status = "ERROR"
            job.last_error = str(exc)[:500]
            session.flush()
    return processed
