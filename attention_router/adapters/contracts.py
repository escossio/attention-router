from typing import Protocol

from attention_router.domain.models import Interaction, Policy


class LanguageAdapter(Protocol):
    def render_reply(self, interaction: Interaction, policy: Policy) -> str: ...


class ChannelAdapter(Protocol):
    def accept_event(self, payload: dict) -> dict: ...


class ActionAdapter(Protocol):
    def dispatch(self, action_key: str, interaction_id: str) -> dict: ...

