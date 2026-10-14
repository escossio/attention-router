"""Read-only metadata audit for the shared GitHub webhook/publisher SQLite database."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

JOB_TABLES = ("distributed_postgres_jobs", "distributed_shadow_jobs")
META_TABLES = ("distributed_postgres_meta", "distributed_shadow_meta")
REQUIRED_COLUMNS = {
    "deliveries": {"delivery_id"},
    "distributed_postgres_jobs": {
        "id", "repository", "pr_number", "head_sha", "state", "check_run_id",
        "created_at", "updated_at",
    },
    "distributed_shadow_jobs": {
        "id", "repository", "pr_number", "head_sha", "suite", "state",
        "check_run_id", "created_at", "updated_at",
    },
    "distributed_postgres_meta": {"key", "value"},
    "distributed_shadow_meta": {"key", "value"},
}


def audit(database: Path) -> dict:
    """Inspect queue metadata without writing to the database or contacting GitHub."""
    if not database.is_file():
        raise ValueError("database file is missing")
    uri = database.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=2) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        for table, required in REQUIRED_COLUMNS.items():
            columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
            if not required <= columns:
                raise ValueError(f"missing required schema: {table}")
        report = {
            "database": str(database),
            "delivery_count": db.execute("SELECT count(*) FROM deliveries").fetchone()[0],
            "last_delivery_rowid": db.execute(
                "SELECT coalesce(max(rowid),0) FROM deliveries"
            ).fetchone()[0],
            "queues": {},
        }
        for jobs, meta in zip(JOB_TABLES, META_TABLES, strict=True):
            cursor_row = db.execute(
                f"SELECT value FROM {meta} WHERE key='delivery_cursor'"
            ).fetchone()
            if cursor_row is None:
                raise ValueError(f"missing delivery cursor: {meta}")
            cursor = int(cursor_row["value"])
            counts = {
                row["state"]: row["n"]
                for row in db.execute(
                    f"SELECT state,count(*) AS n FROM {jobs} GROUP BY state"
                )
            }
            fields = (
                "id,repository,pr_number,head_sha,state,check_run_id,"
                "created_at,updated_at"
            )
            if jobs == "distributed_shadow_jobs":
                fields += ",suite"
            rows = [
                dict(row)
                for row in db.execute(
                    f"SELECT {fields} FROM {jobs} "
                    "WHERE state IN ('PENDING','RUNNING') ORDER BY id"
                )
            ]
            report["queues"][jobs] = {
                "delivery_cursor": cursor,
                "states": counts,
                "unresolved": rows,
            }
        report["needs_review"] = any(
            queue["unresolved"] or queue["delivery_cursor"] != report["last_delivery_rowid"]
            for queue in report["queues"].values()
        )
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(audit(args.database), indent=2, sort_keys=True))
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(2, f"queue audit failed closed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
