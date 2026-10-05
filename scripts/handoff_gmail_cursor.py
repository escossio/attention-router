#!/usr/bin/env python
"""Operator-only Gmail cursor handoff. Dry-run unless --apply is supplied."""

from __future__ import annotations

import argparse
import json

from attention_router.application.gmail_cursor_handoff import (
    GmailCursorHandoffError,
    handoff_gmail_history_cursor,
)
from attention_router.infrastructure.db import SessionLocal


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate or apply a Gmail history cursor handoff between two "
            "already-provisioned tenant-scoped installations."
        )
    )
    parser.add_argument("--source-installation", required=True)
    parser.add_argument("--destination-installation", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="copy the validated source cursor; otherwise perform dry-run only",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    with SessionLocal() as session:
        try:
            result = handoff_gmail_history_cursor(
                session,
                source_installation_id=args.source_installation,
                destination_installation_id=args.destination_installation,
                apply=args.apply,
            )
            if args.apply:
                session.commit()
            else:
                session.rollback()
        except GmailCursorHandoffError as exc:
            session.rollback()
            print(
                json.dumps(
                    {
                        "status": "BLOCKED",
                        "code": exc.code,
                        "applied": False,
                    },
                    sort_keys=True,
                )
            )
            return 2
        except Exception:
            session.rollback()
            print(
                json.dumps(
                    {
                        "status": "BLOCKED",
                        "code": "GMAIL_CURSOR_HANDOFF_UNAVAILABLE",
                        "applied": False,
                    },
                    sort_keys=True,
                )
            )
            return 3

    print(
        json.dumps(
            {
                "status": result.state,
                "source_installation_id": result.source_installation_id,
                "destination_installation_id": result.destination_installation_id,
                "applied": result.cursor_copied,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
