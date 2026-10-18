"""Read-only AR-1002 evidence adapter; never executes model or writes data.

The caller must provide an authorized, consistent SQLAlchemy read-only
transaction and runtime attestation. No database connections are created here.
"""
from __future__ import annotations

from collections.abc import Mapping

from attention_router.infrastructure.hashing import stable_hash
from attention_router.observability.visitor_readiness import REQUIRED, classify


def collect(session, *, default_blueprint_id: str | None, tenant_id: str | None,
            runtime: Mapping[str, bool | None], policy: Mapping[str, bool | None],
            collector_ok: bool, snapshot_consistent: bool,
            blueprint_model: type, version_model: type) -> dict:
    """Produce sanitized evidence. Unknown policy/runtime fields cannot become READY.

    session is supplied by the caller and MUST be a read-only transaction.
    This function uses only Session.get, no commit/flush/refresh or outbound I/O.
    """
    values: dict[str, bool | None] = dict.fromkeys(REQUIRED)
    for key in ("worker_healthy", "visitor_flag_enabled", "guard_installed"):
        values[key] = runtime.get(key)
    for key in ("policy_active", "policy_immutable", "policy_checksum_valid",
                "policy_text_only", "capabilities_empty", "disclosures_empty"):
        values[key] = policy.get(key)
    values["default_configured"] = bool(default_blueprint_id)
    if not collector_ok or not snapshot_consistent or not default_blueprint_id or not tenant_id:
        return classify(values, collector_ok=collector_ok, snapshot_consistent=snapshot_consistent)
    blueprint = session.get(blueprint_model, default_blueprint_id)
    values["blueprint_exists"] = blueprint is not None
    if blueprint is None:
        return classify(values, collector_ok=True, snapshot_consistent=True)
    values["tenant_matches"] = blueprint.tenant_id == tenant_id
    values["blueprint_published"] = blueprint.status == "published"
    version = session.get(version_model, blueprint.current_version_id) if blueprint.current_version_id else None
    values["version_belongs_to_blueprint"] = version is not None and version.blueprint_id == blueprint.id
    if version is not None:
        values["version_immutable"] = version.is_immutable is True
        spec = version.spec
        values["version_checksum_valid"] = isinstance(spec, dict) and version.checksum == stable_hash(spec)
        values["contract_text_only_v1"] = isinstance(spec, dict) and spec.get("behavior") == {"visitor_default_contract": "text-only-v1"} and spec.get("domain") == "general" and spec.get("audiences") == ["unknown"] and spec.get("channels") == ["whatsapp"]
    return classify(values, collector_ok=True, snapshot_consistent=True)
