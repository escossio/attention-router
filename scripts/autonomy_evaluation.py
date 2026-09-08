#!/usr/bin/env python
import argparse
import json

from attention_router.application import autonomy
from attention_router.infrastructure.db import SessionLocal
from attention_router.infrastructure.models import AutonomyEvaluationRow
from sqlalchemy import select


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect autonomy evaluations without exposing message content.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    show = sub.add_parser("show")
    show.add_argument("--evaluation-id", required=True)
    by_decision = sub.add_parser("show-by-decision")
    by_decision.add_argument("--decision-id", required=True)
    args = parser.parse_args()
    with SessionLocal() as session:
        if args.command == "list":
            rows = session.scalars(select(AutonomyEvaluationRow).order_by(AutonomyEvaluationRow.created_at.desc()).limit(100)).all()
        elif args.command == "show":
            row = session.get(AutonomyEvaluationRow, args.evaluation_id)
            if row is None:
                parser.error("autonomy evaluation not found")
            rows = [row]
        else:
            row = session.scalar(select(AutonomyEvaluationRow).where(AutonomyEvaluationRow.agent_decision_id == args.decision_id))
            rows = [row] if row else []
        output = {"items": [autonomy.evaluation_to_dict(row) for row in rows]}
    print(json.dumps(output, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
