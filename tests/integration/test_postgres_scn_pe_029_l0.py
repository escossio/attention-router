"""PostgreSQL isolation smoke test for the SCN-PE-029 L0 boundary.

The full scenario harness is intentionally not inferred from this smoke test;
the test proves that the existing real intent/provider/outbox boundary runs
against the disposable integration database and never the runtime database.
"""

import pytest
from datetime import timedelta
from attention_router.domain.models import new_id, now_utc
from pathlib import Path
from attention_router.platform.scenarios import load_manifest_file
from attention_router.infrastructure.models import (
    ActorBindingRow, ScenarioDefinitionRow, ScenarioVersionRow, ScenarioRunRow,
    ReadinessResultRow,
    ExecutionLeaseRow,
    InboundEventRow,
    InteractionRow,
    AgentDecisionRow,
    EffectConsumptionRow,
    EntityStateRow,
    OperationalObservationRow,
)
from attention_router.platform.execution_safety import (
    provision_scenario_execution_safety_in_transaction,
    activate_scenario_run_for_execution,
)
from sqlalchemy import text

from attention_router.application import execution
from attention_router.application.decision_pipeline import process_agent_decision
from attention_router.application.agents.service import AndyAgentResult
from attention_router.application.agents.output import AndyAgentOutput
from attention_router.platform.standing_directives import (
    create_standing_directive,
    resolve_effective_standing_directives,
    revoke_standing_directive,
)
from attention_router.application.response_review import create_review_for_decision, approve_review
from attention_router.platform.scenarios import ScenarioRunStatus, transition_scenario_run
from attention_router.application.platform.capability_pack import internal_runtime_registry
from attention_router.application.platform.execution import execute_capability
from attention_router.core.capabilities import CapabilityRequest
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.models import OutboxMessageRow
from attention_router.application.platform.registry import ensure_default_tenant
from attention_router.application.platform.registry import sync_platform_registry
from attention_router.application.platform.authority import create_capability_grant
from attention_router.application.platform.context import DatabaseStateRetriever
from attention_router.platform.execution_safety import ExecutionSafetyService, SafetyDenied
from tests.integration.test_postgres_platform_execution_safety import _seed, _request

from tests.test_execution import _intent


@pytest.mark.postgres
def test_scn_pe_029_l0_uses_isolated_postgres_boundary(Session, monkeypatch):
    with Session() as session:
        ensure_default_tenant(session)
        intent = _intent(session)
        session.execute(text("SELECT 1"))
        # The integration fixture owns this database; no application worker
        # or WhatsApp transport is started by this test.
        from attention_router.config import settings
        monkeypatch.setattr(settings, "external_delivery_enabled", True)
        execution.release_intent(session, intent.id, transport_ready=True)
        runtime = internal_runtime_registry(session, DEFAULT_TENANT_ID)
        result = execute_capability(
            session,
            CapabilityRequest(
                capability="conversation.reply",
                parameters={"response_text": "L0", "_execution": {"execution_intent_id": intent.id}},
            ),
            tenant_id=DEFAULT_TENANT_ID,
            grantee_type="ACTOR",
            grantee_id="owner",
            policy_allows=True,
            runtime_registry=runtime,
            approval_granted=True,
            owner_authorized=True,
        )
        assert result.reason_code == "DELEGATED"
        assert session.query(OutboxMessageRow).filter_by(execution_intent_id=intent.id).count() == 1


@pytest.mark.postgres
def test_scn_pe_029_owner_sleeping_full_path_l0_starts_at_scenario_run(Session, monkeypatch):
    """Exercise the canonical scenario/safety entrypoint in isolated Postgres.

    This intentionally does not manufacture a lease: provisioning must create
    the production safety primitives from the real manifest and fail closed if
    the synthetic lineage fixture is incomplete.
    """
    with Session() as session:
        ensure_default_tenant(session)
        manifest = load_manifest_file(Path("config/platform/scenarios/SCN-PE-029.yaml"))
        stamp = now_utc()
        run_id = new_id()
        actor = ActorBindingRow(
            id=new_id(), tenant_id=DEFAULT_TENANT_ID, source="wwebjs",
            external_actor_id="synthetic-l0", actor_key="OWNER",
            actor_category="SYNTHETIC_TEST_ACTOR",
            binding_metadata={
                "synthetic": True,
                "lineage_classification": "SYNTHETIC",
                "test_allowed": True,
                "stimulus_id": "l0-stimulus",
            },
            is_active=True, created_at=stamp, updated_at=stamp,
        )
        definition = ScenarioDefinitionRow(
            id=new_id(), tenant_id=DEFAULT_TENANT_ID, scenario_key="SCN-PE-029",
            title="owner_sleeping_autoreply", description="L0 full-path scenario",
            enabled=True, created_at=stamp, updated_at=stamp,
        )
        session.add_all([actor, definition])
        session.flush()
        version = ScenarioVersionRow(
            id=new_id(), tenant_id=DEFAULT_TENANT_ID, scenario_definition_id=definition.id,
            version=1, schema_version="1", manifest_source_path="config/platform/scenarios/SCN-PE-029.yaml",
            content_hash=manifest.content_hash(), requirements_covered=[], risk_ids=[], config_keys=[],
            risk_classification="BOUNDED_EXTERNAL_EFFECT", enabled=True, source_sha="l0", created_at=stamp,
            is_immutable=True,
        )
        readiness = ReadinessResultRow(
            id=new_id(), tenant_id=DEFAULT_TENANT_ID, dimension="DOMAIN_READINESS",
            subject_type="SCENARIO_RUN", subject_key=run_id, state="READY",
            reason_codes=[], evaluated_at=stamp,
            evidence_fresh_until=stamp + timedelta(minutes=3), source_references=[],
            blocker_references=[], required_dependency_ids=[], provenance={"source_sha": "l0"},
            is_current=True, superseded_at=None, created_at=stamp,
        )
        run = ScenarioRunRow(
            id=run_id, tenant_id=DEFAULT_TENANT_ID, scenario_version_id=version.id,
            synthetic_actor_binding_id=actor.id, status="CREATED", root_correlation_id=new_id(),
            source_sha="l0", runtime_sha="l0", schema_revision="0020", driver_revision="l0",
            readiness_result_id=readiness.id, effect_budget_id=None, created_at=stamp, validated_at=None,
            armed_at=None, started_at=stamp, verifying_at=None, completed_at=None,
            expires_at=stamp + timedelta(minutes=3), terminal_reason=None, cleanup_state="PENDING", updated_at=stamp,
        )
        session.add_all([version, readiness])
        session.flush()
        session.add(run)
        session.flush()
        provision_scenario_execution_safety_in_transaction(
            session, scenario_run_id=run.id, manifest=manifest,
            readiness_max_age_seconds=300, lease_ttl_seconds=180,
        )
        activate_scenario_run_for_execution(
            session,
            scenario_run_id=run.id,
            authority="test-operator",
            requires_bounded_authorization=False,
        )
        assert run.status == "ARMED"
        assert run.effect_budget_id is not None
        lease = session.query(ExecutionLeaseRow).filter_by(
            scenario_run_id=run.id, purpose="SYSTEM_WHATSAPP_TEXT"
        ).one()
        assert lease.status == "AVAILABLE"
        assert lease.claim_count == 0

        owner = ActorBindingRow(
            id=new_id(), tenant_id=DEFAULT_TENANT_ID, source="l0-owner",
            external_actor_id="owner-l0", actor_key="owner-l0", actor_category="owner",
            binding_metadata={"owner": True}, is_active=True,
            created_at=stamp, updated_at=stamp,
        )
        session.add(owner)
        session.flush()
        session.add(EntityStateRow(
            id=new_id(), tenant_id=DEFAULT_TENANT_ID, subject_type="ACTOR",
            subject_id=owner.actor_key, state_namespace="presence", state_key="effective",
            state_value={"status": "sleeping", "audience_scope": "all"}, source="l0",
            effective_at=stamp, expires_at=stamp + timedelta(minutes=3), version=1,
            updated_at=stamp,
        ))
        create_standing_directive(
            session, tenant_id=DEFAULT_TENANT_ID, subject_actor_id=owner.actor_key,
            created_by_actor_id=owner.actor_key, trigger_type="INBOUND_MESSAGE",
            effect_type="DISCLOSE_CURRENT_PRESENCE", audience_selector={"type": "EVERYONE"},
            provenance="SCN-PE-029-L0",
        )
        monkeypatch.setattr("attention_router.config.settings.andy_agent_enabled", True)
        monkeypatch.setattr(
            "attention_router.application.decision_pipeline._agent_enabled_for",
            lambda binding, audience, policy_version, owner_authenticated=False,
            *, event=None, blueprint_configured=False: True,
        )
        monkeypatch.setattr(
            "attention_router.application.decision_pipeline.run_andy",
            lambda context: AndyAgentResult(
                output=AndyAgentOutput(
                    response_text="O owner está dormindo no momento.", intent="STATUS",
                    objective="owner_availability", reason_code="L0_TEST_ADAPTER",
                    confidence="high", conversation_state="answer",
                    requested_capabilities=[CapabilityRequest(
                        capability="conversation.reply",
                        parameters={"response_text": "O owner está dormindo no momento."},
                    )],
                ), duration_ms=1, turn_count=1, tool_call_count=0,
            ),
        )

        # Continue the same run through the real synthetic reservation path.
        # The existing lifecycle fixture supplies the intent rows; only their
        # lineage is attached to this freshly provisioned ScenarioRun.
        intent = _intent(session)
        interaction = session.get(InteractionRow, "interaction-execution-test")
        event = session.get(InboundEventRow, "event-execution-test")
        decision = session.get(AgentDecisionRow, "decision-execution-test")
        interaction.contact_id = actor.actor_key
        interaction.correlation_id = run.root_correlation_id
        event.lineage_classification = "SYNTHETIC"
        event.source = "wwebjs"
        event.scenario_run_id = run.id
        event.correlation_id = run.root_correlation_id
        event.payload = {
            "scenario_id": "SCN-PE-029",
            "stimulus_id": "l0-stimulus",
            "external_actor_id": actor.external_actor_id,
            "actor_id": actor.external_actor_id,
            "channel": "whatsapp",
        }
        decision.actor_binding_id = actor.id
        # Keep the seed decision out of the pipeline idempotency key so the
        # real pipeline must generate the Andy decision for this event.
        decision.decision_pipeline_version = "l0-seed"
        session.flush()
        pipeline_decision = process_agent_decision(session, event.id)
        assert pipeline_decision is not None
        assert pipeline_decision.event_id == event.id
        assert pipeline_decision.interaction_id == interaction.id
        assert pipeline_decision.proposed_response == "O owner está dormindo no momento."
        assert pipeline_decision.semantic_source == "OPENAI_AGENTS_SDK"
        assert len(pipeline_decision.requested_capabilities) == 1
        assert pipeline_decision.requested_capabilities[0]["capability"] == "conversation.reply"
        # The decision created by the real pipeline, rather than the seed
        # decision used to establish the interaction FKs, owns the review and
        # therefore the official intent/reservation lifecycle.
        review = create_review_for_decision(session, pipeline_decision.id)
        from attention_router.config import settings
        monkeypatch.setattr(settings, "external_delivery_enabled", True)
        _, intent = approve_review(session, review.id, reviewer_reference="l0")
        reservation = session.query(EffectConsumptionRow).filter_by(
            execution_intent_id=intent.id,
        ).one_or_none()
        assert reservation is not None
        claimed = session.query(ExecutionLeaseRow).filter_by(
            scenario_run_id=run.id, purpose="SYSTEM_WHATSAPP_TEXT"
        ).one()
        assert claimed.status != "AVAILABLE"
        assert claimed.claim_count == 1
        assert session.query(EffectConsumptionRow).filter_by(
            execution_intent_id=intent.id,
        ).count() == 1
        sync_platform_registry(session)
        create_capability_grant(
            session, tenant_id=DEFAULT_TENANT_ID, grantor_type="OPERATOR",
            grantor_id="l0-authority", grantee_type="ACTOR", grantee_id=actor.actor_key,
            capability_name="conversation.reply", provenance="SCN-PE-029-L0",
        )
        execution.release_intent(session, intent.id, transport_ready=True)
        runtime = internal_runtime_registry(session, DEFAULT_TENANT_ID)
        request = CapabilityRequest.model_validate(
            pipeline_decision.requested_capabilities[0]
        ).model_copy(update={
            "parameters": {
                **pipeline_decision.requested_capabilities[0]["parameters"],
                "_execution": {"execution_intent_id": intent.id},
            }
        })
        result = execute_capability(
            session, request,
            tenant_id=DEFAULT_TENANT_ID, grantee_type="ACTOR", grantee_id=actor.actor_key,
            policy_allows=True, runtime_registry=runtime, approval_granted=True,
            owner_authorized=False,
        )
        assert result.reason_code == "DELEGATED"
        outbox = session.query(OutboxMessageRow).filter_by(
            execution_intent_id=intent.id,
        ).one()
        assert outbox.interaction_id == interaction.id
        observation = OperationalObservationRow(
            id=new_id(), tenant_id=DEFAULT_TENANT_ID, source="synthetic-l0",
            source_type="SCENARIO", observed_at=stamp, received_at=now_utc(),
            freshness_expires_at=stamp + timedelta(minutes=3), dependency_id=None,
            component_key="SCN-PE-029", lineage_classification="SYNTHETIC",
            scenario_run_id=run.id, correlation_id=run.root_correlation_id,
            reason_code="L0_OUTBOX_OBSERVED", status="OBSERVED",
            sanitized_metadata={"interaction_id": interaction.id, "outbox_id": outbox.id},
            source_revision="l0", runtime_revision="l0", schema_revision="0020",
            created_at=now_utc(),
        )
        session.add(observation)
        transition_scenario_run(run, ScenarioRunStatus.RUNNING)
        transition_scenario_run(run, ScenarioRunStatus.VERIFYING)
        transition_scenario_run(run, ScenarioRunStatus.PASSED, reason="L0_DRY_RUN_PASS")
        session.flush()
        assert observation.scenario_run_id == run.id
        assert observation.sanitized_metadata["outbox_id"] == outbox.id
        assert run.status == "PASSED"
        # Formal correlation: every edge below is asserted against persisted
        # identifiers from this one scenario, intent, lease and outbox.
        assert event.scenario_run_id == run.id
        assert event.interaction_id == interaction.id
        assert pipeline_decision.event_id == event.id
        assert pipeline_decision.interaction_id == interaction.id
        assert intent.agent_decision_id == pipeline_decision.id
        assert reservation.execution_intent_id == intent.id
        assert claimed.scenario_run_id == run.id
        assert outbox.execution_intent_id == intent.id
        assert observation.sanitized_metadata["interaction_id"] == outbox.interaction_id
        assert claimed.claim_count == 1
        assert session.query(OutboxMessageRow).filter_by(
            execution_intent_id=intent.id,
        ).count() == 1


@pytest.mark.postgres
def test_scn_pe_029_l0_provider_fail_close_matrix(Session, monkeypatch):
    """Expected blocks never create an outbox or reach a transport."""
    with Session() as session:
        ensure_default_tenant(session)
        sync_platform_registry(session)
        create_capability_grant(
            session, tenant_id=DEFAULT_TENANT_ID, grantor_type="OPERATOR",
            grantor_id="l0-authority", grantee_type="ACTOR", grantee_id="synthetic-l0",
            capability_name="conversation.reply", provenance="SCN-PE-029-NEGATIVE-L0",
        )
        runtime = internal_runtime_registry(session, DEFAULT_TENANT_ID)
        monkeypatch.setattr("attention_router.config.settings.external_delivery_enabled", True)

        missing = execute_capability(
            session,
            CapabilityRequest(capability="conversation.reply", parameters={
                "response_text": "blocked", "_execution": {"execution_intent_id": "missing"},
            }),
            tenant_id=DEFAULT_TENANT_ID, grantee_type="ACTOR", grantee_id="synthetic-l0",
            policy_allows=True, runtime_registry=runtime, approval_granted=True,
        )
        assert missing.status in {"BLOCKED", "FAILED"}
        assert missing.reason_code in {"EXECUTION_INTENT_REQUIRED", "CONTROLLED_EXECUTION_NOT_QUEUED"}

        intent = _intent(session)
        create_capability_grant(
            session, tenant_id=DEFAULT_TENANT_ID, grantor_type="OPERATOR",
            grantor_id="l0-authority", grantee_type="ACTOR", grantee_id="masked",
            capability_name="conversation.reply", provenance="SCN-PE-029-NEGATIVE-L0",
        )
        unreleased = execute_capability(
            session,
            CapabilityRequest(capability="conversation.reply", parameters={
                "response_text": "blocked", "_execution": {"execution_intent_id": intent.id},
            }),
            tenant_id=DEFAULT_TENANT_ID, grantee_type="ACTOR", grantee_id="masked",
            policy_allows=True, runtime_registry=runtime, approval_granted=True,
        )
        assert unreleased.status in {"BLOCKED", "FAILED"}
        assert unreleased.reason_code == "CONTROLLED_EXECUTION_NOT_QUEUED"

        denied = execute_capability(
            session,
            CapabilityRequest(capability="presence.set", parameters={"state": "awake"}),
            tenant_id=DEFAULT_TENANT_ID, grantee_type="ACTOR", grantee_id="masked",
            policy_allows=False, runtime_registry=runtime, approval_granted=False,
        )
        assert denied.status == "BLOCKED"
        assert session.query(OutboxMessageRow).count() == 0


@pytest.mark.postgres
@pytest.mark.parametrize("case_id", [
    "NEG-001", "NEG-002", "NEG-003", "NEG-004", "NEG-005", "NEG-006",
    "NEG-007", "NEG-008", "NEG-009", "NEG-010", "NEG-011", "NEG-012",
    "NEG-013", "NEG-014", "NEG-015", "NEG-016", "NEG-017",
])
def test_scn_pe_029_mandatory_fail_close_cases_are_external_effect_free(
    Session, monkeypatch, case_id,
):
    """Common invariant for every mandatory negative-case boundary.

    Boundary-specific fixtures are added incrementally; this test ensures no
    negative case can accidentally reach the reply provider or create an
    outbox while those variations are exercised.
    """
    with Session() as session:
        ensure_default_tenant(session)
        if case_id in {"NEG-001", "NEG-002", "NEG-003", "NEG-004", "NEG-005", "NEG-006", "NEG-007"}:
            stamp = now_utc()
            directive = None
            if case_id != "NEG-001":
                directive = create_standing_directive(
                    session, tenant_id=DEFAULT_TENANT_ID, subject_actor_id="owner-l0",
                    created_by_actor_id="owner-l0",
                    trigger_type="OTHER" if case_id == "NEG-004" else "INBOUND_MESSAGE",
                    effect_type="DISCLOSE_CURRENT_PRESENCE",
                    audience_selector=(
                        {"audience": "other"} if case_id in {"NEG-005", "NEG-007"}
                        else {"relationship": "other"} if case_id == "NEG-006"
                        else {"type": "EVERYONE"}
                    ),
                    provenance=f"SCN-PE-029-{case_id}",
                    expires_at=stamp - timedelta(seconds=1) if case_id == "NEG-003" else None,
                )
                if case_id == "NEG-002":
                    revoke_standing_directive(session, directive.id, revoked_by="l0-test")
            resolved = resolve_effective_standing_directives(
                session, tenant_id=DEFAULT_TENANT_ID, subject_actor_id="owner-l0",
                trigger_type="INBOUND_MESSAGE", audience="synthetic_test",
                relationship="synthetic_test",
            )
            assert resolved == []
            assert session.query(OutboxMessageRow).count() == 0
            return
        # These cases deliberately mutate the state consumed by the real
        # boundary.  They must not collapse into the generic policy-denied
        # fixture below.
        if case_id in {"NEG-008", "NEG-009"}:
            stamp = now_utc()
            owner = ActorBindingRow(
                id=new_id(), tenant_id=DEFAULT_TENANT_ID, source="l0-owner",
                external_actor_id=f"owner-{case_id}", actor_key=f"owner-{case_id}",
                actor_category="owner", binding_metadata={"owner": True},
                is_active=True, created_at=stamp, updated_at=stamp,
            )
            session.add(owner)
            session.flush()
            state = EntityStateRow(
                id=new_id(), tenant_id=DEFAULT_TENANT_ID, subject_type="ACTOR",
                subject_id=owner.actor_key, state_namespace="presence", state_key="effective",
                state_value={"status": "sleeping", "audience_scope": "all"}, source="l0",
                effective_at=stamp - timedelta(days=2) if case_id == "NEG-009" else stamp,
                expires_at=stamp - timedelta(seconds=1) if case_id == "NEG-009" else None,
                version=1, updated_at=stamp,
            )
            session.add(state)
            session.flush()
            if case_id == "NEG-008":
                session.delete(state)
                session.flush()
            resolved = DatabaseStateRetriever(session).retrieve(
                DEFAULT_TENANT_ID, owner.actor_key, 20,
            )
            assert resolved == []
            assert session.query(OutboxMessageRow).count() == 0
            return

        if case_id in {"NEG-011", "NEG-012", "NEG-016"}:
            intent = _intent(session)
            if case_id == "NEG-011":
                assert intent.status == "BLOCKED"
                assert intent.execution_allowed is False
                assert intent.release_status == "HELD"
                assert session.query(OutboxMessageRow).count() == 0
                return
            if case_id == "NEG-012":
                # The official release boundary requires transport readiness;
                # leave the official intent held and attempt the real provider.
                sync_platform_registry(session)
                runtime = internal_runtime_registry(session, DEFAULT_TENANT_ID)
                result = execute_capability(
                    session, CapabilityRequest(capability="conversation.reply", parameters={
                        "response_text": "blocked", "_execution": {"execution_intent_id": intent.id},
                    }), tenant_id=DEFAULT_TENANT_ID, grantee_type="ACTOR",
                    grantee_id="masked", policy_allows=True, runtime_registry=runtime,
                    approval_granted=True,
                )
                assert result.status in {"BLOCKED", "FAILED"}
                assert intent.release_status == "HELD"
                assert session.query(OutboxMessageRow).count() == 0
                return
            intent.recipient_reference = "invalid-arbitrary-target"
            target_event = session.get(InboundEventRow, "event-execution-test")
            target_event.payload = {
                "external_actor_id": "synthetic-invalid-target",
                "channel": "whatsapp",
                "metadata": {"is_group": True},
            }
            session.flush()
            monkeypatch.setattr("attention_router.config.settings.external_delivery_enabled", True)
            with pytest.raises(Exception, match="RECIPIENT_RESOLUTION_FAILED"):
                execution.release_intent(session, intent.id, transport_ready=True)
            assert session.query(OutboxMessageRow).count() == 0
            return

        if case_id == "NEG-010":
            sync_platform_registry(session)
            runtime = internal_runtime_registry(session, DEFAULT_TENANT_ID)
            request = CapabilityRequest(capability="conversation.reply", parameters={
                "response_text": "denied-by-real-grant-resolution",
            })
            result = execute_capability(
                session, request, tenant_id=DEFAULT_TENANT_ID, grantee_type="ACTOR",
                grantee_id="synthetic-neg-010", policy_allows=True,
                runtime_registry=runtime, approval_granted=True, owner_authorized=False,
            )
            assert request.capability == "conversation.reply"
            assert result.status in {"BLOCKED", "FAILED", "REQUIRES_APPROVAL"}
            assert result.reason_code == "CAPABILITY_GRANT_MISSING"
            assert session.query(OutboxMessageRow).count() == 0
            return

        if case_id == "NEG-014":
            suffix, run_id, budget_id = _seed(
                Session,
                lease_count=1, stimulus_limit=1, lease_expires_delta=timedelta(seconds=-1),
            )
            request = _request(suffix, run_id, budget_id, lease_index=0,
                               claimant="neg-014", logical_effect=f"effect-{suffix}")
            with pytest.raises(SafetyDenied, match="LEASE_EXPIRED"):
                ExecutionSafetyService(Session).claim_and_reserve(request)
            with Session() as verify:
                lease = verify.get(ExecutionLeaseRow, request.lease_id)
                assert lease is not None and lease.status == "EXPIRED"
            assert session.query(OutboxMessageRow).count() == 0
            return

        if case_id == "NEG-015":
            suffix, run_id, budget_id = _seed(
                Session,
                lease_count=1, stimulus_limit=1,
            )
            request = _request(suffix, run_id, budget_id, lease_index=0,
                               claimant="neg-015", logical_effect=f"effect-{suffix}")
            first = ExecutionSafetyService(Session).claim_and_reserve(request)
            second = ExecutionSafetyService(Session).claim_and_reserve(request)
            assert first.consumption_id == second.consumption_id
            assert second.idempotent_retry is True
            with Session() as verify:
                lease = verify.get(ExecutionLeaseRow, request.lease_id)
                assert lease.claim_count == 1
                assert verify.query(EffectConsumptionRow).filter_by(
                    id=first.consumption_id,
                ).count() == 1
            assert session.query(OutboxMessageRow).count() == 0
            return

        sync_platform_registry(session)
        runtime = internal_runtime_registry(session, DEFAULT_TENANT_ID)
        monkeypatch.setattr("attention_router.config.settings.external_delivery_enabled", False)
        if case_id == "NEG-013":
            intent = _intent(session)
            event = session.get(InboundEventRow, "event-execution-test")
            event.lineage_classification = "SYNTHETIC"
            create_capability_grant(
                session, tenant_id=DEFAULT_TENANT_ID, grantor_type="OPERATOR",
                grantor_id="l0-authority", grantee_type="ACTOR", grantee_id="masked",
                capability_name="conversation.reply", provenance="SCN-PE-029-NEG-013",
            )
            monkeypatch.setattr("attention_router.config.settings.external_delivery_enabled", True)
            execution.release_intent(session, intent.id, transport_ready=True)
            result = execute_capability(
                session, CapabilityRequest(capability="conversation.reply", parameters={
                    "response_text": "blocked", "_execution": {"execution_intent_id": intent.id},
                }), tenant_id=DEFAULT_TENANT_ID, grantee_type="ACTOR",
                grantee_id="masked", policy_allows=True, runtime_registry=runtime,
                approval_granted=True,
            )
            assert result.status in {"BLOCKED", "FAILED"}
            assert result.reason_code in {
                "EFFECT_BUDGET_MISSING", "CONTROLLED_EXECUTION_NOT_QUEUED",
            }
            assert session.query(OutboxMessageRow).count() == 0
            return

        if case_id == "NEG-017":
            # The real Synthetic Driver boundary rejects external L1 effects;
            # no test-only authorization flag is introduced here.
            from attention_router.platform.synthetic_driver import (
                DriverReason, SyntheticDriverConfig, SyntheticDriverV1,
            )
            driver = SyntheticDriverV1(config=SyntheticDriverConfig())
            manifest = driver.load("SCN-PE-029")
            preflight = driver.preflight(manifest)
            assert preflight.ready is False
            assert DriverReason.EXTERNAL_SEND_NOT_AUTHORIZED.value in preflight.reasons
            assert DriverReason.L1_NOT_AUTHORIZED.value in preflight.reasons or manifest.scenario.level.value == 0
            assert session.query(OutboxMessageRow).count() == 0
            return

        result = execute_capability(
            session,
            CapabilityRequest(capability="conversation.reply", parameters={
                "response_text": f"negative:{case_id}",
            }),
            tenant_id=DEFAULT_TENANT_ID, grantee_type="ACTOR",
            grantee_id=f"synthetic-{case_id.lower()}", policy_allows=False,
            runtime_registry=runtime, approval_granted=False, owner_authorized=False,
        )
        assert result.status in {"BLOCKED", "REQUIRES_APPROVAL", "FAILED"}
        assert session.query(OutboxMessageRow).count() == 0
