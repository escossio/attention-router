"""Revalidate explicit timeline references in existing Personal Context claims."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.infrastructure.models import MemoryClaimRow, TimelineEventRow


def claim_timeline_support_is_current(
    session: Session, claim: MemoryClaimRow, *, tenant_id: str, now: datetime | None,
    for_action: bool = False, _visited: frozenset[str] = frozenset(),
) -> bool:
    """Check explicit support while preserving legacy claims without that field.

    Pilot attachment observations can support internal retrieval only. The
    recommendation system has a separate existing authority contract.
    """
    if claim.id in _visited or len(_visited) >= 4:
        return False
    stamp = now or datetime.now(UTC)
    context = claim.context or {}
    for key in ("source_claim_id", "source_anomaly_claim_id", "source_sequence_claim_id"):
        if key not in context:
            continue
        parent_id = context[key]
        if not isinstance(parent_id, str) or not parent_id:
            return False
        parent = session.get(MemoryClaimRow, parent_id, populate_existing=True)
        if (
            parent is None
            or parent.subject_actor_id != claim.subject_actor_id
            or parent.status != "ACTIVE"
            or parent.sensitivity_class == "SECRET"
            or (parent.valid_until is not None and _utc(parent.valid_until) <= _utc(stamp))
            or not claim_timeline_support_is_current(
                session, parent, tenant_id=tenant_id, now=stamp,
                for_action=for_action, _visited=_visited | {claim.id},
            )
        ):
            return False
    pilot_claim = (
        (claim.object_json or {}).get("event_type") == "ATTACHMENT_OBSERVED"
        or "exact_conversation_artifact_receipt" in (context.get("source_provenance") or ())
    )
    refs = context.get("evidence_timeline_event_ids")
    first_event_id = context.get("first_event_id")
    if refs is None:
        if pilot_claim and not first_event_id:
            return False
        refs = []
    if "first_event_id" in context and isinstance(refs, list):
        refs = [*refs, first_event_id]
    if refs == []:
        return not pilot_claim and "evidence_timeline_event_ids" not in context
    if not isinstance(refs, list) or not refs or len(refs) > 1000:
        return False
    if not all(isinstance(ref, str) and ref for ref in refs):
        return False
    events = session.scalars(select(TimelineEventRow).where(
        TimelineEventRow.tenant_id == tenant_id, TimelineEventRow.id.in_(refs),
    ).execution_options(populate_existing=True)).all()
    if len(events) != len(set(refs)):
        return False
    if pilot_claim and for_action:
        return False
    from attention_router.application.cognitive_observation import observation_event_is_current

    return all(
        not (event.metadata_json or {}).get("technical_evidence_revoked")
        and (
            event.event_type != "ATTACHMENT_OBSERVED"
            or (not for_action and observation_event_is_current(session, event, now=stamp))
        )
        for event in events
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
