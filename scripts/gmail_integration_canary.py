#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os

from attention_router.integrations.gmail_api_reader import (
    GmailApiReader,
    StaticGmailAccessTokenProvider,
)
from attention_router.integrations.gmail_connector import (
    GmailConnectorConfig,
    GmailInboundConnector,
    IntegrationIngressClient,
)


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value or not value.strip():
        raise SystemExit(f"missing required environment variable: {name}")
    return value.strip()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one bounded, read-only Gmail -> neutral ingress canary. "
            "The script never mutates Gmail."
        )
    )
    parser.add_argument(
        "--query",
        default="in:inbox newer_than:1d -in:spam -in:trash -has:attachment",
        help="Gmail search query; default excludes attachments.",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=1,
        choices=range(1, 6),
        metavar="1..5",
        help="Canary message bound (default: 1; maximum: 5).",
    )
    args = parser.parse_args()

    reader = GmailApiReader(
        token_provider=StaticGmailAccessTokenProvider(
            _required_env("GMAIL_ACCESS_TOKEN")
        )
    )
    ingress = IntegrationIngressClient(
        url=_required_env("ATTENTION_ROUTER_INTEGRATION_INGRESS_URL"),
        bearer=_required_env("ATTENTION_ROUTER_INTEGRATION_BEARER"),
    )
    connector = GmailInboundConnector(
        reader=reader,
        ingress=ingress,
        config=GmailConnectorConfig(
            tenant_id=_required_env("GMAIL_CONNECTOR_TENANT_ID"),
            instance_id=_required_env("GMAIL_CONNECTOR_INSTANCE_ID"),
            account_id=(
                os.environ.get("GMAIL_CONNECTOR_ACCOUNT_ID") or None
            ),
            ingress_url=_required_env(
                "ATTENTION_ROUTER_INTEGRATION_INGRESS_URL"
            ),
            ingress_bearer=_required_env(
                "ATTENTION_ROUTER_INTEGRATION_BEARER"
            ),
        ),
    )

    result = connector.poll(
        query=args.query,
        max_results=args.max_results,
    )
    # Never print message IDs, addresses, subjects, bodies, provider response
    # payloads, access tokens, or neutral-ingress credentials.
    print(
        json.dumps(
            {
                "status": "PASS",
                "selected": result.selected,
                "accepted": result.accepted,
                "duplicates": result.duplicates,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
