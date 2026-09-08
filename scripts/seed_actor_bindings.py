#!/usr/bin/env python
import argparse
import json
from pathlib import Path

from attention_router.infrastructure.db import SessionLocal
from attention_router.infrastructure.repository import actor_binding_to_dict, upsert_actor_binding


def seed_actor_bindings(session, entries: list[dict], require_external_actor_id: bool = False) -> tuple[int, int, list]:
    seeded = 0
    skipped = 0
    rows = []
    for entry in entries:
        external_actor_id = entry.get("external_actor_id")
        if not external_actor_id:
            skipped += 1
            if require_external_actor_id:
                raise ValueError(f"external_actor_id missing for actor_key={entry['actor_key']}")
            continue
        row = upsert_actor_binding(
            session,
            source=entry["source"],
            external_actor_id=external_actor_id,
            actor_key=entry["actor_key"],
            actor_category=entry["actor_category"],
            display_name=entry.get("display_name"),
            active_context=entry.get("active_context"),
            metadata=entry.get("metadata") or {},
            is_active=entry.get("is_active", True),
        )
        seeded += 1
        rows.append(row)
    return seeded, skipped, rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed ActorBinding rows from a JSON file.")
    parser.add_argument("--file", default="config/actor_bindings.example.json")
    parser.add_argument("--require-external-actor-id", action="store_true")
    args = parser.parse_args()

    entries = json.loads(Path(args.file).read_text(encoding="utf-8"))
    with SessionLocal() as session:
        seeded, skipped, rows = seed_actor_bindings(session, entries, args.require_external_actor_id)
        for row in rows:
            print(actor_binding_to_dict(row))
        session.commit()
    print(f"seeded={seeded}")
    print(f"skipped_missing_external_actor_id={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
