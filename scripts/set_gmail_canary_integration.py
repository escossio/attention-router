#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from attention_router.integrations.gmail_canary_runtime import (
    set_gmail_canary_integration_enabled,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Atomically enable or disable neutral integration ingress+dispatch "
            "inside one private Gmail canary env file."
        )
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=Path.cwd() / ".env.gmail-canary",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--enable", action="store_true")
    group.add_argument("--disable", action="store_true")
    args = parser.parse_args()

    path = args.path.resolve()
    enabled = bool(args.enable)
    set_gmail_canary_integration_enabled(path, enabled=enabled)

    print(
        json.dumps(
            {
                "status": "UPDATED",
                "path": str(path),
                "integration_ingress_enabled": enabled,
                "integration_dispatch_enabled": enabled,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
