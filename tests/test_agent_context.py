from attention_router.application.agents.context import ActionCapability, AllowedAgentContext


def test_context_exposes_capability_requirements_without_raw_identity_data():
    context = AllowedAgentContext(
        actor_id="sha256:actor",
        binding_id="sha256:binding",
        audience="autonomy_canary",
        policy_summary="policy=autonomy_canary_v1; mode=REQUIRES_APPROVAL",
        relationship="unknown",
        contact_return_channel_available=True,
        whatsapp_return_channel_available=True,
        already_known_information=["actor_identity", "current_whatsapp_return_channel"],
        action_capabilities=[
            ActionCapability(
                action_type="request_callback",
                optional_parameters=["callback_number"],
                known_channel="current_whatsapp_return_channel",
            )
        ],
        current_message="pedido de callback",
    )

    payload = context.prompt_payload()

    assert "actor_id" not in payload
    assert "binding_id" not in payload
    assert payload["contact_return_channel_available"] is True
    assert payload["action_capabilities"][0]["required_parameters"] == []
    assert payload["action_capabilities"][0]["optional_parameters"] == ["callback_number"]


def test_unavailable_capability_is_explicit_not_executable():
    capability = ActionCapability(action_type="notify_alex", available=False)

    assert capability.available is False
    assert capability.mode == "proposal_only"


def test_presence_state_and_disclosure_permission_reach_agent_payload():
    context = AllowedAgentContext(
        actor_id="sha256:actor",
        binding_id="sha256:binding",
        audience="autonomy_canary",
        policy_summary="policy=morgan_presence_autonomy_v1; mode=AUTO_ALLOWED",
        allowed_disclosures=["availability_hint"],
        interaction_actor={"type": "ACTOR", "id": "actor_morgan"},
        represented_subject={"type": "ACTOR", "id": "actor_owner"},
        current_operational_state=[
            {
                "namespace": "presence",
                "key": "effective",
                "value": {"status": "sleeping", "audience_scope": "everyone"},
                "effective": True,
            }
        ],
        current_message="oi",
        communication_intent={"disclose_current_availability_when_relevant": True},
    )

    payload = context.prompt_payload()

    assert payload["allowed_disclosures"] == ["availability_hint"]
    assert payload["represented_subject"] == {"type": "ACTOR", "id": "actor_owner"}
    assert payload["current_operational_state"][0]["value"] == {
        "status": "sleeping",
        "audience_scope": "everyone",
    }
    assert payload["communication_intent"]["disclose_current_availability_when_relevant"] is True
