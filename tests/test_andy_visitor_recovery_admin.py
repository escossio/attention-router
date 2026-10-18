"""Disposable administration; protected files are operator intent, not remote auth."""

from copy import deepcopy
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import DBAPIError, OperationalError

from attention_router.config import settings
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure import human_identity_models  # noqa: F401
from attention_router.infrastructure.models import (
    ActorBindingRow, AgentBlueprintRow, AgentBlueprintVersionRow, AuditEventRow,
    AgentExecutionIntentRow, CapabilityGrantRow, OutboxMessageRow, PolicyRow, PolicyVersionRow, TenantRow,
)
from attention_router.infrastructure.db import Base
from attention_router.infrastructure.repository import ensure_policy_version, seed_policies
from scripts.forensics import andy_visitor_recovery_admin as admin


PROPOSAL = Path(__file__).resolve().parents[1] / "config/examples/andy_visitor_recovery.proposed.json"


def approved(tmp_path, snapshot, operation):
    path = tmp_path / (operation + "-" + new_id() + ".json")
    admin.write_private_artifact(path, {
        **admin.approval_scope(snapshot, operation),
        "operator_reference": "synthetic-host-operator", "approval_reference": "synthetic-review-only",
    })
    return path


@pytest.fixture()
def admin_case(session, tmp_path):
    manifest = json.loads(PROPOSAL.read_text())
    policy = session.get(PolicyRow, "desconhecido")
    plan = admin.prepare(session, manifest, tenant_id=policy.tenant_id, policy_id=policy.identifier,
                         expected_policy_version_id=policy.current_version_id)
    return plan


def observed(session, plan, tmp_path):
    result = admin.create_observe(session, plan, approval_path=approved(tmp_path, plan, "CREATE_OBSERVE"))
    session.commit()
    return result


def activated(session, snapshot, tmp_path):
    result = admin.apply(session, snapshot, approval_path=approved(tmp_path, snapshot, "ACTIVATE"))
    session.commit()
    return result


def test_prepare_observe_activate_and_rollback_preserve_history(session, admin_case, tmp_path):
    plan = admin_case
    default_before = settings.agent_decision_default_blueprint_id
    assert session.scalar(select(func.count()).select_from(AgentBlueprintRow)) == 0
    counts_before = {model.__name__: session.scalar(select(func.count()).select_from(model))
                     for model in (ActorBindingRow, CapabilityGrantRow)}
    snapshot_path = tmp_path / "snapshot.json"
    admin.write_private_artifact(snapshot_path, plan)
    assert admin.read_private_artifact(snapshot_path) == plan
    draft = observed(session, plan, tmp_path)
    bp = session.get(AgentBlueprintRow, plan["blueprint_id"])
    v1 = session.get(AgentBlueprintVersionRow, plan["draft_version_id"])
    original_spec = deepcopy(v1.spec)
    assert bp.status == "draft" and bp.current_version_id == v1.id
    assert v1.spec["autonomy"]["execution_mode"] == "OBSERVE"
    active = activated(session, draft, tmp_path)
    v2 = session.get(AgentBlueprintVersionRow, plan["active_version_id"])
    assert bp.status == "published" and bp.current_version_id == v2.id
    assert v2.spec["autonomy"]["execution_mode"] == "AUTO_ALLOWED"
    policy = session.get(PolicyRow, plan["policy_id"])
    current = session.get(PolicyVersionRow, policy.current_version_id)
    assert current.config["allowed_capabilities"] == [] and current.config["allowed_disclosures"] == []
    assert set(current.config["allowed_actions"]) == {"respond", "request_information"}
    assert active["worker_overlay"] == {admin.DEFAULT_KEY: bp.id, admin.GUARD_KEY: "true"}
    assert settings.agent_decision_default_blueprint_id == default_before
    restored = admin.rollback(session, active, approval_path=approved(tmp_path, active, "ROLLBACK"))
    session.commit()
    assert admin._policy_state(policy) == plan["before_policy"]
    assert bp.status == "draft" and bp.current_version_id == v1.id and v1.spec == original_spec
    assert v1.is_immutable and v2.is_immutable
    assert session.scalar(select(func.count()).select_from(AgentBlueprintVersionRow)) == 2
    assert restored["worker_overlay"] == plan["previous_worker_overlay"]
    for model in (ActorBindingRow, CapabilityGrantRow):
        assert session.scalar(select(func.count()).select_from(model)) == counts_before[model.__name__]
    events = session.scalars(select(AuditEventRow).where(AuditEventRow.origin == "operator_local_visitor_recovery")).all()
    assert {row.event_type for row in events} >= {
        "visitor_recovery.create_observe", "visitor_recovery.activate", "visitor_recovery.rollback",
    }


def test_stale_policy_blocks_creation_and_activation(session, admin_case, tmp_path):
    plan = admin_case
    policy = session.get(PolicyRow, plan["policy_id"])
    old_tone = policy.tone
    policy.tone = "synthetic-concurrent-edit"
    session.commit()
    with pytest.raises(ValueError, match="RECOVERY_POLICY_CAS_CONFLICT"):
        admin.create_observe(session, plan, approval_path=approved(tmp_path, plan, "CREATE_OBSERVE"))
    assert not session.get(AgentBlueprintRow, plan["blueprint_id"])
    policy.tone = old_tone
    session.commit()
    draft = observed(session, plan, tmp_path)
    policy.tone = "another-concurrent-edit"
    session.commit()
    with pytest.raises(ValueError, match="RECOVERY_POLICY_CAS_CONFLICT"):
        admin.apply(session, draft, approval_path=approved(tmp_path, draft, "ACTIVATE"))
    assert session.get(AgentBlueprintRow, plan["blueprint_id"]).status == "draft"


def test_tenant_mismatch_and_stale_expected_pointer_are_rejected(session, admin_case):
    plan = admin_case
    tenant = TenantRow(id=new_id(), slug="synthetic-admin-other", name="Synthetic other", status="ACTIVE",
                       created_at=now_utc(), updated_at=now_utc())
    session.add(tenant)
    session.commit()
    with pytest.raises(ValueError, match="RECOVERY_TENANT_MISMATCH"):
        admin.prepare(session, plan["manifest"], tenant_id=tenant.id, policy_id=plan["policy_id"],
                      expected_policy_version_id=plan["before_policy"]["current_version_id"])
    with pytest.raises(ValueError, match="RECOVERY_POLICY_CAS_CONFLICT"):
        admin.prepare(session, plan["manifest"], tenant_id=plan["tenant_id"], policy_id=plan["policy_id"],
                      expected_policy_version_id="stale-synthetic-pointer")


def test_snapshot_and_approval_are_scoped_and_private(session, admin_case, tmp_path):
    plan = admin_case
    path = approved(tmp_path, plan, "CREATE_OBSERVE")
    changed = deepcopy(plan)
    changed["tenant_id"] = "different-synthetic-tenant"
    with pytest.raises(ValueError, match="RECOVERY_SNAPSHOT_CHANGED"):
        admin.create_observe(session, changed, approval_path=path)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="RECOVERY_PRIVATE_ARTIFACT_REQUIRED"):
        admin.create_observe(session, plan, approval_path=path)
    path.chmod(0o600)
    wrong_operation = approved(tmp_path, plan, "ACTIVATE")
    with pytest.raises(ValueError, match="RECOVERY_APPROVAL_SCOPE_MISMATCH"):
        admin.create_observe(session, plan, approval_path=wrong_operation)
    with pytest.raises(FileExistsError):
        admin.write_private_artifact(path, {})
    link = tmp_path / "approval-link.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        admin.read_private_artifact(link)


def test_failed_activation_rolls_back_whole_administrative_step(session, admin_case, tmp_path):
    plan = admin_case
    draft = observed(session, plan, tmp_path)

    def unavailable(db, *_):
        if any(isinstance(row, AgentBlueprintVersionRow) and row.id == plan["active_version_id"] for row in db.new):
            raise OperationalError("synthetic version persistence unavailable", {}, RuntimeError())

    event.listen(session, "before_flush", unavailable)
    try:
        with pytest.raises(OperationalError):
            admin.apply(session, draft, approval_path=approved(tmp_path, draft, "ACTIVATE"))
    finally:
        event.remove(session, "before_flush", unavailable)
    session.expire_all()
    assert session.get(AgentBlueprintRow, plan["blueprint_id"]).status == "draft"
    assert session.get(AgentBlueprintVersionRow, plan["active_version_id"]) is None
    assert admin._policy_state(session.get(PolicyRow, plan["policy_id"])) == plan["before_policy"]


def test_activation_replay_is_rejected_without_duplicate_versions(session, admin_case, tmp_path):
    draft = observed(session, admin_case, tmp_path)
    activated(session, draft, tmp_path)
    with pytest.raises(ValueError, match="RECOVERY_POLICY_CAS_CONFLICT"):
        admin.apply(session, draft, approval_path=approved(tmp_path, draft, "ACTIVATE"))
    assert session.scalar(select(func.count()).select_from(AgentBlueprintVersionRow)) == 2


def test_rollback_does_not_overwrite_a_concurrent_policy_edit(session, admin_case, tmp_path):
    draft = observed(session, admin_case, tmp_path)
    active = activated(session, draft, tmp_path)
    policy = session.get(PolicyRow, active["policy_id"])
    policy.tone = "synthetic-change-after-activation"
    session.commit()
    with pytest.raises(ValueError, match="RECOVERY_POLICY_CAS_CONFLICT"):
        admin.rollback(session, active, approval_path=approved(tmp_path, active, "ROLLBACK"))
    assert policy.tone == "synthetic-change-after-activation"
    assert session.get(AgentBlueprintRow, active["blueprint_id"]).status == "published"


def test_rollback_refuses_a_changed_draft_version(session, admin_case, tmp_path):
    draft = observed(session, admin_case, tmp_path)
    active = activated(session, draft, tmp_path)
    version = session.get(AgentBlueprintVersionRow, draft["draft_version_id"])
    version.spec = {**version.spec, "purpose": "synthetic-corruption"}
    session.commit()
    with pytest.raises(ValueError, match="RECOVERY_DRAFT_VERSION_CHANGED"):
        admin.rollback(session, active, approval_path=approved(tmp_path, active, "ROLLBACK"))
    assert session.get(AgentBlueprintRow, active["blueprint_id"]).status == "published"


def test_rollback_refuses_an_original_policy_that_became_mutable(session, admin_case, tmp_path):
    draft = observed(session, admin_case, tmp_path)
    active = activated(session, draft, tmp_path)
    original = session.get(PolicyVersionRow, active["before_policy"]["current_version_id"])
    if session.bind.dialect.name == "postgresql":
        # PostgreSQL prevents this corruption before the administrative guard.
        # Keep the trigger active and prove the rejected write does not break rollback.
        with pytest.raises(DBAPIError, match="versioned authority semantics are immutable"):
            with session.begin_nested():
                original.is_immutable = False
                session.flush()
        session.refresh(original)
        assert original.is_immutable is True
        restored = admin.rollback(session, active, approval_path=approved(tmp_path, active, "ROLLBACK"))
        session.commit()
        assert restored["phase"] == "ROLLED_BACK"
        assert admin._policy_state(session.get(PolicyRow, active["policy_id"])) == active["before_policy"]
        return
    original.is_immutable = False
    session.commit()
    with pytest.raises(ValueError, match="RECOVERY_ORIGINAL_POLICY_CHANGED"):
        admin.rollback(session, active, approval_path=approved(tmp_path, active, "ROLLBACK"))
    assert session.get(AgentBlueprintRow, active["blueprint_id"]).status == "published"


def test_successful_steps_remain_owned_by_the_callers_outer_transaction(session, admin_case, tmp_path):
    plan = admin_case
    admin.create_observe(session, plan, approval_path=approved(tmp_path, plan, "CREATE_OBSERVE"))
    session.rollback()
    assert session.get(AgentBlueprintRow, plan["blueprint_id"]) is None
    draft = observed(session, plan, tmp_path)
    admin.apply(session, draft, approval_path=approved(tmp_path, draft, "ACTIVATE"))
    session.rollback()
    assert session.get(AgentBlueprintRow, plan["blueprint_id"]).status == "draft"
    assert session.get(AgentBlueprintVersionRow, plan["active_version_id"]) is None
    assert admin._policy_state(session.get(PolicyRow, plan["policy_id"])) == plan["before_policy"]


def test_activation_refuses_reused_retired_policy_version(session, admin_case, tmp_path):
    draft = observed(session, admin_case, tmp_path)
    policy = session.get(PolicyRow, draft["policy_id"])
    config = deepcopy(draft["before_policy_config"])
    config.update({key: value for key, value in draft["manifest"]["policy_proposal"].items() if key != "audience"})
    invalid = ensure_policy_version(session, policy, config, "synthetic-retired-version")
    invalid.status = "RETIRED"
    policy.current_version_id = draft["before_policy"]["current_version_id"]
    session.commit()
    with pytest.raises(ValueError, match="RECOVERY_TARGET_POLICY_VERSION_INVALID"):
        admin.apply(session, draft, approval_path=approved(tmp_path, draft, "ACTIVATE"))
    assert session.get(AgentBlueprintVersionRow, draft["active_version_id"]) is None
    assert session.get(AgentBlueprintRow, draft["blueprint_id"]).status == "draft"


def test_non_default_tenant_keeps_policy_version_audit_in_its_scope(session, admin_case, tmp_path):
    original = admin_case
    tenant = TenantRow(id=new_id(), slug="synthetic-approved-other", name="Synthetic approved", status="ACTIVE",
                       created_at=now_utc(), updated_at=now_utc())
    session.add(tenant)
    session.flush()
    state = deepcopy(original["before_policy"])
    state.update(identifier="synthetic-other-unknown", tenant_id=tenant.id, current_version_id=None)
    policy = PolicyRow(**state)
    session.add(policy)
    session.flush()
    config = deepcopy(original["before_policy_config"])
    config["identifier"] = policy.identifier
    ensure_policy_version(session, policy, config, "synthetic-tenant-policy")
    session.commit()
    plan = admin.prepare(session, original["manifest"], tenant_id=tenant.id, policy_id=policy.identifier,
                         expected_policy_version_id=policy.current_version_id)
    active = activated(session, observed(session, plan, tmp_path), tmp_path)
    events = session.scalars(select(AuditEventRow).where(
        AuditEventRow.policy_version_id == active["after_policy"]["current_version_id"]
    )).all()
    assert events and all(row.tenant_id == tenant.id for row in events)
    admin.rollback(session, active, approval_path=approved(tmp_path, active, "ROLLBACK"))
    session.commit()
    assert admin._policy_state(policy) == plan["before_policy"]


def test_cli_requires_explicit_database_and_target_before_creating_output(tmp_path, monkeypatch):
    monkeypatch.delenv("ANDY_RECOVERY_DATABASE_URL", raising=False)
    output = tmp_path / "never-created.json"
    with pytest.raises(SystemExit):
        admin.main(["prepare", "--request", str(tmp_path / "request.json"),
                    "--manifest", str(PROPOSAL), "--output", str(output)])
    assert not output.exists()


def test_cli_readonly_prepare_and_commit_journal_reconciliation(tmp_path, monkeypatch, capsys):
    engine = create_engine("sqlite+pysqlite:///" + str(tmp_path / "disposable.sqlite"))
    Base.metadata.create_all(engine)
    with admin.Session(engine) as db:
        seed_policies(db)
        db.commit()
        policy = db.get(PolicyRow, "desconhecido")
        request = {
            "tenant_id": policy.tenant_id, "policy_id": policy.identifier,
            "expected_policy_version_id": policy.current_version_id,
            "previous_default_blueprint_id": None, "previous_visitor_only": False,
        }
        audit_count = db.scalar(select(func.count()).select_from(AuditEventRow))
    monkeypatch.setenv("ANDY_RECOVERY_DATABASE_URL", str(engine.url))
    request_path, output = tmp_path / "request.json", tmp_path / "prepared.json"
    admin.write_private_artifact(request_path, request)
    assert admin.main(["prepare", "--request", str(request_path), "--manifest", str(PROPOSAL),
                       "--output", str(output)]) == 0
    snapshot = admin.read_private_artifact(output)
    assert snapshot["phase"] == "PREPARED" and output.stat().st_mode & 0o077 == 0
    with admin.Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(AuditEventRow)) == audit_count
        assert not db.scalar(select(AgentBlueprintRow))
    draft_path = tmp_path / "cli-draft.json"
    assert admin.main(["create-observe", "--snapshot", str(output),
                       "--approval", str(approved(tmp_path, snapshot, "CREATE_OBSERVE")),
                       "--output", str(draft_path)]) == 0
    draft = admin.read_private_artifact(draft_path)
    active_path = tmp_path / "cli-active.json"

    def disk_failure(*_):
        raise OSError("synthetic final receipt unavailable")

    with monkeypatch.context() as failure:
        failure.setattr(admin, "_finalize_private_artifact", disk_failure)
        assert admin.main(["activate", "--snapshot", str(draft_path),
                           "--approval", str(approved(tmp_path, draft, "ACTIVATE")),
                           "--output", str(active_path)]) == 1
    journal = active_path.with_name(active_path.name + ".pending")
    pending = admin.read_private_artifact(journal)
    assert pending["transaction_status"] == "UNKNOWN" and pending["proposed"]["phase"] == "ACTIVE"
    assert "commit_evidence" not in pending["proposed"]
    recovered_path = tmp_path / "cli-reconciled.json"
    assert admin.main(["reconcile", "--snapshot", str(journal), "--output", str(recovered_path)]) == 0
    recovered = admin.read_private_artifact(recovered_path)
    assert recovered["commit_evidence"] == "DATABASE_STATE_AND_AUDIT_VERIFIED"
    restored_path = tmp_path / "cli-restored.json"
    assert admin.main(["rollback", "--snapshot", str(recovered_path),
                       "--approval", str(approved(tmp_path, recovered, "ROLLBACK")),
                       "--output", str(restored_path)]) == 0
    assert admin.read_private_artifact(journal) == pending  # Never overwrite the durable journal.
    # An old journal cannot certify a different, later database state.
    assert admin.main(["reconcile", "--snapshot", str(journal),
                       "--output", str(tmp_path / "stale-reconciliation.json")]) == 1
    public = capsys.readouterr().out
    assert request["tenant_id"] not in public and request["policy_id"] not in public
    engine.dispose()


def test_administrative_package_drives_real_reply_and_rollback(session, tmp_path, monkeypatch):
    from attention_router.application import autonomy, services
    from tests.test_andy_visitor_recovery import decide, enqueue
    from tests.test_direct_conversation import direct_event, setup_direct
    from tests.test_owner_control import _LocalTransportRecorder

    captured = setup_direct(session, monkeypatch)
    monkeypatch.setattr(settings, "agent_decision_default_blueprint_id", None)
    monkeypatch.setattr(settings, "agent_decision_default_visitor_only", False)
    recorder = _LocalTransportRecorder()
    monkeypatch.setattr(services, "local_transport_outbound", recorder)
    session.commit()
    policy = session.get(PolicyRow, "desconhecido")
    plan = admin.prepare(session, json.loads(PROPOSAL.read_text()), tenant_id=policy.tenant_id,
                         policy_id=policy.identifier, expected_policy_version_id=policy.current_version_id)
    draft = observed(session, plan, tmp_path)
    active = activated(session, draft, tmp_path)
    monkeypatch.setattr(settings, "agent_decision_default_blueprint_id", active["worker_overlay"][admin.DEFAULT_KEY])
    monkeypatch.setattr(settings, "agent_decision_default_visitor_only", active["worker_overlay"][admin.GUARD_KEY] == "true")
    decision, _ = decide(session, direct_event(content="Explique luz em uma frase."))
    assert decision.agent_blueprint_id == plan["blueprint_id"] and decision.actor_binding_id is None
    outbox = enqueue(session, decision)
    services.process_outbox(session, "synthetic-admin-rehearsal")
    assert outbox.status == "DONE" and len(recorder.calls) == 1 and len(captured) == 1
    rolled = admin.rollback(session, active, approval_path=approved(tmp_path, active, "ROLLBACK"))
    session.commit()
    monkeypatch.setattr(settings, "agent_decision_default_blueprint_id", rolled["worker_overlay"][admin.DEFAULT_KEY])
    monkeypatch.setattr(settings, "agent_decision_default_visitor_only", rolled["worker_overlay"][admin.GUARD_KEY] == "true")
    after, _ = decide(session, direct_event(content="Explique uma segunda pergunta nova."))
    assert after.agent_blueprint_id is None and len(captured) == 1
    assert not autonomy.evaluate_and_route(session, after).automatic_execution_allowed
    assert session.scalar(select(func.count()).select_from(AgentExecutionIntentRow)) == 1
    assert session.scalar(select(func.count()).select_from(OutboxMessageRow)) == 1
    assert session.get(OutboxMessageRow, outbox.id).status == "DONE"
    versions = session.scalars(select(AgentBlueprintVersionRow).where(
        AgentBlueprintVersionRow.blueprint_id == plan["blueprint_id"]
    )).all()
    assert len(versions) == 2 and all(version.is_immutable for version in versions)
