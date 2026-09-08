#!/usr/bin/env python
import argparse
import json

from attention_router.application import execution
from attention_router.infrastructure.db import SessionLocal
from attention_router.infrastructure.models import AgentExecutionIntentRow


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect and operate controlled execution intents.")
    sub = parser.add_subparsers(dest="command", required=True)
    list_parser = sub.add_parser("list-held")
    list_parser.add_argument("--status", default="BLOCKED")
    for name in ("show", "release", "cancel", "status"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--intent-id", required=True)
    args = parser.parse_args()
    with SessionLocal() as session:
        if args.command == "list-held":
            rows = session.query(AgentExecutionIntentRow).filter_by(status=args.status).order_by(AgentExecutionIntentRow.created_at).all()
            output = {"items": [execution.intent_to_dict(session, row) for row in rows]}
        else:
            row = session.get(AgentExecutionIntentRow, args.intent_id)
            if row is None:
                parser.error("execution intent not found")
            if args.command == "release":
                row = execution.release_intent(session, args.intent_id)
            elif args.command == "cancel":
                row = execution.cancel_intent(session, args.intent_id)
            output = execution.intent_to_dict(session, row)
        session.commit()
    print(json.dumps(output, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
