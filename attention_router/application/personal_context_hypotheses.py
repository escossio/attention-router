    hypothesis_id: str,
) -> list[MemoryClaimRow]:
    rows = session.scalars(
        select(MemoryClaimRow)
        .where(
            MemoryClaimRow.subject_actor_id == actor_id,
            MemoryClaimRow.predicate == PATTERN_CLAIM_PREDICATE,
            MemoryClaimRow.status == "ACTIVE",
            MemoryClaimRow.source_quality == PATTERN_CLAIM_SOURCE_QUALITY,
        )
        .order_by(MemoryClaimRow.updated_at.desc(), MemoryClaimRow.id.desc())
    ).all()
    return [
        row
        for row in rows
        if (row.context or {}).get("hypothesis_id") == hypothesis_id
    ]


def persist_context_pattern_hypothesis(
    session: Session,
    *,
    hypothesis: ContextPatternHypothesis,
    now: datetime | None = None,
) -> tuple[MemoryClaimRow, bool]:
    """Persist one governed pattern hypothesis into Personal Context.

    The claim remains explicitly inferred and non-authoritative. Replaying the
    exact same hypothesis snapshot is idempotent. A changed snapshot supersedes
    the previous active claim while preserving history.
    """

    stamp = _utc(now or datetime.now(UTC))
    _assert_admissible(hypothesis, now=stamp)
    evidence = _validated_timeline_evidence(
        session,
        hypothesis=hypothesis,
    )
    actor = _memory_actor(
        session,
        tenant_id=hypothesis.tenant_id,
        actor_key=hypothesis.actor_id,
    )
    corrections = session.scalars(
        select(MemoryClaimRow)
        .where(
            MemoryClaimRow.subject_actor_id == actor.id,
            MemoryClaimRow.predicate == "context.pattern.owner_correction",
            MemoryClaimRow.source_quality == "USER_DECLARED",
            MemoryClaimRow.status == "ACTIVE",
        )
        .order_by(MemoryClaimRow.updated_at.desc(), MemoryClaimRow.id.desc())
    ).all()
    active_corrections = [
        row
        for row in corrections
        if (row.context or {}).get("hypothesis_id") == hypothesis.hypothesis_id
        and row.valid_until is not None
        and _utc(row.valid_until) > stamp
    ]
    if len(active_corrections) > 1:
        raise ContextHypothesisPersistenceError(
            "PATTERN_CORRECTION_ACTIVE_CONFLICT"
        )
    requalifying_correction: MemoryClaimRow | None = None
    if active_corrections:
        correction = active_corrections[0]
        correction_at = _utc(
            correction.valid_from
            or correction.first_observed_at
            or stamp
        )
        post_correction_evidence = sum(
            _utc(row.occurred_at) > correction_at
            for row in evidence
        )
        if post_correction_evidence < 3:
            raise ContextHypothesisPersistenceError(
                "PATTERN_HYPOTHESIS_SUPPRESSED_BY_OWNER_CORRECTION"
            )
        requalifying_correction = correction

    active = _active_same_hypothesis(
        session,
        actor_id=actor.id,
        hypothesis_id=hypothesis.hypothesis_id,
    )
    if len(active) > 1:
        raise ContextHypothesisPersistenceError(
            "PATTERN_HYPOTHESIS_ACTIVE_CONFLICT"
        )

    fingerprint = _snapshot_fingerprint(hypothesis)
    previous = active[0] if active else None
    if (
        previous is not None
        and (previous.context or {}).get("snapshot_fingerprint")
        == fingerprint
    ):
        return previous, False

    if previous is not None:
        previous.status = "SUPERSEDED"
        replacement_time = _utc(hypothesis.last_observed_at)
        previous.valid_until = (
            min(_utc(previous.valid_until), replacement_time)
            if previous.valid_until is not None
            else replacement_time
        )
        previous.updated_at = now_utc()

    if requalifying_correction is not None:
        requalifying_correction.status = "SUPERSEDED"
        requalifying_correction.valid_until = min(
            _utc(requalifying_correction.valid_until),
            stamp,
        )
        requalifying_correction.updated_at = now_utc()

    claim = MemoryClaimRow(
        id=new_id(),
        subject_actor_id=actor.id,
        subject_entity_id=None,
        predicate=PATTERN_CLAIM_PREDICATE,
        object_type="JSON",
        object_text=None,
        object_actor_id=None,
        object_entity_id=None,
        object_json={
            "pattern_type": hypothesis.pattern_type,
            "event_type": hypothesis.event_type,
            "signature_kind": hypothesis.signature_kind,
            "signature_value": hypothesis.signature_value,
            "cadence_seconds": hypothesis.cadence_seconds,
            "evidence_class": "INFERRED",
            "hypothesis_status": "HYPOTHESIS",
            "grants_authority": False,
            "recommendation_ready": False,
        },
        context={
            "hypothesis_id": hypothesis.hypothesis_id,
            "snapshot_fingerprint": fingerprint,
            "evidence_timeline_event_ids": [
                row.id for row in evidence
            ],
            "source_provenance": list(hypothesis.source_provenance),
            "occurrence_count": hypothesis.occurrence_count,
            "anomaly_count": hypothesis.anomaly_count,
            "support_ratio": hypothesis.support_ratio,
        },
        confidence=hypothesis.confidence,
        sensitivity_class="PRIVATE",
        source_quality=PATTERN_CLAIM_SOURCE_QUALITY,
        valid_from=_utc(hypothesis.first_observed_at),
        valid_until=_utc(hypothesis.valid_until),
        status="ACTIVE",
        staleness_class="PERISHABLE",
        supersedes_claim_id=previous.id if previous is not None else None,
        conflict_group_id=None,
        first_observed_at=_utc(hypothesis.first_observed_at),
        last_observed_at=_utc(hypothesis.last_observed_at),
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    session.add(claim)
    session.flush()
    return claim, True


__all__ = [
    "ContextHypothesisPersistenceError",
    "MIN_PERSISTED_PATTERN_CONFIDENCE",
    "MIN_PERSISTED_PATTERN_SUPPORT_RATIO",
    "PATTERN_CLAIM_PREDICATE",
    "PATTERN_CLAIM_SOURCE_QUALITY",