from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import (
    EvidenceReferenceRow,
    ScenarioDefinitionRow,
    ScenarioRunRow,
    ScenarioStepRunRow,
    ScenarioVersionRow,
)


CAPABILITY_LAB_AUTHORITY = "OBSERVATION_ONLY"


def _step_summary(steps: list[ScenarioStepRunRow]) -> dict[str, Any]:
    statuses = Counter(step.status for step in steps)
    return {
        "total": len(steps),
        "statuses": dict(sorted(statuses.items())),
    }


def _evidence_summary(rows: list[EvidenceReferenceRow]) -> list[dict[str, Any]]:
    return [
        {
            "id": row.id,
            "evidence_type": row.evidence_type,
            "source_sha": row.source_sha,
            "created_at": row.created_at,
        }
        for row in rows
    ]


def read_scenario_engine_snapshot(
    session: Session,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
    run_limit: int = 50,
) -> dict[str, Any]:
    """Project existing Scenario Engine state for the Capability Lab.

    This function is intentionally read-only. It does not register manifests,
    create runs, execute steps, create evidence, or infer production authority.
    """

    if run_limit < 1 or run_limit > 200:
        raise ValueError("CAPABILITY_LAB_RUN_LIMIT_OUT_OF_RANGE")

    definitions = list(
        session.scalars(
            select(ScenarioDefinitionRow)
            .where(ScenarioDefinitionRow.tenant_id == tenant_id)
            .order_by(ScenarioDefinitionRow.scenario_key)
        ).all()
    )
    versions = list(
        session.scalars(
            select(ScenarioVersionRow).where(ScenarioVersionRow.tenant_id == tenant_id)
        ).all()
    )
    recent_runs = list(
        session.scalars(
            select(ScenarioRunRow)
            .where(ScenarioRunRow.tenant_id == tenant_id)
            .order_by(ScenarioRunRow.created_at.desc(), ScenarioRunRow.id.desc())
            .limit(run_limit)
        ).all()
    )

    definition_by_id = {row.id: row for row in definitions}
    version_by_id = {row.id: row for row in versions}
    latest_version_by_definition: dict[str, ScenarioVersionRow] = {}
    for version in versions:
        current = latest_version_by_definition.get(version.scenario_definition_id)
        if current is None or version.version > current.version:
            latest_version_by_definition[version.scenario_definition_id] = version

    run_ids = [row.id for row in recent_runs]
    steps_by_run: dict[str, list[ScenarioStepRunRow]] = defaultdict(list)
    evidence_by_run: dict[str, list[EvidenceReferenceRow]] = defaultdict(list)

    if run_ids:
        for step in session.scalars(
            select(ScenarioStepRunRow)
            .where(
                ScenarioStepRunRow.tenant_id == tenant_id,
                ScenarioStepRunRow.scenario_run_id.in_(run_ids),
            )
            .order_by(ScenarioStepRunRow.scenario_run_id, ScenarioStepRunRow.step_order)
        ).all():
            steps_by_run[step.scenario_run_id].append(step)

        for evidence in session.scalars(
            select(EvidenceReferenceRow)
            .where(
                EvidenceReferenceRow.tenant_id == tenant_id,
                EvidenceReferenceRow.internal_entity_type == "scenario_run",
                EvidenceReferenceRow.internal_entity_id.in_(run_ids),
            )
            .order_by(EvidenceReferenceRow.created_at, EvidenceReferenceRow.id)
        ).all():
            if evidence.internal_entity_id is not None:
                evidence_by_run[evidence.internal_entity_id].append(evidence)

    projected_runs: list[dict[str, Any]] = []
    latest_run_by_definition: dict[str, dict[str, Any]] = {}
    for run in recent_runs:
        version = version_by_id.get(run.scenario_version_id)
        definition = definition_by_id.get(version.scenario_definition_id) if version else None
        projected = {
            "run_id": run.id,
            "scenario_key": definition.scenario_key if definition else None,
            "scenario_version": version.version if version else None,
            "status": run.status,
            "cleanup_state": run.cleanup_state,
            "source_sha": run.source_sha,
            "runtime_sha": run.runtime_sha,
            "schema_revision": run.schema_revision,
            "created_at": run.created_at,
            "completed_at": run.completed_at,
            "terminal_reason": run.terminal_reason,
            "correlation_id": run.root_correlation_id,
            "step_summary": _step_summary(steps_by_run.get(run.id, [])),
            "evidence_refs": _evidence_summary(evidence_by_run.get(run.id, [])),
        }
        projected_runs.append(projected)
        if definition is not None and definition.id not in latest_run_by_definition:
            latest_run_by_definition[definition.id] = projected

    projected_scenarios = []
    for definition in definitions:
        latest_version = latest_version_by_definition.get(definition.id)
        projected_scenarios.append(
            {
                "scenario_key": definition.scenario_key,
                "title": definition.title,
                "enabled": definition.enabled,
                "latest_version": (
                    {
                        "version": latest_version.version,
                        "source_sha": latest_version.source_sha,
                        "content_hash": latest_version.content_hash,
                        "risk_classification": latest_version.risk_classification,
                    }
                    if latest_version is not None
                    else None
                ),
                "latest_run": latest_run_by_definition.get(definition.id),
            }
        )

    return {
        "read_only": True,
        "authority": CAPABILITY_LAB_AUTHORITY,
        "tenant_id": tenant_id,
        "registered_scenario_count": len(definitions),
        "recent_run_count": len(projected_runs),
        "scenarios": projected_scenarios,
        "recent_runs": projected_runs,
    }
