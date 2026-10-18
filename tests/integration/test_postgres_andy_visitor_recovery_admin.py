"""Distributed PostgreSQL certification of administrative CAS and rollback."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier

import pytest
from sqlalchemy import func, select

from attention_router.infrastructure.models import AgentBlueprintRow, AgentBlueprintVersionRow, AuditEventRow, PolicyRow
from scripts.forensics import andy_visitor_recovery_admin as admin
from tests.test_andy_visitor_recovery_admin import (
    admin_case as admin_case,
    approved,
    observed,
    test_prepare_observe_activate_and_rollback_preserve_history as test_prepare_observe_activate_and_rollback_preserve_history,
    test_stale_policy_blocks_creation_and_activation as test_stale_policy_blocks_creation_and_activation,
    test_tenant_mismatch_and_stale_expected_pointer_are_rejected as test_tenant_mismatch_and_stale_expected_pointer_are_rejected,
    test_snapshot_and_approval_are_scoped_and_private as test_snapshot_and_approval_are_scoped_and_private,
    test_failed_activation_rolls_back_whole_administrative_step as test_failed_activation_rolls_back_whole_administrative_step,
    test_activation_replay_is_rejected_without_duplicate_versions as test_activation_replay_is_rejected_without_duplicate_versions,
    test_rollback_does_not_overwrite_a_concurrent_policy_edit as test_rollback_does_not_overwrite_a_concurrent_policy_edit,
    test_rollback_refuses_a_changed_draft_version as test_rollback_refuses_a_changed_draft_version,
    test_rollback_refuses_an_original_policy_that_became_mutable as test_rollback_refuses_an_original_policy_that_became_mutable,
    test_successful_steps_remain_owned_by_the_callers_outer_transaction as test_successful_steps_remain_owned_by_the_callers_outer_transaction,
    test_activation_refuses_reused_retired_policy_version as test_activation_refuses_reused_retired_policy_version,
    test_non_default_tenant_keeps_policy_version_audit_in_its_scope as test_non_default_tenant_keeps_policy_version_audit_in_its_scope,
    test_administrative_package_drives_real_reply_and_rollback as test_administrative_package_drives_real_reply_and_rollback,
)


pytestmark = pytest.mark.postgres


@pytest.fixture()
def session(Session):
    with Session() as db:
        yield db


def test_two_concurrent_activations_only_one_wins_cas(Session, session, request, tmp_path):
    draft = observed(session, request.getfixturevalue("admin_case"), tmp_path)
    approval_path = approved(tmp_path, draft, "ACTIVATE")
    barrier = Barrier(2)

    def attempt():
        with Session() as db:
            barrier.wait(timeout=10)
            try:
                admin.apply(db, draft, approval_path=approval_path)
                db.commit()
                return "ACTIVATED"
            except ValueError as exc:
                db.rollback()
                return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: attempt(), range(2)))
    assert sorted(outcomes) == ["ACTIVATED", "RECOVERY_POLICY_CAS_CONFLICT"]
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(AgentBlueprintVersionRow)) == 2


def test_operator_cli_postgres_prepare_create_activate_rollback(session, tmp_path):
    """Exact CLI and commit boundary; credentials travel only in the child environment."""
    root = Path(__file__).resolve().parents[2]
    policy = session.get(PolicyRow, "desconhecido")
    before_audit = session.scalar(select(func.count()).select_from(AuditEventRow))
    request_path = tmp_path / "target.json"
    admin.write_private_artifact(request_path, {
        "tenant_id": policy.tenant_id, "policy_id": policy.identifier,
        "expected_policy_version_id": policy.current_version_id,
        "previous_default_blueprint_id": None, "previous_visitor_only": False,
    })
    environment = os.environ.copy()
    database_url = session.bind.url.render_as_string(hide_password=False)
    environment.update(ANDY_RECOVERY_DATABASE_URL=database_url, DATABASE_URL=database_url,
                       APP_ENV="test", OPENAI_API_KEY="", OTEL_TRACING_ENABLED="false")

    def command(operation, output, *arguments):
        result = subprocess.run([
            sys.executable, "-m", "scripts.forensics.andy_visitor_recovery_admin", operation,
            *map(str, arguments), "--output", str(output),
        ], cwd=root, env=environment, capture_output=True, text=True, timeout=20)
        # Never include child stderr, DSNs or private snapshot contents in a failed assertion.
        assert result.returncode == 0, f"operator CLI {operation} failed with exit {result.returncode}"
        public = json.loads(result.stdout)
        assert public["production_recovery_proven"] is False
        assert policy.tenant_id not in result.stdout and database_url not in result.stdout
        return admin.read_private_artifact(output)

    plan = command("prepare", tmp_path / "plan.json", "--manifest",
                   root / "config/examples/andy_visitor_recovery.proposed.json", "--request", request_path)
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(AuditEventRow)) == before_audit
    assert not session.get(AgentBlueprintRow, plan["blueprint_id"])
    draft = command("create-observe", tmp_path / "draft.json", "--snapshot", tmp_path / "plan.json",
                    "--approval", approved(tmp_path, plan, "CREATE_OBSERVE"))
    session.expire_all()
    blueprint = session.get(AgentBlueprintRow, plan["blueprint_id"])
    assert blueprint.status == "draft" and blueprint.current_version_id == plan["draft_version_id"]
    active = command("activate", tmp_path / "active.json", "--snapshot", tmp_path / "draft.json",
                     "--approval", approved(tmp_path, draft, "ACTIVATE"))
    session.expire_all()
    assert blueprint.status == "published" and active["worker_overlay"][admin.GUARD_KEY] == "true"
    restored = command("rollback", tmp_path / "restored.json", "--snapshot", tmp_path / "active.json",
                       "--approval", approved(tmp_path, active, "ROLLBACK"))
    session.expire_all()
    assert admin._policy_state(policy) == plan["before_policy"]
    assert blueprint.status == "draft" and blueprint.current_version_id == plan["draft_version_id"]
    assert restored["worker_overlay"] == plan["previous_worker_overlay"]
    assert session.scalar(select(func.count()).select_from(AgentBlueprintVersionRow)) == 2


def test_cli_post_commit_receipt_failure_is_reconciled_without_replaying_mutation(session, request, tmp_path):
    root = Path(__file__).resolve().parents[2]
    draft = observed(session, request.getfixturevalue("admin_case"), tmp_path)
    source_path, output = tmp_path / "source.json", tmp_path / "failed-result.json"
    admin.write_private_artifact(source_path, draft)
    approval_path = approved(tmp_path, draft, "ACTIVATE")
    environment = os.environ.copy()
    url = session.bind.url.render_as_string(hide_password=False)
    environment.update(ANDY_RECOVERY_DATABASE_URL=url, DATABASE_URL=url,
                       APP_ENV="test", OPENAI_API_KEY="", OTEL_TRACING_ENABLED="false")
    # Test-only fault injection inside a child; the product exposes no eval/fault flag.
    fault = """
from scripts.forensics import andy_visitor_recovery_admin as command
def fail_final_receipt(*args):
    raise OSError('synthetic final receipt failure')
command._finalize_private_artifact = fail_final_receipt
raise SystemExit(command.main())
"""
    failed = subprocess.run([
        sys.executable, "-c", fault, "activate", "--snapshot", str(source_path),
        "--approval", str(approval_path), "--output", str(output),
    ], cwd=root, env=environment, capture_output=True, text=True, timeout=20)
    assert failed.returncode == 1
    public = json.loads(failed.stdout)
    assert public["database_commit_completed"] and public["reconciliation_required"]
    journal = output.with_name(output.name + ".pending")
    before = admin.read_private_artifact(journal)
    assert "commit_evidence" not in before["proposed"]
    session.expire_all()
    assert session.get(AgentBlueprintRow, draft["blueprint_id"]).status == "published"
    audit_count = session.scalar(select(func.count()).select_from(AuditEventRow))
    recovered_path = tmp_path / "reconciled.json"
    reconciled = subprocess.run([
        sys.executable, "-m", "scripts.forensics.andy_visitor_recovery_admin", "reconcile",
        "--snapshot", str(journal), "--output", str(recovered_path),
    ], cwd=root, env=environment, capture_output=True, text=True, timeout=20)
    assert reconciled.returncode == 0, "read-only CLI reconciliation failed"
    recovered = admin.read_private_artifact(recovered_path)
    assert recovered["phase"] == "ACTIVE"
    assert recovered["commit_evidence"] == "DATABASE_STATE_AND_AUDIT_VERIFIED"
    assert admin.read_private_artifact(journal) == before
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(AuditEventRow)) == audit_count
    assert session.scalar(select(func.count()).select_from(AgentBlueprintVersionRow)) == 2
    # No repeat of activate occurred: only one activation audit exists.
    assert session.scalar(select(func.count()).select_from(AuditEventRow).where(
        AuditEventRow.event_type == "visitor_recovery.activate"
    )) == 1
