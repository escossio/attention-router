#!/usr/bin/env python
"""Rebind the single owner channel identity without rewriting history.

The old and new values are supplied by the operator from the authenticated
transport's canonical identity representation. This command only changes the
active binding state; historical inbound/audit rows remain untouched.
"""

import os

from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.db import SessionLocal
from attention_router.infrastructure.repository import (
    audit,
    set_actor_binding_active,
    upsert_actor_binding,
)
from attention_router.infrastructure.models import ActorBindingRow
from sqlalchemy import select


def migrate_owner_identity(
    session,
    *,
    old_identity_ref: str,
    new_identity_ref: str,
    source: str = "wwebjs",
    tenant_id: str = DEFAULT_TENANT_ID,
):
    if not old_identity_ref or not new_identity_ref or old_identity_ref == new_identity_ref:
        raise ValueError("distinct old and new owner identity references are required")
    owners = session.scalars(
        select(ActorBindingRow).where(
            ActorBindingRow.tenant_id == tenant_id,
            ActorBindingRow.source == source,
            ActorBindingRow.is_active.is_(True),
            (ActorBindingRow.actor_category == "owner")
            | ActorBindingRow.binding_metadata["owner"].as_boolean().is_(True),
        )
    ).all()
    old = next((row for row in owners if row.external_actor_id == old_identity_ref), None)
    if old is None:
        raise ValueError("active retired owner binding not found")
    if len(owners) != 1:
        raise ValueError("owner migration requires exactly one active owner binding")
    existing_new = session.scalars(
        select(ActorBindingRow).where(
            ActorBindingRow.tenant_id == tenant_id,
            ActorBindingRow.source == source,
            ActorBindingRow.external_actor_id == new_identity_ref,
        )
    ).first()
    if existing_new is not None and existing_new.id != old.id and existing_new.is_active:
        raise ValueError("new identity is already active; refusing duplicate owner authority")

    metadata = dict(old.binding_metadata or {})
    metadata.update({"owner": True, "owner_channel_role": "PRIMARY_OWNER_WHATSAPP"})
    new = upsert_actor_binding(
        session,
        source=source,
        external_actor_id=new_identity_ref,
        actor_key=old.actor_key,
        actor_category="owner",
        display_name=old.display_name,
        active_context=old.active_context,
        metadata=metadata,
        is_active=True,
        tenant_id=tenant_id,
    )
    set_actor_binding_active(session, old.id, False, tenant_id)
    audit(
        session,
        None,
        "owner_channel_identity_migrated",
        {
            "old_binding_id": old.id,
            "new_binding_id": new.id,
            "actor_key": old.actor_key,
            "old_status": "RETIRED",
            "new_status": "ACTIVE",
        },
        origin="operator_identity_migration",
        tenant_id=tenant_id,
    )
    session.flush()
    return old, new


def main() -> int:
    old_identity_ref = os.environ.get("OLD_OWNER_WHATSAPP_IDENTITY_REF", "")
    new_identity_ref = os.environ.get("OWNER_WHATSAPP_IDENTITY_REF", "")
    with SessionLocal() as session:
        old, new = migrate_owner_identity(
            session,
            old_identity_ref=old_identity_ref,
            new_identity_ref=new_identity_ref,
        )
        session.commit()
    print(f"OLD_OWNER_BINDING_RETIRED={old.id}")
    print(f"NEW_OWNER_BINDING_ACTIVE={new.id}")
    print("HISTORICAL_EVIDENCE_PRESERVED=YES")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
