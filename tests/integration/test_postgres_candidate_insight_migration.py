"""Inspect Personal Context V2E Candidate Insight schema."""

import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text


pytestmark = pytest.mark.postgres


def test_candidate_insight_migration_schema():
    engine = create_engine(
        os.environ["PUBLIC_POSTGRES_TEST_URL"],
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalars().all() == ["0063_location_context_v1"]

            schema = inspect(connection)
            assert {
                "candidate_insights",
                "candidate_insight_evidence",
            } <= set(schema.get_table_names())

            insight_uniques = {
                item["name"]: item["column_names"]
                for item in schema.get_unique_constraints("candidate_insights")
            }
            assert insight_uniques[
                "uq_candidate_insight_tenant_idempotency"
            ] == ["tenant_id", "idempotency_key"]

            evidence_uniques = {
                item["name"]: item["column_names"]
                for item in schema.get_unique_constraints(
                    "candidate_insight_evidence"
                )
            }
            assert evidence_uniques[
                "uq_candidate_insight_evidence_source"
            ] == [
                "candidate_id",
                "evidence_type",
                "source_ref",
                "evidence_role",
            ]

            insight_checks = {
                item["name"]
                for item in schema.get_check_constraints("candidate_insights")
            }
            assert {
                "ck_candidate_insight_type",
                "ck_candidate_insight_subject_type",
                "ck_candidate_insight_source_engine",
                "ck_candidate_insight_confidence",
                "ck_candidate_insight_sensitivity",
                "ck_candidate_insight_state",
                "ck_candidate_insight_temporal_order",
            } <= insight_checks

            evidence_checks = {
                item["name"]
                for item in schema.get_check_constraints(
                    "candidate_insight_evidence"
                )
            }
            assert {
                "ck_candidate_insight_evidence_type",
                "ck_candidate_insight_evidence_role",
                "ck_candidate_insight_evidence_confidence",
                "ck_candidate_insight_evidence_sensitivity",
            } <= evidence_checks
    finally:
        engine.dispose()


@pytest.fixture
def candidate_insight_migration_db(pg_url):
    name = "candidate_insight_migration_" + uuid.uuid4().hex[:12]
    admin = create_engine(
        pg_url.rsplit("/", 1)[0] + "/postgres",
        isolation_level="AUTOCOMMIT",
    )
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))

    url = pg_url.rsplit("/", 1)[0] + "/" + name
    engine = create_engine(url)

    def migrate(direction, target, check=True):
        return subprocess.run(
            [sys.executable, "-m", "alembic", direction, target],
            env={**os.environ, "DATABASE_URL": url},
            check=check,
            capture_output=True,
            text=True,
        )

    try:
        migrate("upgrade", "head")
        yield engine, migrate
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def test_candidate_insight_refuses_destructive_downgrade(
    candidate_insight_migration_db,
):
    engine, migrate = candidate_insight_migration_db
    tenant_id = "00000000-0000-4000-8000-00000000d054"

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO tenants
                    (id, slug, name, status, created_at, updated_at)
                VALUES
                    (:id, :slug, :name, 'ACTIVE', now(), now())
                """
            ),
            {
                "id": tenant_id,
                "slug": "candidate-insight-migration",
                "name": "Candidate Insight Migration",
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO candidate_insights (
                    id,
                    tenant_id,
                    semantic_key,
                    idempotency_key,
                    insight_type,
                    subject_type,
                    subject_id,
                    predicate,
                    proposed_value,
                    source_engine,
                    confidence,
                    sensitivity_class,
                    valid_from,
                    valid_until,
                    contradiction_refs,
                    state,
                    supersedes_insight_id,
                    decision_kind,
                    decision_actor_key,
                    decision_ref,
                    provenance,
                    created_at,
                    updated_at,
                    decided_at
                )
                VALUES (
                    'candidate-migration',
                    :tenant_id,
                    'candidate-semantic-key',
                    'candidate-idempotency-key',
                    'CLAIM_PROPOSAL',
                    'RESOURCE',
                    'resource-1',
                    'context.test',
                    '{"value": true}'::jsonb,
                    'RULE',
                    1.0,
                    'PRIVATE',
                    now(),
                    NULL,
                    '[]'::jsonb,
                    'PROPOSED',
                    NULL,
                    NULL,
                    NULL,
                    NULL,
                    '{}'::jsonb,
                    now(),
                    now(),
                    NULL
                )
                """
            ),
            {"tenant_id": tenant_id},
        )

    result = migrate(
        "downgrade",
        "0053_semantic_episode_v0",
        check=False,
    )
    assert result.returncode != 0
    assert "CANDIDATE_INSIGHT_DOWNGRADE_REQUIRES_DATA_EXPORT" in result.stderr

    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT version_num FROM alembic_version")
        ) == "0063_location_context_v1"
        assert connection.scalar(
            text("SELECT count(*) FROM candidate_insights")
        ) == 1


def test_language_candidate_lifecycle_on_disposable_postgres(Session,tmp_path):
    from tests.test_candidate_insights import (
        test_language_v2e_review_graph_and_deletion,
        test_structured_event_evolution_multiple_authors_and_contradictions,
    )
    with Session() as session:
        test_language_v2e_review_graph_and_deletion(session,tmp_path)
        session.rollback()
    with Session() as session:
        test_structured_event_evolution_multiple_authors_and_contradictions(session,tmp_path)
        session.rollback()


def test_governed_memory_restore_on_disposable_postgres(Session, tmp_path):
    from tests.test_candidate_insights import test_governed_lifecycle_restore_and_export
    with Session() as session:
        test_governed_lifecycle_restore_and_export(session,tmp_path)


def test_governed_revocation_race_on_disposable_postgres(Session, tmp_path, monkeypatch):
    from tests.test_candidate_insights import test_governed_concurrent_revocation_linearizes_after_commit
    with Session() as session:
        test_governed_concurrent_revocation_linearizes_after_commit(session, tmp_path, monkeypatch)


def test_governed_capture_pause_on_disposable_postgres(Session, tmp_path):
    from tests.test_candidate_insights import test_governed_pause_prevents_archive_capture_and_replay
    with Session() as session:
        test_governed_pause_prevents_archive_capture_and_replay(session, tmp_path)


def test_precert_review_and_anchor_on_disposable_postgres(Session, tmp_path, monkeypatch):
    from tests.test_semantic_precertification import (
        test_anchor_recovers_only_exact_prepared_payload,
        test_preview_api_review_and_export_synthetic,
    )
    for index, test in enumerate((test_anchor_recovers_only_exact_prepared_payload,
                                test_preview_api_review_and_export_synthetic)):
        work = tmp_path/str(index)
        work.mkdir()
        with Session() as session:
            if index == 0:
                test(session, work, monkeypatch)
            else:
                test(session, work)
