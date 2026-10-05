from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "ops/provisioning/channel-sync"


def test_compose_has_one_outbound_only_dedicated_service():
    compose = yaml.safe_load((PACKAGE / "channel-sync.compose.yaml").read_text())
    assert set(compose["services"]) == {"channel-sync"}
    service = compose["services"]["channel-sync"]
    command = ["python", "-m", "attention_router.infrastructure.channel_sync_service"]
    assert service["command"] == command
    assert service["restart"] == "on-failure"
    assert service["healthcheck"]["test"] == ["CMD", *command, "--check"]
    assert service["image"].startswith("${CHANNEL_SYNC_IMAGE:?")
    assert service["env_file"][0].startswith("${CHANNEL_SYNC_ENV_FILE:?")
    assert service["networks"] == ["channel_sync"]
    assert compose["networks"] == {
        "channel_sync": {
            "external": True,
            "name": "${CHANNEL_SYNC_NETWORK_NAME:?set the IPAM-governed host-provisioned network}",
        },
    }
    for forbidden in (
        "ports", "expose", "privileged", "network_mode", "cap_add", "volumes",
        "devices", "build", "depends_on", "container_name",
    ):
        assert forbidden not in service
    assert service["cap_drop"] == ["ALL"]
    assert service["read_only"] is True
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["init"] is True
    assert service["stop_grace_period"] == "60s"
    assert service["pids_limit"] > 0
    assert service["logging"]["options"]["max-file"] == "3"


def test_public_package_has_no_address_allocation_or_secret_values():
    compose = (PACKAGE / "channel-sync.compose.yaml").read_text()
    for forbidden in (
        "ipv4_address", "subnet", "gateway", "parent:", "driver_opts",
        "docker.sock", "NET_ADMIN", "vlan_id", "10.77.10.",
    ):
        assert forbidden not in compose
    env = (PACKAGE / "channel-sync.env.example").read_text()
    values = dict(
        line.split("=", 1) for line in env.splitlines()
        if line.strip() and not line.startswith("#")
    )
    assert {key: value for key, value in values.items() if key.endswith("_ENABLED")} == {
        "ADMIN_AUTH_ENABLED": "false",
        "CLIENT_SESSION_ENABLED": "false",
        "GMAIL_CONNECT_ENABLED": "false",
        "GMAIL_PRODUCT_RUNNER_ENABLED": "false",
        "GMAIL_PRODUCT_SCHEDULER_ENABLED": "false",
        "GMAIL_BODY_INGESTION_ENABLED": "false",
        "GMAIL_ATTACHMENT_INGESTION_ENABLED": "false",
    }
    assert not any(
        marker in key for key in values for marker in ("SECRET", "TOKEN", "KEY", "DATABASE_URL")
    )
    for path in PACKAGE.iterdir():
        text = path.read_text()
        assert not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
        assert not re.search(r"(?i)\bvlan[\s_:-]*\d+\b", text)
        assert not re.search(r"\b(?:enp\d+s\d+|eth\d+)(?:\.\d+)?\b", text)
