from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import (
    AssertionResultRow,
    EvidenceReferenceRow,
    ScenarioDefinitionRow,
    ScenarioRunRow,
    ScenarioStepRunRow,
    ScenarioVersionRow,
)
from attention_router.platform.privacy import sanitized_summary


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


def _assertion_summary(
    rows: list[AssertionResultRow],
    *,
    evidence_by_id: dict[str, EvidenceReferenceRow],
) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for row in rows:
        reference_ids = tuple(str(item) for item in (row.evidence_reference_ids or []))
        resolved = [evidence_by_id[item] for item in reference_ids if item in evidence_by_id]
        projected.append(
            {
                "result_id": row.id,
                "assertion_id": row.assertion_id,
                "result": row.result,
                "expected_property": sanitized_summary(
                    row.expected_property,
                    maximum_length=500,
                ),
                "observed_summary": sanitized_summary(
                    row.observed_summary,
                    maximum_length=1000,
                ),
                "evaluator": row.evaluator,
                "blocking": row.blocking,
                "evaluated_at": row.evaluated_at,
                "evidence_refs": _evidence_summary(resolved),
                "unresolved_evidence_ref_count": len(reference_ids) - len(resolved),
            }
        )
    return projected


def _assertion_result_summary(rows: list[AssertionResultRow]) -> dict[str, Any]:
    statuses = Counter(row.result for row in rows)
    return {
        "total": len(rows),
        "results": dict(sorted(statuses.items())),
    }


def read_scenario_engine_snapshot(
    session: Session,
    *,
    tenant_id: str = DEFAULT_TENANT_ID,
    run_limit: int = 50,
) -> dict[str, Any]:
    """Project existing Scenario Engine state for the Capability Lab.

    This function is intentionally read-only. It does not register manifests,
    create runs, execute steps, create evidence, or infer production authority.
    Assertion projection is deliberately minimized: no provenance payload, finding,
    correlation, requester identity, or evidence metadata crosses this boundary.
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
    assertions_by_run: dict[str, list[AssertionResultRow]] = defaultdict(list)
    evidence_by_run: dict[str, list[EvidenceReferenceRow]] = defaultdict(list)
    evidence_by_id: dict[str, EvidenceReferenceRow] = {}

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

        assertion_evidence_ids: set[str] = set()
        for assertion in session.scalars(
            select(AssertionResultRow)
            .where(
                AssertionResultRow.tenant_id == tenant_id,
                AssertionResultRow.scenario_run_id.in_(run_ids),
            )
            .order_by(
                AssertionResultRow.scenario_run_id,
                AssertionResultRow.evaluated_at,
                AssertionResultRow.id,
            )
        ).all():
            assertions_by_run[assertion.scenario_run_id].append(assertion)
            assertion_evidence_ids.update(
                str(item) for item in (assertion.evidence_reference_ids or [])
            )

        for evidence in session.scalars(
            select(EvidenceReferenceRow)
            .where(
                EvidenceReferenceRow.tenant_id == tenant_id,
                EvidenceReferenceRow.internal_entity_type == "scenario_run",
                EvidenceReferenceRow.internal_entity_id.in_(run_ids),
            )
            .order_by(EvidenceReferenceRow.created_at, EvidenceReferenceRow.id)
        ).all():
            evidence_by_id[evidence.id] = evidence
            if evidence.internal_entity_id is not None:
                evidence_by_run[evidence.internal_entity_id].append(evidence)

        if assertion_evidence_ids:
            for evidence in session.scalars(
                select(EvidenceReferenceRow)
                .where(
                    EvidenceReferenceRow.tenant_id == tenant_id,
                    EvidenceReferenceRow.id.in_(sorted(assertion_evidence_ids)),
                )
                .order_by(EvidenceReferenceRow.created_at, EvidenceReferenceRow.id)
            ).all():
                evidence_by_id[evidence.id] = evidence

    projected_runs: list[dict[str, Any]] = []
    latest_run_by_definition: dict[str, dict[str, Any]] = {}
    for run in recent_runs:
        version = version_by_id.get(run.scenario_version_id)
        definition = definition_by_id.get(version.scenario_definition_id) if version else None
        run_assertions = assertions_by_run.get(run.id, [])
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
            "step_summary": _step_summary(steps_by_run.get(run.id, [])),
            "assertion_summary": _assertion_result_summary(run_assertions),
            "semantic_assertions": _assertion_summary(
                run_assertions,
                evidence_by_id=evidence_by_id,
            ),
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
