from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import FindingOccurrenceRow, FindingRow
from attention_router.platform.findings import (
    FindingAction,
    FindingCandidate,
    FindingCategory,
    FindingInputClass,
    FindingSeverity,
    FindingStatus,
    finding_fingerprint,
    record_finding,
    resolve_finding,
    suppress_finding,
)


NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _candidate(
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
    fingerprint_version: int = 1,
    input_class: FindingInputClass = FindingInputClass.UNEXPECTED,
) -> FindingCandidate:
    return FindingCandidate(
        tenant_id=tenant_id,
        category=FindingCategory.ANOMALY,
        severity=FindingSeverity.HIGH,
        title="Dependency drift detected",
        summary="Runtime source differs from expected source.",
        component_key="api",
        reason_code="SOURCE_RUNTIME_DRIFT",
        fingerprint_version=fingerprint_version,
        normalized_scope={"dependency": "api"},
        correlation_id="correlation-fixture",
        input_class=input_class,
    )


def test_finding_dedupes_and_occurrences_remain_append_only(session) -> None:
    created = record_finding(session, _candidate(), observed_at=NOW)
    aggregated = record_finding(session, _candidate(), observed_at=NOW + timedelta(seconds=1))

    assert created.action is FindingAction.CREATED
    assert aggregated.action is FindingAction.AGGREGATED
    assert created.finding_id == aggregated.finding_id
    finding = session.get(FindingRow, created.finding_id)
    assert finding.occurrence_count == 2
    assert finding.status == FindingStatus.ACTIVE.value
    assert session.scalar(select(func.count()).select_from(FindingOccurrenceRow)) == 2


def test_expected_deny_does_not_create_finding_spam(session) -> None:
    result = record_finding(
        session,
        _candidate(input_class=FindingInputClass.EXPECTED_DENY),
        observed_at=NOW,
    )

    assert result.action is FindingAction.EXPECTED_DENY_ONLY
    assert session.scalar(select(func.count()).select_from(FindingRow)) == 0
    assert session.scalar(select(func.count()).select_from(FindingOccurrenceRow)) == 0


def test_resolved_finding_reopens_without_erasing_history(session) -> None:
    created = record_finding(session, _candidate(), observed_at=NOW)
    resolve_finding(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        finding_id=created.finding_id,
        reason="Authoritative source reconciled.",
        now=NOW + timedelta(seconds=1),
    )
    reopened = record_finding(session, _candidate(), observed_at=NOW + timedelta(seconds=2))

    finding = session.get(FindingRow, created.finding_id)
    assert reopened.action is FindingAction.REOPENED
    assert finding.status == FindingStatus.ACTIVE.value
    assert finding.resolved_at is None
    assert finding.occurrence_count == 2
    assert session.scalar(select(func.count()).select_from(FindingOccurrenceRow)) == 2


def test_suppression_is_not_resolution_and_does_not_delete_occurrences(session) -> None:
    created = record_finding(session, _candidate(), observed_at=NOW)
    suppress_finding(
        session,
        tenant_id=DEFAULT_TENANT_ID,
        finding_id=created.finding_id,
        scope="candidate-only",
        reason="Known fixture condition.",
        now=NOW + timedelta(seconds=1),
    )
    record_finding(session, _candidate(), observed_at=NOW + timedelta(seconds=2))

    finding = session.get(FindingRow, created.finding_id)
    assert finding.status == FindingStatus.SUPPRESSED.value
    assert finding.resolved_at is None
    assert finding.occurrence_count == 2


def test_fingerprint_is_tenant_and_version_scoped() -> None:
    baseline = finding_fingerprint(_candidate())
    other_tenant = finding_fingerprint(_candidate(tenant_id="other-tenant"))
    next_version = finding_fingerprint(_candidate(fingerprint_version=2))

    assert len({baseline, other_tenant, next_version}) == 3
