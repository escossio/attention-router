#!/usr/bin/env python3
import argparse
import json

from attention_router.application.lab_conversation import start_session, stop_session
from attention_router.infrastructure.db import SessionLocal
from attention_router.infrastructure.models import LabConversationSessionRow
from sqlalchemy import select


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage an isolated Attention Router lab conversation.")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--binding", required=True)
    start.add_argument("--policy", required=True)
    start.add_argument("--max-inbounds", type=int, default=12)
    start.add_argument("--max-sends", type=int, default=8)
    status = sub.add_parser("status")
    status.add_argument("--session")
    stop = sub.add_parser("stop")
    stop.add_argument("session")
    args = parser.parse_args()
    with SessionLocal() as session:
        if args.command == "start":
            row = start_session(session, args.binding, args.policy, args.max_inbounds, args.max_sends)
        elif args.command == "stop":
            row = stop_session(session, args.session)
        else:
            row = session.get(LabConversationSessionRow, args.session) if args.session else session.scalar(select(LabConversationSessionRow).where(LabConversationSessionRow.status == "ACTIVE"))
            if row is None:
                parser.error("lab session not found")
        session.commit()
        print(json.dumps({
            "id": row.id, "status": row.status, "target_binding_id": row.target_binding_id,
            "target_policy": row.target_policy, "max_inbounds": row.max_inbounds,
            "inbound_count": row.inbound_count, "max_send_permits": row.max_send_permits,
            "send_permit_count": row.send_permit_count,
        }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
