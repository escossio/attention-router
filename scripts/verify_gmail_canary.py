#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from sqlalchemy import func, select

from attention_router.infrastructure.db import SessionLocal
from attention_router.infrastructure.models import (
    CanonicalEventRow,
    IntegrationInboxRow,
    TimelineEventRow,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify one Gmail canary integration binding reached the canonical "
            "timeline without exposing provider-private identifiers."
        )
    )
    parser.add_argument("--binding-id", required=True)
    args = parser.parse_args()

    with SessionLocal() as session:
        inbox_rows = session.scalars(
            select(IntegrationInboxRow)
            .where(
                IntegrationInboxRow.binding_id == args.binding_id,
            )
            .order_by(
                IntegrationInboxRow.admitted_at.desc(),
                IntegrationInboxRow.id.desc(),
            )
        ).all()

        processed = [
            row for row in inbox_rows
            if row.state == "PROCESSED"
            and row.canonical_event_id is not None
        ]
        blocked = sum(row.state == "BLOCKED" for row in inbox_rows)
        pending = sum(row.state == "PENDING" for row in inbox_rows)

        canonical_count = 0
        timeline_count = 0
        if processed:
            canonical_ids = [
                row.canonical_event_id
                for row in processed
                if row.canonical_event_id is not None
            ]
            canonical_count = session.scalar(
                select(func.count())
                .select_from(CanonicalEventRow)
                .where(CanonicalEventRow.id.in_(canonical_ids))
            ) or 0
            timeline_count = session.scalar(
                select(func.count())
                .select_from(TimelineEventRow)
                .where(
                    TimelineEventRow.canonical_event_id.in_(canonical_ids)
                )
            ) or 0

    success = (
        len(processed) >= 1
        and canonical_count == len(processed)
        and timeline_count == len(processed)
        and blocked == 0
        and pending == 0
    )
    print(
        json.dumps(
            {
                "status": "PASS" if success else "FAIL",
                "receipts": len(inbox_rows),
                "processed": len(processed),
                "pending": pending,
                "blocked": blocked,
                "canonical_events": canonical_count,
                "timeline_events": timeline_count,
            },
            sort_keys=True,
        )
    )
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
