#!/usr/bin/env python
import argparse

from sqlalchemy import select

from attention_router.application.services import enqueue_wwebjs_controlled_outbound
from attention_router.infrastructure.db import SessionLocal
from attention_router.infrastructure.models import InboundEventRow
from attention_router.infrastructure.repository import mask_identifier


def main() -> int:
    parser = argparse.ArgumentParser(description="Create one controlled wwebjs outbound outbox item.")
    parser.add_argument("--interaction-id", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--confirm-controlled-test", action="store_true")
    args = parser.parse_args()
    if not args.confirm_controlled_test:
        raise SystemExit("--confirm-controlled-test is required")

    with SessionLocal() as session:
        receipt = session.scalars(
            select(InboundEventRow)
            .where(InboundEventRow.interaction_id == args.interaction_id, InboundEventRow.source == "wwebjs")
            .order_by(InboundEventRow.received_at.desc())
            .limit(1)
        ).first()
        if not receipt:
            raise SystemExit("interaction is not a wwebjs inbound interaction")
        actor = receipt.payload.get("external_actor_id") or receipt.payload.get("actor_id")
        if not actor:
            raise SystemExit("interaction does not contain external_actor_id")
        print(f"wwebjs recipient={mask_identifier(actor)}")
        outbox = enqueue_wwebjs_controlled_outbound(session, args.interaction_id, args.text)
        session.commit()
        print(f"outbox_id={outbox.id}")
        print(f"idempotency_key={outbox.idempotency_key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
