#!/usr/bin/env python3
from __future__ import annotations

import argparse


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Run one bounded governed Gmail metadata poll."
    )
    root.add_argument("--installation-id", required=True)
    root.add_argument("--max-results", type=int)
    return root


def main() -> None:
    args = parser().parse_args()

    from attention_router.application.gmail_product_runner import GmailProductRunner
    from attention_router.config import settings
    from attention_router.infrastructure.db import SessionLocal

    with SessionLocal() as session:
        result = GmailProductRunner(settings=settings).run_once(
            session,
            installation_id=args.installation_id,
            max_results=args.max_results,
        )
    print("GMAIL_PRODUCT_RUN=PASS")
    print(f"INSTALLATION_ID={result.installation_id}")
    print(f"BINDING_ID={result.binding_id}")
    print(f"SELECTED={result.poll.selected}")
    print(f"ACCEPTED={result.poll.accepted}")
    print(f"DUPLICATES={result.poll.duplicates}")


if __name__ == "__main__":
    main()
