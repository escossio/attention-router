"""Deterministic Cognitive Attention and Salience Engine V0."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from attention_router.application.personal_context_controls import (
    claim_is_owner_non_actionable,
    claim_is_owner_private,
)
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.attention_models import AttentionAssessmentRow
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    ActorBindingRow,
    MemoryActorRow,
    MemoryClaimRow,
)
from attention_router.infrastructure.obligation_models import (
    ObligationInstanceRow,
    RecurringObligationDefinitionRow,
)


ATTENTION_IGNORE_THRESHOLD: Final = 0.65
ATTENTION_OWNER_SUGGESTION_THRESHOLD: Final = 0.82
DEFAULT_ATTENTION_COOLDOWN: Final = timedelta(hours=6)
MAX_OWNER_SUPPRESSION: Final = timedelta(days=30)

_COMPONENT_WEIGHTS: Final = {
    "impact": 0.14,
    "urgency": 0.12,
    "novelty": 0.08,
    "confidence": 0.10,
    "temporal_proximity": 0.10,
    "relationship_relevance": 0.08,
    "owner_relevance": 0.12,
    "expectation_violation": 0.14,
    "recurrence_stability": 0.06,
    "source_quality": 0.06,
}


class AttentionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AttentionSuggestionCandidate:
    assessment_id: str
    tenant_id: str
    signal_key: str
    source_type: str
    source_ref: str
    score: float
    reason_codes: tuple[str, ...]
    generated_at: datetime
    requires_user_confirmation: bool = True
    execution_requested: bool = False
    grants_authority: bool = False


def _utc(value: datetime | None = None) -> datetime:
    stamp = value or now_utc()
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def _clamp(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AttentionError("ATTENTION_COMPONENT_INVALID")
    return max(0.0, min(float(value), 1.0))


def _score(components: dict[str, float]) -> float:
    if set(components) != set(_COMPONENT_WEIGHTS):
        raise AttentionError("ATTENTION_COMPONENT_SET_INVALID")
    total = sum(
        _clamp(components[name]) * weight
        for name, weight in _COMPONENT_WEIGHTS.items()
    )
    return round(max(0.0, min(total, 1.0)), 6)


def _class_for_score(score: float) -> str:
    if score < ATTENTION_IGNORE_THRESHOLD:
        return "IGNORE"
    if score < ATTENTION_OWNER_SUGGESTION_THRESHOLD:
        return "REASONING_QUEUE"
    return "OWNER_SUGGESTION_CANDIDATE"


def _reason_codes(components: dict[str, float]) -> list[str]:
    codes: list[str] = []
    thresholds = {
        "impact": (0.70, "HIGH_IMPACT"),
        "urgency": (0.70, "HIGH_URGENCY"),
        "novelty": (0.70, "NOVEL_CHANGE"),
        "confidence": (0.80, "HIGH_CONFIDENCE"),
        "temporal_proximity": (0.80, "TEMPORALLY_NEAR"),
        "relationship_relevance": (0.75, "RELATIONSHIP_RELEVANT"),
        "owner_relevance": (0.80, "OWNER_RELEVANT"),
        "expectation_violation": (0.70, "EXPECTATION_VIOLATION"),
        "recurrence_stability": (0.80, "STABLE_RECURRENCE"),
        "source_quality": (0.80, "STRONG_SOURCE"),
    }
    for name, (threshold, code) in thresholds.items():
        if components[name] >= threshold:
            codes.append(code)
    return codes


def _bounded(value: str, *, code: str, limit: int) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise AttentionError(f"{code}_REQUIRED")
    if len(normalized) > limit:
        raise AttentionError(f"{code}_TOO_LONG")
    return normalized


def _require_owner(
    session: Session,
    *,
    tenant_id: str,
    actor_key: str,
) -> str:
    actor = _bounded(actor_key, code="ATTENTION_OWNER_ACTOR", limit=120)
    bindings = list(
        session.scalars(
            select(ActorBindingRow).where(
                ActorBindingRow.tenant_id == tenant_id,
                ActorBindingRow.is_active.is_(True),
                or_(
                    ActorBindingRow.actor_category == "owner",
                    ActorBindingRow.binding_metadata["owner"]
                    .as_boolean()
                    .is_(True),
                ),
            )
        ).all()
    )
    owners = {row.actor_key for row in bindings}
    if len(owners) != 1:
        raise AttentionError("ATTENTION_OWNER_AMBIGUOUS")
    if actor not in owners:
        raise AttentionError("ATTENTION_OWNER_AUTHORITY_REQUIRED")
    return actor


def _latest_assessment(
    session: Session,
    *,
    tenant_id: str,
    signal_key: str,
) -> AttentionAssessmentRow | None:
    return session.scalar(
        select(AttentionAssessmentRow)
        .where(
            AttentionAssessmentRow.tenant_id == tenant_id,
            AttentionAssessmentRow.signal_key == signal_key,
        )
        .order_by(
            AttentionAssessmentRow.updated_at.desc(),
            AttentionAssessmentRow.id.desc(),
        )
        .limit(1)
    )


def _exact_assessment(
    session: Session,
    *,
    tenant_id: str,
    signal_key: str,
    snapshot_fingerprint: str,
) -> AttentionAssessmentRow | None:
    return session.scalar(
        select(AttentionAssessmentRow).where(
            AttentionAssessmentRow.tenant_id == tenant_id,
            AttentionAssessmentRow.signal_key == signal_key,
            AttentionAssessmentRow.snapshot_fingerprint
            == snapshot_fingerprint,
        )
    )


def _novelty(
    prior: AttentionAssessmentRow | None,
    *,
    source_state: str,
) -> float:
    if prior is None:
        return 1.0
    if prior.source_state != source_state:
        return 1.0
    return 0.4


def _temporal_proximity(reference: datetime, now: datetime) -> float:
    delta = abs((now - _utc(reference)).total_seconds())
    if delta <= 24 * 60 * 60:
        return 1.0
    if delta <= 3 * 24 * 60 * 60:
        return 0.8
    if delta <= 7 * 24 * 60 * 60:
        return 0.5
    return 0.2


def _deadline_urgency(
    *,
    state: str,
    deadline: datetime,
    now: datetime,
) -> float:
    remaining = (_utc(deadline) - now).total_seconds()
    if state == "UNCONFIRMED_AFTER_DUE":
        return 0.95
    if state == "PARTIALLY_SATISFIED" and remaining < 0:
        return 0.90
    if state in {"SATISFIED", "WAIVED", "SUPERSEDED"}:
        return 0.05
    if remaining <= 24 * 60 * 60:
        return 0.85
    if remaining <= 3 * 24 * 60 * 60:
        return 0.65
    if remaining <= 7 * 24 * 60 * 60:
        return 0.40
    return 0.20


def _state_impact(state: str) -> float:
    return {
        "EXPECTED": 0.20,
        "SATISFIED": 0.10,
        "PARTIALLY_SATISFIED": 0.65,
        "EXTENDED": 0.25,
        "WAIVED": 0.05,
        "UNCONFIRMED_AFTER_DUE": 0.90,
        "SUPERSEDED": 0.05,
    }.get(state, 0.20)


def _expectation_violation(
    *,
    state: str,
    deadline: datetime,
    now: datetime,
) -> float:
    if state == "UNCONFIRMED_AFTER_DUE":
        return 1.0
    if state == "PARTIALLY_SATISFIED":
        return 0.75 if _utc(deadline) < now else 0.55
    if state == "EXTENDED":
        return 0.10
    return 0.0


def _cap_owner_actionability(
    effective_class: str,
    *,
    owner_private: bool,
    owner_non_actionable: bool,
    reasons: list[str],
) -> str:
    result = effective_class
    if owner_private and result == "OWNER_SUGGESTION_CANDIDATE":
        result = "REASONING_QUEUE"
        reasons.append("OWNER_PRIVATE_ACTIONABILITY_CAP")
    if owner_non_actionable and result == "OWNER_SUGGESTION_CANDIDATE":
        result = "REASONING_QUEUE"
        reasons.append("OWNER_NON_ACTIONABLE_CAP")
    return result


def _refresh_exact(
    row: AttentionAssessmentRow,
    *,
    owner_private: bool,
    owner_non_actionable: bool,
    now: datetime,
) -> bool:
    if row.status == "ACKNOWLEDGED":
        return False

    reasons = [
        code
        for code in row.reason_codes
        if code
        not in {
            "OWNER_SUPPRESSED",
            "OWNER_PRIVATE_ACTIONABILITY_CAP",
            "OWNER_NON_ACTIONABLE_CAP",
        }
    ]
    effective = row.score_class
    status = "ACTIVE"
    if row.owner_suppressed_until is not None and _utc(
        row.owner_suppressed_until
    ) > now:
        effective = "IGNORE"
        status = "SUPPRESSED"
        reasons.append("OWNER_SUPPRESSED")
    else:
        effective = _cap_owner_actionability(
            effective,
            owner_private=owner_private,
            owner_non_actionable=owner_non_actionable,
            reasons=reasons,
        )

    changed = (
        row.effective_class != effective
        or row.status != status
        or row.reason_codes != sorted(set(reasons))
    )
    if changed:
        row.effective_class = effective
        row.status = status
        row.reason_codes = sorted(set(reasons))
        row.updated_at = now
    return changed


def _persist_assessment(
    session: Session,
    *,
    tenant_id: str,
    signal_key: str,
    source_type: str,
    source_ref: str,
    source_state: str,
    snapshot_payload: dict,
    base_components: dict[str, float],
    sensitivity_class: str,
    owner_private: bool = False,
    owner_non_actionable: bool = False,
    provenance: dict | None = None,
    now: datetime | None = None,
) -> tuple[AttentionAssessmentRow, bool]:
    stamp = _utc(now)
    sensitivity = sensitivity_class.strip().upper()
    if sensitivity == "SECRET":
        raise AttentionError("ATTENTION_SECRET_SOURCE_EXCLUDED")
    if sensitivity not in {"NORMAL", "PRIVATE"}:
        raise AttentionError("ATTENTION_SENSITIVITY_INVALID")

    signal = _bounded(signal_key, code="ATTENTION_SIGNAL_KEY", limit=128)
    source_reference = _bounded(
        source_ref,
        code="ATTENTION_SOURCE_REF",
        limit=160,
    )
    snapshot_fingerprint = stable_hash(snapshot_payload)

    exact = _exact_assessment(
        session,
        tenant_id=tenant_id,
        signal_key=signal,
        snapshot_fingerprint=snapshot_fingerprint,
    )
    if exact is not None:
        changed = _refresh_exact(
            exact,
            owner_private=owner_private,
            owner_non_actionable=owner_non_actionable,
            now=stamp,
        )
        if changed:
            session.flush()
        return exact, False

    prior = _latest_assessment(
        session,
        tenant_id=tenant_id,
        signal_key=signal,
    )
    components = {
        **base_components,
        "novelty": _novelty(prior, source_state=source_state),
    }
    components = {key: round(_clamp(value), 6) for key, value in components.items()}
    score = _score(components)
    score_class = _class_for_score(score)
    reasons = _reason_codes(components)
    effective_class = _cap_owner_actionability(
        score_class,
        owner_private=owner_private,
        owner_non_actionable=owner_non_actionable,
        reasons=reasons,
    )

    owner_suppressed_until: datetime | None = None
    owner_suppression_reason: str | None = None
    status = "ACTIVE"
    if (
        prior is not None
        and prior.owner_suppressed_until is not None
        and _utc(prior.owner_suppressed_until) > stamp
    ):
        owner_suppressed_until = _utc(prior.owner_suppressed_until)
        owner_suppression_reason = prior.owner_suppression_reason
        effective_class = "IGNORE"
        status = "SUPPRESSED"
        reasons.append("OWNER_SUPPRESSED")

    cooldown_until: datetime | None = None
    if (
        status == "ACTIVE"
        and prior is not None
        and prior.effective_class == "OWNER_SUGGESTION_CANDIDATE"
        and prior.cooldown_until is not None
        and _utc(prior.cooldown_until) > stamp
        and score <= prior.score + 0.15
        and effective_class == "OWNER_SUGGESTION_CANDIDATE"
    ):
        effective_class = "REASONING_QUEUE"
        cooldown_until = _utc(prior.cooldown_until)
        reasons.append("DEDUP_SUPPRESSION_WINDOW")
    elif effective_class == "OWNER_SUGGESTION_CANDIDATE":
        cooldown_until = stamp + DEFAULT_ATTENTION_COOLDOWN

    if prior is not None and prior.status == "ACTIVE":
        prior.status = "SUPERSEDED"
        prior.updated_at = stamp
        session.flush()

    row = AttentionAssessmentRow(
        id=new_id(),
        tenant_id=tenant_id,
        signal_key=signal,
        snapshot_fingerprint=snapshot_fingerprint,
        source_type=source_type,
        source_ref=source_reference,
        source_state=source_state,
        score=score,
        score_class=score_class,
        effective_class=effective_class,
        components=components,
        reason_codes=sorted(set(reasons)),
        sensitivity_class=sensitivity,
        cooldown_until=cooldown_until,
        owner_suppressed_until=owner_suppressed_until,
        owner_suppression_reason=owner_suppression_reason,
        owner_suppressed_by=(
            prior.owner_suppressed_by
            if prior is not None and owner_suppressed_until is not None
            else None
        ),
        acknowledged_at=None,
        acknowledged_by=None,
        owner_decision_ref=None,
        status=status,
        supersedes_assessment_id=prior.id if prior is not None else None,
        provenance={
            **(provenance or {}),
            "owner_private": owner_private,
            "owner_non_actionable": owner_non_actionable,
            "grants_authority": False,
            "grants_disclosure_authority": False,
            "creates_outbound": False,
        },
        created_at=stamp,
        updated_at=stamp,
    )
    session.add(row)
    session.flush()
    return row, True


def assess_obligation_attention(
    session: Session,
    *,
    instance_id: str,
    now: datetime | None = None,
) -> tuple[AttentionAssessmentRow, bool]:
    stamp = _utc(now)
    instance = session.get(ObligationInstanceRow, instance_id)
    if instance is None:
        raise AttentionError("ATTENTION_OBLIGATION_INSTANCE_NOT_FOUND")
    definition = session.get(
        RecurringObligationDefinitionRow,
        instance.definition_id,
    )
    if (
        definition is None
        or definition.tenant_id != instance.tenant_id
    ):
        raise AttentionError("ATTENTION_OBLIGATION_DEFINITION_INVALID")
    if instance.sensitivity_class == "SECRET" or definition.sensitivity_class == "SECRET":
        raise AttentionError("ATTENTION_SECRET_SOURCE_EXCLUDED")

    deadline = _utc(
        instance.extension_until
        if instance.extension_until is not None
        else instance.due_window_end
    )
    base_components = {
        "impact": _state_impact(instance.state),
        "urgency": _deadline_urgency(
            state=instance.state,
            deadline=deadline,
            now=stamp,
        ),
        "confidence": definition.confidence,
        "temporal_proximity": _temporal_proximity(deadline, stamp),
        "relationship_relevance": (
            0.80 if definition.subject_type == "RELATIONSHIP" else 0.65
        ),
        "owner_relevance": (
            1.0 if definition.source_kind == "OWNER_DECLARED" else 0.90
        ),
        "expectation_violation": _expectation_violation(
            state=instance.state,
            deadline=deadline,
            now=stamp,
        ),
        "recurrence_stability": 0.90,
        "source_quality": (
            1.0 if definition.source_kind == "OWNER_DECLARED" else 0.85
        ),
    }
    source_state = ":".join(
        [
            instance.state,
            instance.reconciliation_status,
            instance.uncertainty_code or "NONE",
        ]
    )
    snapshot = {
        "instance_id": instance.id,
        "definition_id": definition.id,
        "source_state": source_state,
        "satisfaction_ratio": instance.satisfaction_ratio,
        "expected_by": _utc(instance.expected_by).isoformat(),
        "deadline": deadline.isoformat(),
        "updated_at": _utc(instance.updated_at).isoformat(),
    }
    return _persist_assessment(
        session,
        tenant_id=instance.tenant_id,
        signal_key=f"obligation-instance:{instance.id}",
        source_type="OBLIGATION_INSTANCE",
        source_ref=instance.id,
        source_state=source_state,
        snapshot_payload=snapshot,
        base_components=base_components,
        sensitivity_class=instance.sensitivity_class,
        provenance={
            "definition_id": definition.id,
            "definition_semantic_key": definition.semantic_key,
            "obligation_kind": definition.obligation_kind,
            "absence_is_fact": False,
        },
        now=stamp,
    )


def _age_weight(reference: datetime, now: datetime) -> float:
    age = max(0.0, (now - _utc(reference)).total_seconds())
    if age <= 6 * 60 * 60:
        return 0.85
    if age <= 24 * 60 * 60:
        return 0.70
    if age <= 3 * 24 * 60 * 60:
        return 0.40
    return 0.20


def assess_missing_step_attention(
    session: Session,
    *,
    anomaly_claim_id: str,
    now: datetime | None = None,
) -> tuple[AttentionAssessmentRow, bool]:
    stamp = _utc(now)
    claim = session.get(MemoryClaimRow, anomaly_claim_id)
    if claim is None:
        raise AttentionError("ATTENTION_ANOMALY_NOT_FOUND")
    if claim.status != "ACTIVE":
        raise AttentionError("ATTENTION_ANOMALY_NOT_ACTIVE")
    if claim.sensitivity_class == "SECRET":
        raise AttentionError("ATTENTION_SECRET_SOURCE_EXCLUDED")

    value = claim.object_json or {}
    context = claim.context or {}
    if (
        claim.predicate != "context.pattern.sequence_anomaly"
        or claim.source_quality != "DERIVED_PATTERN"
        or value.get("anomaly_type") != "MISSING_EXPECTED_STEP"
        or value.get("evidence_class") != "INFERRED"
        or value.get("grants_authority") is not False
    ):
        raise AttentionError("ATTENTION_ANOMALY_SOURCE_INVALID")

    actor = session.get(MemoryActorRow, claim.subject_actor_id)
    if actor is None:
        raise AttentionError("ATTENTION_ANOMALY_ACTOR_INVALID")

    source_id = context.get("source_sequence_claim_id")
    if not isinstance(source_id, str) or not source_id:
        raise AttentionError("ATTENTION_ANOMALY_SEQUENCE_MISSING")
    sequence = session.get(MemoryClaimRow, source_id)
    if (
        sequence is None
        or sequence.subject_actor_id != claim.subject_actor_id
        or sequence.status != "ACTIVE"
        or sequence.predicate != "context.pattern.event_sequence"
        or sequence.source_quality != "DERIVED_PATTERN"
        or sequence.sensitivity_class == "SECRET"
    ):
        raise AttentionError("ATTENTION_ANOMALY_SEQUENCE_INVALID")

    owner_private = claim_is_owner_private(session, claim=sequence)
    owner_non_actionable = claim_is_owner_non_actionable(
        session,
        claim=sequence,
    )
    support = (sequence.context or {}).get("support_ratio")
    occurrences = (sequence.context or {}).get("occurrence_count")
    support_value = _clamp(float(support)) if isinstance(support, (int, float)) else 0.0
    occurrence_value = (
        _clamp(float(occurrences) / 5.0)
        if isinstance(occurrences, int)
        else 0.0
    )
    stability = (support_value + occurrence_value) / 2.0

    observed_at = _utc(claim.last_observed_at or claim.created_at)
    age = _age_weight(observed_at, stamp)
    signature_kind = value.get("expected_second_signature_kind")
    relationship_relevance = {
        "RELATIONSHIP": 0.80,
        "RESOURCE": 0.65,
        "PATTERN_KEY": 0.45,
    }.get(signature_kind, 0.45)

    base_components = {
        "impact": 0.60,
        "urgency": age,
        "confidence": claim.confidence,
        "temporal_proximity": age,
        "relationship_relevance": relationship_relevance,
        "owner_relevance": 0.90,
        "expectation_violation": 0.80,
        "recurrence_stability": stability,
        "source_quality": 0.75,
    }
    anomaly_id = context.get("anomaly_id")
    if not isinstance(anomaly_id, str) or not anomaly_id:
        raise AttentionError("ATTENTION_ANOMALY_ID_MISSING")

    source_state = f"ACTIVE:{anomaly_id}"
    snapshot = {
        "claim_id": claim.id,
        "anomaly_id": anomaly_id,
        "source_sequence_claim_id": sequence.id,
        "claim_confidence": claim.confidence,
        "support_ratio": support_value,
        "occurrence_count": occurrences,
        "last_observed_at": observed_at.isoformat(),
        "valid_until": (
            _utc(claim.valid_until).isoformat()
            if claim.valid_until is not None
            else None
        ),
    }
    return _persist_assessment(
        session,
        tenant_id=actor.tenant_id,
        signal_key=f"missing-step-anomaly:{anomaly_id}",
        source_type="MEMORY_CLAIM_ANOMALY",
        source_ref=claim.id,
        source_state=source_state,
        snapshot_payload=snapshot,
        base_components=base_components,
        sensitivity_class=claim.sensitivity_class,
        owner_private=owner_private,
        owner_non_actionable=owner_non_actionable,
        provenance={
            "source_sequence_claim_id": sequence.id,
            "anomaly_id": anomaly_id,
            "source_quality": claim.source_quality,
        },
        now=stamp,
    )


def suppress_attention_assessment(
    session: Session,
    *,
    assessment_id: str,
    decision_actor_key: str,
    decision_ref: str,
    until: datetime,
    reason_code: str = "OWNER_SUPPRESSED",
    now: datetime | None = None,
) -> AttentionAssessmentRow:
    row = session.get(AttentionAssessmentRow, assessment_id)
    if row is None:
        raise AttentionError("ATTENTION_ASSESSMENT_NOT_FOUND")
    if row.status == "SUPERSEDED":
        raise AttentionError("ATTENTION_ASSESSMENT_STALE")
    owner = _require_owner(
        session,
        tenant_id=row.tenant_id,
        actor_key=decision_actor_key,
    )
    reference = _bounded(
        decision_ref,
        code="ATTENTION_DECISION_REF",
        limit=240,
    )
    reason = _bounded(
        reason_code,
        code="ATTENTION_SUPPRESSION_REASON",
        limit=64,
    ).upper()
    stamp = _utc(now)
    suppressed_until = _utc(until)
    if (
        suppressed_until <= stamp
        or suppressed_until > stamp + MAX_OWNER_SUPPRESSION
    ):
        raise AttentionError("ATTENTION_SUPPRESSION_WINDOW_INVALID")

    row.owner_suppressed_until = suppressed_until
    row.owner_suppression_reason = reason
    row.owner_suppressed_by = owner
    row.owner_decision_ref = reference
    row.status = "SUPPRESSED"
    row.effective_class = "IGNORE"
    row.reason_codes = sorted(set([*row.reason_codes, "OWNER_SUPPRESSED"]))
    row.updated_at = stamp
    session.flush()
    return row


def acknowledge_attention_assessment(
    session: Session,
    *,
    assessment_id: str,
    decision_actor_key: str,
    decision_ref: str,
    now: datetime | None = None,
) -> AttentionAssessmentRow:
    row = session.get(AttentionAssessmentRow, assessment_id)
    if row is None:
        raise AttentionError("ATTENTION_ASSESSMENT_NOT_FOUND")
    if row.status == "ACKNOWLEDGED":
        return row
    if row.status == "SUPERSEDED":
        raise AttentionError("ATTENTION_ASSESSMENT_STALE")
    owner = _require_owner(
        session,
        tenant_id=row.tenant_id,
        actor_key=decision_actor_key,
    )
    reference = _bounded(
        decision_ref,
        code="ATTENTION_DECISION_REF",
        limit=240,
    )
    stamp = _utc(now)
    row.status = "ACKNOWLEDGED"
    row.acknowledged_at = stamp
    row.acknowledged_by = owner
    row.owner_decision_ref = reference
    row.reason_codes = sorted(set([*row.reason_codes, "OWNER_ACKNOWLEDGED"]))
    row.updated_at = stamp
    session.flush()
    return row


def build_owner_suggestion_candidate(
    session: Session,
    *,
    assessment_id: str,
    now: datetime | None = None,
) -> AttentionSuggestionCandidate | None:
    stamp = _utc(now)
    row = session.get(AttentionAssessmentRow, assessment_id)
    if row is None:
        raise AttentionError("ATTENTION_ASSESSMENT_NOT_FOUND")
    if row.sensitivity_class == "SECRET":
        return None
    if row.status != "ACTIVE":
        return None
    if (
        row.owner_suppressed_until is not None
        and _utc(row.owner_suppressed_until) > stamp
    ):
        return None
    if row.effective_class != "OWNER_SUGGESTION_CANDIDATE":
        return None

    if row.source_type == "OBLIGATION_INSTANCE":
        instance = session.get(ObligationInstanceRow, row.source_ref)
        if instance is None or instance.sensitivity_class == "SECRET":
            return None
        current_state = ":".join(
            [
                instance.state,
                instance.reconciliation_status,
                instance.uncertainty_code or "NONE",
            ]
        )
        if current_state != row.source_state:
            return None
    elif row.source_type == "MEMORY_CLAIM_ANOMALY":
        claim = session.get(MemoryClaimRow, row.source_ref)
        if (
            claim is None
            or claim.status != "ACTIVE"
            or claim.sensitivity_class == "SECRET"
            or claim_is_owner_private(session, claim=claim)
            or claim_is_owner_non_actionable(session, claim=claim)
        ):
            return None
    else:
        return None

    return AttentionSuggestionCandidate(
        assessment_id=row.id,
        tenant_id=row.tenant_id,
        signal_key=row.signal_key,
        source_type=row.source_type,
        source_ref=row.source_ref,
        score=row.score,
        reason_codes=tuple(row.reason_codes),
        generated_at=stamp,
    )


__all__ = [
    "ATTENTION_IGNORE_THRESHOLD",
    "ATTENTION_OWNER_SUGGESTION_THRESHOLD",
    "AttentionError",
    "AttentionSuggestionCandidate",
    "acknowledge_attention_assessment",
    "assess_missing_step_attention",
    "assess_obligation_attention",
    "build_owner_suggestion_candidate",
    "suppress_attention_assessment",
]
