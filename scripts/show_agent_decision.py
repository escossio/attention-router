#!/usr/bin/env python
import argparse
import json

from attention_router.application.decision_pipeline import decision_to_dict
from attention_router.infrastructure.db import SessionLocal
from attention_router.infrastructure.models import AgentDecisionRow


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect one sanitized dry-run agent decision.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--decision-id")
    group.add_argument("--event-id")
    group.add_argument("--interaction-id")
    args = parser.parse_args()
    with SessionLocal() as session:
        query = session.query(AgentDecisionRow)
        if args.decision_id:
            row = query.filter_by(id=args.decision_id).first()
        elif args.event_id:
            row = query.filter_by(event_id=args.event_id).first()
        else:
            row = query.filter_by(interaction_id=args.interaction_id).order_by(AgentDecisionRow.created_at.desc()).first()
        if row is None:
            print("agent decision not found")
            return 1
        print(json.dumps(decision_to_dict(row), indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
