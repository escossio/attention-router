"""Pure review of the proposed visitor package; no application or DB mutation.

The caller supplies synthetic scope/version assertions. This module does not
authenticate an operator, look up tenants or versions, change the selector, or
grant deployment authority. Its checksums bind the proposal reviewed offline.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path

from pydantic import ValidationError

from attention_router.domain.agent_builder import AgentBlueprintSpec
from attention_router.infrastructure.hashing import stable_hash


SCHEMA_VERSION = "andy-visitor-recovery-proposal-v1"
TEXT_ACTIONS = {"respond", "request_information"}
FORBIDDEN_ACTIONS = {
    "tool_execution", "administrative_commands", "gmail_access",
    "private_data_access", "owner_impersonation", "infrastructure_changes",
}
AUTHORITY_FIELDS = {
    "owner_authenticated", "tools_enabled", "admin_enabled", "gmail_enabled",
    "private_data_disclosure_enabled",
}
SELECTION_IMPACT = {
    "process_default_with_tenant_check": True,
    "explicit_binding_blueprint_precedence_preserved": True,
    "bindings_without_blueprint_may_select_default": True,
    "owner_without_explicit_blueprint_may_select_default": True,
    "non_direct_and_groups_may_select_default": True,
    "non_direct_and_groups_automatic_effects_authorized": False,
    "unknown_policy_overlay_requires_separate_approval": True,
    "unknown_policy_change_may_affect_other_unknown_flows": True,
    "acknowledged_for_review": True,
    "requires_operator_review": True,
}
MANIFEST_FIELDS = {
    "schema_version", "review_only", "name", "tenant_scope", "creation_status",
    "creation_spec", "proposed_activation", "policy_proposal",
    "authority_boundary", "selection_impact", "notes",
}


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError(code)


def _fields(value: object, expected: set[str], code: str) -> dict:
    _require(type(value) is dict and set(value) == expected, code)
    return value


def _same_strings(value: object, expected: set[str]) -> bool:
    return (
        type(value) is list and all(type(item) is str for item in value)
        and len(value) == len(expected) and set(value) == expected
    )


def _validated_parts(manifest: dict) -> tuple[dict, dict, dict]:
    _fields(manifest, MANIFEST_FIELDS, "VISITOR_RECOVERY_MANIFEST_INVALID")
    _require(manifest["schema_version"] == SCHEMA_VERSION, "VISITOR_RECOVERY_SCHEMA_INVALID")
    _require(manifest["review_only"] is True, "VISITOR_RECOVERY_REVIEW_ONLY_REQUIRED")
    _require(
        type(manifest["name"]) is str and bool(manifest["name"].strip()),
        "VISITOR_RECOVERY_NAME_INVALID",
    )
    _require(
        manifest["tenant_scope"] == "EXACT_APPROVED_WHATSAPP_TENANT",
        "VISITOR_RECOVERY_TENANT_SCOPE_INVALID",
    )
    _require(manifest["creation_status"] == "draft", "VISITOR_RECOVERY_CREATION_STATUS_INVALID")
    raw = _fields(
        manifest["creation_spec"], set(AgentBlueprintSpec.model_fields),
        "VISITOR_RECOVERY_SPEC_FIELDS_INVALID",
    )
    for field, keys in (
        ("identity", {"name", "description"}),
        ("tone", {"style", "notes"}),
        ("actions", {"allowed", "requires_approval", "forbidden"}),
        ("autonomy", {"level", "execution_mode", "notes"}),
    ):
        _fields(raw[field], keys, "VISITOR_RECOVERY_SPEC_FIELDS_INVALID")
    try:
        creation = AgentBlueprintSpec.model_validate(raw).model_dump(mode="json")
    except ValidationError:
        raise ValueError("VISITOR_RECOVERY_SPEC_INVALID") from None
    _require(creation["domain"] == "general", "VISITOR_RECOVERY_DOMAIN_INVALID")
    _require(creation["identity"]["name"] == "Andy", "VISITOR_RECOVERY_IDENTITY_INVALID")
    _require(creation["audiences"] == ["unknown"], "VISITOR_RECOVERY_AUDIENCE_INVALID")
    _require(creation["channels"] == ["whatsapp"], "VISITOR_RECOVERY_CHANNEL_INVALID")
    _require(
        creation["autonomy"]["level"] == "observe"
        and creation["autonomy"]["execution_mode"] == "OBSERVE",
        "VISITOR_RECOVERY_INITIAL_AUTONOMY_INVALID",
    )
    _require(
        _same_strings(creation["actions"]["allowed"], TEXT_ACTIONS)
        and creation["actions"]["requires_approval"] == []
        and _same_strings(creation["actions"]["forbidden"], FORBIDDEN_ACTIONS),
        "VISITOR_RECOVERY_ACTIONS_INVALID",
    )
    _require(
        creation["known_information"] == {} and creation["behavior"] == {}
        and all(creation[key] == [] for key in (
            "knowledge_requirements", "missing_information", "assumptions",
            "escalation", "blocking_questions",
        )),
        "VISITOR_RECOVERY_PRIVATE_OR_IMPLICIT_CONTEXT_INVALID",
    )
    _require(
        creation["configuration_completeness"] == 100
        and bool(creation["purpose"].strip())
        and all(creation[key] for key in ("goals", "rules", "constraints", "success_criteria")),
        "VISITOR_RECOVERY_SPEC_INCOMPLETE",
    )
    activation = _fields(
        manifest["proposed_activation"],
        {"status", "autonomy", "requires_operator_approval", "changes_autonomy"},
        "VISITOR_RECOVERY_ACTIVATION_INVALID",
    )
    _fields(activation["autonomy"], {"execution_mode"}, "VISITOR_RECOVERY_ACTIVATION_INVALID")
    _require(activation["status"] == "published", "VISITOR_RECOVERY_PUBLICATION_REQUIRED")
    _require(
        activation["autonomy"]["execution_mode"] == "AUTO_ALLOWED"
        and activation["changes_autonomy"] is True
        and activation["requires_operator_approval"] is True,
        "VISITOR_RECOVERY_AUTONOMY_CHANGE_NOT_EXPLICIT",
    )
    active = deepcopy(creation)
    active["autonomy"].update(activation["autonomy"])
    # This existing schema accepts execution_mode with legacy level=observe.
    # The runtime explicitly gives execution_mode precedence; this is a change.
    active = AgentBlueprintSpec.model_validate(active).model_dump(mode="json")
    policy = _fields(
        manifest["policy_proposal"],
        {"audience", "allowed_actions", "conversational_execution", "allowed_capabilities", "allowed_disclosures"},
        "VISITOR_RECOVERY_POLICY_INVALID",
    )
    conversation = _fields(
        policy["conversational_execution"], {"execution_mode", "allowed_actions"},
        "VISITOR_RECOVERY_POLICY_INVALID",
    )
    _require(
        policy["audience"] == "unknown" and conversation["execution_mode"] == "AUTO_ALLOWED"
        and _same_strings(policy["allowed_actions"], TEXT_ACTIONS)
        and _same_strings(conversation["allowed_actions"], TEXT_ACTIONS)
        and policy["allowed_capabilities"] == [] and policy["allowed_disclosures"] == [],
        "VISITOR_RECOVERY_POLICY_AUTHORITY_INVALID",
    )
    authority = _fields(
        manifest["authority_boundary"], AUTHORITY_FIELDS, "VISITOR_RECOVERY_AUTHORITY_INVALID",
    )
    _require(all(value is False for value in authority.values()), "VISITOR_RECOVERY_AUTHORITY_INVALID")
    impact = _fields(
        manifest["selection_impact"], set(SELECTION_IMPACT), "VISITOR_RECOVERY_BLAST_RADIUS_INVALID",
    )
    _require(
        all(impact[key] is value for key, value in SELECTION_IMPACT.items()),
        "VISITOR_RECOVERY_BLAST_RADIUS_NOT_ACKNOWLEDGED",
    )
    _require(
        type(manifest["notes"]) is list
        and all(type(note) is str for note in manifest["notes"]),
        "VISITOR_RECOVERY_NOTES_INVALID",
    )
    return creation, active, deepcopy(policy)


def simulation_spec(manifest: dict) -> dict:
    """Return the approved-shape activation overlay for synthetic tests only.

    No operator approval is inferred. No row is created or published, and no
    runtime settings or authorization rules are changed.
    """
    return _validated_parts(manifest)[1]


def preflight_manifest(manifest: dict, *, tenant_approved: bool, version_valid: bool) -> dict:
    """Validate a review package using caller assertions, without persistence.

    The booleans deliberately carry no tenant ID or credential. Passing True
    in a fixture does not authenticate a real tenant/version or an operator.
    """
    _require(tenant_approved is True, "VISITOR_RECOVERY_TENANT_NOT_APPROVED")
    _require(version_valid is True, "VISITOR_RECOVERY_VERSION_INVALID")
    creation, active, policy = _validated_parts(manifest)
    return {
        "schema_version": SCHEMA_VERSION,
        "proposal_valid": True,
        "review_only": True,
        "applies_changes": False,
        "operational_approval_granted": False,
        "production_recovery_proven": False,
        "scope_and_version_evidence": "CALLER_ASSERTIONS_ONLY",
        "manifest_sha256": stable_hash(manifest),
        "creation_spec_sha256": stable_hash(creation),
        "activation_spec_sha256": stable_hash(active),
        "policy_sha256": stable_hash(policy),
        "creation_status": "draft",
        "proposed_status": "published",
        "initial_execution_mode": "OBSERVE",
        "proposed_execution_mode": "AUTO_ALLOWED",
        "legacy_level": "observe",
        "execution_mode_overrides_level": True,
        "autonomy_increase_requires_approval": True,
        "selector_domain_or_publication_guard_installed": False,
        "selection_impact": deepcopy(SELECTION_IMPACT),
        "plan": [
            "Review this exact manifest, spec and policy checksums.",
            "Revalidate tenant/version authority through the approved operational procedure.",
            "Certify synthetic regressions and disposable persistence/rollback on the exact SHA.",
            "Obtain explicit publication, autonomy, unknown-policy and default-selection approval.",
            "Only then use the reviewed administrative path and full existing Worker composition.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--synthetic-tenant-match", action="store_true")
    parser.add_argument("--synthetic-version-valid", action="store_true")
    args = parser.parse_args()
    try:
        if args.manifest.stat().st_size > 128 * 1024:
            raise ValueError("VISITOR_RECOVERY_MANIFEST_TOO_LARGE")
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        result = preflight_manifest(
            manifest, tenant_approved=args.synthetic_tenant_match,
            version_valid=args.synthetic_version_valid,
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        result = {"proposal_valid": False, "reason_code": "VISITOR_RECOVERY_MANIFEST_UNREADABLE"}
    except ValueError as exc:
        result = {"proposal_valid": False, "reason_code": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["proposal_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
