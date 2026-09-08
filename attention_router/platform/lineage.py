from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class LineageClassification(StrEnum):
    ORGANIC = "ORGANIC"
    SYNTHETIC = "SYNTHETIC"
    HISTORICAL_UNKNOWN = "HISTORICAL_UNKNOWN"


class LineageDenied(ValueError):
    """A structural lineage rule denied an authority or organic write."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class EventLineage:
    tenant_id: str
    actor_id: str
    classification: LineageClassification
    scenario_id: str | None = None
    scenario_run_id: str | None = None
    stimulus_id: str | None = None

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.actor_id:
            raise ValueError("LINEAGE_SCOPE_REQUIRED")
        if self.classification is LineageClassification.SYNTHETIC:
            if not self.scenario_id or not self.scenario_run_id or not self.stimulus_id:
                raise ValueError("SYNTHETIC_SCENARIO_LINEAGE_REQUIRED")
        elif self.scenario_id or self.scenario_run_id or self.stimulus_id:
            raise ValueError("NON_SYNTHETIC_SCENARIO_LINEAGE_FORBIDDEN")

    @property
    def synthetic(self) -> bool:
        return self.classification is LineageClassification.SYNTHETIC

    @property
    def may_promote_to_organic(self) -> bool:
        return self.classification is LineageClassification.ORGANIC


def build_event_lineage(
    *,
    tenant_id: str,
    actor_id: str,
    synthetic: bool | None,
    scenario_id: str | None = None,
    scenario_run_id: str | None = None,
    stimulus_id: str | None = None,
) -> EventLineage:
    """Build lineage without inventing a classification for historical rows."""

    if synthetic is True:
        classification = LineageClassification.SYNTHETIC
    elif synthetic is False:
        classification = LineageClassification.ORGANIC
    else:
        classification = LineageClassification.HISTORICAL_UNKNOWN
    return EventLineage(
        tenant_id=tenant_id,
        actor_id=actor_id,
        classification=classification,
        scenario_id=scenario_id,
        scenario_run_id=scenario_run_id,
        stimulus_id=stimulus_id,
    )


def require_scenario_actor_scope(
    lineage: EventLineage,
    *,
    scenario_tenant_id: str,
    owner_actor_id: str | None,
    actor_category: str | None = None,
) -> None:
    """Enforce tenant and owner separation at the scenario actor boundary."""

    if lineage.tenant_id != scenario_tenant_id:
        raise LineageDenied("CROSS_TENANT_SCENARIO_ACTOR")
    if not lineage.synthetic:
        raise LineageDenied("SCENARIO_ACTOR_NOT_STRUCTURALLY_SYNTHETIC")
    if owner_actor_id and lineage.actor_id == owner_actor_id:
        raise LineageDenied("SYNTHETIC_ACTOR_OWNER_COLLISION")
    if actor_category and actor_category.strip().lower() == "owner":
        raise LineageDenied("SYNTHETIC_ACTOR_OWNER_CATEGORY")


def require_structurally_synthetic_binding(binding: Any) -> Mapping[str, Any]:
    """Validate the single canonical structural marker for a synthetic actor."""

    metadata = getattr(binding, "binding_metadata", None) or {}
    declared_synthetic = metadata.get("synthetic")
    classification = metadata.get("lineage_classification")
    if (
        str(getattr(binding, "actor_category", "")).upper()
        != "SYNTHETIC_TEST_ACTOR"
    ):
        raise LineageDenied("ACTOR_BINDING_NOT_STRUCTURALLY_SYNTHETIC")
    if (declared_synthetic is True) != (
        classification == LineageClassification.SYNTHETIC.value
    ):
        raise LineageDenied("SYNTHETIC_BINDING_METADATA_CONFLICT")
    if (
        declared_synthetic is not True
        or classification != LineageClassification.SYNTHETIC.value
    ):
        raise LineageDenied("ACTOR_BINDING_NOT_STRUCTURALLY_SYNTHETIC")
    return metadata


def require_unambiguous_stimulus_id(
    *,
    event_stimulus_id: str | None,
    binding_stimulus_id: str | None,
    step_stimulus_ids: tuple[str, ...] = (),
) -> str:
    """Resolve one structural stimulus identity, rejecting absence or conflict."""

    candidates = {
        value.strip()
        for value in (event_stimulus_id, binding_stimulus_id, *step_stimulus_ids)
        if isinstance(value, str) and value.strip()
    }
    if not candidates:
        raise LineageDenied("SYNTHETIC_STIMULUS_CORRELATION_MISSING")
    if len(candidates) != 1:
        raise LineageDenied("SYNTHETIC_STIMULUS_CORRELATION_AMBIGUOUS")
    return candidates.pop()


def require_non_owner_authority(
    lineage: EventLineage,
    *,
    self_chat: bool = False,
    owner_only_grant: bool = False,
) -> None:
    """Synthetic actors cannot enter an owner command or owner-grant path."""

    if not lineage.synthetic:
        return
    if self_chat:
        raise LineageDenied("SYNTHETIC_SELF_CHAT_OWNER_PATH_DENIED")
    if owner_only_grant:
        raise LineageDenied("SYNTHETIC_OWNER_ONLY_GRANT_DENIED")


def require_organic_write(lineage: EventLineage, *, writer: str) -> None:
    """Fail closed for memory, fact, and relationship-learning promotion."""

    if lineage.classification is LineageClassification.SYNTHETIC:
        raise LineageDenied(f"SYNTHETIC_{writer.upper()}_PROMOTION_DENIED")
    if lineage.classification is LineageClassification.HISTORICAL_UNKNOWN:
        raise LineageDenied(f"UNKNOWN_LINEAGE_{writer.upper()}_PROMOTION_DENIED")


def sanitized_lineage_reference(lineage: EventLineage) -> dict[str, str | bool | None]:
    """Return only structural identifiers suitable for evidence metadata."""

    return {
        "tenant_id": lineage.tenant_id,
        "actor_id": lineage.actor_id,
        "synthetic": lineage.synthetic,
        "classification": lineage.classification.value,
        "scenario_id": lineage.scenario_id,
        "scenario_run_id": lineage.scenario_run_id,
        "stimulus_id": lineage.stimulus_id,
    }


def stamp_structural_lineage(
    event_row: Any,
    lineage: EventLineage,
    *,
    scenario_step_run_id: str | None = None,
) -> None:
    """Apply lineage only at the canonical event boundary after tenant validation."""

    if event_row.tenant_id != lineage.tenant_id:
        raise LineageDenied("CROSS_TENANT_EVENT_LINEAGE_DENIED")
    if scenario_step_run_id and not lineage.synthetic:
        raise LineageDenied("NON_SYNTHETIC_SCENARIO_STEP_LINEAGE_DENIED")
    event_row.lineage_classification = lineage.classification.value
    event_row.scenario_run_id = lineage.scenario_run_id
    event_row.scenario_step_run_id = scenario_step_run_id


def lineage_from_event_row(
    event_row: Any,
    *,
    actor_id: str | None = None,
    scenario_id: str | None = None,
    stimulus_id: str | None = None,
) -> EventLineage:
    resolved_actor = actor_id or getattr(event_row, "actor_id", None)
    if not resolved_actor:
        raise LineageDenied("EVENT_ACTOR_REQUIRED_FOR_LINEAGE")
    try:
        classification = LineageClassification(event_row.lineage_classification)
    except (AttributeError, ValueError) as exc:
        raise LineageDenied("EVENT_LINEAGE_CLASSIFICATION_INVALID") from exc
    payload = getattr(event_row, "payload", None) or {}
    metadata = getattr(event_row, "metadata_sanitized", None) or {}
    return EventLineage(
        tenant_id=event_row.tenant_id,
        actor_id=resolved_actor,
        classification=classification,
        scenario_id=(
            scenario_id
            or getattr(event_row, "scenario_id", None)
            or payload.get("scenario_id")
            or metadata.get("scenario_id")
        ),
        scenario_run_id=getattr(event_row, "scenario_run_id", None),
        stimulus_id=(
            stimulus_id
            or getattr(event_row, "stimulus_id", None)
            or payload.get("stimulus_id")
            or metadata.get("stimulus_id")
        ),
    )
