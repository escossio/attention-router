from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest
from sqlalchemy import func, select

from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import FindingOccurrenceRow, FindingRow
from attention_router.platform.findings import (
    FindingAction,
    FindingCandidate,
    FindingCategory,
    FindingSeverity,
    record_finding,
)


pytestmark = pytest.mark.postgres


def test_postgres_finding_fingerprint_upsert_is_atomic(Session) -> None:
    barrier = Barrier(2)
    candidate = FindingCandidate(
        tenant_id=DEFAULT_TENANT_ID,
        category=FindingCategory.ANOMALY,
        severity=FindingSeverity.HIGH,
        title="Concurrent operational signature",
        summary="Two workers observed the same sanitized signature.",
        component_key="worker",
        reason_code="CONCURRENT_SIGNATURE",
        normalized_scope={"scope": "postgres-concurrency-fixture"},
        provenance="postgres-test:v1",
    )

    def record() -> tuple[FindingAction, str | None]:
        barrier.wait()
        with Session() as session, session.begin():
            result = record_finding(session, candidate, observed_at=datetime.now(UTC))
            return result.action, result.finding_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: record(), range(2)))

    finding_ids = {finding_id for _, finding_id in results}
    assert len(finding_ids) == 1
    assert {action for action, _ in results} == {
        FindingAction.CREATED,
        FindingAction.AGGREGATED,
    }
    with Session() as session:
        finding = session.scalar(
            select(FindingRow).where(FindingRow.id == next(iter(finding_ids)))
        )
        assert finding.occurrence_count == 2
        assert session.scalar(select(func.count()).select_from(FindingOccurrenceRow)) == 2
