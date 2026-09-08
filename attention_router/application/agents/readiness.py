"""Read-only ANDY invocation readiness."""
from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from attention_router.config import settings


@dataclass(frozen=True, slots=True)
class AndyReadiness:
    state: str
    reason_code: str
    observed_at: datetime


def get_andy_readiness(*, now: datetime | None = None) -> AndyReadiness:
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    if not settings.agent_decision_pipeline_enabled:
        return AndyReadiness("NOT_READY", "ANDY_PIPELINE_DISABLED", observed_at)
    if settings.andy_agent_enabled:
        if importlib.util.find_spec("agents") is None:
            return AndyReadiness("UNKNOWN", "ANDY_AGENT_DEPENDENCY_MISSING", observed_at)
        if not settings.andy_agent_model.strip() or settings.andy_agent_max_turns <= 0:
            return AndyReadiness("NOT_READY", "ANDY_AGENT_CONFIGURATION_INVALID", observed_at)
        if not settings.openai_api_key or not settings.openai_api_key.strip():
            return AndyReadiness("NOT_READY", "ANDY_AGENT_API_KEY_MISSING", observed_at)
        return AndyReadiness("READY", "ANDY_AGENT_READY", observed_at)
    if settings.andy_behavior_enabled:
        path = Path(settings.andy_behavior_profile_path)
        try:
            profile = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return AndyReadiness("UNKNOWN", "ANDY_BEHAVIOR_PROFILE_UNAVAILABLE", observed_at)
        if not isinstance(profile, dict) or not settings.andy_behavior_canary_binding_id:
            return AndyReadiness("NOT_READY", "ANDY_BEHAVIOR_CONFIGURATION_INVALID", observed_at)
        return AndyReadiness("READY", "ANDY_BEHAVIOR_READY", observed_at)
    return AndyReadiness("NOT_READY", "ANDY_RUNTIME_DISABLED", observed_at)
