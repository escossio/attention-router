#!/usr/bin/env python3
"""Private offline memory/archive utility; it never contacts WhatsApp."""

import argparse

from sqlalchemy import func, select

from attention_router.application.memory import search_archive, search_memory, get_memory_evidence
from attention_router.infrastructure.db import SessionLocal
from attention_router.infrastructure.models import ConversationMessageRow, MemoryClaimRow, MemoryCandidateRow


def main() -> None:
    parser = argparse.ArgumentParser(prog="memory")
    sub = parser.add_subparsers(dest="command", required=True)
    search = sub.add_parser("search")
    search.add_argument("query")
    evidence = sub.add_parser("evidence")
    evidence.add_argument("claim_id")
    sub.add_parser("stats")
    backfill = sub.add_parser("backfill")
    backfill.add_argument("--dry-run", action="store_true")
    backfill.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    with SessionLocal() as session:
        if args.command == "search":
            print({"memory": search_memory(session, args.query), "archive": search_archive(session, args.query)})
        elif args.command == "evidence":
            print(get_memory_evidence(session, args.claim_id))
        elif args.command == "stats":
            print({"messages": session.scalar(select(func.count()).select_from(ConversationMessageRow)), "claims": session.scalar(select(func.count()).select_from(MemoryClaimRow)), "candidates": session.scalar(select(func.count()).select_from(MemoryCandidateRow))})
        elif args.command == "backfill":
            print({"mode": "BACKFILL", "dry_run": args.dry_run, "resume": args.resume, "transport": "not_connected"})


if __name__ == "__main__":
    main()
