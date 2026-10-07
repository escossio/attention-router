#!/usr/bin/env python3
"""Stage one private owner selection; never creates or queues a BootstrapRun."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from attention_router.application.personal_context_bootstrap_selection import (
    stage_selection,
)
from attention_router.config import settings
from attention_router.infrastructure.db import SessionLocal


EXPECTED_INDICES = {4, 13, 17, 23, 25}
EXPECTED_CONSENT_REF = "owner-explicit-whatsapp-bootstrap-20261007-selected-4-13-17-23-25"


def _private_json(path: Path) -> dict:
    if (
        not path.is_absolute() or path.is_symlink() or not path.is_file()
        or path.stat().st_mode & 0o077 or path.stat().st_uid != os.getuid()
    ):
        raise ValueError("private input must be an absolute regular file owned by the current user, mode 0600")
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("invalid private input")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection-file", type=Path, required=True)
    parser.add_argument("--chat-map", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        payload = _private_json(args.selection_file)
        chat_map = _private_json(args.chat_map)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    if not isinstance(payload, dict) or set(payload) != {
        "owner_human_identity_id", "chats", "consent_ref", "processing_budget"
    }:
        parser.error("invalid selection specification")
    chats = payload["chats"]
    if (
        not isinstance(chats, list) or len(chats) != 5
        or any(not isinstance(chat, dict) for chat in chats)
        or {chat.get("index") for chat in chats} != EXPECTED_INDICES
        or any(chat.get("external_thread_key") != chat_map.get(str(chat["index"])) for chat in chats)
        or any(chat.get("thread_type") != ("GROUP" if chat["index"] == 4 else "DIRECT") for chat in chats)
        or payload["consent_ref"] != EXPECTED_CONSENT_REF
    ):
        parser.error("selection does not match the five approved chat indices")
    if not args.apply:
        print(json.dumps({"status": "DRY_RUN", "chat_count": len(chats),
                          "database_changed": False}, sort_keys=True))
        return 0
    with SessionLocal.begin() as session:
        row = stage_selection(
            session, settings=settings,
            owner_human_identity_id=payload["owner_human_identity_id"],
            chats=payload["chats"],
            consent_ref=payload["consent_ref"],
            processing_budget=payload["processing_budget"],
        )
    print(json.dumps({"status": "STAGED", "selection_id": row.id,
                      "chat_count": len(row.chat_keys), "expires_at": row.expires_at.isoformat()},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
