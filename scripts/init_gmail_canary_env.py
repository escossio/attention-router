#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from attention_router.integrations.gmail_canary_runtime import (
    write_gmail_canary_env,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create a private Gmail canary runtime env file. "
            "Integration ingress/dispatch remain disabled."
        )
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=Path.cwd() / ".env.gmail-canary",
        help="Absolute output path. Defaults to .env.gmail-canary in cwd.",
    )
    args = parser.parse_args()

    path = args.path.resolve()
    write_gmail_canary_env(path)

    print(
        json.dumps(
            {
                "status": "CREATED",
                "path": str(path),
                "mode": "0600",
                "integration_ingress_enabled": False,
                "integration_dispatch_enabled": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
