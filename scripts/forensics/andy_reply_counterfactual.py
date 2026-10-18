"""Compare the actual versioned selection prefix with synthetic, offline fixtures.

This deliberately does not import application settings or open a database. Only
three source functions and the direct-eligibility value class/regex are loaded.
Session.get, readiness and settings are fixture boundaries, so the result proves
selection/enabling behavior, never complete authority or delivery.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace


PIPELINE_PATH = "attention_router/application/decision_pipeline.py"
DIRECT_PATH = "attention_router/application/direct_conversation.py"
FUNCTIONS = ("_blueprint", "_agent_enabled_for", "direct_conversation_eligibility")
LIMITS = (
    "Synthetic Session.get, settings and READY readiness; no real persistence/provider.",
    "No actor-binding lookup, alias authentication, policy, autonomy or tools certified.",
    "No decision generation, outbox, transport, tracing or end-to-end recovery certified.",
    "A configured blueprint does not prove appropriate domain, publication or authority.",
)


def _selected_nodes(source: str, names: set[str]) -> list[ast.stmt]:
    result = []
    for node in ast.parse(source).body:
        name = getattr(node, "name", None)
        if isinstance(node, ast.Assign):
            name = next((target.id for target in node.targets if isinstance(target, ast.Name)), None)
        if name in names:
            result.append(node)
    if len(result) != len(names):
        raise ValueError("Required versioned selection definitions are missing")
    return result


def compare_sources(pipeline_source: str, direct_source: str) -> dict:
    """Execute exact source definitions under an intentionally narrow fixture boundary."""
    nodes = _selected_nodes(pipeline_source, {"_blueprint", "_agent_enabled_for"})
    nodes += _selected_nodes(
        direct_source, {"DirectConversationEligibility", "_PEER", "direct_conversation_eligibility"}
    )
    namespace = {
        "__name__": __name__, "dataclass": dataclass, "re": re,
        "settings": SimpleNamespace(),
        "get_andy_readiness": lambda: SimpleNamespace(state="READY"),
        "AgentBlueprintRow": type("FixtureBlueprint", (), {}),
        "AgentBlueprintVersionRow": type("FixtureVersion", (), {}),
    }
    module = ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes],
        type_ignores=[],
    )
    exec(compile(ast.fix_missing_locations(module), "<versioned selection prefix>", "exec"), namespace)
    fingerprints = {
        node.name: hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()
        for node in nodes if isinstance(node, ast.FunctionDef)
    }
    cases = []
    configurations = (
        ("unknown_with_explicit_default", "valid", None, "tenant-a", "valid", True, True),
        ("unknown_without_default", None, None, "tenant-a", "valid", False, False),
        ("known_binding_without_default", None, "valid", "tenant-a", "valid", True, True),
        ("invalid_binding_does_not_fall_back_to_default", "valid", "missing", "tenant-a", "valid", False, False),
        ("binding_of_other_tenant", "valid", "foreign", "tenant-a", "valid", False, False),
        ("default_of_other_tenant", "foreign", None, "tenant-a", "valid", False, False),
        ("blueprint_without_current_version", "valid", None, "tenant-a", "no_current", False, False),
        ("missing_version", "valid", None, "tenant-a", "missing", False, False),
        ("version_belongs_to_other_blueprint", "valid", None, "tenant-a", "foreign", False, False),
        ("agent_disabled", "valid", None, "tenant-a", "valid", True, False),
        ("readiness_not_ready", "valid", None, "tenant-a", "valid", True, False),
        ("non_direct_source", "valid", None, "tenant-a", "valid", True, False),
        ("lid_with_pn_alias", "valid", None, "tenant-a", "valid", True, True),
        ("domain_and_draft_status_not_validated_by_selector", "valid", None, "tenant-a", "valid", True, True),
    )
    for name, default, binding_id, tenant, version_state, expected_selected, expected_agent in configurations:
        blueprint = SimpleNamespace(
            id="valid", tenant_id="tenant-a", current_version_id="v1",
            status="draft", domain="synthetic-other-domain",
        )
        version = SimpleNamespace(id="v1", blueprint_id="valid")
        rows = {
            "valid": blueprint, "v1": version,
            "foreign": SimpleNamespace(id="foreign", tenant_id="tenant-b", current_version_id="v2"),
        }
        if version_state == "no_current":
            blueprint.current_version_id = None
        elif version_state == "missing":
            rows.pop("v1")
        elif version_state == "foreign":
            version.blueprint_id = "foreign"
        session = SimpleNamespace(get=lambda _model, key: rows.get(key))
        binding = SimpleNamespace(binding_metadata={"agent_blueprint_id": binding_id}) if binding_id else None
        namespace["settings"] = SimpleNamespace(
            agent_decision_default_blueprint_id=default, andy_agent_enabled=name != "agent_disabled"
        )
        namespace["get_andy_readiness"] = lambda: SimpleNamespace(
            state="NOT_READY" if name == "readiness_not_ready" else "READY"
        )
        peer = "5500000000001@lid"
        aliases = [peer, "5500000000002@c.us"] if name == "lid_with_pn_alias" else [peer]
        event = SimpleNamespace(
            source="synthetic-other" if name == "non_direct_source" else "wwebjs",
            event_type="message", lineage_classification="ORGANIC",
            payload={
                "channel": "whatsapp", "event_type": "message",
                "event_origin": "EXTERNAL_INBOUND", "owner_authenticated": False,
                "actor_id": peer,
                "metadata": {
                    "from_me": False, "conversation_state": "READY", "is_group": False,
                    "conversation_key": "wwebjs:" + peer, "source_account": "synthetic",
                    "peer_identifiers": aliases,
                },
            },
        )
        row, resolved_version = namespace["_blueprint"](session, binding, tenant, explicit_only=True)
        configured = row is not None and resolved_version is not None
        enabled = namespace["_agent_enabled_for"](
            binding, "unknown", None, owner_authenticated=False,
            event=event, blueprint_configured=configured,
        )
        cases.append({
            "case": name, "blueprint_configured": configured, "agent_path_enabled": enabled,
            "passed": configured == expected_selected and enabled == expected_agent,
        })
    return {
        "function_ast_sha256": fingerprints, "cases": cases,
        "passed": all(case["passed"] for case in cases),
        "owner_authority_proven": False, "end_to_end_recovery_proven": False,
        "limits": list(LIMITS),
    }


def revision_report(repository: Path, revision: str | None = None) -> dict:
    if revision is None:
        sources = [(repository / path).read_text(encoding="utf-8") for path in (PIPELINE_PATH, DIRECT_PATH)]
    else:
        if not re.fullmatch(r"[0-9a-f]{7,40}", revision):
            raise ValueError("Use an explicit hexadecimal Git commit")
        sources = [
            subprocess.run(
                ["git", "show", f"{revision}:{path}"], cwd=repository, check=True,
                capture_output=True, text=True, timeout=10,
            ).stdout for path in (PIPELINE_PATH, DIRECT_PATH)
        ]
    return {"revision": revision or "WORKTREE", **compare_sources(*sources)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revisions", nargs="*", help="Explicit local Git commit SHAs; no checkout/fetch")
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    reports = [revision_report(args.repository, revision) for revision in args.revisions or [None]]
    print(json.dumps({"schema": "andy-selection-counterfactual-v1", "reports": reports}, indent=2))
    return 0 if all(report["passed"] for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
