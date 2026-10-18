"""Opt-in visitor default uses real routing/authority with a synthetic Agent."""

from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from attention_router.application import autonomy, decision_pipeline as pipeline, execution, services
from attention_router.application.agents.output import AndyAgentOutput
from attention_router.application.agents.service import AndyAgentResult
from attention_router.application.owner_reply_grace import process_due_grace_windows
from attention_router.application.platform.capability_pack import provision_internal_providers
from attention_router.application.voice_transcription import process_voice_transcriptions
from attention_router.application.voice_tts import ensure_tts_derivation, intent_is_voice_response, process_tts_derivations
from attention_router.config import Settings, settings
from attention_router.core.capabilities import CapabilityRequest
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure import human_identity_models  # noqa: F401
from attention_router.infrastructure.hashing import stable_hash
from attention_router.infrastructure.db import Base
from attention_router.infrastructure.models import (
    AgentBlueprintRow, AgentBlueprintVersionRow, AgentDecisionRow, AgentExecutionIntentRow, AuditEventRow,
    CapabilityDefinitionRow, CapabilityGrantRow, ConversationResponseGraceWindowRow,
    InboundEventRow, OutboxMessageRow, PolicyRow, PolicyVersionRow, TTSDerivationRow,
)
from attention_router.infrastructure.repository import ensure_policy_version, seed_policies, upsert_actor_binding
from tests.test_direct_conversation import PEER, direct_event, setup_direct
from tests.test_agent_conversation_history import _interaction, _outbox


def setup_guarded_visitor(session, monkeypatch):
    captured = setup_direct(session, monkeypatch)
    monkeypatch.setattr(settings, "agent_decision_default_visitor_only", True)
    blueprint = session.get(AgentBlueprintRow, settings.agent_decision_default_blueprint_id)
    version = session.get(AgentBlueprintVersionRow, blueprint.current_version_id)
    version.spec = {
        "domain": "general", "audiences": ["unknown"], "channels": ["whatsapp"],
        "actions": {"allowed": ["respond", "request_information"], "requires_approval": []},
        "autonomy": {"execution_mode": "AUTO_ALLOWED"},
        "behavior": {"visitor_default_contract": "text-only-v1"},
    }
    version.checksum = stable_hash(version.spec)
    policy = session.get(PolicyRow, "desconhecido")
    config = deepcopy(session.get(PolicyVersionRow, policy.current_version_id).config)
    config.update({
        "allowed_actions": ["respond", "request_information"],
        "conversational_execution": {
            "execution_mode": "AUTO_ALLOWED", "allowed_actions": ["respond", "request_information"],
        },
        "allowed_capabilities": [], "allowed_disclosures": [],
    })
    ensure_policy_version(session, policy, config, "synthetic visitor guard")
    monkeypatch.setattr(
        pipeline, "execute_owner_capability", lambda *a, **k: pytest.fail("unexpected owner execution"),
    )
    return captured, blueprint, version


@pytest.fixture()
def guarded_visitor(session, monkeypatch):
    return setup_guarded_visitor(session, monkeypatch)


@pytest.fixture()
def independent_sessions(tmp_path):
    engine = create_engine("sqlite+pysqlite:///" + str(tmp_path / "visitor-scope.sqlite"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        seed_policies(db)
        db.commit()
    yield factory
    engine.dispose()


def visitor_decision(session, event=None):
    received = services.receive_normalized_inbound_event(session, event or direct_event())
    window = session.scalar(select(ConversationResponseGraceWindowRow).where(
        ConversationResponseGraceWindowRow.anchor_interaction_id == received["id"],
    ))
    if window is not None:
        process_due_grace_windows(session, "synthetic-visitor-guard", timestamp=window.due_at + timedelta(seconds=1))
    return pipeline.process_agent_decision(session, received["inbound_event_id"])


def assert_no_visitor_intent(session, decision, captured):
    assert decision.agent_blueprint_id is None and captured == []
    assert not autonomy.evaluate_and_route(session, decision).automatic_execution_allowed
    assert not session.scalar(select(AgentExecutionIntentRow))
    assert not session.scalar(select(OutboxMessageRow))


def insert_invalid_policy_version(session, policy, config, *, mutable=False, bad_checksum=False):
    """Insert deliberately invalid evidence without changing immutable history."""
    current = session.get(PolicyVersionRow, policy.current_version_id)
    config = {**deepcopy(config), "tone": config["tone"] + " synthetic-" + new_id()}
    version = PolicyVersionRow(
        id=new_id(), policy_id=policy.identifier, version=current.version + 1, config=config,
        checksum="0" * 64 if bad_checksum else stable_hash(config),
        created_at=now_utc(), created_by="synthetic guard fixture", is_immutable=not mutable,
    )
    session.add(version)
    session.flush()
    policy.current_version_id = version.id
    return version


def test_opt_in_guard_permits_unbound_question_and_text_outbox(session, guarded_visitor):
    captured, blueprint, _ = guarded_visitor
    decision = visitor_decision(session)
    assert decision.agent_blueprint_id == blueprint.id and len(captured) == 1
    assert autonomy.evaluate_and_route(session, decision).automatic_execution_allowed
    assert execution.enqueue_ready_intents(session, transport_ready=True) == 1
    assert session.scalar(select(OutboxMessageRow)).payload["external_actor_id"] == PEER


def visitor_voice_decision(session, monkeypatch, tmp_path):
    from tests.test_whatsapp_voice_flow import attach_media, release_grace, voice_event

    event = voice_event()
    received = services.receive_normalized_inbound_event(session, event)
    attach_media(session, monkeypatch, tmp_path, {
        **received, "external_event_id": event.external_event_id,
    })
    assert process_voice_transcriptions(session, "synthetic-visitor-stt", client=SimpleNamespace(
        transcribe=lambda *_: {
            "transcript": "Explique fibra óptica em uma frase.", "provider": "synthetic",
            "model": "synthetic", "request_id": "synthetic-visitor-transcription",
        },
    )) == 1
    release_grace(session)
    return pipeline.process_agent_decision(session, received["inbound_event_id"])


def test_guard_transcribed_ptt_enqueues_text_without_tts(session, guarded_visitor, monkeypatch, tmp_path):
    captured, blueprint, _ = guarded_visitor
    decision = visitor_voice_decision(session, monkeypatch, tmp_path)
    assert decision.agent_blueprint_id == blueprint.id
    assert captured[0].current_message == "Explique fibra óptica em uma frase."
    assert autonomy.evaluate_and_route(session, decision).automatic_execution_allowed
    intent = session.scalar(select(AgentExecutionIntentRow))
    assert not intent_is_voice_response(session, intent)
    assert execution.enqueue_ready_intents(session, transport_ready=True) == 1
    outbox = session.scalar(select(OutboxMessageRow))
    assert outbox.execution_intent_id == intent.id and intent.status == "QUEUED"
    assert outbox.action_type == "agent_execution_text" and outbox.payload["message_type"] == "text"
    assert outbox.payload["external_actor_id"] == PEER
    assert outbox.payload["text"] == intent.effective_response_snapshot
    assert not session.scalar(select(TTSDerivationRow))


def test_guard_rejects_synthetic_tts_after_flag_loss_and_pointer_change(
    session, guarded_visitor, monkeypatch, tmp_path,
):
    _, blueprint, recorded_version = guarded_visitor
    decision = visitor_voice_decision(session, monkeypatch, tmp_path)
    assert autonomy.evaluate_and_route(session, decision).automatic_execution_allowed
    intent = session.scalar(select(AgentExecutionIntentRow))
    monkeypatch.setattr(settings, "agent_decision_default_visitor_only", False)
    monkeypatch.setattr(settings, "agent_decision_default_blueprint_id", None)
    spec = {**deepcopy(recorded_version.spec), "behavior": {}}
    unmarked = AgentBlueprintVersionRow(
        id=new_id(), blueprint_id=blueprint.id, version=recorded_version.version + 1,
        spec=spec, checksum=stable_hash(spec), created_at=now_utc(), created_by="synthetic",
        change_reason="synthetic pointer change", is_immutable=True,
    )
    session.add(unmarked)
    session.flush()
    blueprint.current_version_id = unmarked.id
    assert not intent_is_voice_response(session, intent)
    derivation = ensure_tts_derivation(session, intent)
    assert process_tts_derivations(session, "synthetic-visitor-tts", client=SimpleNamespace(
        synthesize=lambda *a, **k: pytest.fail("text-only visitor called TTS provider"),
    )) == 1
    assert derivation.status == "FAILED" and derivation.attempt_count == 0
    assert intent.status == "BLOCKED" and intent.blocked_reason == "TTS_DERIVATION_FAILED"
    assert not session.scalar(select(OutboxMessageRow))


@pytest.mark.parametrize("category,explicit", [("owner", False), ("unknown", False), ("unknown", True)])
def test_guard_never_inherits_active_binding_authority(session, guarded_visitor, category, explicit):
    captured, blueprint, _ = guarded_visitor
    metadata = {"owner": category == "owner", "audience": category}
    if explicit:
        metadata["agent_blueprint_id"] = blueprint.id
    upsert_actor_binding(session, "wwebjs", PEER, "bound-synthetic", category, metadata=metadata)
    decision = visitor_decision(session)
    assert_no_visitor_intent(session, decision, captured)
    audit = session.scalar(select(AuditEventRow).where(AuditEventRow.event_type == "visitor_default.blocked"))
    assert audit.payload == {"reason_code": "VISITOR_DEFAULT_SCOPE_DENIED"}


@pytest.mark.parametrize("kind", ["group", "non-direct", "owner", "partial", "audience", "tenant"])
def test_guard_default_scope_cannot_be_widened(session, guarded_visitor, kind):
    _, blueprint, _ = guarded_visitor
    event = direct_event(peer="5500000000030@g.us") if kind == "group" else direct_event()
    if kind == "non-direct":
        event.source = "synthetic-other"
    if kind == "owner":
        event.event_origin = "OWNER_COMMAND"
        event.owner_authenticated = True
        event.metadata["from_me"] = True
    if kind == "partial":
        event.metadata["conversation_state"] = "AMBIGUOUS"
    receipt = SimpleNamespace(
        source=event.source, event_type=event.event_type, tenant_id=event.tenant_id,
        payload=event.normalized_payload, lineage_classification=event.lineage_classification,
    )
    if kind == "tenant":
        receipt.tenant_id = "synthetic-other-tenant"
    policy = session.get(PolicyRow, "desconhecido")
    row, version = pipeline._blueprint(
        session, None, blueprint.tenant_id, event=receipt,
        audience="owner" if kind == "audience" else "unknown",
        policy_version=session.get(PolicyVersionRow, policy.current_version_id),
    )
    assert row is blueprint and version is None


@pytest.mark.parametrize("mutation", [
    "food_service", "draft", "wrong-actions", "wrong-channel", "malformed", "mutable", "marker", "checksum",
])
def test_guard_rejects_incompatible_blueprint_at_runtime(session, guarded_visitor, mutation):
    captured, blueprint, version = guarded_visitor
    spec = deepcopy(version.spec)
    if mutation == "food_service":
        spec["domain"] = "food_service"
    elif mutation == "draft":
        blueprint.status = "draft"
    elif mutation == "wrong-actions":
        spec["actions"]["allowed"].append("grant.create")
    elif mutation == "wrong-channel":
        spec["channels"] = ["gmail"]
    elif mutation == "mutable":
        version.is_immutable = False
    elif mutation == "marker":
        spec["behavior"]["visitor_default_contract"] = "unsupported-version"
    elif mutation == "checksum":
        version.checksum = "0" * 64
    else:
        spec["actions"] = "malformed"
    version.spec = spec
    if mutation != "checksum":
        version.checksum = stable_hash(spec)
    decision = visitor_decision(session)
    assert_no_visitor_intent(session, decision, captured)


@pytest.mark.parametrize("mutation", [
    "capability", "disclosure", "legacy-action", "implicit", "wider-conversation",
    "retired", "mutable", "checksum",
])
def test_guard_rejects_policy_authority_beyond_text(session, guarded_visitor, mutation):
    captured, _, _ = guarded_visitor
    policy = session.get(PolicyRow, "desconhecido")
    config = deepcopy(session.get(PolicyVersionRow, policy.current_version_id).config)
    if mutation == "capability":
        config["allowed_capabilities"] = ["grant.create"]
    elif mutation == "disclosure":
        config["allowed_disclosures"] = ["availability_hint"]
    elif mutation == "legacy-action":
        config["allowed_actions"].append("soft_ping")
    elif mutation == "implicit":
        del config["conversational_execution"]
    elif mutation == "wider-conversation":
        config["conversational_execution"]["allowed_actions"].append("grant.create")
    if mutation in {"mutable", "checksum"}:
        insert_invalid_policy_version(
            session, policy, config, mutable=mutation == "mutable", bad_checksum=mutation == "checksum",
        )
    else:
        version = ensure_policy_version(session, policy, config, "synthetic widened policy")
    if mutation == "retired":
        version.status = "RETIRED"
    decision = visitor_decision(session)
    assert_no_visitor_intent(session, decision, captured)
    audit = session.scalar(select(AuditEventRow).where(AuditEventRow.event_type == "visitor_default.blocked"))
    assert audit.payload == {"reason_code": "VISITOR_DEFAULT_POLICY_DENIED"}


def test_guard_sanitizes_poisoned_owner_context_and_preserves_same_chat_history(session, guarded_visitor, monkeypatch):
    captured, _, _ = guarded_visitor
    sentinel = {"private": "SYNTHETIC_OWNER_SECRET"}
    poisoned = SimpleNamespace(**{
        field: [sentinel] for field in (
            "current_operational_state", "available_capabilities", "relevant_facts",
            "relevant_decisions", "effective_authority",
        )
    })
    monkeypatch.setattr(pipeline, "build_context_snapshot", lambda *a, **k: poisoned)
    directive = SimpleNamespace(
        id="SYNTHETIC_OWNER_SECRET", trigger_type="INBOUND_MESSAGE",
        effect_type="DISCLOSE_CURRENT_PRESENCE", audience_selector={"type": "all"},
    )
    monkeypatch.setattr(pipeline, "resolve_effective_standing_directives", lambda *a, **k: [directive])
    history = [{"role": "user", "text": "SYNTHETIC_SAME_CHAT_HISTORY"}]
    monkeypatch.setattr(pipeline, "_recent_agent_turns", lambda *a, **k: history)
    visitor_decision(session)
    context = captured[0]
    assert context.represented_subject is None
    assert context.recent_turns == history and context.allowed_disclosures == []
    for field in (
        "allowed_facts", "available_action_capabilities", "action_capabilities",
        "available_capabilities", "current_operational_state", "relevant_facts",
        "relevant_decisions", "effective_authority", "effective_standing_directives",
    ):
        assert getattr(context, field) == [], field
    assert "SYNTHETIC_OWNER_SECRET" not in json.dumps(context.prompt_payload())


def test_guard_denies_existing_privileged_grant_to_unbound_actor(session, guarded_visitor, monkeypatch):
    provision_internal_providers(session, DEFAULT_TENANT_ID)
    capability = session.scalar(select(CapabilityDefinitionRow).where(
        CapabilityDefinitionRow.canonical_name == "grant.create",
    ))
    grant = CapabilityGrantRow(
        id=new_id(), tenant_id=DEFAULT_TENANT_ID, grantor_type="ACTOR", grantor_id="owner_direct_test",
        grantee_type="ACTOR", grantee_id=PEER, capability_id=capability.id, scope={},
        status="ACTIVE", valid_from=now_utc() - timedelta(seconds=1),
        provenance="synthetic test", created_at=now_utc(),
    )
    session.add(grant)

    def hostile(context):
        assert context.available_capabilities == [] and context.effective_authority == []
        return AndyAgentResult(AndyAgentOutput(
            response_text="Não posso conceder esse acesso.", confidence="high",
            intent="request_action", objective="refuse",
            conversation_state="answer", reason_code="SYNTHETIC_REFUSAL",
            requested_capabilities=[CapabilityRequest(capability="grant.create", user_requested=True)],
        ), 1, 1, 0)

    monkeypatch.setattr(pipeline, "run_andy", hostile)
    decision = visitor_decision(session)
    audit = session.scalar(select(AuditEventRow).where(
        AuditEventRow.interaction_id == decision.interaction_id,
        AuditEventRow.event_type == "capability.requests_evaluated",
    ))
    assert audit.payload["resolutions"][0]["reason_code"] == "POLICY_DENIED"
    assert "execution_status" not in audit.payload["resolutions"][0]
    assert grant.status == "ACTIVE"
    assert autonomy.evaluate_and_route(session, decision).automatic_execution_allowed


def test_guard_preserves_proven_visitor_history_with_fake_delivery(session, guarded_visitor):
    captured, _, _ = guarded_visitor
    first = visitor_decision(session, direct_event(content="SYNTHETIC_FIRST_VISITOR_QUESTION"))
    assert autonomy.evaluate_and_route(session, first).automatic_execution_allowed
    assert execution.enqueue_ready_intents(session, transport_ready=True) == 1
    outbox = session.scalar(select(OutboxMessageRow))
    outbox.status, outbox.completed_at = "DONE", now_utc()
    second = visitor_decision(session, direct_event(content="SYNTHETIC_SECOND_VISITOR_QUESTION"))
    assert second.interaction_id != first.interaction_id
    assert captured[1].recent_turns == [
        {"role": "user", "content": "SYNTHETIC_FIRST_VISITOR_QUESTION"},
        {"role": "assistant", "content": first.proposed_response},
    ]


def test_guard_excludes_legacy_owner_history_after_binding_revocation(session, guarded_visitor):
    captured, _, _ = guarded_visitor
    prior = _interaction(session, text="SYNTHETIC_OLD_OWNER_PRIVATE_INPUT", offset=-120, contact_id=PEER)
    prior.relationship_category = "owner"
    receipt = InboundEventRow(
        id=new_id(), tenant_id=prior.tenant_id, source="wwebjs", external_event_id=new_id(),
        event_type="message", payload={}, payload_hash=stable_hash({}), received_at=prior.created_at,
        processed_at=prior.created_at, interaction_id=prior.id, status="PROCESSED",
        correlation_id=prior.correlation_id,
    )
    session.add(receipt)
    session.flush()
    decision = AgentDecisionRow(
        id=new_id(), event_id=receipt.id, interaction_id=prior.id, actor_id=PEER, audience="owner",
        decision_pipeline_version="synthetic-private-history", decision_type="RESPOND",
        recommended_action="respond", escalation_required=False, missing_information=[],
        execution_allowed=True, external_delivery_allowed=True, reasoning_summary="synthetic legacy history",
        status="SUCCESS", created_at=prior.created_at,
    )
    session.add(decision)
    session.flush()
    prior_intent = AgentExecutionIntentRow(
        id=new_id(), agent_decision_id=decision.id, authorization_source="POLICY_AUTONOMY",
        intent_type="WHATSAPP_RESPONSE", effective_response_snapshot="SYNTHETIC_OLD_OWNER_PRIVATE_RESPONSE",
        status="SENT", execution_allowed=True, external_delivery_allowed=True, release_status="RELEASED",
        idempotency_key=new_id(), created_at=prior.created_at, executed_at=prior.created_at,
    )
    session.add(prior_intent)
    session.flush()
    _outbox(session, prior, prior_intent)
    upsert_actor_binding(session, "wwebjs", PEER, PEER, "owner", metadata={"owner": True}, is_active=False)
    visitor_decision(session)
    assert captured[0].recent_turns == []
    assert "SYNTHETIC_OLD_OWNER_PRIVATE" not in json.dumps(captured[0].prompt_payload())
    # The historical explicit contract is unchanged when the new filter is not requested.
    current = session.scalar(select(AgentExecutionIntentRow).where(
        AgentExecutionIntentRow.id == prior_intent.id,
    ))
    assert current is prior_intent and prior_intent.effective_response_snapshot.endswith("PRIVATE_RESPONSE")


def test_guard_keeps_other_explicit_blueprint_and_legacy_flag_off(session, guarded_visitor, monkeypatch):
    _, default, version = guarded_visitor
    explicit = AgentBlueprintRow(
        id=new_id(), name="Synthetic explicit historical", status="draft",
        created_at=now_utc(), updated_at=now_utc(),
    )
    session.add(explicit)
    session.flush()
    spec = deepcopy(version.spec)
    spec["domain"] = "food_service"
    spec["behavior"] = {}
    explicit_version = AgentBlueprintVersionRow(
        id=new_id(), blueprint_id=explicit.id, version=1, spec=spec, checksum=new_id(),
        created_at=now_utc(), created_by="test", change_reason="test", is_immutable=True,
    )
    session.add(explicit_version)
    explicit.current_version_id = explicit_version.id
    binding = upsert_actor_binding(
        session, "wwebjs", PEER, "explicit-owner", "owner",
        metadata={"agent_blueprint_id": explicit.id, "owner": True},
    )
    assert pipeline._blueprint(session, binding, default.tenant_id) == (explicit, explicit_version)
    # Without the opt-in, the historical default contract remains unchanged.
    monkeypatch.setattr(settings, "agent_decision_default_visitor_only", False)
    default.status = "draft"
    version.spec = {**version.spec, "behavior": {}}
    assert pipeline._blueprint(session, None, default.tenant_id) == (default, version)


def test_invalid_explicit_reference_cannot_rank_guarded_default(session, guarded_visitor, monkeypatch):
    _, default, _ = guarded_visitor
    binding = upsert_actor_binding(
        session, "wwebjs", PEER, "missing-explicit", "unknown",
        metadata={"agent_blueprint_id": "synthetic-missing"},
    )
    assert pipeline._blueprint(session, binding, default.tenant_id) == (None, None)


def test_marker_preserves_guard_when_rollout_flag_disappears(session, guarded_visitor, monkeypatch):
    _, blueprint, _ = guarded_visitor
    monkeypatch.setattr(settings, "agent_decision_default_visitor_only", False)
    decision = visitor_decision(session)
    assert decision.agent_blueprint_id == blueprint.id
    # The same immutable package cannot later become a general ranking candidate.
    assert pipeline._blueprint(session, None, blueprint.tenant_id)[1] is None
    # Moving the process default cannot turn an explicit reference into authority.
    monkeypatch.setattr(settings, "agent_decision_default_blueprint_id", None)
    binding = upsert_actor_binding(
        session, "wwebjs", PEER, "owner-marker-reference", "owner",
        metadata={"agent_blueprint_id": blueprint.id},
    )
    assert pipeline._blueprint(session, binding, blueprint.tenant_id)[1] is None
    assert pipeline._blueprint(session, None, blueprint.tenant_id) == (None, None)


@pytest.mark.parametrize("drift", ["domain", "policy", "binding", "marker"])
def test_guard_is_revalidated_before_enqueue(session, guarded_visitor, drift):
    _, _, version = guarded_visitor
    decision = visitor_decision(session)
    assert autonomy.evaluate_and_route(session, decision).automatic_execution_allowed
    if drift == "domain":
        version.spec = {**version.spec, "domain": "food_service"}
        version.checksum = stable_hash(version.spec)
    elif drift == "marker":
        version.spec = {**version.spec, "behavior": {}}
        version.checksum = stable_hash(version.spec)
    elif drift == "binding":
        upsert_actor_binding(session, "wwebjs", PEER, "late-binding", "unknown")
    else:
        policy = session.get(PolicyRow, "desconhecido")
        policy_version = session.get(PolicyVersionRow, policy.current_version_id)
        ensure_policy_version(session, policy, {
            **policy_version.config, "allowed_capabilities": ["grant.create"],
        }, "synthetic late policy widening")
    session.flush()
    assert execution.enqueue_ready_intents(session, transport_ready=True) == 0
    assert not session.scalar(select(OutboxMessageRow))
    assert session.scalar(select(AgentExecutionIntentRow)).blocked_reason == (
        "CONVERSATION_POLICY_STALE" if drift == "policy" else "CONVERSATION_BLUEPRINT_STALE"
    )


def test_guard_preserves_stricter_silence_policy(session, guarded_visitor):
    captured, blueprint, _ = guarded_visitor
    policy = session.get(PolicyRow, "desconhecido")
    config = deepcopy(session.get(PolicyVersionRow, policy.current_version_id).config)
    config["conversational_execution"] = {"execution_mode": "OBSERVE", "allowed_actions": []}
    ensure_policy_version(session, policy, config, "synthetic silence")
    decision = visitor_decision(session)
    assert decision.agent_blueprint_id == blueprint.id and len(captured) == 1
    evaluation = autonomy.evaluate_and_route(session, decision)
    assert not evaluation.automatic_execution_allowed and evaluation.reason_code == "OBSERVE_ONLY"
    assert not session.scalar(select(AgentExecutionIntentRow))


@pytest.mark.parametrize("drift", [
    "blueprint-pointer", "policy-pointer", "policy-status", "policy-checksum",
    "blueprint-domain", "blueprint-checksum",
])
def test_independent_session_cannot_dispatch_cached_visitor_authority(independent_sessions, monkeypatch, drift):
    with independent_sessions() as first:
        _, blueprint, version = setup_guarded_visitor(first, monkeypatch)
        decision = visitor_decision(first)
        assert autonomy.evaluate_and_route(first, decision).automatic_execution_allowed
        policy = first.get(PolicyRow, "desconhecido")
        original_pointer = policy.current_version_id
        original_version = first.get(PolicyVersionRow, original_pointer)
        first.commit()
        monkeypatch.setattr(settings, "agent_decision_default_visitor_only", False)
        with independent_sessions() as second:
            if drift == "blueprint-pointer":
                row = second.get(AgentBlueprintRow, blueprint.id)
                spec = {"autonomy": {"execution_mode": "OBSERVE"}}
                replacement = AgentBlueprintVersionRow(
                    id=new_id(), blueprint_id=row.id, version=2, spec=spec, checksum=stable_hash(spec),
                    created_at=now_utc(), created_by="test", change_reason="synthetic rollback", is_immutable=True,
                )
                second.add(replacement)
                row.current_version_id, row.status = replacement.id, "draft"
            elif drift.startswith("blueprint-"):
                target = second.get(AgentBlueprintVersionRow, version.id)
                if drift == "blueprint-domain":
                    target.spec = {**target.spec, "domain": "food_service"}
                    target.checksum = stable_hash(target.spec)
                else:
                    target.checksum = "0" * 64
            elif drift == "policy-pointer":
                row = second.get(PolicyRow, policy.identifier)
                config = deepcopy(second.get(PolicyVersionRow, row.current_version_id).config)
                config["conversational_execution"] = {"execution_mode": "OBSERVE", "allowed_actions": []}
                ensure_policy_version(second, row, config, "synthetic independent revocation")
            else:
                target = second.get(PolicyVersionRow, original_pointer)
                if drift == "policy-status":
                    target.status = "RETIRED"
                else:
                    insert_invalid_policy_version(
                        second, second.get(PolicyRow, policy.identifier), target.config, bad_checksum=True,
                    )
            second.commit()
        # These old objects are deliberately cached in an expire_on_commit=False session.
        assert blueprint.status == "published" and blueprint.current_version_id == version.id
        assert policy.current_version_id == original_pointer and original_version.status == "ACTIVE"
        assert execution.enqueue_ready_intents(first, transport_ready=True) == 0
        assert not first.scalar(select(OutboxMessageRow))
        assert first.scalar(select(AgentExecutionIntentRow)).blocked_reason in {
            "CONVERSATION_BLUEPRINT_STALE", "CONVERSATION_POLICY_STALE",
        }


def test_visitor_default_setting_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_DECISION_DEFAULT_VISITOR_ONLY", raising=False)
    assert Settings(_env_file=None).agent_decision_default_visitor_only is False


def test_guard_enabled_startup_requires_explicit_default_blueprint():
    for value in (None, "", "   "):
        with pytest.raises(ValueError, match="VISITOR_DEFAULT_BLUEPRINT_REQUIRED"):
            Settings(_env_file=None, agent_decision_default_visitor_only=True,
                     agent_decision_default_blueprint_id=value)
    assert Settings(_env_file=None, agent_decision_default_visitor_only=True,
                    agent_decision_default_blueprint_id="synthetic-approved-id").agent_decision_default_visitor_only
