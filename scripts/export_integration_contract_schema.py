from __future__ import annotations

import json
from pathlib import Path

from attention_router.contracts.integration import integration_contract_json_schema


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "contracts/integration/v1/integration-contract.schema.json"


def main() -> None:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(
        json.dumps(
            integration_contract_json_schema(),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n"
    )
    print(TARGET)


if __name__ == "__main__":
    main()
