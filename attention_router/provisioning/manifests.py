from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from attention_router.core.capabilities import (
    CAPABILITY_NAME_RE,
    CapabilityAvailability,
    OperationType,
)
from attention_router.infrastructure.hashing import stable_hash


REPO_ROOT = Path(__file__).resolve().parents[2]
CAPABILITY_MANIFEST_PATH = REPO_ROOT / "config/platform/capabilities.v1.json"
PROVIDER_MANIFEST_PATH = REPO_ROOT / "config/platform/providers.v1.json"


class CapabilityManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_name: str
    version: int = Field(ge=1)
    domain: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1)
    operation_type: OperationType
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    required_permissions: list[str]
    required_provider_interface: str | None = None
    sensitivity: str = Field(min_length=1, max_length=40)
    side_effect: bool
    default_approval_policy: str = Field(min_length=1, max_length=40)
    availability_state: CapabilityAvailability
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("canonical_name")
    @classmethod
    def canonical_capability_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not CAPABILITY_NAME_RE.fullmatch(normalized):
            raise ValueError("invalid capability canonical name")
        return normalized

    @model_validator(mode="after")
    def provider_state_is_coherent(self) -> "CapabilityManifestEntry":
        if self.availability_state == CapabilityAvailability.PROVIDER_MISSING and not self.required_provider_interface:
            raise ValueError("PROVIDER_MISSING requires required_provider_interface")
        return self

    @property
    def checksum(self) -> str:
        return stable_hash(self.model_dump(mode="json"))


class CapabilityManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"]
    capabilities: list[CapabilityManifestEntry]

    @model_validator(mode="after")
    def unique_entries(self) -> "CapabilityManifest":
        keys = [(item.canonical_name, item.version) for item in self.capabilities]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate capability name/version")
        return self


class ProviderManifestEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    interface_name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1)
    contract_version: int = Field(ge=1)


class ProviderManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"]
    providers: list[ProviderManifestEntry]

    @model_validator(mode="after")
    def unique_entries(self) -> "ProviderManifest":
        names = [item.canonical_name for item in self.providers]
        if len(names) != len(set(names)):
            raise ValueError("duplicate provider definition")
        return self


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_capability_manifest(path: Path = CAPABILITY_MANIFEST_PATH) -> CapabilityManifest:
    return CapabilityManifest.model_validate(_load(path))


def load_provider_manifest(path: Path = PROVIDER_MANIFEST_PATH) -> ProviderManifest:
    return ProviderManifest.model_validate(_load(path))
