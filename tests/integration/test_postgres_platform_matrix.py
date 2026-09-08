from __future__ import annotations

import threading

import pytest
from sqlalchemy import select

from attention_router.application.platform.entities import set_entity_state
from attention_router.application.platform.registry import sync_platform_registry
from attention_router.core.entities import EntityReference
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import now_utc
from attention_router.infrastructure.models import ActorBindingRow, EntityStateRow, TenantRow


pytestmark = pytest.mark.postgres


def _binding(tenant_id: str, row_id: str) -> ActorBindingRow:
    return ActorBindingRow(
        id=row_id,
        tenant_id=tenant_id,
        source="wwebjs",
        external_actor_id="same-external@lid",
        actor_key="same-actor",
        actor_category="test",
        binding_metadata={},
        is_active=True,
        created_at=now_utc(),
        updated_at=now_utc(),
    )


def test_postgres_tenant_scoped_actor_identity_and_registry(Session):
    tenant_b = "00000000-0000-4000-8000-000000000092"
    with Session() as session:
        sync_platform_registry(session)
        session.add(TenantRow(
            id=tenant_b,
            slug="tenant_pg_b",
            name="Tenant PG B",
            status="ACTIVE",
            created_at=now_utc(),
            updated_at=now_utc(),
        ))
        session.flush()
        session.add_all([
            _binding(DEFAULT_TENANT_ID, "binding-pg-a"),
            _binding(tenant_b, "binding-pg-b"),
        ])
        session.commit()
    with Session() as session:
        assert session.scalar(select(TenantRow).where(TenantRow.id == tenant_b))
        assert session.scalar(
            select(ActorBindingRow).where(
                ActorBindingRow.tenant_id == DEFAULT_TENANT_ID,
                ActorBindingRow.external_actor_id == "same-external@lid",
            )
        ).id == "binding-pg-a"
        assert session.scalar(
            select(ActorBindingRow).where(
                ActorBindingRow.tenant_id == tenant_b,
                ActorBindingRow.external_actor_id == "same-external@lid",
            )
        ).id == "binding-pg-b"


def test_postgres_state_optimistic_version_is_atomic(Session):
    with Session() as session:
        if session.get(ActorBindingRow, "binding-state-pg") is None:
            session.add(ActorBindingRow(
                id="binding-state-pg",
                tenant_id=DEFAULT_TENANT_ID,
                source="wwebjs",
                external_actor_id="state-pg@lid",
                actor_key="state-pg",
                actor_category="test",
                binding_metadata={},
                is_active=True,
                created_at=now_utc(),
                updated_at=now_utc(),
            ))
            session.flush()
        set_entity_state(
            session,
            tenant_id=DEFAULT_TENANT_ID,
            subject=EntityReference(entity_type="ACTOR", entity_id="state-pg"),
            namespace="test",
            key="mode",
            value={"value": "initial"},
            source="postgres-test",
            expected_version=0,
        )
        session.commit()

    outcomes: list[str] = []

    def update_state(value: str) -> None:
        with Session() as session:
            try:
                set_entity_state(
                    session,
                    tenant_id=DEFAULT_TENANT_ID,
                    subject=EntityReference(entity_type="ACTOR", entity_id="state-pg"),
                    namespace="test",
                    key="mode",
                    value={"value": value},
                    source="postgres-test",
                    expected_version=1,
                )
                session.commit()
                outcomes.append("updated")
            except ValueError as exc:
                session.rollback()
                outcomes.append(str(exc))

    threads = [threading.Thread(target=update_state, args=(value,)) for value in ("one", "two")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("updated") == 1
    assert outcomes.count("STATE_VERSION_CONFLICT") == 1
    with Session() as session:
        row = session.scalar(select(EntityStateRow).where(
            EntityStateRow.tenant_id == DEFAULT_TENANT_ID,
            EntityStateRow.subject_id == "state-pg",
        ))
        assert row.version == 2
