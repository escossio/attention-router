#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from sqlalchemy import select

from attention_router.application.platform.registry import (
    capability_and_version,
    matrix_status,
    sync_platform_registry,
)
from attention_router.core.tenancy import DEFAULT_TENANT_ID
from attention_router.infrastructure.db import SessionLocal
from attention_router.infrastructure.models import ProviderDefinitionRow, ProviderInstanceRow


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Provision and inspect Platform Matrix V1.")
    parser.add_argument("--tenant", default=DEFAULT_TENANT_ID)
    area = parser.add_subparsers(dest="area", required=True)
    capability = area.add_parser("capability")
    capability_cmd = capability.add_subparsers(dest="command", required=True)
    capability_cmd.add_parser("sync")
    capability_cmd.add_parser("list")
    inspect = capability_cmd.add_parser("inspect")
    inspect.add_argument("name")
    provider = area.add_parser("provider")
    provider.add_subparsers(dest="command", required=True).add_parser("list")
    matrix = area.add_parser("matrix")
    matrix.add_subparsers(dest="command", required=True).add_parser("status")
    return parser


def main() -> int:
    args = _parser().parse_args()
    with SessionLocal() as session:
        if args.area == "capability" and args.command == "sync":
            result = sync_platform_registry(session, tenant_id=args.tenant)
            session.commit()
        elif args.area == "capability" and args.command == "list":
            result = matrix_status(session, tenant_id=args.tenant)
        elif args.area == "capability" and args.command == "inspect":
            definition, version = capability_and_version(session, args.tenant, args.name)
            if definition is None or version is None:
                raise SystemExit("capability not found")
            result = {
                "capability": definition.canonical_name,
                "version": version.version,
                "state": definition.availability_state,
                "operation_type": version.operation_type,
                "required_provider": version.required_provider_interface,
                "side_effect": version.side_effect,
                "sensitivity": version.sensitivity,
                "default_approval_policy": version.default_approval_policy,
            }
        elif args.area == "provider":
            definitions = session.scalars(
                select(ProviderDefinitionRow).order_by(ProviderDefinitionRow.canonical_name)
            ).all()
            instances = session.scalars(
                select(ProviderInstanceRow).where(ProviderInstanceRow.tenant_id == args.tenant)
            ).all()
            result = {
                "definitions": [
                    {
                        "name": row.canonical_name,
                        "interface": row.interface_name,
                        "contract_version": row.contract_version,
                    }
                    for row in definitions
                ],
                "instances": [
                    {"name": row.canonical_name, "state": row.state, "health": row.health}
                    for row in instances
                ],
            }
        else:
            result = matrix_status(session, tenant_id=args.tenant)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
