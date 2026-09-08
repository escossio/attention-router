#!/usr/bin/env python3
"""Synthetic 0013 fixture used only for isolated migration compatibility proofs."""

from __future__ import annotations

from sqlalchemy import MetaData, Table, update

from attention_router.config import settings
from attention_router.domain.models import now_utc
from attention_router.infrastructure.db import SessionLocal


def _table(metadata: MetaData, name: str) -> Table:
    return metadata.tables[name]


def main() -> int:
    if settings.app_env != "test" or "matrix_" not in settings.database_url:
        raise RuntimeError("legacy fixture is restricted to isolated Matrix test databases")
    metadata = MetaData()
    metadata.reflect(bind=SessionLocal.kw["bind"])
    stamp = now_utc()
    policy_config = {
        "identifier": "legacy-policy",
        "name": "Legacy Fixture",
        "match_criteria": {"relationship_category": ["test"]},
        "priority": 1,
        "specificity": 1,
        "tone": "neutral",
        "initial_wait_seconds": 1,
        "allowed_disclosures": [],
        "allowed_actions": [],
        "escalation_steps": [],
        "ack_timeout_seconds": 300,
        "repetition_limit": 1,
        "cancellation_conditions": [],
        "completion_conditions": [],
    }
    with SessionLocal() as session:
        policies = _table(metadata, "policies")
        versions = _table(metadata, "policy_versions")
        session.execute(policies.insert().values(**policy_config, current_version_id=None, is_active=True))
        session.execute(versions.insert().values(
            id="legacy-policy-version",
            policy_id="legacy-policy",
            version=1,
            config=policy_config,
            checksum="legacy-policy-checksum",
            created_at=stamp,
            created_by="migration_test",
            is_immutable=True,
        ))
        session.execute(
            update(policies)
            .where(policies.c.identifier == "legacy-policy")
            .values(current_version_id="legacy-policy-version")
        )
        session.execute(_table(metadata, "actor_bindings").insert().values(
            id="legacy-binding",
            source="synthetic",
            external_actor_id="legacy-external",
            actor_key="legacy-actor",
            display_name="Legacy Fixture",
            actor_category="test",
            active_context=None,
            is_active=True,
            metadata={},
            created_at=stamp,
            updated_at=stamp,
        ))
        session.execute(_table(metadata, "interactions").insert().values(
            id="legacy-interaction",
            event_type="message",
            contact_id="legacy-actor",
            contact_name="Legacy Fixture",
            relationship_category="test",
            active_context=None,
            inbound_text="synthetic migration fixture",
            state="RECEIVED",
            policy_id="legacy-policy",
            policy_version_id="legacy-policy-version",
            correlation_id="legacy-correlation",
            causation_id=None,
            lia_speech=None,
            created_at=stamp,
            updated_at=stamp,
        ))
        session.execute(_table(metadata, "inbound_events").insert().values(
            id="legacy-inbound",
            source="synthetic",
            external_event_id="legacy-event",
            event_type="message",
            payload={"channel": "synthetic", "actor_id": "legacy-actor"},
            payload_hash="legacy-payload-hash",
            received_at=stamp,
            processed_at=stamp,
            interaction_id="legacy-interaction",
            status="PROCESSED",
            error=None,
            correlation_id="legacy-correlation",
        ))
        session.execute(_table(metadata, "agent_decisions").insert().values(
            id="legacy-agent-decision",
            event_id="legacy-inbound",
            interaction_id="legacy-interaction",
            actor_id="legacy-actor",
            actor_binding_id="legacy-binding",
            audience="test",
            policy_version_id="legacy-policy-version",
            decision_pipeline_version="v1",
            decision_type="RESPOND",
            recommended_action="respond",
            proposed_response="Synthetic response.",
            escalation_required=False,
            confidence=1.0,
            missing_information=[],
            intent="fixture",
            objective="fixture/migration",
            self_contained=True,
            context_sufficient=True,
            context_requirements=[],
            response_source="OPENAI_AGENTS_SDK",
            execution_allowed=False,
            external_delivery_allowed=False,
            reasoning_summary="fixture",
            status="DRY_RUN",
            created_at=stamp,
        ))
        session.execute(_table(metadata, "agent_response_reviews").insert().values(
            id="legacy-review",
            agent_decision_id="legacy-agent-decision",
            status="PENDING",
            proposed_response_snapshot="Synthetic response.",
            effective_response="Synthetic response.",
            reviewer_type="operator",
            version=1,
            created_at=stamp,
            updated_at=stamp,
        ))
        session.execute(_table(metadata, "memory_actors").insert().values(
            id="legacy-memory-actor",
            actor_key="legacy-actor",
            metadata={},
            created_at=stamp,
            updated_at=stamp,
        ))
        session.execute(_table(metadata, "conversation_threads").insert().values(
            id="legacy-thread",
            source="synthetic",
            source_account="default",
            external_thread_key="legacy-thread",
            thread_type="DIRECT",
            title="Legacy Fixture",
            first_message_at=stamp,
            last_message_at=stamp,
            created_at=stamp,
            updated_at=stamp,
        ))
        session.execute(_table(metadata, "conversation_messages").insert().values(
            id="legacy-message",
            conversation_id="legacy-thread",
            source="synthetic",
            source_account="default",
            source_message_id="legacy-message",
            sender_actor_id="legacy-memory-actor",
            external_sender_key="legacy-actor",
            direction="INBOUND",
            from_me=False,
            sent_at=stamp,
            message_type="TEXT",
            text="synthetic migration fixture",
            normalized_text="synthetic migration fixture",
            content_hash="legacy-content-hash",
            sensitivity_class="NORMAL",
            searchable=True,
            metadata={},
            reply_to_message_id=None,
            imported_at=stamp,
            created_at=stamp,
        ))
        session.execute(_table(metadata, "audit_events").insert().values(
            id="legacy-audit",
            interaction_id="legacy-interaction",
            event_type="legacy_fixture_created",
            payload={"fixture": True},
            created_at=stamp,
            correlation_id="legacy-correlation",
            causation_id=None,
            previous_state=None,
            next_state=None,
            policy_version_id="legacy-policy-version",
            origin="migration_test",
        ))
        session.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
