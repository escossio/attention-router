"""Deterministic, governed Semantic Episode Builder V0."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    RelationshipRow,
    ResourceRow,
    TimelineEventRow,
)
from attention_router.infrastructure.semantic_episode_models import (
    SemanticEpisodeMembershipRow,
    SemanticEpisodeRow,
)


DEFAULT_MAX_INACTIVITY: Final = timedelta(days=30)
MAX_MAX_INACTIVITY: Final = timedelta(days=365)
_SENSITIVITY_ORDER: Final = {"NORMAL": 0, "PRIVATE": 1, "SECRET": 2}


class SemanticEpisodeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EpisodeAssignmentResult:
    episode: SemanticEpisodeRow
    membership: SemanticEpisodeMembershipRow
    created_episode: bool
    created_membership: bool


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _bounded(
    value: str,
    *,
    code: str,
    limit: int,
) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise SemanticEpisodeError(f"{code}_REQUIRED")
    if len(normalized) > limit:
        raise SemanticEpisodeError(f"{code}_TOO_LONG")
    return normalized


def _event_sensitivity(event: TimelineEventRow) -> str:
    visibility = (event.visibility or "").strip().upper()
    if visibility == "SECRET":
        return "SECRET"
    if visibility in {"PRIVATE", "OWNER_PRIVATE"}:
        return "PRIVATE"
    return "NORMAL"


def _max_sensitivity(first: str, second: str) -> str:
    if first not in _SENSITIVITY_ORDER or second not in _SENSITIVITY_ORDER:
        raise SemanticEpisodeError("SEMANTIC_EPISODE_SENSITIVITY_INVALID")
    return max(
        (first, second),
        key=lambda item: _SENSITIVITY_ORDER[item],
    )


def _scope_ref(
    session: Session,
    *,
    event: TimelineEventRow,
    scope_type: str,
) -> tuple[str, str]:
    normalized = scope_type.strip().upper()
    if normalized == "RESOURCE":
        if not event.resource_id:
            raise SemanticEpisodeError(
                "SEMANTIC_EPISODE_RESOURCE_SCOPE_MISSING"
            )
        resource = session.get(ResourceRow, event.resource_id)
        if resource is None or resource.tenant_id != event.tenant_id:
            raise SemanticEpisodeError(
                "SEMANTIC_EPISODE_RESOURCE_SCOPE_INVALID"
            )
        return normalized, event.resource_id

    if normalized == "RELATIONSHIP":
        if not event.relationship_id:
            raise SemanticEpisodeError(
                "SEMANTIC_EPISODE_RELATIONSHIP_SCOPE_MISSING"
            )
        relationship = session.get(RelationshipRow, event.relationship_id)
        if relationship is None or relationship.tenant_id != event.tenant_id:
            raise SemanticEpisodeError(
                "SEMANTIC_EPISODE_RELATIONSHIP_SCOPE_INVALID"
            )
        return normalized, event.relationship_id

    raise SemanticEpisodeError("SEMANTIC_EPISODE_SCOPE_UNSUPPORTED")


def _existing_assignment(
    session: Session,
    *,
    tenant_id: str,
    event_id: str,
    episode_type: str,
    scope_type: str,
    scope_ref: str,
) -> tuple[SemanticEpisodeRow, SemanticEpisodeMembershipRow] | None:
    rows = session.execute(
        select(SemanticEpisodeRow, SemanticEpisodeMembershipRow)
        .join(
            SemanticEpisodeMembershipRow,
            SemanticEpisodeMembershipRow.episode_id == SemanticEpisodeRow.id,
        )
        .where(
            SemanticEpisodeRow.tenant_id == tenant_id,
            SemanticEpisodeRow.episode_type == episode_type,
            SemanticEpisodeRow.scope_type == scope_type,
            SemanticEpisodeRow.scope_ref == scope_ref,
            SemanticEpisodeMembershipRow.tenant_id == tenant_id,
            SemanticEpisodeMembershipRow.member_type == "TIMELINE_EVENT",
            SemanticEpisodeMembershipRow.member_ref == event_id,
        )
    ).all()
    if len(rows) > 1:
        raise SemanticEpisodeError(
            "SEMANTIC_EPISODE_EXISTING_ASSIGNMENT_NOT_UNIQUE"
        )
    return rows[0] if rows else None


def _candidate_episodes(
    session: Session,
    *,
    tenant_id: str,
    episode_type: str,
    scope_type: str,
    scope_ref: str,
    occurred_at: datetime,
    max_inactivity: timedelta,
) -> list[SemanticEpisodeRow]:
    rows = session.scalars(
        select(SemanticEpisodeRow)
        .where(
            SemanticEpisodeRow.tenant_id == tenant_id,
            SemanticEpisodeRow.episode_type == episode_type,
            SemanticEpisodeRow.scope_type == scope_type,
            SemanticEpisodeRow.scope_ref == scope_ref,
            SemanticEpisodeRow.state == "ACTIVE",
        )
        .order_by(
            SemanticEpisodeRow.last_activity_at.desc(),
            SemanticEpisodeRow.id,
        )
    ).all()
    stamp = _utc(occurred_at)
    return [
        row
        for row in rows
        if _utc(row.started_at) - max_inactivity
        <= stamp
        <= _utc(row.last_activity_at) + max_inactivity
    ]


def assign_timeline_event_to_episode(
    session: Session,
    *,
    tenant_id: str,
    event_id: str,
    episode_type: str,
    scope_type: str,
    max_inactivity: timedelta = DEFAULT_MAX_INACTIVITY,
    confidence: float = 1.0,
    now: datetime | None = None,
) -> EpisodeAssignmentResult:
    """Assign one timeline event using exact, bounded structural scope.

    This creates organizational context only. It creates zero execution,
    recommendation or disclosure authority.
    """
    if (
        max_inactivity <= timedelta(0)
        or max_inactivity > MAX_MAX_INACTIVITY
    ):
        raise SemanticEpisodeError(
            "SEMANTIC_EPISODE_MAX_INACTIVITY_INVALID"
        )
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or confidence < 0
        or confidence > 1
    ):
        raise SemanticEpisodeError("SEMANTIC_EPISODE_CONFIDENCE_INVALID")

    event = session.get(TimelineEventRow, event_id)
    if event is None:
        raise SemanticEpisodeError("SEMANTIC_EPISODE_EVENT_NOT_FOUND")
    if event.tenant_id != tenant_id:
        raise SemanticEpisodeError("SEMANTIC_EPISODE_TENANT_MISMATCH")

    kind = _bounded(
        episode_type,
        code="SEMANTIC_EPISODE_TYPE",
        limit=80,
    )
    normalized_scope, scope_ref = _scope_ref(
        session,
        event=event,
        scope_type=scope_type,
    )

    existing = _existing_assignment(
        session,
        tenant_id=tenant_id,
        event_id=event.id,
        episode_type=kind,
        scope_type=normalized_scope,
        scope_ref=scope_ref,
    )
    if existing is not None:
        episode, membership = existing
        return EpisodeAssignmentResult(
            episode=episode,
            membership=membership,
            created_episode=False,
            created_membership=False,
        )

    candidates = _candidate_episodes(
        session,
        tenant_id=tenant_id,
        episode_type=kind,
        scope_type=normalized_scope,
        scope_ref=scope_ref,
        occurred_at=event.occurred_at,
        max_inactivity=max_inactivity,
    )
    if len(candidates) > 1:
        raise SemanticEpisodeError(
            "SEMANTIC_EPISODE_ASSIGNMENT_AMBIGUOUS"
        )

    stamp = _utc(now or now_utc())
    event_time = _utc(event.occurred_at)
    event_sensitivity = _event_sensitivity(event)
    created_episode = False

    if candidates:
        episode = candidates[0]
        episode.started_at = min(_utc(episode.started_at), event_time)
        episode.last_activity_at = max(
            _utc(episode.last_activity_at),
            event_time,
        )
        episode.confidence = min(episode.confidence, float(confidence))
        episode.sensitivity_class = _max_sensitivity(
            episode.sensitivity_class,
            event_sensitivity,
        )
        episode.updated_at = stamp
    else:
        semantic_key = (
            "episode:"
            + stable_hash(
                {
                    "schema_version": "semantic-episode-v0",
                    "tenant_id": tenant_id,
                    "episode_type": kind,
                    "scope_type": normalized_scope,
                    "scope_ref": scope_ref,
                    "anchor_event_id": event.id,
                }
            )[:96]
        )
        episode = SemanticEpisodeRow(
            id=new_id(),
            tenant_id=tenant_id,
            episode_type=kind,
            semantic_key=semantic_key,
            scope_type=normalized_scope,
            scope_ref=scope_ref,
            state="ACTIVE",
            confidence=float(confidence),
            sensitivity_class=event_sensitivity,
            started_at=event_time,
            last_activity_at=event_time,
            ended_at=None,
            supersedes_episode_id=None,
            split_from_episode_id=None,
            merged_from_episode_ids=[],
            provenance={
                "builder": "DETERMINISTIC_V0",
                "anchor_event_id": event.id,
                "max_inactivity_seconds": int(
                    max_inactivity.total_seconds()
                ),
            },
            created_at=stamp,
            updated_at=stamp,
        )
        session.add(episode)
        session.flush()
        created_episode = True

    reason = (
        "EXACT_RESOURCE"
        if normalized_scope == "RESOURCE"
        else "EXACT_RELATIONSHIP"
    )
    membership = SemanticEpisodeMembershipRow(
        id=new_id(),
        tenant_id=tenant_id,
        episode_id=episode.id,
        member_type="TIMELINE_EVENT",
        member_ref=event.id,
        association_reason=reason,
        association_source="DETERMINISTIC_V0",
        confidence=float(confidence),
        ambiguous=False,
        observed_at=event_time,
        metadata_json={
            "event_type": event.event_type,
            "scope_ref": scope_ref,
        },
        created_at=stamp,
    )
    session.add(membership)
    session.flush()
    return EpisodeAssignmentResult(
        episode=episode,
        membership=membership,
        created_episode=created_episode,
        created_membership=True,
    )


def close_episode(
    session: Session,
    *,
    episode_id: str,
    ended_at: datetime,
    now: datetime | None = None,
) -> SemanticEpisodeRow:
    row = session.get(SemanticEpisodeRow, episode_id)
    if row is None:
        raise SemanticEpisodeError("SEMANTIC_EPISODE_NOT_FOUND")
    if row.state == "CLOSED":
        return row
    if row.state != "ACTIVE":
        raise SemanticEpisodeError("SEMANTIC_EPISODE_NOT_CLOSABLE")
    terminal = _utc(ended_at)
    if terminal < _utc(row.last_activity_at):
        raise SemanticEpisodeError(
            "SEMANTIC_EPISODE_END_BEFORE_LAST_ACTIVITY"
        )
    row.state = "CLOSED"
    row.ended_at = terminal
    row.updated_at = _utc(now or now_utc())
    session.flush()
    return row


__all__ = [
    "DEFAULT_MAX_INACTIVITY",
    "EpisodeAssignmentResult",
    "SemanticEpisodeError",
    "assign_timeline_event_to_episode",
    "close_episode",
]
