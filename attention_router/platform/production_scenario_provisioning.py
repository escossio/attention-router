"""Versioned, inert provisioning for SCN-PE-029 v2 production authority."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from attention_router.infrastructure.models import (
    ExecutionClassVersionRow,
    ExecutionIntentRow,
    ExecutionIntentTargetBindingRow,
    PolicyRow,
    PolicyVersionRow,
    RecipientEndpointRow,
    SafetySetVersionRow,
    ScenarioDefinitionRow,
    ScenarioVersionAuthorityBindingRow,
    ScenarioVersionRow,
    StaticIntentAuthorityProfileRow,
)
from attention_router.platform.production_authority import canonical_recipient_address


SCENARIO_KEY = "SCN-PE-029"
SCENARIO_VERSION = 2
TRANSPORT = "meta_whatsapp"
OPERATION = CAPABILITY = "conversation.reply"
EXECUTION_CLASS_ID = "scn-pe-029-production-reply-class-v1"
SAFETY_SET_ID = "scn-pe-029-production-reply-safety-v1"
AUTHORITY_PROFILE_ID = "scn-pe-029-production-reply-profile-v1"


@dataclass(frozen=True, slots=True)
class StaticProductionScenarioAuthority:
    scenario: ScenarioVersionRow
    execution_class: ExecutionClassVersionRow
    safety_set: SafetySetVersionRow
    policy: PolicyVersionRow
    authority_profile: StaticIntentAuthorityProfileRow


@dataclass(frozen=True, slots=True)
class ProductionScenarioAuthority:
    """Compatibility view for isolated callers that also prepare a recipient."""
    static: StaticProductionScenarioAuthority
    recipient_endpoint: RecipientEndpointRow

    @property
    def scenario(self) -> ScenarioVersionRow:
        return self.static.scenario

    @property
    def execution_class(self) -> ExecutionClassVersionRow:
        return self.static.execution_class

    @property
    def safety_set(self) -> SafetySetVersionRow:
        return self.static.safety_set

    @property
    def policy(self) -> PolicyVersionRow:
        return self.static.policy

    @property
    def authority_profile(self) -> StaticIntentAuthorityProfileRow:
        return self.static.authority_profile


def _checksum(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def provision_production_conversation_reply_authority(
    session: Session,
    *,
    tenant_id: str,
    policy_id: str,
    source_sha: str,
    now: datetime | None = None,
) -> StaticProductionScenarioAuthority:
    """Create/reuse SCN-PE-029 v2 authority without recipient or execution identity."""
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    policy_row = session.get(PolicyRow, policy_id)
    if policy_row is None or policy_row.tenant_id != tenant_id or OPERATION not in policy_row.allowed_actions:
        raise ValueError("PRODUCTION_POLICY_NOT_ELIGIBLE")
    policy = _production_policy_version(session, policy_row, timestamp)
    execution_class = _get_or_create_execution_class(session, timestamp)
    safety_set = _get_or_create_safety_set(session, execution_class, timestamp)
    profile = _get_or_create_profile(session, timestamp)
    scenario = _get_or_create_scenario(session, tenant_id, source_sha, timestamp)
    _bind_scenario(session, scenario, execution_class, safety_set, policy, profile, timestamp)
    return StaticProductionScenarioAuthority(scenario, execution_class, safety_set, policy, profile)


def prepare_production_conversation_reply_target(
    session: Session,
    *,
    execution_intent_id: str,
    tenant_id: str,
    recipient_address: str,
    now: datetime | None = None,
) -> RecipientEndpointRow:
    """Bind exactly one dynamic recipient to a PREPARED production ExecutionIntent."""
    parent = session.get(ExecutionIntentRow, execution_intent_id)
    if parent is None or parent.state != "PREPARED":
        raise ValueError("PRODUCTION_TARGET_REQUIRES_PREPARED_EXECUTION_INTENT")
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    endpoint = _resolve_production_recipient_endpoint(
        session, tenant_id=tenant_id, recipient_address=recipient_address, timestamp=timestamp,
    )
    existing = list(session.scalars(select(ExecutionIntentTargetBindingRow).where(
        ExecutionIntentTargetBindingRow.execution_intent_id == parent.id
    )))
    if existing:
        if len(existing) == 1 and existing[0].recipient_endpoint_id == endpoint.id:
            return endpoint
        raise ValueError("PRODUCTION_EXECUTION_INTENT_TARGET_ALREADY_BOUND")
    session.add(ExecutionIntentTargetBindingRow(
        id=f"production-target-{_checksum({'intent': parent.id, 'endpoint': endpoint.id})[:24]}",
        execution_intent_id=parent.id, target_type="WHATSAPP_RECIPIENT_ENDPOINT",
        recipient_endpoint_id=endpoint.id, ordinal=0, created_at=timestamp,
    ))
    session.flush()
    return endpoint


def _resolve_production_recipient_endpoint(
    session: Session,
    *,
    tenant_id: str,
    recipient_address: str,
    timestamp: datetime,
) -> RecipientEndpointRow:
    """Resolve/create only dynamic recipient identity; never provisions static authority."""
    address = canonical_recipient_address(TRANSPORT, recipient_address)
    endpoint = session.scalar(select(RecipientEndpointRow).where(
        RecipientEndpointRow.tenant_id == tenant_id,
        RecipientEndpointRow.transport == TRANSPORT,
        RecipientEndpointRow.canonical_address == address,
    ))
    if endpoint is None:
        endpoint = RecipientEndpointRow(
            id=f"scn-pe-029-endpoint-{_checksum({'tenant': tenant_id, 'address': address})[:24]}",
            tenant_id=tenant_id, transport=TRANSPORT, canonical_address=address,
            status="ACTIVE", created_at=timestamp,
        )
        session.add(endpoint)
        session.flush()
    if endpoint.status != "ACTIVE":
        raise ValueError("PRODUCTION_RECIPIENT_ENDPOINT_NOT_ACTIVE")
    return endpoint


def provision_production_conversation_reply(
    session: Session,
    *,
    tenant_id: str,
    policy_id: str,
    recipient_address: str,
    source_sha: str,
    now: datetime | None = None,
) -> ProductionScenarioAuthority:
    """Compatibility composition for isolated fixtures; production deploy uses static API."""
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    static = provision_production_conversation_reply_authority(
        session, tenant_id=tenant_id, policy_id=policy_id, source_sha=source_sha, now=timestamp,
    )
    endpoint = _resolve_production_recipient_endpoint(
        session, tenant_id=tenant_id, recipient_address=recipient_address, timestamp=timestamp,
    )
    return ProductionScenarioAuthority(static=static, recipient_endpoint=endpoint)


def _production_policy_version(session: Session, row: PolicyRow, timestamp: datetime) -> PolicyVersionRow:
    config = {"allowed_actions": [OPERATION], "authority_scope": "single-recipient-reply"}
    checksum = _checksum(config)
    existing = session.scalar(select(PolicyVersionRow).where(
        PolicyVersionRow.policy_id == row.identifier,
        PolicyVersionRow.checksum == checksum,
        PolicyVersionRow.environment_classification == "PRODUCTION",
    ))
    if existing is not None:
        return existing
    version = (session.scalar(select(func.max(PolicyVersionRow.version)).where(
        PolicyVersionRow.policy_id == row.identifier
    )) or 0) + 1
    item = PolicyVersionRow(
        id=f"prod-reply-policy-{_checksum({'policy': row.identifier, 'version': version})[:32]}",
        policy_id=row.identifier, version=version, config=config, checksum=checksum,
        created_at=timestamp, created_by="production-scenario-provisioning",
        is_immutable=True, environment_classification="PRODUCTION", status="ACTIVE",
    )
    session.add(item)
    session.flush()
    return item


def _get_or_create_execution_class(session: Session, timestamp: datetime) -> ExecutionClassVersionRow:
    item = session.get(ExecutionClassVersionRow, EXECUTION_CLASS_ID)
    if item is None:
        item = ExecutionClassVersionRow(
            id=EXECUTION_CLASS_ID, identity="production-conversation-reply", version=1,
            environment_classification="PRODUCTION", status="ACTIVE",
            allowed_transports=[TRANSPORT], allowed_operations=[OPERATION],
            allowed_capabilities=[CAPABILITY], external_effect_class="CONTROLLED_EXTERNAL_MESSAGE",
            max_target_cardinality=1, requires_human_authorization=True,
            requires_fresh_readiness=True, requires_handoff=True,
            requires_bounded_authorization=True, direct_execution_allowed=False,
            created_at=timestamp, is_immutable=True,
        )
        session.add(item)
        session.flush()
    return item


def _get_or_create_safety_set(session: Session, execution_class: ExecutionClassVersionRow, timestamp: datetime) -> SafetySetVersionRow:
    item = session.get(SafetySetVersionRow, SAFETY_SET_ID)
    if item is None:
        item = SafetySetVersionRow(
            id=SAFETY_SET_ID, identity="production-conversation-reply-safety", version=1,
            environment_classification="PRODUCTION", status="ACTIVE",
            execution_class_version_id=execution_class.id, allowed_transport=TRANSPORT,
            allowed_operation=OPERATION, allowed_capability=CAPABILITY,
            max_target_cardinality=1, max_outbound_messages=1, max_action_count=1,
            required_invariants=["single-recipient", "human-approved", "approved-stop"],
            created_at=timestamp, is_immutable=True,
        )
        session.add(item)
        session.flush()
    return item


def _get_or_create_profile(session: Session, timestamp: datetime) -> StaticIntentAuthorityProfileRow:
    item = session.get(StaticIntentAuthorityProfileRow, AUTHORITY_PROFILE_ID)
    if item is None:
        item = StaticIntentAuthorityProfileRow(
            id=AUTHORITY_PROFILE_ID, identity="production-conversation-reply-authority", version=1,
            environment_classification="PRODUCTION", status="ACTIVE",
            max_target_cardinality=1, max_outbound_messages=1, max_action_count=1,
            max_retries=0, allowed_transport=TRANSPORT, allowed_operation=OPERATION,
            allowed_capability=CAPABILITY, created_at=timestamp, is_immutable=True,
        )
        session.add(item)
        session.flush()
    return item


def _get_or_create_scenario(session: Session, tenant_id: str, source_sha: str, timestamp: datetime) -> ScenarioVersionRow:
    definition = session.scalar(select(ScenarioDefinitionRow).where(
        ScenarioDefinitionRow.tenant_id == tenant_id, ScenarioDefinitionRow.scenario_key == SCENARIO_KEY
    ))
    if definition is None:
        definition = ScenarioDefinitionRow(
            id="scn-pe-029-production-definition", tenant_id=tenant_id, scenario_key=SCENARIO_KEY,
            title="Production conversation reply", description="Production-only human-approved reply authority.",
            enabled=True, created_at=timestamp, updated_at=timestamp,
        )
        session.add(definition)
        session.flush()
    item = session.scalar(select(ScenarioVersionRow).where(
        ScenarioVersionRow.scenario_definition_id == definition.id,
        ScenarioVersionRow.version == SCENARIO_VERSION,
    ))
    if item is None:
        content_hash = _checksum({"scenario": SCENARIO_KEY, "version": SCENARIO_VERSION, "operation": OPERATION})
        item = ScenarioVersionRow(
            id="scn-pe-029-production-v2", tenant_id=tenant_id, scenario_definition_id=definition.id,
            version=SCENARIO_VERSION, schema_version="production-authority-v1",
            manifest_source_path="provisioning/scn_pe_029_production_v2", content_hash=content_hash,
            requirements_covered=["human-approval", "single-recipient"], risk_ids=["controlled-external-reply"],
            config_keys=["authority_profile", "recipient_endpoint"], risk_classification="CONTROLLED",
            enabled=True, source_sha=source_sha, created_at=timestamp, is_immutable=True,
            environment_classification="PRODUCTION",
        )
        session.add(item)
        session.flush()
    if item.environment_classification != "PRODUCTION" or not item.enabled:
        raise ValueError("PRODUCTION_SCENARIO_NOT_ELIGIBLE")
    return item


def _bind_scenario(session: Session, scenario, execution_class, safety_set, policy, profile, timestamp: datetime) -> None:
    expected = {
        "EXECUTION_CLASS": {"execution_class_version_id": execution_class.id},
        "SAFETY_SET": {"safety_set_version_id": safety_set.id},
        "POLICY": {"policy_version_id": policy.id},
        "AUTHORITY_PROFILE": {"authority_profile_id": profile.id},
    }
    existing = {row.binding_role: row for row in session.scalars(select(ScenarioVersionAuthorityBindingRow).where(
        ScenarioVersionAuthorityBindingRow.scenario_version_id == scenario.id
    ))}
    for role, values in expected.items():
        if role not in existing:
            session.add(ScenarioVersionAuthorityBindingRow(
                scenario_version_id=scenario.id, binding_role=role, created_at=timestamp, **values,
            ))
    session.flush()


__all__ = [
    "ProductionScenarioAuthority",
    "StaticProductionScenarioAuthority",
    "prepare_production_conversation_reply_target",
    "provision_production_conversation_reply",
    "provision_production_conversation_reply_authority",
]
