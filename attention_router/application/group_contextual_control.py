"""Private operator CLI for immediate mode changes and a gated group notice."""

import argparse

from sqlalchemy import select

from attention_router.application.group_contextual_presence import enqueue_group_notice, set_group_mode
from attention_router.config import settings
from attention_router.infrastructure.db import SessionLocal
from attention_router.infrastructure.models import GroupContextualEventRow, GroupContextualPresenceRow, OutboxMessageRow


def main() -> None:
    parser = argparse.ArgumentParser(description="Control one allowlisted WhatsApp group")
    parser.add_argument("command", choices=["off", "observe", "mention-only", "contextual", "notice", "status"])
    args = parser.parse_args()
    group_id = settings.group_contextual_presence_group_id
    if not group_id or not group_id.endswith("@g.us"):
        raise SystemExit("private group allowlist is unavailable")
    with SessionLocal() as session:
        state = session.scalar(select(GroupContextualPresenceRow).where(GroupContextualPresenceRow.group_id == group_id))
        if args.command == "status":
            if state is None:
                print("mode=OFF configured=no")
                return
            observed = session.scalars(select(GroupContextualEventRow).where(
                GroupContextualEventRow.presence_id == state.id
            )).all()
            sent = session.scalars(select(OutboxMessageRow).where(
                OutboxMessageRow.action_type == "wwebjs_group_contextual_text",
                OutboxMessageRow.status == "DONE",
            )).all()
            sent_count = sum((row.payload or {}).get("external_actor_id") == group_id for row in sent)
            print(f"mode={state.mode} observed={len(observed)} sent={sent_count} notice_sent={state.notice_sent_at is not None} notice_confirmed={state.notice_confirmed_at is not None}")
            return
        if args.command == "notice":
            row = enqueue_group_notice(session, group_id=group_id)
            session.commit()
            print(f"notice_status={row.status} outbox_id={row.id}")
            return
        tenant_id = settings.group_contextual_presence_tenant_id or (state.tenant_id if state else None)
        if not tenant_id:
            raise SystemExit("private group tenant is unavailable")
        mode = {"off": "OFF", "observe": "OBSERVE", "mention-only": "MENTION_ONLY", "contextual": "CONTEXTUAL"}[args.command]
        row = set_group_mode(session, group_id=group_id, tenant_id=tenant_id, mode=mode)
        session.commit()
        print(f"mode={row.mode}")


if __name__ == "__main__":
    main()
