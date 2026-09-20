#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from attention_router.config import settings
from attention_router.infrastructure.db import SessionLocal
from attention_router.integrations.gmail_canary_provisioning import (
    provision_gmail_canary,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Provision one read-only Gmail canary integration binding and "
            "neutral-ingress credential. Dry-run unless --apply is present."
        )
    )
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--account-id")
    parser.add_argument(
        "--audience",
        default=settings.integration_ingress_audience,
    )
    parser.add_argument(
        "--lifetime-hours",
        type=int,
        default=24,
        choices=range(1, 169),
        metavar="1..168",
    )
    parser.add_argument(
        "--secret-file",
        type=Path,
        help=(
            "Absolute output path for the one-time neutral-ingress bearer. "
            "Required with --apply; created mode 0600 and never overwritten."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the credential file and PostgreSQL records.",
    )
    args = parser.parse_args()

    if not args.apply:
        print(
            json.dumps(
                {
                    "status": "DRY_RUN",
                    "kind": "CHANNEL",
                    "name": "channel.email",
                    "tenant_id": args.tenant_id,
                    "instance_id": args.instance_id,
                    "account_id_present": bool(args.account_id),
                    "audience": args.audience,
                    "lifetime_hours": args.lifetime_hours,
                    "database_changed": False,
                    "secret_file_created": False,
                },
                sort_keys=True,
            )
        )
        return 0

    if args.secret_file is None:
        parser.error("--secret-file is required with --apply")
    if not args.secret_file.is_absolute():
        parser.error("--secret-file must be an absolute path")

    installation = provision_gmail_canary(
        SessionLocal,
        tenant_id=args.tenant_id,
        audience=args.audience,
        instance_id=args.instance_id,
        account_id=args.account_id,
        secret_path=args.secret_file,
        lifetime_hours=args.lifetime_hours,
    )

    print(
        json.dumps(
            {
                "status": "PROVISIONED",
                "binding_id": installation.binding.binding_id,
                "credential_id": installation.credential.credential_id,
                "tenant_id": installation.binding.tenant_id,
                "instance_id": installation.binding.instance_id,
                "account_id_present": bool(installation.binding.account_id),
                "audience": installation.binding.audience,
                "expires_at": installation.credential.expires_at.isoformat(),
                "secret_file": str(args.secret_file),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
