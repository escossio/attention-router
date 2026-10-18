"""Operator-local, transactional administration of the reviewed visitor package.

There is no HTTP endpoint or remote grant. A protected approval artifact binds
an already authenticated host operator's instruction to an exact snapshot and
operation. It is not independent proof of human identity or GitHub approval.
The operator must obtain that approval through the deployment procedure first.

prepare() is read-only. Every mutation requires a fresh scoped artifact and is
atomic in a SAVEPOINT; the caller commits or rolls back the surrounding session.
The returned Worker overlay is data, never an environment/settings mutation.
Only disposable fixtures are authorized during pre-production certification.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import stat

from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from attention_router.domain.agent_builder import AgentBlueprintSpec
from attention_router.domain.models import new_id, now_utc
from attention_router.domain.policies import resolve_policy
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    AgentBlueprintRow, AgentBlueprintVersionRow, AuditEventRow, PolicyRow, PolicyVersionRow, TenantRow,
)
from attention_router.infrastructure.repository import audit, ensure_policy_version, list_policies
from scripts.forensics.andy_visitor_recovery_preflight import preflight_manifest, simulation_spec


SCHEMA = "andy-visitor-recovery-admin-v1"
APPROVAL_SCHEMA = "andy-visitor-recovery-local-approval-v1"
DEFAULT_KEY = "AGENT_DECISION_DEFAULT_BLUEPRINT_ID"
GUARD_KEY = "AGENT_DECISION_DEFAULT_VISITOR_ONLY"


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError(code)


def _seal(value: dict) -> dict:
    result = deepcopy(value)
    result.pop("snapshot_sha256", None)
    result["snapshot_sha256"] = stable_hash(result)
    return result


def _transition(snapshot: dict, **changes) -> dict:
    result = {**snapshot, **changes}
    # An acknowledged earlier phase is not evidence that this new phase committed.
    result.pop("commit_evidence", None)
    return _seal(result)


def _verify(snapshot: dict, phase: str) -> None:
    _require(type(snapshot) is dict and snapshot.get("schema_version") == SCHEMA,
             "RECOVERY_SNAPSHOT_INVALID")
    _require(snapshot.get("phase") == phase, "RECOVERY_PHASE_INVALID")
    _require(snapshot.get("snapshot_sha256") == _seal(snapshot)["snapshot_sha256"],
             "RECOVERY_SNAPSHOT_CHANGED")
    check = preflight_manifest(snapshot["manifest"], tenant_approved=True, version_valid=True)
    _require(check["manifest_sha256"] == snapshot["manifest_sha256"], "RECOVERY_MANIFEST_CHANGED")


def write_private_artifact(path: Path, value: dict) -> None:
    """Create a new private snapshot/approval; never replace an existing file."""
    directory = path.parent.stat()
    _require(directory.st_uid == os.geteuid() and directory.st_mode & 0o022 == 0,
             "RECOVERY_PRIVATE_DIRECTORY_REQUIRED")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def read_private_artifact(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        _require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                 and info.st_mode & 0o077 == 0 and info.st_size <= 1024 * 1024,
                 "RECOVERY_PRIVATE_ARTIFACT_REQUIRED")
        value = json.load(stream)
    _require(type(value) is dict, "RECOVERY_ARTIFACT_INVALID")
    return value


def approval_scope(snapshot: dict, operation: str) -> dict:
    """Fields to review; the host operator adds their two explicit references."""
    return {
        "schema_version": APPROVAL_SCHEMA, "operation": operation,
        "snapshot_sha256": snapshot["snapshot_sha256"],
        "tenant_id": snapshot["tenant_id"], "manifest_sha256": snapshot["manifest_sha256"],
    }


def _authorize(snapshot: dict, operation: str, path: Path) -> dict:
    value = read_private_artifact(path)
    expected = approval_scope(snapshot, operation)
    _require(set(value) == set(expected) | {"operator_reference", "approval_reference"},
             "RECOVERY_APPROVAL_FIELDS_INVALID")
    _require(all(value.get(key) == item for key, item in expected.items()),
             "RECOVERY_APPROVAL_SCOPE_MISMATCH")
    _require(all(type(value[key]) is str and 0 < len(value[key].strip()) <= 240
                 for key in ("operator_reference", "approval_reference")),
             "RECOVERY_APPROVAL_REFERENCE_REQUIRED")
    return value


def _policy_state(row: PolicyRow) -> dict:
    return {column.name: deepcopy(getattr(row, column.name)) for column in row.__table__.columns}


def _policy(session: Session, tenant_id: str, policy_id: str, *, lock: bool = False):
    # Consistent lock order for all administrative phases: Tenant -> Policy -> Blueprint.
    query = select(TenantRow).where(TenantRow.id == tenant_id)
    tenant = session.scalar((query.with_for_update() if lock else query).execution_options(populate_existing=True))
    _require(tenant is not None and tenant.status == "ACTIVE", "RECOVERY_TENANT_NOT_ACTIVE")
    query = select(PolicyRow).where(PolicyRow.identifier == policy_id)
    row = session.scalar((query.with_for_update() if lock else query).execution_options(populate_existing=True))
    _require(row is not None and row.tenant_id == tenant_id, "RECOVERY_TENANT_MISMATCH")
    _require(row.is_active and row.current_version_id is not None, "RECOVERY_POLICY_NOT_ACTIVE")
    version = session.get(PolicyVersionRow, row.current_version_id, populate_existing=True)
    _require(version is not None and version.policy_id == row.identifier and version.status == "ACTIVE"
             and version.is_immutable and version.checksum == stable_hash(version.config),
             "RECOVERY_POLICY_VERSION_INVALID")
    selected = resolve_policy(list_policies(session, tenant_id), "visitor-recovery-preflight", "unknown", None,
                              audience="unknown", binding_id=None)
    _require(selected.winner.identifier == row.identifier, "RECOVERY_POLICY_NOT_UNKNOWN_ROUTE")
    return row, version


def _clean(session: Session) -> None:
    _require(not session.new and not session.dirty and not session.deleted, "RECOVERY_CLEAN_SESSION_REQUIRED")


def _savepoint(session: Session):
    # sqlite3 legacy transaction control does not BEGIN on SELECT/SAVEPOINT.
    # Without this, releasing the first savepoint commits behind Session.rollback.
    if session.bind.dialect.name == "sqlite":
        connection = session.connection()
        driver = connection.connection.driver_connection
        _require(hasattr(driver, "in_transaction"), "RECOVERY_SQLITE_TRANSACTION_UNPROVEN")
        if not driver.in_transaction:
            connection.exec_driver_sql("BEGIN")
    return session.begin_nested()


def prepare(session: Session, manifest: dict, *, tenant_id: str, policy_id: str,
            expected_policy_version_id: str, previous_default_blueprint_id: str | None = None,
            previous_visitor_only: bool = False) -> dict:
    """Read the exact target and prepare the snapshot for operator review."""
    _clean(session)
    reviewed = preflight_manifest(manifest, tenant_approved=True, version_valid=True)
    row, version = _policy(session, tenant_id, policy_id)
    _require(row.current_version_id == expected_policy_version_id, "RECOVERY_POLICY_CAS_CONFLICT")
    _require(type(previous_visitor_only) is bool, "RECOVERY_PREVIOUS_GUARD_INVALID")
    _require(previous_default_blueprint_id is None or type(previous_default_blueprint_id) is str,
             "RECOVERY_PREVIOUS_DEFAULT_INVALID")
    return _seal({
        "schema_version": SCHEMA, "phase": "PREPARED", "plan_id": new_id(),
        "tenant_id": tenant_id, "policy_id": policy_id, "manifest": deepcopy(manifest),
        "manifest_sha256": reviewed["manifest_sha256"],
        "before_policy": _policy_state(row), "before_policy_config": deepcopy(version.config),
        "before_policy_checksum": version.checksum,
        "blueprint_id": new_id(), "draft_version_id": new_id(), "active_version_id": new_id(),
        "previous_worker_overlay": {
            DEFAULT_KEY: previous_default_blueprint_id, GUARD_KEY: "true" if previous_visitor_only else "false",
        },
    })


def _cas_policy(session: Session, snapshot: dict, expected: dict):
    row, version = _policy(session, snapshot["tenant_id"], snapshot["policy_id"], lock=True)
    _require(_policy_state(row) == expected, "RECOVERY_POLICY_CAS_CONFLICT")
    return row, version


def _cas_blueprint(session: Session, snapshot: dict, *, active: bool, lock: bool = True):
    query = select(AgentBlueprintRow).where(
        AgentBlueprintRow.id == snapshot["blueprint_id"]
    )
    blueprint = session.scalar((query.with_for_update() if lock else query).execution_options(populate_existing=True))
    expected_id = snapshot["active_version_id"] if active else snapshot["draft_version_id"]
    _require(blueprint is not None and blueprint.tenant_id == snapshot["tenant_id"],
             "RECOVERY_BLUEPRINT_TENANT_MISMATCH")
    _require(blueprint.current_version_id == expected_id
             and blueprint.status == ("published" if active else "draft"),
             "RECOVERY_BLUEPRINT_CAS_CONFLICT")
    version = session.get(AgentBlueprintVersionRow, expected_id, populate_existing=True)
    spec = simulation_spec(snapshot["manifest"]) if active else snapshot["manifest"]["creation_spec"]
    _require(version is not None and version.blueprint_id == blueprint.id and version.is_immutable
             and version.version == (2 if active else 1)
             and version.checksum == stable_hash(version.spec) == stable_hash(spec),
             "RECOVERY_BLUEPRINT_VERSION_CHANGED")
    return blueprint


def _audit(session: Session, snapshot: dict, operation: str, approval: dict) -> dict:
    payload = {
        "administrative_operation_id": new_id(),
        "plan_id": snapshot["plan_id"], "snapshot_sha256": snapshot["snapshot_sha256"],
        "manifest_sha256": snapshot["manifest_sha256"],
        "approval_reference_sha256": stable_hash(approval),
    }
    event_type = "visitor_recovery." + operation.lower()
    audit(session, None, event_type, payload,
          tenant_id=snapshot["tenant_id"], origin="operator_local_visitor_recovery")
    return {"event_type": event_type, "payload": payload}


def create_observe(session: Session, snapshot: dict, *, approval_path: Path) -> dict:
    _verify(snapshot, "PREPARED")
    approval = _authorize(snapshot, "CREATE_OBSERVE", approval_path)
    _clean(session)
    with _savepoint(session):
        _cas_policy(session, snapshot, snapshot["before_policy"])
        _require(session.get(AgentBlueprintRow, snapshot["blueprint_id"]) is None,
                 "RECOVERY_BLUEPRINT_ALREADY_EXISTS")
        spec = AgentBlueprintSpec.model_validate(snapshot["manifest"]["creation_spec"]).model_dump(mode="json")
        stamp = now_utc()
        session.add(AgentBlueprintRow(id=snapshot["blueprint_id"], tenant_id=snapshot["tenant_id"],
            name=snapshot["manifest"]["name"], status="draft", current_version_id=None,
            created_at=stamp, updated_at=stamp))
        session.flush()
        session.add(AgentBlueprintVersionRow(id=snapshot["draft_version_id"], blueprint_id=snapshot["blueprint_id"],
            version=1, spec=spec, checksum=stable_hash(spec), created_at=stamp,
            created_by="operator_local_visitor_recovery", change_reason="approved OBSERVE creation", is_immutable=True))
        session.flush()
        session.get(AgentBlueprintRow, snapshot["blueprint_id"]).current_version_id = snapshot["draft_version_id"]
        operation_audit = _audit(session, snapshot, "CREATE_OBSERVE", approval)
        session.flush()
    return _transition(snapshot, phase="OBSERVE", operation_audit=operation_audit)


def apply(session: Session, snapshot: dict, *, approval_path: Path) -> dict:
    """Activate a new immutable version and policy, returning only an overlay proposal."""
    _verify(snapshot, "OBSERVE")
    approval = _authorize(snapshot, "ACTIVATE", approval_path)
    _clean(session)
    with _savepoint(session):
        row, version = _cas_policy(session, snapshot, snapshot["before_policy"])
        _require(version.checksum == snapshot["before_policy_checksum"], "RECOVERY_POLICY_VERSION_CHANGED")
        blueprint = _cas_blueprint(session, snapshot, active=False)
        spec = simulation_spec(snapshot["manifest"])
        stamp = now_utc()
        session.add(AgentBlueprintVersionRow(id=snapshot["active_version_id"], blueprint_id=blueprint.id,
            version=2, spec=spec, checksum=stable_hash(spec), created_at=stamp,
            created_by="operator_local_visitor_recovery", change_reason="explicit approved AUTO_ALLOWED activation",
            is_immutable=True))
        blueprint.current_version_id = snapshot["active_version_id"]
        blueprint.status, blueprint.updated_at = "published", stamp
        config = deepcopy(snapshot["before_policy_config"])
        config.update({key: deepcopy(value) for key, value in snapshot["manifest"]["policy_proposal"].items()
                       if key != "audience"})
        policy_version = ensure_policy_version(session, row, config, "operator_local_visitor_recovery")
        _require(policy_version.policy_id == row.identifier and policy_version.status == "ACTIVE"
                 and policy_version.is_immutable
                 and policy_version.checksum == stable_hash(policy_version.config) == stable_hash(config),
                 "RECOVERY_TARGET_POLICY_VERSION_INVALID")
        row.current_version_id = policy_version.id
        row.allowed_actions, row.allowed_disclosures = config["allowed_actions"], config["allowed_disclosures"]
        operation_audit = _audit(session, snapshot, "ACTIVATE", approval)
        session.flush()
        after = _policy_state(row)
    return _transition(snapshot, phase="ACTIVE", after_policy=after,
                       after_policy_checksum=policy_version.checksum,
                       operation_audit=operation_audit,
                       worker_overlay={DEFAULT_KEY: snapshot["blueprint_id"], GUARD_KEY: "true"})


def rollback(session: Session, receipt: dict, *, approval_path: Path) -> dict:
    """Restore selection/policy; preserve immutable versions and audit evidence."""
    _verify(receipt, "ACTIVE")
    approval = _authorize(receipt, "ROLLBACK", approval_path)
    _clean(session)
    with _savepoint(session):
        row, version = _cas_policy(session, receipt, receipt["after_policy"])
        _require(version.checksum == receipt["after_policy_checksum"], "RECOVERY_POLICY_VERSION_CHANGED")
        blueprint = _cas_blueprint(session, receipt, active=True)
        draft = session.get(AgentBlueprintVersionRow, receipt["draft_version_id"], populate_existing=True)
        _require(draft is not None and draft.blueprint_id == blueprint.id and draft.is_immutable and draft.version == 1
                 and draft.checksum == stable_hash(draft.spec) == stable_hash(receipt["manifest"]["creation_spec"]),
                 "RECOVERY_DRAFT_VERSION_CHANGED")
        original = session.get(PolicyVersionRow, receipt["before_policy"]["current_version_id"], populate_existing=True)
        _require(original is not None and original.policy_id == row.identifier and original.status == "ACTIVE"
                 and original.is_immutable
                 and original.checksum == stable_hash(original.config) == receipt["before_policy_checksum"],
                 "RECOVERY_ORIGINAL_POLICY_CHANGED")
        row.current_version_id = original.id
        row.allowed_actions = deepcopy(receipt["before_policy"]["allowed_actions"])
        row.allowed_disclosures = deepcopy(receipt["before_policy"]["allowed_disclosures"])
        blueprint.current_version_id, blueprint.status = receipt["draft_version_id"], "draft"
        blueprint.updated_at = now_utc()
        operation_audit = _audit(session, receipt, "ROLLBACK", approval)
        session.flush()
    return _transition(receipt, phase="ROLLED_BACK", operation_audit=operation_audit,
                       worker_overlay=deepcopy(receipt["previous_worker_overlay"]))


def reconcile(session: Session, pending: dict) -> dict:
    """Read-only proof of a journaled result; never repeat or compensate its mutation."""
    _clean(session)
    _require(pending.get("schema_version") == SCHEMA + "-pending"
             and pending.get("phase") == "PENDING" and pending.get("transaction_status") == "UNKNOWN"
             and pending.get("snapshot_sha256") == _seal(pending)["snapshot_sha256"],
             "RECOVERY_PENDING_INVALID")
    result = pending["proposed"]
    operation_phase = {"create-observe": "OBSERVE", "activate": "ACTIVE", "rollback": "ROLLED_BACK"}
    phase = operation_phase.get(pending.get("operation"))
    _require(phase is not None and result.get("phase") == phase, "RECOVERY_PENDING_INVALID")
    _verify(result, phase)
    expected = result["after_policy"] if phase == "ACTIVE" else result["before_policy"]
    row, policy_version = _policy(session, result["tenant_id"], result["policy_id"])
    _require(_policy_state(row) == expected, "RECOVERY_COMMIT_NOT_PROVEN")
    expected_checksum = result["after_policy_checksum"] if phase == "ACTIVE" else result["before_policy_checksum"]
    _require(policy_version.checksum == expected_checksum, "RECOVERY_COMMIT_NOT_PROVEN")
    _cas_blueprint(session, result, active=phase == "ACTIVE", lock=False)
    draft = session.get(AgentBlueprintVersionRow, result["draft_version_id"], populate_existing=True)
    _require(draft is not None and draft.blueprint_id == result["blueprint_id"] and draft.version == 1
             and draft.is_immutable and draft.checksum == stable_hash(draft.spec) == stable_hash(result["manifest"]["creation_spec"]),
             "RECOVERY_COMMIT_NOT_PROVEN")
    v2 = session.get(AgentBlueprintVersionRow, result["active_version_id"], populate_existing=True)
    if phase == "OBSERVE":
        _require(v2 is None, "RECOVERY_COMMIT_NOT_PROVEN")
    else:
        _require(v2 is not None and v2.blueprint_id == result["blueprint_id"] and v2.version == 2
                 and v2.is_immutable and v2.checksum == stable_hash(v2.spec) == stable_hash(simulation_spec(result["manifest"])),
                 "RECOVERY_COMMIT_NOT_PROVEN")
    expected_audit = result["operation_audit"]
    payload = expected_audit["payload"]
    expected_event = {"create-observe": "create_observe", "activate": "activate", "rollback": "rollback"}[pending["operation"]]
    _require(expected_audit["event_type"] == "visitor_recovery." + expected_event
             and payload["plan_id"] == result["plan_id"]
             and payload["manifest_sha256"] == result["manifest_sha256"]
             and payload["snapshot_sha256"] == pending["source_snapshot_sha256"], "RECOVERY_PENDING_INVALID")
    operation = session.scalar(select(AuditEventRow).where(
        AuditEventRow.tenant_id == result["tenant_id"],
        AuditEventRow.origin == "operator_local_visitor_recovery",
        AuditEventRow.event_type == expected_audit["event_type"],
        AuditEventRow.payload["administrative_operation_id"].as_string() == payload["administrative_operation_id"],
    ))
    _require(operation is not None and operation.payload == payload, "RECOVERY_AUDIT_NOT_PROVEN")
    return _seal({**result, "commit_evidence": "DATABASE_STATE_AND_AUDIT_VERIFIED"})


def _finalize_private_artifact(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".final-" + new_id())
    try:
        write_private_artifact(temporary, value)
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    """Explicit operator-local command; credentials are read only from the environment.

    prepare uses a database-enforced read-only transaction. The other operations
    require the protected approval scope and own the final commit boundary.
    All output artifacts are new private files; stdout contains no tenant/IDs.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "create-observe", "activate", "rollback", "reconcile"))
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--request", type=Path, help="Private JSON target/pointer/previous-overlay snapshot")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.operation == "prepare":
        if not args.request or not args.manifest or args.snapshot or args.approval:
            parser.error("prepare requires --request and --manifest only")
    elif args.operation == "reconcile":
        if not args.snapshot or args.approval or args.request or args.manifest:
            parser.error("reconcile requires --snapshot only")
    elif not args.snapshot or not args.approval or args.request or args.manifest:
        parser.error("mutations require --snapshot and --approval only")
    database_url = os.environ.get("ANDY_RECOVERY_DATABASE_URL")
    if not database_url:
        parser.error("ANDY_RECOVERY_DATABASE_URL must be supplied by the authorized host environment")
    engine = None
    output_created = False
    committed = False
    pending_ready = False
    try:
        # Reserve the result before any database mutation; never overwrite evidence.
        write_private_artifact(args.output, {"phase": "PENDING", "operation": args.operation})
        output_created = True
        engine = create_engine(database_url, future=True)
        with Session(engine, expire_on_commit=False) as session:
            if session.bind.dialect.name == "postgresql":
                if args.operation in {"prepare", "reconcile"}:
                    session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                session.execute(text("SET LOCAL statement_timeout = '3s'"))
                session.execute(text("SET LOCAL lock_timeout = '3s'"))
            elif args.operation in {"prepare", "reconcile"} and session.bind.dialect.name == "sqlite":
                session.execute(text("PRAGMA query_only = ON"))
                session.connection().exec_driver_sql("BEGIN")
            if args.operation == "prepare":
                request = read_private_artifact(args.request)
                _require(set(request) == {
                    "tenant_id", "policy_id", "expected_policy_version_id",
                    "previous_default_blueprint_id", "previous_visitor_only",
                }, "RECOVERY_REQUEST_FIELDS_INVALID")
                manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
                result = prepare(session, manifest, **request)
                session.rollback()
            elif args.operation == "reconcile":
                result = reconcile(session, read_private_artifact(args.snapshot))
                session.rollback()
            else:
                snapshot = read_private_artifact(args.snapshot)
                operation = {"create-observe": create_observe, "activate": apply, "rollback": rollback}[args.operation]
                result = operation(session, snapshot, approval_path=args.approval)
                pending = _seal({
                    "schema_version": SCHEMA + "-pending", "phase": "PENDING",
                    "operation": args.operation, "transaction_status": "UNKNOWN",
                    "source_snapshot_sha256": snapshot["snapshot_sha256"], "proposed": result,
                })
                write_private_artifact(args.output.with_name(args.output.name + ".pending"), pending)
                pending_ready = True
                session.commit()
                committed = True
                result = _seal({**result, "commit_evidence": "COMMIT_ACKNOWLEDGED"})
        # The immutable .pending journal remains as the pre-commit backup.
        _finalize_private_artifact(args.output, result)
        print(json.dumps({"operation": args.operation, "phase": result["phase"],
                          "snapshot_sha256": result["snapshot_sha256"],
                          "production_recovery_proven": False}))
        return 0
    except (ValueError, OSError, SQLAlchemyError) as exc:
        if output_created and not committed and not pending_ready:
            args.output.unlink(missing_ok=True)
        code = str(exc) if type(exc) is ValueError and str(exc).startswith("RECOVERY_") else "RECOVERY_OPERATION_FAILED"
        print(json.dumps({"reason_code": code, "database_commit_completed": committed,
                          "reconciliation_required": committed or pending_ready}))
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
