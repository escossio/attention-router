#!/usr/bin/env python
import argparse
import json

from attention_router.application import response_review
from attention_router.infrastructure.db import SessionLocal


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect and operate agent response reviews.")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--decision-id", required=True)
    sub.add_parser("list-pending")
    show = sub.add_parser("show")
    show.add_argument("--review-id", required=True)
    edit = sub.add_parser("edit")
    edit.add_argument("--review-id", required=True)
    edit.add_argument("--text", required=True)
    approve = sub.add_parser("approve")
    approve.add_argument("--review-id", required=True)
    reject = sub.add_parser("reject")
    reject.add_argument("--review-id", required=True)
    reject.add_argument("--reason")
    args = parser.parse_args()
    with SessionLocal() as session:
        if args.command == "create":
            row = response_review.create_review_for_decision(session, args.decision_id)
            output = response_review.review_to_dict(session, row)
        elif args.command == "list-pending":
            output = {"items": [response_review.review_to_dict(session, row) for row in response_review.list_reviews(session, "PENDING")]}
        else:
            row = session.get(response_review.AgentResponseReviewRow, args.review_id)
            if row is None:
                parser.error("review not found")
            if args.command == "show":
                output = response_review.review_to_dict(session, row)
            elif args.command == "edit":
                output = response_review.review_to_dict(session, response_review.edit_review(session, args.review_id, args.text))
            elif args.command == "approve":
                review, intent = response_review.approve_review(session, args.review_id)
                output = response_review.review_to_dict(session, review)
                output["execution_intent"] = response_review.intent_to_dict(intent)
            else:
                output = response_review.review_to_dict(session, response_review.reject_review(session, args.review_id, args.reason))
        session.commit()
    print(json.dumps(output, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
