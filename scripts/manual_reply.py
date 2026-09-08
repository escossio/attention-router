#!/usr/bin/env python
import argparse

from attention_router.application.services import enqueue_wwebjs_manual_reply, validate_wwebjs_manual_reply_target
from attention_router.infrastructure.db import SessionLocal
from attention_router.infrastructure.repository import mask_identifier


def main() -> int:
    parser = argparse.ArgumentParser(description="Create one explicit manual reply in the transactional outbox.")
    parser.add_argument("--interaction-id", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--confirm-manual-reply", action="store_true")
    parser.add_argument("--idempotency-key")
    args = parser.parse_args()

    with SessionLocal() as session:
        row, receipt, external_actor_id = validate_wwebjs_manual_reply_target(session, args.interaction_id)
        print(f"interaction_id={row.id}")
        print(f"horario={receipt.received_at.isoformat()}")
        print(f"source={receipt.source}")
        print(f"actor={mask_identifier(external_actor_id)}")
        print(f"estado={row.state}")
        if not args.confirm_manual_reply:
            return 2
        outbox = enqueue_wwebjs_manual_reply(session, args.interaction_id, args.text, args.idempotency_key)
        session.commit()
        print(f"outbox_id={outbox.id}")
        print(f"idempotency_key={outbox.idempotency_key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
