from dataclasses import asdict, dataclass, field

from attention_router.application.conversation_language import resolve_conversation_locale


@dataclass(frozen=True)
class ActionCapability:
    action_type: str
    available: bool = True
    mode: str = "proposal_only"
    required_parameters: list[str] = field(default_factory=list)
    optional_parameters: list[str] = field(default_factory=list)
    known_channel: str | None = None
    description: str = ""


@dataclass(frozen=True)
class AllowedAgentContext:
    actor_id: str | None
    binding_id: str | None
    audience: str
    policy_summary: str
    allowed_disclosures: list[str] = field(default_factory=list)
    interaction_actor: dict[str, str] | None = None
    represented_subject: dict[str, str] | None = None
    allowed_facts: list[str] = field(default_factory=list)
    recent_turns: list[dict[str, str]] = field(default_factory=list)
    available_action_capabilities: list[str] = field(default_factory=list)
    current_message: str = ""
    relationship: str | None = None
    contact_return_channel_available: bool = False
    whatsapp_return_channel_available: bool = False
    already_known_information: list[str] = field(default_factory=list)
    action_capabilities: list[ActionCapability] = field(default_factory=list)
    available_capabilities: list[dict] = field(default_factory=list)
    current_operational_state: list[dict] = field(default_factory=list)
    relevant_facts: list[dict] = field(default_factory=list)
    relevant_decisions: list[dict] = field(default_factory=list)
    effective_authority: list[dict] = field(default_factory=list)
    effective_standing_directives: list[dict] = field(default_factory=list)
    communication_intent: dict[str, object] | None = None
    response_locale: str = ""
    response_locale_source: str = ""

    def __post_init__(self) -> None:
        if self.response_locale:
            return
        resolved = resolve_conversation_locale(self.current_message, self.recent_turns)
        object.__setattr__(self, "response_locale", resolved.locale)
        object.__setattr__(self, "response_locale_source", resolved.source)

    def prompt_payload(self) -> dict:
        payload = {k: v for k, v in asdict(self).items() if k not in {"actor_id", "binding_id"}}
        payload["action_capabilities"] = [asdict(item) for item in self.action_capabilities]
        return payload
