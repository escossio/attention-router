"""CI publisher portability contract; do not start any live jobs in CI."""
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
D = ROOT / "ops/provisioning/github-app-control-plane/publisher-containers"
CONFIG = yaml.safe_load((D / "compose.yaml").read_text())


def test_publishers_are_explicitly_cutover_gated():
    services = CONFIG["services"]
    assert set(services) == {"postgres-publisher", "shadow-publisher"}
    for service in services.values():
        assert service["profiles"] == ["activate-after-drain"]
        assert service["user"] == "10001:10001"
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert service["restart"] == "unless-stopped"
        assert "ports" not in service
        assert service.get("privileged", False) is False
        assert service.get("network_mode") != "host"
        assert "daemon" in service["command"]
        assert "docker.sock" not in str(service)
    assert services["postgres-publisher"]["command"][3].endswith("distributed_postgres_check.py")
    assert services["shadow-publisher"]["command"][3].endswith("distributed_shadow_checks.py")


def test_private_mounts_are_explicit_and_not_baked_in():
    s = CONFIG["services"]["postgres-publisher"]
    mounts = {v["target"]: v for v in s["volumes"]}
    assert set(mounts) == {
        "/var/lib/andy-github-app", "/var/lib/andy-ci", "/var/log/andy-ci",
        "/ci-profiles", "/etc/andy-github-app", "/home/ci/.ssh",
        "/usr/local/lib/andy-ci/worker-capacity.json",
    }
    assert mounts["/etc/andy-github-app"]["read_only"] is True
    assert mounts["/home/ci/.ssh"]["read_only"] is True
    assert mounts["/usr/local/lib/andy-ci/worker-capacity.json"]["read_only"] is True
    assert mounts["/var/lib/andy-github-app"].get("read_only", False) is False
    for target in mounts:
        assert mounts[target]["source"].startswith("$" + "{GITHUB_CI_")
    assert "CI_GITHUB_APP_PRIVATE_KEY" not in (D / "compose.yaml").read_text()


def test_ci_image_reproduces_versioned_dispatcher_without_host_package():
    image = (D / "Dockerfile.ci").read_text()
    assert image.startswith("FROM python:3.13-slim@sha256:")
    assert "cryptography==46.0.3" in image
    assert "openssh-client" in image
    assert "USER 10001:10001" in image
    assert "COPY ops/provisioning/distributed-ci-lab/control-plane/andy-ci-distributed " in image
    assert "COPY ops/provisioning/distributed-ci-lab/control-plane/andy-ci-distributed-suite " in image
    assert "COPY ops/provisioning/github-app-control-plane/distributed_postgres_check.py " in image
    assert "COPY ops/provisioning/github-app-control-plane/distributed_shadow_checks.py " in image
    assert "COPY ops/provisioning/github-app-control-plane/probe.py " in image
    assert "ADD /" not in image
    assert "github-webhook" not in image.lower()
