"""Governed live orchestration for already-merged cognitive engines.

V0 deliberately starts narrow: it projects an ORGANIC-only Cognitive Graph and
runs deterministic V2I relation candidates through the existing V2E
CandidateInsight boundary.  It does not invent episode semantics, confirm
identity, materialize canonical relationships, or create execution authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.application.cognitive_graph import (
    build_cognitive_graph_slice,
)
from attention_router.application.graph_intelligence import (
    default_graph_intelligence_engines,
    persist_relation_candidate_insight,
)
from attention_router.domain.graph_intelligence import GraphIntelligenceBudget
from attention_router.infrastructure.models import TenantRow


logger = logging.getLogger(__name__)

MAX_CANDIDATE_LIMIT = 64


@dataclass(frozen=True, slots=True)
class CognitiveRuntimeCycleResult:
    tenants_considered: int = 0
    tenants_succeeded: int = 0
    tenants_failed: int = 0
    relation_candidates: int = 0
    candidate_insights_created: int = 0
    candidate_insights_reused: int = 0


def _utc(value: datetime | None = None) -> datetime:
    stamp = value or datetime.now(UTC)
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def _active_tenant_ids(
    session: Session,
    *,
    tenant_limit: int,
) -> tuple[str, ...]:
    return tuple(
        session.scalars(
            select(TenantRow.id)
            .where(TenantRow.status == "ACTIVE")
            .order_by(TenantRow.id)
            .limit(tenant_limit)
        ).all()
    )


def run_cognitive_runtime_cycle(
    session: Session,
    *,
    tenant_limit: int = 50,
    graph_limit_per_kind: int = 200,
    candidate_limit: int = 24,
    now: datetime | None = None,
) -> CognitiveRuntimeCycleResult:
    """Run one bounded, replay-safe candidate-only cognitive cycle."""
    if tenant_limit < 1 or tenant_limit > 500:
        raise ValueError("COGNITIVE_RUNTIME_TENANT_LIMIT_OUT_OF_RANGE")
    if graph_limit_per_kind < 1 or graph_limit_per_kind > 1000:
        raise ValueError(
            "COGNITIVE_RUNTIME_GRAPH_LIMIT_PER_KIND_OUT_OF_RANGE"
        )
    if candidate_limit < 1 or candidate_limit > MAX_CANDIDATE_LIMIT:
        raise ValueError("COGNITIVE_RUNTIME_CANDIDATE_LIMIT_OUT_OF_RANGE")

    stamp = _utc(now)
    tenant_ids = _active_tenant_ids(
        session,
        tenant_limit=tenant_limit,
    )
    engines = default_graph_intelligence_engines()

    succeeded = 0
    failed = 0
    relation_candidates = 0
    created = 0
    reused = 0

    for tenant_id in tenant_ids:
        try:
            tenant_relation_candidates = 0
            tenant_created = 0
            tenant_reused = 0
            with session.begin_nested():
                graph = build_cognitive_graph_slice(
                    session,
                    tenant_id,
                    limit_per_kind=graph_limit_per_kind,
                    include_secret=False,
                    timeline_lineage="ORGANIC",
                    now=stamp,
                )
                candidates = engines.relation_candidates.candidates(
                    graph,
                    budget=GraphIntelligenceBudget(
                        max_candidates=candidate_limit,
                    ),
                )
                tenant_relation_candidates = len(candidates)
                for candidate in candidates:
                    _row, was_created = persist_relation_candidate_insight(
                        session,
                        graph=graph,
                        candidate=candidate,
                        now=stamp,
                    )
                    if was_created:
                        tenant_created += 1
                    else:
                        tenant_reused += 1
            relation_candidates += tenant_relation_candidates
            created += tenant_created
            reused += tenant_reused
            succeeded += 1
        except Exception:
            failed += 1
            logger.exception(
                "cognitive runtime tenant cycle failed tenant_id=%s",
                tenant_id,
            )

    return CognitiveRuntimeCycleResult(
        tenants_considered=len(tenant_ids),
        tenants_succeeded=succeeded,
        tenants_failed=failed,
        relation_candidates=relation_candidates,
        candidate_insights_created=created,
        candidate_insights_reused=reused,
    )


__all__ = [
    "CognitiveRuntimeCycleResult",
    "run_cognitive_runtime_cycle",
]
