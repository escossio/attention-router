from __future__ import annotations

from pathlib import Path
import stat

import yaml

from attention_router.integrations.gmail_canary_runtime import (
    GmailCanaryRuntimeSecrets,
    render_gmail_canary_env,
    write_gmail_canary_env,
)


ROOT = Path(__file__).resolve().parents[1]


def _env_map(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        result[key] = value
    return result


def test_gmail_canary_compose_is_isolated_and_minimal():
    data = yaml.safe_load(
        (ROOT / "compose.gmail-canary.yaml").read_text()
    )
    services = data["services"]

    assert set(services) == {"db", "migrate", "ingress", "worker"}
    assert "api" not in services
    assert "internal-ingress" not in services

    assert services["ingress"]["ports"] == [
        "127.0.0.1:18201:18101"
    ]
    assert "ports" not in services["db"]
    assert "ports" not in services["migrate"]
    assert "ports" not in services["worker"]

    assert services["migrate"]["depends_on"]["db"]["condition"] == (
        "service_healthy"
    )
    assert services["ingress"]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["worker"]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )

    assert data["volumes"] == {"gmail_canary_database": None}
    assert services["db"]["volumes"] == [
        "gmail_canary_database:/var/lib/postgresql/data"
    ]

    for service in ("db", "migrate", "ingress", "worker"):
        assert services[service]["env_file"] == [
            ".env.gmail-canary"
        ]


def test_gmail_canary_env_defaults_fail_closed():
    rendered = render_gmail_canary_env(
        secrets_bundle=GmailCanaryRuntimeSecrets(
            postgres_password="P" * 43,
            internal_ingress_hmac_secret="H" * 43,
        )
    )
    env = _env_map(rendered)

    assert env["APP_ENV"] == "private"
    assert env["INTEGRATION_INGRESS_ENABLED"] == "false"
    assert env["INTEGRATION_DISPATCH_ENABLED"] == "false"
    assert env["AGENT_DECISION_PIPELINE_ENABLED"] == "false"
    assert env["AGENT_RESPONSE_REVIEW_ENABLED"] == "false"
    assert env["AGENT_EXECUTION_ENABLED"] == "false"
    assert env["EXTERNAL_DELIVERY_ENABLED"] == "false"
    assert env["AUTONOMOUS_EXECUTION_ENABLED"] == "false"
    assert env["META_WHATSAPP_ENABLED"] == "false"
    assert env["TTS_ENABLED"] == "false"
    assert env["STT_ENABLED"] == "false"
    assert env["ANDY_AGENT_ENABLED"] == "false"
    assert env["PERSONAL_CONTEXT_RUNTIME_ENABLED"] == "false"
    assert env["ADMIN_AUTH_ENABLED"] == "false"
    assert env["INTEGRATION_INGRESS_AUDIENCE"] == "andy-gmail-canary"
    assert env["INTEGRATION_DISPATCH_BATCH_SIZE"] == "5"

    assert env["POSTGRES_PASSWORD"] == "P" * 43
    assert "P" * 43 in env["DATABASE_URL"]
    assert env["INTERNAL_INGRESS_HMAC_SECRET"] == "H" * 43


def test_gmail_canary_env_file_is_create_once_mode_0600(tmp_path):
    target = (tmp_path / ".env.gmail-canary").resolve()
    secrets_bundle = GmailCanaryRuntimeSecrets(
        postgres_password="P" * 43,
        internal_ingress_hmac_secret="H" * 43,
    )

    write_gmail_canary_env(
        target,
        secrets_bundle=secrets_bundle,
    )

    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    contents = target.read_text()
    assert "INTEGRATION_INGRESS_ENABLED=false" in contents
    assert "INTEGRATION_DISPATCH_ENABLED=false" in contents

    try:
        write_gmail_canary_env(
            target,
            secrets_bundle=secrets_bundle,
        )
    except FileExistsError:
        pass
    else:
        raise AssertionError("existing canary env must not be overwritten")


def test_canary_env_file_is_gitignored():
    gitignore = (ROOT / ".gitignore").read_text()
    assert ".env.*" in gitignore
