#!/usr/bin/env python3
"""Instrumented Matrix V1 proof. Never creates reviews, outbox rows, or external sends."""

from __future__ import annotations

import json

from sqlalchemy import func, select

from attention_router.application.agents import run_andy
from attention_router.application.agents.context import AllowedAgentContext
from attention_router.application.platform.authority import create_capability_grant
from attention_router.application.platform.context import build_context_snapshot
from attention_router.application.platform.events import create_canonical_event, normalize_inbound_event
from attention_router.application.platform.execution import execute_capability
from attention_router.application.platform.registry import (
    bind_provider,
    register_provider_instance,
    resolve_capability_request,
    sync_capability_definitions,
    sync_platform_registry,
)
from attention_router.config import settings
from attention_router.core.capabilities import CapabilityRequest, CapabilityResolutionStatus
from attention_router.core.events import EventOrigin, OperatorAuthority
from attention_router.core.providers import ProviderResult, ProviderRuntimeRegistry
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.db import SessionLocal
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.models import (
    InboundEventRow,
    OutboxMessageRow,
    ProviderDefinitionRow,
    ProviderInstanceRow,
)
from attention_router.infrastructure.repository import audit, seed_policies
from attention_router.observability.tracing import (
    configure_tracing,
    flush_tracing,
    safe_set_attribute,
    set_outcome,
    start_span,
)
from attention_router.provisioning.manifests import CapabilityManifest


class FakeCoffeeMachineProvider:
    interface_name = "CoffeeMachineProvider"

    def health(self) -> str:
        return "HEALTHY"

    def execute(self, capability: str, parameters: dict) -> ProviderResult:
        del parameters
        if capability != "coffee.make":
            return ProviderResult(False, reason_code="UNSUPPORTED_CAPABILITY")
        return ProviderResult(True, {"fixture": "completed"}, "FAKE_COFFEE_COMPLETED")


def _coffee_manifest(version: int, state: str) -> CapabilityManifest:
    return CapabilityManifest.model_validate({
        "schema_version": "1",
        "capabilities": [{
            "canonical_name": "coffee.make",
            "version": version,
            "domain": "lab",
            "description": "Candidate-only extensibility proof.",
            "operation_type": "ACTION",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "required_permissions": ["coffee.make"],
            "required_provider_interface": "CoffeeMachineProvider",
            "sensitivity": "NORMAL",
            "side_effect": True,
            "default_approval_policy": "REQUIRES_APPROVAL",
            "availability_state": state,
            "metadata": {"fixture": True},
        }],
    })


def _trace_id(span) -> str:
    return f"{span.get_span_context().trace_id:032x}"


def _provider_instance(session) -> ProviderInstanceRow:
    definition = session.scalar(
        select(ProviderDefinitionRow).where(
            ProviderDefinitionRow.canonical_name == "fake_coffee_machine"
        )
    )
    if definition is None:
        definition = ProviderDefinitionRow(
            id=new_id(),
            canonical_name="fake_coffee_machine",
            interface_name="CoffeeMachineProvider",
            description="Candidate-only fake provider.",
            contract_version=1,
            created_at=now_utc(),
        )
        session.add(definition)
        session.flush()
    instance = session.scalar(
        select(ProviderInstanceRow).where(
            ProviderInstanceRow.tenant_id == DEFAULT_TENANT_ID,
            ProviderInstanceRow.canonical_name == "fake_coffee_candidate",
        )
    )
    if instance is None:
        instance = register_provider_instance(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            provider_definition_name=definition.canonical_name,
            canonical_name="fake_coffee_candidate",
        )
        bind_provider(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            capability_name="coffee.make",
            provider_instance_id=instance.id,
        )
    return instance


def main() -> int:
    if settings.app_env not in {"test", "development"}:
        raise SystemExit("candidate proof requires APP_ENV=test or development")
    if settings.external_delivery_enabled:
        raise SystemExit("candidate proof requires EXTERNAL_DELIVERY_ENABLED=false")

    configure_tracing(service_name="attention-router-platform-matrix-candidate")
    results: dict[str, object] = {
        "external_delivery_enabled": False,
        "real_send_count": 0,
    }
    with SessionLocal() as session:
        seed_policies(session)
        sync_platform_registry(session)
        outbox_before = session.scalar(select(func.count()).select_from(OutboxMessageRow)) or 0

        with start_span("platform.matrix.candidate") as root:
            matrix_trace_id = _trace_id(root)
            results["matrix_candidate_trace_id"] = matrix_trace_id
            inbound = InboundEventRow(
                id=new_id(),
                tenant_id=DEFAULT_TENANT_ID,
                source="wwebjs",
                external_event_id=f"matrix-candidate-{new_id()}",
                event_type="message",
                payload={"channel": "whatsapp_web", "content": "synthetic-candidate-only"},
                payload_hash=stable_hash({"fixture": "matrix-candidate"}),
                received_at=now_utc(),
                status="PROCESSED",
                correlation_id=new_id(),
            )
            session.add(inbound)
            session.flush()
            canonical = normalize_inbound_event(session, inbound, actor_id="candidate-actor")
            canonical.metadata_sanitized = {
                **canonical.metadata_sanitized,
                "candidate_trace_id": matrix_trace_id,
            }
            context = build_context_snapshot(session, canonical)
            with start_span("andy.agent.context_build") as context_span:
                safe_set_attribute(
                    context_span,
                    "attention.agent.available_capability_count",
                    len(context.available_capabilities),
                )
                set_outcome(context_span, "BUILT")
            with start_span("andy.agent.run") as agent_span:
                agent_result = run_andy(AllowedAgentContext(
                    actor_id="candidate-actor",
                    binding_id="candidate-binding",
                    audience="candidate",
                    policy_summary=(
                        "Candidate proof: produza uma resposta conversacional em "
                        "action_requested, registre location.current em requested_capabilities, "
                        "e não execute efeitos externos."
                    ),
                    current_message=(
                        "Quero consultar minha localização atual. "
                        "Solicito a capability disponível location.current."
                    ),
                    available_capabilities=context.available_capabilities,
                    current_operational_state=context.current_operational_state,
                    relevant_facts=context.relevant_facts,
                    relevant_decisions=context.relevant_decisions,
                    effective_authority=context.effective_authority,
                ))
                safe_set_attribute(agent_span, "attention.agent.model", settings.andy_agent_model)
                safe_set_attribute(agent_span, "attention.agent.objective", agent_result.output.objective)
                safe_set_attribute(agent_span, "attention.agent.intent", agent_result.output.intent)
                set_outcome(agent_span, "COMPLETED")
            with start_span("andy.agent.validate") as validate_span:
                safe_set_attribute(validate_span, "attention.agent.output_valid", True)
                safe_set_attribute(
                    validate_span,
                    "attention.agent.requested_capability_count",
                    len(agent_result.output.requested_capabilities),
                )
                set_outcome(validate_span, "VALID")
            requested_names = {
                item.capability for item in agent_result.output.requested_capabilities
            }
            if "location.current" not in requested_names:
                raise RuntimeError("Andy did not emit the requested registered capability")
            location = resolve_capability_request(
                session,
                CapabilityRequest(capability="location.current", user_requested=True),
                tenant_id=DEFAULT_TENANT_ID,
                grantee_type="ACTOR",
                grantee_id="candidate-actor",
                policy_allows=True,
            )
            if location.status != CapabilityResolutionStatus.KNOWN_BUT_UNAVAILABLE:
                raise RuntimeError("location.current did not fail closed without provider")
            results["andy_agent"] = {
                "semantic_source": "OPENAI_AGENTS_SDK",
                "objective": agent_result.output.objective,
                "output_valid": True,
                "requested_capability_count": len(agent_result.output.requested_capabilities),
                "location_result": location.reason_code,
            }
            results["legacy_whatsapp_bridge"] = canonical.payload_ref == {"inbound_event_id": inbound.id}
            audit(
                session,
                None,
                "platform_matrix_candidate_proved",
                {
                    "trace_id": matrix_trace_id,
                    "canonical_event_id": canonical.id,
                    "semantic_source": "OPENAI_AGENTS_SDK",
                    "objective": agent_result.output.objective,
                    "requested_capability_count": len(
                        agent_result.output.requested_capabilities
                    ),
                    "location_result": location.reason_code,
                },
                tenant_id=DEFAULT_TENANT_ID,
                origin="matrix_candidate",
            )
            set_outcome(root, "PASS")

        sync_capability_definitions(session, _coffee_manifest(1, "PROVIDER_MISSING"))
        with start_span("platform.matrix.coffee_missing") as root:
            missing_trace_id = _trace_id(root)
            results["coffee_missing_provider_trace_id"] = missing_trace_id
            missing = resolve_capability_request(
                session,
                CapabilityRequest(capability="coffee.make", user_requested=True),
                tenant_id=DEFAULT_TENANT_ID,
                grantee_type="ACTOR",
                grantee_id="coffee-candidate",
                policy_allows=True,
            )
            if missing.status != CapabilityResolutionStatus.KNOWN_BUT_UNAVAILABLE:
                raise RuntimeError("coffee.make did not fail closed without provider")
            results["coffee_missing"] = missing.reason_code
            audit(
                session,
                None,
                "platform_matrix_provider_missing_proved",
                {
                    "trace_id": missing_trace_id,
                    "capability": "coffee.make",
                    "result": missing.reason_code,
                },
                tenant_id=DEFAULT_TENANT_ID,
                origin="matrix_candidate",
            )
            set_outcome(root, "CAPABILITY_UNAVAILABLE")

        sync_capability_definitions(session, _coffee_manifest(2, "SANDBOX_PROVED"))
        provider = _provider_instance(session)
        create_capability_grant(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            grantor_type="OPERATOR",
            grantor_id="matrix-candidate",
            grantee_type="ACTOR",
            grantee_id="coffee-candidate",
            capability_name="coffee.make",
            provenance="candidate",
        )
        runtime = ProviderRuntimeRegistry()
        runtime.register(provider.id, FakeCoffeeMachineProvider())
        with start_span("platform.matrix.coffee_fake_provider") as root:
            fake_trace_id = _trace_id(root)
            results["coffee_fake_provider_trace_id"] = fake_trace_id
            outcome = execute_capability(
                session,
                CapabilityRequest(capability="coffee.make", user_requested=True),
                tenant_id=DEFAULT_TENANT_ID,
                grantee_type="ACTOR",
                grantee_id="coffee-candidate",
                policy_allows=True,
                runtime_registry=runtime,
                approval_granted=True,
            )
            if outcome.status != "EXECUTED":
                raise RuntimeError(f"fake provider proof failed: {outcome.reason_code}")
            results["coffee_fake_provider"] = outcome.reason_code
            audit(
                session,
                None,
                "platform_matrix_fake_provider_proved",
                {
                    "trace_id": fake_trace_id,
                    "capability": "coffee.make",
                    "provider_instance_id": outcome.provider_instance_id,
                    "result": outcome.reason_code,
                },
                tenant_id=DEFAULT_TENANT_ID,
                origin="matrix_candidate",
            )
            set_outcome(root, "PASS")

        with start_span("platform.matrix.owner_command") as root:
            owner_trace_id = _trace_id(root)
            results["owner_command_trace_id"] = owner_trace_id
            owner_event = create_canonical_event(
                session,
                tenant_id=DEFAULT_TENANT_ID,
                requested_origin=EventOrigin.OWNER_COMMAND,
                event_type="command.text",
                payload_type="CANDIDATE_REFERENCE",
                payload_ref={"fixture_id": "owner-command-matrix-v1"},
                correlation_id=new_id(),
                actor_id="candidate-owner",
                operator_authority=OperatorAuthority(
                    operator_actor_id="candidate-owner",
                    tenant_id=DEFAULT_TENANT_ID,
                    authenticated=True,
                    roles=["OWNER"],
                ),
            )
            presence = resolve_capability_request(
                session,
                CapabilityRequest(capability="presence.set", user_requested=True),
                tenant_id=DEFAULT_TENANT_ID,
                grantee_type="ACTOR",
                grantee_id="candidate-owner",
                policy_allows=True,
            )
            results["owner_command"] = {
                "origin": owner_event.origin,
                "capability_result": presence.status.value,
            }
            owner_event.metadata_sanitized = {
                **owner_event.metadata_sanitized,
                "candidate_trace_id": owner_trace_id,
            }
            audit(
                session,
                None,
                "platform_matrix_owner_command_proved",
                {
                    "trace_id": owner_trace_id,
                    "canonical_event_id": owner_event.id,
                    "origin": owner_event.origin,
                    "capability_result": presence.status.value,
                },
                tenant_id=DEFAULT_TENANT_ID,
                origin="matrix_candidate",
            )
            set_outcome(root, "PASS")

        session.commit()
        outbox_after = session.scalar(select(func.count()).select_from(OutboxMessageRow)) or 0
        if outbox_after != outbox_before:
            raise RuntimeError("candidate proof unexpectedly changed outbox count")

    results["trace_flush"] = flush_tracing()
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
