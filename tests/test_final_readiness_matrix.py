from datetime import UTC, datetime, timedelta
from pathlib import Path

from attention_router.application.agents.readiness import AndyReadiness
from attention_router.application.platform.entities import create_relationship
from attention_router.core.entities import EntityReference
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import EntityStateRow
from attention_router.infrastructure.repository import upsert_actor_binding
from attention_router.infrastructure.repository import create_policy
from attention_router.platform.live_readiness import evaluate_live_scenario_readiness
from attention_router.platform.scenarios import load_manifest_file
from attention_router.application.platform.capability_pack import provision_internal_providers
from attention_router.application.execution import TransportStatusSnapshot

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def test_scn_pe_029_default_stack_happy_path_uses_canonical_sources(session, monkeypatch):
    upsert_actor_binding(session, "test", "synthetic", "actor_synthetic_test_actor", "SYNTHETIC_TEST_ACTOR", metadata={"audience": "synthetic_test"})
    upsert_actor_binding(session, "test", "owner", "owner", "owner", metadata={"owner": True})
    create_relationship(session, tenant_id=DEFAULT_TENANT_ID,
                        source=EntityReference(entity_type="ACTOR", entity_id="actor_synthetic_test_actor"),
                        target=EntityReference(entity_type="ACTOR", entity_id="owner"), relationship_type="synthetic_test")
    session.add(EntityStateRow(id="presence-happy", tenant_id=DEFAULT_TENANT_ID, subject_type="ACTOR", subject_id="owner",
                               state_namespace="presence", state_key="effective", state_value={"status": "sleeping", "audience_scope": "all"},
                               source="test", effective_at=NOW - timedelta(seconds=1), expires_at=NOW + timedelta(minutes=1), version=1, updated_at=NOW))
    from attention_router.platform.standing_directives import create_standing_directive
    create_standing_directive(session, tenant_id=DEFAULT_TENANT_ID, subject_actor_id="owner", created_by_actor_id="owner",
                              trigger_type="INBOUND_MESSAGE", effect_type="DISCLOSE_CURRENT_PRESENCE",
                              audience_selector={"type": "AUDIENCE", "audience": "synthetic_test"}, provenance="test",
                              valid_from=NOW - timedelta(seconds=1), expires_at=NOW + timedelta(minutes=1))
    provision_internal_providers(session, DEFAULT_TENANT_ID)
    create_policy(session, "synthetic-disclosure-test", {
        "identifier": "synthetic-disclosure-test", "name": "synthetic disclosure test",
        "match_criteria": {"audience": "synthetic_test"}, "priority": 10, "specificity": 10,
        "tone": "neutral", "initial_wait_seconds": 1,
        "allowed_disclosures": ["availability_hint"], "allowed_actions": ["respond"],
        "escalation_steps": [], "ack_timeout_seconds": 10, "repetition_limit": 1,
        "cancellation_conditions": [], "completion_conditions": ["safe_completion"],
    }, tenant_id=DEFAULT_TENANT_ID)
    monkeypatch.setattr("attention_router.platform.live_readiness_adapters.get_andy_readiness", lambda now: AndyReadiness("READY", "TEST_READY", now))
    manifest = load_manifest_file(Path("config/platform/scenarios/SCN-PE-029.yaml"))
    status = TransportStatusSnapshot("synthetic", "synthetic", "READY", True, True, True, True, NOW, NOW + timedelta(minutes=1), "TEST_READY", "fake")
    from attention_router.platform.live_readiness_adapters import DatabaseStandingDirectiveAdapter, DatabasePresenceAdapter
    from attention_router.platform.scenario_readiness import ScenarioReadinessContext
    context = ScenarioReadinessContext(DEFAULT_TENANT_ID, manifest.scenario.id, manifest.scenario.version, NOW, {}, {"session": session})
    DatabaseStandingDirectiveAdapter().resolve(context, "STANDING_DIRECTIVE_ACTIVE")
    DatabasePresenceAdapter().resolve(context, "OWNER_PRESENCE_FRESH")
    result = evaluate_live_scenario_readiness(tenant_id=DEFAULT_TENANT_ID, manifest=manifest,
        services={"session": session, "synthetic_transport_status": status,
                  }, now=NOW)
    assert result.domain.state.value == "READY", result.domain.reason_codes
    assert len(result.evidence) == len(manifest.preconditions)
