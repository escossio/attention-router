"""Bounded operator-driven observation of exact WhatsApp artifact evidence.

Run only against an isolated database. The caller supplies an explicitly
authorized tenant/account/binding and decides whether to commit the unit of
work. This module has no worker registration, provider calls, or delivery path.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from attention_router.application.cognitive_graph import build_cognitive_graph_slice
from attention_router.application.context_canary_gate import canary_scope
from attention_router.application.conversation_live_capture import _fields, _utc
from attention_router.application.graph_intelligence import (
    DeterministicRelationCandidateEngine,
    persist_relation_candidate_insight,
)
from attention_router.application.personal_context_bootstrap import resolve_represented_owner_actor_key
from attention_router.application.personal_context_patterns import detect_temporal_recurrence_hypotheses
from attention_router.application.platform.artifacts import context_receipt_refusal
from attention_router.application.platform.events import record_timeline_event
from attention_router.application.semantic_episodes import assign_timeline_event_to_episode
from attention_router.config import settings
from attention_router.infrastructure.artifact_models import ArtifactReceiptRow, ArtifactRow
from attention_router.infrastructure.candidate_insight_models import CandidateInsightEvidenceRow, CandidateInsightRow
from attention_router.infrastructure.semantic_episode_models import SemanticEpisodeMembershipRow, SemanticEpisodeRow
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    ActorBindingRow,
    CanonicalEventRow,
    ConversationMessageRow,
    ConversationThreadRow,
    InboundEventRow,
    ResourceRow,
    TenantRow,
    TimelineEventRow,
)
from attention_router.infrastructure.repository import audit, resolve_actor_binding

_EVENT_TYPE = "ATTACHMENT_OBSERVED"
_PROVENANCE = "exact_conversation_artifact_receipt"


@dataclass(frozen=True)
class ObservationResult:
    received: int
    linked: int
    new_links: int
    resources: int
    identified_people: int
    new_episodes: int
    new_memberships: int
    revised_episodes: int
    candidates: int
    new_candidates: int
    pairs_with_single_observation: int
    recurrences: int
    graph_nodes: int
    graph_edges: int
    rejected: dict[str, int]


def _owner_authority_revision(session, tenant_id, human_id, binding, *, lock=False):
    from attention_router.infrastructure.client_bootstrap_models import ClientTenantMembershipRow

    query = select(ClientTenantMembershipRow.id, ClientTenantMembershipRow.context_revision).where(
        ClientTenantMembershipRow.tenant_id == tenant_id,
        ClientTenantMembershipRow.human_identity_id == human_id,
        ClientTenantMembershipRow.role == "OWNER",
        ClientTenantMembershipRow.status == "ACTIVE",
    )
    if lock:
        query = query.with_for_update(read=True)
    membership = session.execute(query).one_or_none()
    if membership is None:
        return None
    return [binding.context_revision, membership.id, membership.context_revision]


def _lock_owner(
    session: Session, tenant_id: str, binding_id: str, owner_human_identity_id: str
) -> tuple[ActorBindingRow, list[str]]:
    if not canary_scope(
        tenant_id=tenant_id, binding_id=binding_id, owner_id=owner_human_identity_id,
    ):
        raise ValueError("OBSERVATION_CONSENT_AUTHORITY_UNAVAILABLE")
    if (
        not tenant_id
        or not binding_id
        or tenant_id != settings.cognitive_runtime_canary_tenant_id
        or binding_id != settings.persistent_memory_canary_binding_id
    ):
        raise ValueError("OBSERVATION_CANARY_SCOPE_MISMATCH")
    owners = session.scalars(
        select(ActorBindingRow)
        .join(TenantRow, TenantRow.id == ActorBindingRow.tenant_id)
        .where(
            TenantRow.status == "ACTIVE",
            ActorBindingRow.tenant_id == tenant_id,
            ActorBindingRow.is_active.is_(True),
            or_(
                ActorBindingRow.actor_category == "owner",
                ActorBindingRow.binding_metadata["owner"].as_boolean().is_(True),
            ),
        )
        .order_by(ActorBindingRow.id)
        .with_for_update(of=ActorBindingRow)
        .execution_options(populate_existing=True)
    ).all()
    if len(owners) != 1 or owners[0].id != binding_id:
        raise ValueError("OBSERVATION_CANARY_OWNER_UNRESOLVED")
    actor_key = resolve_represented_owner_actor_key(
        session, tenant_id=tenant_id, owner_human_identity_id=owner_human_identity_id,
    )
    if actor_key != owners[0].actor_key:
        raise ValueError("OBSERVATION_CANARY_OWNER_UNRESOLVED")
    revision = _owner_authority_revision(session, tenant_id, owner_human_identity_id, owners[0], lock=True)
    if revision is None:
        raise ValueError("OBSERVATION_CANARY_OWNER_UNRESOLVED")
    return owners[0], revision


def _locked_observation_sources(session, tenant_id, events):
    return lock_conversation_sources(
        session, tenant_id, (event.event_ref.get("conversation_message_id") for event in events),
    )


def lock_conversation_sources(session, tenant_id, message_ids):
    """Load bounded tenant-local source rows for this transaction's projection."""
    messages = _lock_source_rows(session, tenant_id, ConversationMessageRow, message_ids)
    return lock_conversation_dependencies(session, tenant_id, messages)


def _lock_source_rows(session, tenant_id, model, ids):
    keys = {key for key in ids if isinstance(key, str) and key}
    if not keys:
        return ()
    return session.scalars(
        select(model).where(model.tenant_id == tenant_id, model.id.in_(keys))
        .order_by(model.id).with_for_update(read=True)
        .execution_options(populate_existing=True)
    ).all()


def lock_conversation_dependencies(session, tenant_id, messages):
    """Use only message rows already locked in this transaction by the caller."""
    if any(message.tenant_id != tenant_id for message in messages):
        raise ValueError("CONVERSATION_SOURCE_LOCK_SCOPE")
    sources = {}
    if messages:
        sources[ConversationMessageRow] = {message.id: message for message in messages}

    def load(model, ids):
        keys = {key for key in ids if isinstance(key, str) and key}
        if not keys:
            return ()
        rows = _lock_source_rows(session, tenant_id, model, keys)
        sources[model] = {row.id: row for row in rows}
        return rows

    from_refs = [message.metadata_json or {} for message in messages]
    load(InboundEventRow, (refs.get("source_inbound_event_id") for refs in from_refs))
    load(ConversationThreadRow, (message.conversation_id for message in messages))
    receipts = load(ArtifactReceiptRow, (refs.get("artifact_receipt_id") for refs in from_refs))
    artifacts = load(ArtifactRow, (receipt.artifact_id for receipt in receipts))
    load(ResourceRow, (artifact.resource_id for artifact in artifacts))
    return sources


def _observe_message(
    session: Session,
    message: ConversationMessageRow,
    *,
    binding_id: str,
    start: datetime,
    stamp: datetime,
    require_existing: bool = False,
    lock_sources: bool = False,
    _source_rows=None,
) -> tuple[TimelineEventRow | None, str]:
    read_options = {"populate_existing": True}
    if lock_sources:
        read_options["with_for_update"] = {"read": True}
    def source(model, key):
        if _source_rows is not None:
            return _source_rows.get(model, {}).get(key) if isinstance(key, str) else None
        return session.get(model, key, **read_options)

    tenant_id = message.tenant_id
    meta = message.metadata_json or {}
    if (
        not message.searchable
        or meta.get("context_evidence_withdrawn") is True
        or message.sensitivity_class != "NORMAL"
        or message.from_me
        or message.direction != "INBOUND"
        or meta.get("capture_contract") != "LIVE_CONVERSATION_ARCHIVE_V0"
        or (meta.get("platform_lineage") or {}).get("classification") != "ORGANIC"
        or meta.get("attachment_state") != "READY"
    ):
        return None, "ARCHIVE_INELIGIBLE"
    inbound = source(InboundEventRow, meta.get("source_inbound_event_id") or "")
    if (
        inbound is None
        or inbound.tenant_id != tenant_id
        or inbound.source != message.source
        or inbound.external_event_id != message.source_message_id
        or inbound.scenario_run_id is not None
        or inbound.scenario_step_run_id is not None
        or not start <= _utc(inbound.received_at) <= stamp
    ):
        return None, "RECEIPT_UNPROVEN"
    details = _fields(inbound)
    thread = source(ConversationThreadRow, message.conversation_id)
    if (
        details is None
        or details["origin"] != "EXTERNAL_INBOUND"
        or not details["has_media"]
        or details["timestamp_basis"] != "PROVIDER_OCCURRED_AT"
        or details["source_account"] != message.source_account
        or details["message_kind"] != message.message_type
        or details["sent_at"] != _utc(message.sent_at)
        or not start <= details["sent_at"] <= stamp
        or thread is None
        or thread.tenant_id != tenant_id
        or thread.source != inbound.source
        or thread.source_account != message.source_account
        or thread.external_thread_key != details["conversation_key"]
        or thread.thread_type != details["thread_type"]
    ):
        return None, "SOURCE_CONTEXT_UNPROVEN"

    canonical_query = select(CanonicalEventRow).where(
        CanonicalEventRow.tenant_id == tenant_id,
        CanonicalEventRow.inbound_event_id == inbound.id,
        CanonicalEventRow.lineage_classification == "ORGANIC",
        CanonicalEventRow.origin == "EXTERNAL_INBOUND",
        CanonicalEventRow.scenario_run_id.is_(None),
        CanonicalEventRow.scenario_step_run_id.is_(None),
    ).execution_options(populate_existing=True)
    if lock_sources:
        canonical_query = canonical_query.with_for_update(read=True)
    canonical = session.scalar(canonical_query)
    if canonical is None or _utc(canonical.occurred_at) != details["sent_at"]:
        return None, "CANONICAL_EVENT_UNPROVEN"

    receipt = source(ArtifactReceiptRow, meta.get("artifact_receipt_id") or "")
    if (
        receipt is None
        or receipt.tenant_id != tenant_id
        or receipt.source_channel != "channel.whatsapp"
        or receipt.source_account != message.source_account
        or receipt.external_receipt_id != f"whatsapp:{inbound.external_event_id}:media"
        or receipt.artifact_id != meta.get("artifact_id")
    ):
        return None, "ARTIFACT_RECEIPT_UNPROVEN"
    refusal = context_receipt_refusal(receipt.metadata_json, now=stamp)
    if refusal:
        return None, refusal
    artifact = source(ArtifactRow, receipt.artifact_id)
    resource = source(ResourceRow, artifact.resource_id) if artifact else None
    if (
        artifact is None
        or artifact.tenant_id != tenant_id
        or artifact.status != "AVAILABLE"
        or resource is None
        or resource.tenant_id != tenant_id
        or resource.resource_type != "ARTIFACT"
        or resource.status != "ACTIVE"
        or (resource.metadata_json or {}).get("artifact_id") != artifact.id
    ):
        return None, "ARTIFACT_UNAVAILABLE"

    sender = details["sender_key"]
    actor = resolve_actor_binding(session, inbound.source, sender, tenant_id) if sender else None
    if actor is not None and lock_sources:
        actor = session.get(ActorBindingRow, actor.id, **read_options)
        if actor is not None and not actor.is_active:
            actor = None
    if actor is not None and (actor.id == binding_id or actor.actor_category == "owner"):
        # An inbound speaker claim cannot establish owner authorship.
        actor = None
    actor_id = actor.actor_key if actor else None
    event_ref = {
        "owner_binding_id": binding_id,
        "conversation_message_id": message.id,
        "conversation_message_revision": message.context_revision,
        "inbound_event_id": inbound.id,
        "artifact_receipt_id": receipt.id,
        "artifact_receipt_revision": receipt.context_revision,
        "artifact_context_revision": artifact.context_revision,
        "sender_binding_revision": actor.context_revision if actor else None,
    }
    existing_query = select(TimelineEventRow).where(
        TimelineEventRow.tenant_id == tenant_id,
        TimelineEventRow.canonical_event_id == canonical.id,
        TimelineEventRow.event_type == _EVENT_TYPE,
    ).execution_options(populate_existing=True)
    if _source_rows is not None:
        existing_query = existing_query.with_for_update(read=True)
    existing = session.scalar(existing_query)
    if existing is not None:
        if (existing.metadata_json or {}).get("technical_evidence_revoked"):
            return None, "OBSERVATION_EVIDENCE_REVOKED"
        if (
            existing.provenance != _PROVENANCE
            or existing.event_ref != event_ref
            or existing.resource_id != resource.id
            or existing.actor_id != actor_id
        ):
            return None, "OBSERVATION_CONFLICT"
        return existing, "REUSED"
    if require_existing:
        return None, "OBSERVATION_SOURCE_MISMATCH"
    row = record_timeline_event(
        session,
        tenant_id=tenant_id,
        canonical_event_id=canonical.id,
        event_type=_EVENT_TYPE,
        occurred_at=details["sent_at"],
        provenance=_PROVENANCE,
        actor_id=actor_id,
        resource_id=resource.id,
        event_ref=event_ref,
        visibility="OWNER_PRIVATE",
    )
    return row, "CREATED"



def observation_event_is_current(
    session: Session,
    event: TimelineEventRow,
    *,
    binding_id: str | None = None,
    source_account: str | None = None,
    start: datetime | None = None,
    now: datetime | None = None,
    lock_sources: bool = False,
    _source_rows=None,
) -> bool:
    """Revalidate one pilot event against its current, exact source receipt.

    Other timeline event types are outside this pilot contract.
    Does not insert a new event or mutate evidence.
    """
    if event.event_type != _EVENT_TYPE:
        return True
    if (
        not canary_scope(
            tenant_id=event.tenant_id,
            binding_id=(event.event_ref if isinstance(event.event_ref, dict) else {}).get("owner_binding_id"),
            owner_id=(event.metadata_json or {}).get("owner_human_identity_id"),
        ) or (event.metadata_json or {}).get("technical_evidence_revoked")
    ):
        return False
    if event.provenance != _PROVENANCE or not isinstance(event.event_ref, dict):
        return False
    refs = event.event_ref
    owner_binding = refs.get("owner_binding_id")
    if (
        not isinstance(owner_binding, str)
        or owner_binding != settings.persistent_memory_canary_binding_id
        or event.tenant_id != settings.cognitive_runtime_canary_tenant_id
        or (binding_id is not None and binding_id != owner_binding)
    ):
        return False
    tenant = session.get(TenantRow, event.tenant_id, populate_existing=True)
    binding = session.get(ActorBindingRow, owner_binding, populate_existing=True)
    if (
        tenant is None
        or tenant.status != "ACTIVE"
        or binding is None
        or binding.tenant_id != event.tenant_id
        or not binding.is_active
        or not (
            binding.actor_category == "owner"
            or (binding.binding_metadata or {}).get("owner") is True
        )
    ):
        return False
    human_owner = (event.metadata_json or {}).get("owner_human_identity_id")
    if not human_owner:
        return False
    revision = _owner_authority_revision(session, event.tenant_id, human_owner, binding)
    if revision is None or revision != (event.metadata_json or {}).get("owner_authority_revision"):
        return False
    try:
        if resolve_represented_owner_actor_key(
            session, tenant_id=event.tenant_id, owner_human_identity_id=human_owner,
        ) != binding.actor_key:
            return False
    except RuntimeError:
        return False
    message_id = refs.get("conversation_message_id") or ""
    if _source_rows is None:
        message = session.get(
            ConversationMessageRow, message_id,
            populate_existing=True, with_for_update={"read": True} if lock_sources else None,
        )
    else:
        message = _source_rows.get(ConversationMessageRow, {}).get(message_id) if isinstance(message_id, str) else None
    if (
        message is None
        or message.tenant_id != event.tenant_id
        or message.source != "wwebjs"
        or not message.source_account
        or message.source_account == "default"
        or (source_account is not None and message.source_account != source_account)
    ):
        return False
    scope = (event.metadata_json or {}).get("observation_scope")
    if scope is not None and scope != stable_hash({
        "binding_id": owner_binding, "source_account": message.source_account,
    }):
        return False
    linked, status = _observe_message(
        session, message, binding_id=owner_binding,
        start=start or datetime(1970, 1, 1, tzinfo=UTC),
        stamp=now or datetime.now(UTC), require_existing=True, lock_sources=lock_sources,
        _source_rows=_source_rows,
    )
    return (
        status == "REUSED"
        and linked is not None
        and linked.id == event.id
        and _utc(linked.occurred_at) == _utc(message.sent_at)
    )


def _event_scope_is_proven(
    session: Session, event: TimelineEventRow, *, binding_id: str, source_account: str,
) -> bool:
    if (
        event.provenance != _PROVENANCE
        or not isinstance(event.event_ref, dict)
        or event.event_ref.get("owner_binding_id") != binding_id
    ):
        return False
    scope = (event.metadata_json or {}).get("observation_scope")
    if scope is not None:
        return scope == stable_hash({"binding_id": binding_id, "source_account": source_account})
    # Older observations have no stored account fingerprint. Reuse the exact
    # receipt revision only while its original account coordinate is provable.
    receipt_id = event.event_ref.get("artifact_receipt_id")
    if not isinstance(receipt_id, str) or not receipt_id:
        return False
    receipt = session.get(ArtifactReceiptRow, receipt_id, populate_existing=True)
    return (
        receipt is not None and receipt.tenant_id == event.tenant_id
        and receipt.source_channel == "channel.whatsapp"
        and receipt.source_account == source_account
        and receipt.context_revision == event.event_ref.get("artifact_receipt_revision")
    )


def _reconcile_observation_support(
    session: Session,
    *,
    tenant_id: str,
    binding_id: str,
    source_account: str,
    start: datetime,
    stamp: datetime,
) -> int:
    """Technical withdrawal of pilot-derived context, not an owner decision.

    Only episodes associated with this exact binding/account pilot are touched.
    Source events and their evidence memberships remain durable for audit.
    """
    scope_key = stable_hash({"binding_id": binding_id, "source_account": source_account})
    episodes = session.scalars(
        select(SemanticEpisodeRow).where(
            SemanticEpisodeRow.tenant_id == tenant_id,
            SemanticEpisodeRow.episode_type == f"{_EVENT_TYPE}:{scope_key[:32]}",
            SemanticEpisodeRow.state.in_(("ACTIVE", "CLOSED")),
            SemanticEpisodeRow.evidence_status == "CURRENT",
        ).order_by(SemanticEpisodeRow.id).limit(101).with_for_update()
    ).all()
    if len(episodes) > 100:
        raise ValueError("OBSERVATION_RECONCILIATION_BUDGET_EXCEEDED")
    invalid_event_ids: set[str] = set()
    revised_episode_ids: set[str] = set()
    for episode in episodes:
        memberships = session.scalars(
            select(SemanticEpisodeMembershipRow).where(
                SemanticEpisodeMembershipRow.tenant_id == tenant_id,
                SemanticEpisodeMembershipRow.episode_id == episode.id,
            ).order_by(SemanticEpisodeMembershipRow.id).limit(1001)
        ).all()
        if not memberships or len(memberships) > 1000:
            raise ValueError("OBSERVATION_RECONCILIATION_MEMBERSHIP_UNPROVEN")
        valid = []
        invalid = []
        for membership in memberships:
            event = (
                session.get(TimelineEventRow, membership.member_ref)
                if membership.member_type == "TIMELINE_EVENT"
                else None
            )
            if (
                event is None
                or event.tenant_id != tenant_id
                or membership.ambiguous
                or event.event_type != _EVENT_TYPE
            ):
                invalid.append(membership.member_ref)
                continue
            if observation_event_is_current(
                session, event, binding_id=binding_id,
                source_account=source_account, start=start, now=stamp,
            ):
                valid.append((membership, event))
                continue

            invalid.append(membership.member_ref)
            if _event_scope_is_proven(
                session, event, binding_id=binding_id, source_account=source_account,
            ):
                invalid_event_ids.add(event.id)
                event.metadata_json = {**(event.metadata_json or {}), "technical_evidence_revoked": True}
        if not invalid:
            continue
        marker = {
            "reason": "CURRENT_SUPPORT_REVOKED",
            "invalid_member_refs": sorted(set(invalid)),
            "validated_member_refs": sorted(event.id for _, event in valid),
            "checked_at": stamp.isoformat(),
        }
        provenance = episode.provenance or {}
        previous = provenance.get("technical_evidence_reconciliation", {})
        if all(previous.get(key) == marker[key] for key in ("invalid_member_refs", "validated_member_refs")):
            continue
        audit(
            session, None, "CONTEXT_EPISODE_SUPPORT_REVISED",
            {
                "episode_id": episode.id, "revision": stable_hash(marker), "support": marker,
                "previous_started_at": episode.started_at.isoformat(),
                "previous_last_activity_at": episode.last_activity_at.isoformat(),
                "previous_confidence": episode.confidence,
            },
            tenant_id=tenant_id, origin="context_validity", created_at=stamp,
        )
        revised_episode_ids.add(episode.id)
        episode.provenance = {**provenance, "technical_evidence_reconciliation": marker}
        episode.updated_at = stamp
        if not valid:
            episode.evidence_status = "EVIDENCE_REVOKED"
        else:
            episode.started_at = min(_utc(event.occurred_at) for _, event in valid)
            episode.last_activity_at = max(_utc(event.occurred_at) for _, event in valid)
            episode.confidence = min(member.confidence for member, _ in valid)

    if not invalid_event_ids and not revised_episode_ids:
        return 0
    invalid_evidence = session.scalars(
        select(CandidateInsightEvidenceRow).where(
            CandidateInsightEvidenceRow.tenant_id == tenant_id,
            CandidateInsightEvidenceRow.evidence_role == "SUPPORT",
            (
                (
                    CandidateInsightEvidenceRow.evidence_type == "TIMELINE_EVENT"
                ) & CandidateInsightEvidenceRow.source_ref.in_(invalid_event_ids)
            ) | (
                (
                    CandidateInsightEvidenceRow.evidence_type == "SEMANTIC_EPISODE"
                ) & CandidateInsightEvidenceRow.source_ref.in_(revised_episode_ids)
            ),
        )
    ).all()
    affected_candidate_ids = {row.candidate_id for row in invalid_evidence}
    if len(affected_candidate_ids) > 200:
        raise ValueError("OBSERVATION_RECONCILIATION_CANDIDATE_BUDGET_EXCEEDED")
    for candidate_id in sorted(affected_candidate_ids):
        candidate = session.get(CandidateInsightRow, candidate_id)
        if (
            candidate is None
            or candidate.tenant_id != tenant_id
            or candidate.evidence_status == "EVIDENCE_REVOKED"
        ):
            continue
        # Technical validity applies to historical candidates too. Preserve
        # the review disposition and its decision metadata.
        candidate.evidence_status = "EVIDENCE_REVOKED"
        candidate.provenance = {
            **(candidate.provenance or {}),
            "technical_evidence_reconciliation": {
                "reason": "CURRENT_SUPPORT_REVOKED",
                "invalid_source_refs": sorted(
                    row.source_ref for row in invalid_evidence
                    if row.candidate_id == candidate_id
                ),
                "checked_at": stamp.isoformat(),
            },
        }
        candidate.updated_at = stamp
    session.flush()
    return len(revised_episode_ids)


def observe_attachment_context(
    session: Session,
    *,
    tenant_id: str,
    binding_id: str,
    owner_human_identity_id: str,
    source_account: str,
    message_ids: tuple[str, ...],
    start: datetime,
    now: datetime | None = None,
    observations_only: bool = False,
) -> ObservationResult:
    """Reconcile an explicit, prospective batch using the existing engines."""
    stamp = now or datetime.now(UTC)
    if (
        not source_account.strip()
        or source_account == "default"
        or not 1 <= len(message_ids) <= 100
        or start.utcoffset() is None
        or stamp.utcoffset() is None
        or start >= stamp
    ):
        raise ValueError("OBSERVATION_BATCH_INVALID")

    with session.begin_nested():
        _, authority_revision = _lock_owner(session, tenant_id, binding_id, owner_human_identity_id)
        query = select(ConversationMessageRow).where(
            ConversationMessageRow.id.in_(message_ids),
            ConversationMessageRow.tenant_id == tenant_id,
            ConversationMessageRow.source == "wwebjs",
            ConversationMessageRow.source_account == source_account,
        ).order_by(ConversationMessageRow.sent_at, ConversationMessageRow.id)
        lock_sources = session.get_bind().dialect.name == "postgresql"
        if lock_sources:
            query = query.with_for_update(read=True).execution_options(populate_existing=True)
        messages = session.scalars(query).all()

        received = len(set(message_ids))
        statuses = Counter({"OUTSIDE_SCOPE_OR_MISSING": received - len(messages)})
        events = []
        episodes = memberships = 0
        scope_key = stable_hash({"binding_id": binding_id, "source_account": source_account})
        for message in messages:
            event, status = _observe_message(session, message, binding_id=binding_id, start=start, stamp=stamp, lock_sources=lock_sources)
            if status == "REUSED" and (event.metadata_json or {}).get("owner_authority_revision") != authority_revision:
                event, status = None, "OBSERVATION_AUTHORITY_CHANGED"
            statuses[status] += 1
            if event is None:
                continue
            if status == "CREATED":
                event.metadata_json = {
                    **(event.metadata_json or {}),
                    "owner_human_identity_id": owner_human_identity_id,
                    "owner_authority_revision": authority_revision,
                    "observation_scope": scope_key,
                }
            events.append(event)
            if observations_only:
                continue
            assignment = assign_timeline_event_to_episode(
                session,
                tenant_id=tenant_id,
                event_id=event.id,
                episode_type=f"{_EVENT_TYPE}:{scope_key[:32]}",
                scope_type="RESOURCE",
                now=stamp,
            )
            episodes += assignment.created_episode
            memberships += assignment.created_membership

        revised_episodes = 0
        if not observations_only:
            revised_episodes = _reconcile_observation_support(
                session, tenant_id=tenant_id, binding_id=binding_id,
                source_account=source_account, start=start, stamp=stamp,
            )

        event_ids = tuple(event.id for event in events)
        people = {event.actor_id for event in events if event.actor_id}
        pairs = Counter((event.actor_id, event.resource_id) for event in events if event.actor_id)
        candidates = new_candidates = 0
        recurrences = graph_nodes = graph_edges = 0
        if events and not observations_only:
            graph = build_cognitive_graph_slice(
                session, tenant_id, timeline_lineage="ORGANIC", timeline_event_ids=event_ids, now=stamp,
            )
            graph_nodes, graph_edges = len(graph.nodes), len(graph.edges)
            for candidate in DeterministicRelationCandidateEngine().candidates(graph):
                row, created = persist_relation_candidate_insight(session, graph=graph, candidate=candidate, now=stamp)
                if created:
                    prior = session.scalars(select(CandidateInsightRow.id).where(
                        CandidateInsightRow.tenant_id == tenant_id,
                        CandidateInsightRow.semantic_key == row.semantic_key,
                        CandidateInsightRow.evidence_status == "EVIDENCE_REVOKED",
                        CandidateInsightRow.provenance["observation_scope"].as_string() == scope_key,
                        CandidateInsightRow.proposed_value["target_id"].as_string() == row.proposed_value["target_id"],
                    ).order_by(CandidateInsightRow.updated_at.desc(), CandidateInsightRow.id).limit(1)).first()
                    row.provenance = {**(row.provenance or {}), "observation_scope": scope_key}
                    if prior:
                        row.provenance = {**row.provenance, "technical_revision_of": prior}
                candidates += 1
                new_candidates += created
            for actor_key in sorted(people):
                recurrences += len(detect_temporal_recurrence_hypotheses(
                    session, tenant_id=tenant_id, actor_id=actor_key, now=stamp, timeline_event_ids=event_ids,
                ))

        return ObservationResult(
            received=received,
            linked=len(events),
            new_links=statuses.pop("CREATED", 0),
            resources=len({event.resource_id for event in events}),
            identified_people=len(people),
            new_episodes=episodes,
            new_memberships=memberships,
            revised_episodes=revised_episodes,
            candidates=candidates,
            new_candidates=new_candidates,
            pairs_with_single_observation=sum(count == 1 for count in pairs.values()),
            recurrences=recurrences,
            graph_nodes=graph_nodes,
            graph_edges=graph_edges,
            rejected={status: count for status, count in statuses.items() if count and status != "REUSED"},
        )
