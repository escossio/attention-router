from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EntityKind(StrEnum):
    ACTOR = "ACTOR"
    RESOURCE = "RESOURCE"
    DEVICE = "DEVICE"
    RELATIONSHIP = "RELATIONSHIP"
    GENERIC = "GENERIC"


class FactClass(StrEnum):
    CLAIM = "CLAIM"
    EVIDENCE = "EVIDENCE"
    AUTHORITATIVE_FACT = "AUTHORITATIVE_FACT"
    EXECUTED_FACT = "EXECUTED_FACT"


class EntityReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: str = Field(min_length=1, max_length=40)
    entity_id: str = Field(min_length=1, max_length=120)


class ResourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_type: str = Field(min_length=1, max_length=80)
    canonical_name: str = Field(min_length=1, max_length=160)
    metadata_sanitized: dict[str, Any] = Field(default_factory=dict)
