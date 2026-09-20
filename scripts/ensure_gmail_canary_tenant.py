#!/usr/bin/env python3
from __future__ import annotations

import json

from attention_router.application.platform.registry import (
    ensure_default_tenant,
)
from attention_router.infrastructure.db import SessionLocal


def main() -> int:
    with SessionLocal.begin() as session:
        tenant = ensure_default_tenant(session)

    print(
        json.dumps(
            {
                "status": "READY",
                "tenant_id": tenant.id,
                "tenant_status": tenant.status,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
