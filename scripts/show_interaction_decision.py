#!/usr/bin/env python
import argparse

from attention_router.application.services import build_attention_logic_decision
from attention_router.infrastructure.db import SessionLocal
from attention_router.infrastructure.models import InteractionRow


def main() -> int:
    parser = argparse.ArgumentParser(description="Show the dry-run attention decision for one interaction.")
    parser.add_argument("--interaction-id", required=True)
    args = parser.parse_args()

    with SessionLocal() as session:
        row = session.get(InteractionRow, args.interaction_id)
        if not row:
            print("interaction not found")
            return 1
        result = build_attention_logic_decision(session, row)
        profile = result["actor_profile"]
        signals = result["signals"]
        decision = result["decision"]

        print("Pessoa:")
        print(profile["display_name"])
        print()
        print("Prioridade:")
        print(profile["priority"])
        print()
        print("test_allowed:")
        print(str(profile["test_allowed"]).lower())
        print()
        print("Policy:")
        print(result["policy_version"])
        print()
        print("Mensagens recentes:")
        print(signals["message_count_recent"])
        print()
        print("Classificacao:")
        print(signals["classification"])
        print()
        print("Attention level:")
        print(decision["attention_level"])
        print()
        print("Motivos:")
        for reason in decision["reason_codes"]:
            print(reason)
        print()
        print("Acao sugerida:")
        print(decision["suggested_action"])
        print()
        print("AUTO ACTION:")
        print("disabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
