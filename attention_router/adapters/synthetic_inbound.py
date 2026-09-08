from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from attention_router.adapters.inbound import InboundAdapter, NormalizedInboundEvent


class SyntheticInboundPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["synthetic-1"] = "synthetic-1"
    synthetic_event_id: str = Field(min_length=1, max_length=180)
    event_type: Literal["message", "call"]
    contact_id: str = Field(min_length=1, max_length=120)
    contact_name: str = Field(min_length=1, max_length=160)
    relationship_category: str = Field(min_length=1, max_length=120)
    active_context: str | None = None
    text: str = Field(min_length=1)
    channel: str = "synthetic"
    metadata: dict[str, Any] = Field(default_factory=dict)
    scenario_id: str = Field(min_length=1, max_length=64)
    scenario_run_id: str = Field(min_length=1, max_length=64)
    scenario_step_run_id: str | None = Field(default=None, max_length=64)
    stimulus_id: str = Field(min_length=1, max_length=128)


class SyntheticInboundAdapter(InboundAdapter):
    source = "synthetic"

    def normalize(self, raw_event: dict[str, Any]) -> NormalizedInboundEvent:
        payload = SyntheticInboundPayload.model_validate(raw_event)
        return NormalizedInboundEvent(
            schema_version="1",
            source=self.source,
            external_event_id=payload.synthetic_event_id,
            event_type=payload.event_type,
            actor_id=payload.contact_id,
            actor_display_name=payload.contact_name,
            actor_category=payload.relationship_category,
            active_context=payload.active_context,
            channel=payload.channel,
            content=payload.text,
            metadata=payload.metadata,
            lineage_classification="SYNTHETIC",
            scenario_id=payload.scenario_id,
            scenario_run_id=payload.scenario_run_id,
            scenario_step_run_id=payload.scenario_step_run_id,
            stimulus_id=payload.stimulus_id,
            raw_reference=f"synthetic:{payload.synthetic_event_id}",
        )

    def normalize_many(self, raw_event: dict[str, Any]) -> list[NormalizedInboundEvent]:
        return [self.normalize(raw_event)]
