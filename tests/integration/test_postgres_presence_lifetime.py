from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

import pytest
from sqlalchemy import select

from attention_router.application.platform.capability_pack import (
    InternalPresenceProvider,
    provision_internal_providers,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.domain.models import new_id, now_utc
from attention_router.infrastructure.models import ActorBindingRow, EntityStateRow


pytestmark = pytest.mark.postgres

OWNER = "postgres-presence-lifetime-owner"


def _params(state, ttl, correlation):
    return {
        "state": state,
        "ttl_seconds": ttl,
        "_execution": {
            "actor_id": OWNER,
            "correlation_id": correlation,
            "capability": "presence.set",
            "owner_authenticated": True,
        },
    }


def test_concurrent_presence_updates_keep_last_writer_integral_and_finite(Session):
    with Session() as setup:
        stamp = now_utc()
        setup.add(
            ActorBindingRow(
                id=new_id(),
                tenant_id=DEFAULT_TENANT_ID,
                source="test",
                external_actor_id=f"{OWNER}@test",
                actor_key=OWNER,
                actor_category="owner",
                binding_metadata={"owner": True},
                is_active=True,
                created_at=stamp,
                updated_at=stamp,
            )
        )
        provision_internal_providers(setup, DEFAULT_TENANT_ID)
        initial = InternalPresenceProvider(
            setup, DEFAULT_TENANT_ID, "InternalPresenceProvider"
        ).execute("presence.set", _params("available", 30, "presence-initial"))
        assert initial.success
        setup.commit()

    barrier = Barrier(2)

    def update(state, ttl):
        with Session() as session:
            barrier.wait()
            result = InternalPresenceProvider(
                session, DEFAULT_TENANT_ID, "InternalPresenceProvider"
            ).execute("presence.set", _params(state, ttl, new_id()))
            assert result.success
            session.commit()
            return result.result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            pool.submit(update, "busy", 600),
            pool.submit(update, "away", 1_200),
        ]
        outcomes = [future.result(timeout=10) for future in results]

    with Session() as verify:
        row = verify.scalar(
            select(EntityStateRow).where(
                EntityStateRow.tenant_id == DEFAULT_TENANT_ID,
                EntityStateRow.subject_id == OWNER,
                EntityStateRow.state_namespace == "presence",
                EntityStateRow.state_key == "effective",
            )
        )
        last = max(outcomes, key=lambda item: item["version"])
        persisted_expiry = row.expires_at
        if persisted_expiry.tzinfo is None:
            persisted_expiry = persisted_expiry.replace(tzinfo=timezone.utc)

        assert sorted(item["version"] for item in outcomes) == [2, 3]
        assert row.version == last["version"] == 3
        assert row.state_value["status"] == last["status"]
        assert persisted_expiry == datetime.fromisoformat(last["expires_at"])
        assert row.expires_at is not None
        assert last["expiry_source"] == "EXPLICIT_TTL"
