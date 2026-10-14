"""Contract for GoAccess replacing two native systemd services (without runtime IO)."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "ops/observability/goaccess"
CONFIG = yaml.safe_load((DIRECTORY / "compose.yaml").read_text())


def test_goaccess_services_are_nonroot_and_isolated():
    assert CONFIG["name"] == "attention-router-roc-goaccess"
    services = CONFIG["services"]
    assert set(services) == {"roc-goaccess", "roc-goaccess-web"}
    for service in services.values():
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert service["user"].startswith("10001:")
        assert service["restart"] == "unless-stopped"
        assert "healthcheck" in service
        assert "privileged" not in service
        assert not any("docker.sock" in str(m) for m in service["volumes"])
        assert service["ports"][0].startswith("$" + "{GOACCESS_LISTEN_HOST")
    assert services["roc-goaccess"]["ports"][0].endswith(":7890")
    assert services["roc-goaccess-web"]["ports"][0].endswith(":7891")


def test_source_log_is_read_only_and_reports_are_persistent():
    services = CONFIG["services"]
    log_mount = next(x for x in services["roc-goaccess"]["volumes"]
                     if x["target"] == "/logs")
    assert log_mount["read_only"] is True
    assert "GOACCESS_LOG_DIR" in log_mount["source"]
    report_mounts = [next(x for x in services[s]["volumes"]
                          if x["target"] == "/reports")
                     for s in ("roc-goaccess", "roc-goaccess-web")]
    assert report_mounts[0]["source"] == report_mounts[1]["source"]
    assert report_mounts[0].get("read_only", False) is False
    assert report_mounts[1]["read_only"] is True


def test_reproduces_native_log_format_and_requires_explicit_ws():
    source = (DIRECTORY / "start-goaccess.sh").read_text()
    assert "tail -n +1 -F" in source
    assert "GOACCESS_WS_URL" in source
    assert "NF >= 6" in source
    for option in ("--real-time-html", "--log-format=", "--date-format=",
                   "--time-format=", "--ws-url=", "--no-progress"):
        assert option in source
    assert "rm -rf" not in source
    assert "truncate" not in source


def test_metadata_only_no_static_runtime_credential_or_host_interface():
    for file in ("Dockerfile", "compose.yaml", "start-goaccess.sh"):
        source = (DIRECTORY / file).read_text()
        assert "POSTGRES_PASSWORD" not in source
        assert "10.45.0.2" not in source
        assert "/var/log/apache2/attention-router-client-api-access.log" not in source
        assert "/var/run/docker.sock" not in source
