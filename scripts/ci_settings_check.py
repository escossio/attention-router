"""CI-only configuration bootstrap with diagnostics that omit input values."""

import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
SAFE_ERROR_TYPES = {
    "value_error", "missing", "string_type", "bool_parsing", "int_parsing",
    "literal_error", "extra_forbidden", "greater_than", "greater_than_equal",
}


def safe_diagnostic(exc: Exception) -> dict:
    diagnostic = {"exception": type(exc).__name__}
    if isinstance(exc, ValidationError):
        errors = exc.errors(include_input=False, include_context=False, include_url=False)
        diagnostic["error_types"] = [
            error["type"] if error["type"] in SAFE_ERROR_TYPES else "validation_error"
            for error in errors
        ]
    return diagnostic


def check_redaction() -> None:
    sentinels = [secrets.token_urlsafe(32), secrets.token_hex(4), secrets.token_urlsafe(32)]
    env = dict(os.environ)
    env.update({
        "ADMIN_AUTH_ENABLED": "true",
        "ADMIN_TOKEN": sentinels[0],
        "INTERNAL_INGRESS_HMAC_SECRET": sentinels[1],
        "DATABASE_URL": (
            "postgresql+psycopg://ar_test:" + sentinels[2]
            + "@127.0.0.1:5432/attention_router_test"
        ),
    })
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )
    output = result.stdout + result.stderr
    if (
        result.returncode != 1
        or result.stderr
        or any(value in output for value in sentinels)
        or "input_value" in output
        or json.loads(result.stdout) != {
            "exception": "ValidationError", "error_types": ["value_error"],
        }
    ):
        raise RuntimeError("CI diagnostic redaction proof failed")
    print("DIAGNOSTIC_INPUT_REDACTION_TEST=PASS")


def main() -> int:
    try:
        if sys.argv[1:] == ["--self-test"]:
            check_redaction()
        elif sys.argv[1:]:
            raise ValueError("Unsupported CI diagnostic arguments")
        else:
            sys.path.insert(0, str(ROOT))
            from attention_router import config

            if Path(config.__file__).resolve() != ROOT / "attention_router" / "config.py":
                raise RuntimeError("Unexpected configuration module provenance")
            print("CI_SETTINGS_LOAD=PASS")
    except Exception as exc:
        print(json.dumps(safe_diagnostic(exc), sort_keys=True))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
