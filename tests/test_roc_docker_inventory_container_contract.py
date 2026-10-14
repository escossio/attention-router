"""Portable, container-only Zabbix Engine inventory contract, no live IO."""
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parents[1] / "ops/observability/docker-inventory"
C = BASE / "containers"
COMPOSE = yaml.safe_load((C / "compose.yaml").read_text())


def test_two_isolated_services_without_host_agent_or_host_ports():
    services = COMPOSE["services"]
    assert set(services) == {"roc-docker-view", "roc-agent2"}
    proxy = services["roc-docker-view"]
    agent = services["roc-agent2"]
    assert proxy["network_mode"] == "none"
    assert "network_mode" not in agent
    assert agent["networks"]["monitoring"]["aliases"] == ["roc-agent2"]
    assert "ports" not in proxy and "ports" not in agent
    assert all(s["read_only"] is True for s in services.values())
    assert all(s["cap_drop"] == ["ALL"] for s in services.values())
    assert all("no-new-privileges:true" in s["security_opt"] for s in services.values())
    assert all("privileged" not in s or s["privileged"] is False for s in services.values())
    assert COMPOSE["networks"]["monitoring"]["external"] is True


def test_only_allowlisted_proxy_gets_root_equivalent_docker_socket():
    proxy = COMPOSE["services"]["roc-docker-view"]
    agent = COMPOSE["services"]["roc-agent2"]
    docker_mounts = [m for m in proxy["volumes"] if m["target"] == "/var/run/docker.sock"]
    assert len(docker_mounts) == 1
    assert docker_mounts[0]["source"] == "${ROC_DOCKER_SOCKET_PATH:-/var/run/docker.sock}"
    assert docker_mounts[0]["read_only"] is True
    assert not any(m["target"] == "/var/run/docker.sock" for m in agent["volumes"])
    assert proxy["user"] == "0:1995"
    assert agent["user"] == "1997:1995"
    volumes = [m for m in agent["volumes"] if m["target"] == "/run/andy-zabbix-docker"]
    assert len(volumes) == 1 and volumes[0]["read_only"] is True
    assert proxy["restart"] == agent["restart"] == "unless-stopped"


def test_zabbix_engine_agent_is_private_and_not_host_os():
    conf = (C / "agent2.conf").read_text()
    assert "Server=roc-zabbix-server" in conf
    assert "Hostname=Andy Engine - AGT01" in conf
    assert "ListenPort=10050" in conf
    assert "ServerActive=" not in conf
    assert "10.45.0.2" not in conf
    assert "127.0.0.1:10050" not in conf
    assert "UserParameter=andy.docker.inventory" in (C / "docker-inventory.conf").read_text()
    assert "Plugins.Docker.Endpoint=unix:///run/andy-zabbix-docker/docker.sock" in (C / "docker-plugin.conf").read_text()
    for name in ("Dockerfile.view", "Dockerfile.agent2"):
        df = (C / name).read_text()
        assert "FROM " in df
        assert "zabbix_agent2" in df or "zabbix_proxy.py" in df
        assert "--privileged" not in df


def test_no_app_secrets_and_api_proxy_versioned_source():
    payload = (C / "compose.yaml").read_text()
    assert "ZABBIX_ADMIN_PASSWORD" not in payload
    assert "POSTGRES_PASSWORD" not in payload
    assert "docker.sock:ro" not in payload or "read_only: true" in payload
    source = (BASE / "zabbix_proxy.py").read_text()
    assert "do_PUT = do_PATCH = do_DELETE = do_POST" in source
    assert "self.send_error(405" in source
    assert "SENSITIVE_LABEL" in source
    assert "response.read(16 * 1024 * 1024 + 1)" in source
    assert "COPY zabbix_proxy.py " in (C / "Dockerfile.view").read_text()
    assert "COPY zabbix_inventory.py " in (C / "Dockerfile.agent2").read_text()
