"""Dozzle proxy and frontend are portable OCI services, not host Python."""
from pathlib import Path
import importlib.util

import yaml

ROOT = Path(__file__).resolve().parents[1]
D = ROOT / "ops/observability/dozzle"
CFG = yaml.safe_load((D / "compose.yaml").read_text())


def test_two_services_one_filtered_socket_no_direct_access():
    assert CFG["name"] == "attention-router-dozzle-v1"
    services = CFG["services"]
    assert set(services) == {"roc-dozzle-view", "dozzle"}
    proxy = services["roc-dozzle-view"]
    web = services["dozzle"]
    assert proxy["network_mode"] == "none"
    assert "ports" not in proxy
    assert web["ports"][0].endswith(":8080")
    assert "DOZZLE_CONTAINER_NAME" in web["container_name"]
    assert web["image"].startswith("amir20/dozzle@sha256:")
    for service in services.values():
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert service["restart"] == "unless-stopped"
        assert service.get("privileged", False) is False
    p_socket = next(x for x in proxy["volumes"] if x["target"] == "/var/run/docker.sock")
    assert p_socket["read_only"] is True
    assert not any(x["target"] == "/var/run/docker.sock" for x in web["volumes"])
    view = next(x for x in web["volumes"] if x["target"] == "/run/andy-dozzle-view")
    assert view["read_only"] is True


def test_proxy_has_explicit_get_route_allowlist_and_rejects_writes():
    s = (D / "proxy.py").read_text()
    assert "is_allowed_route(path)" in s
    assert "self.send_error(403" in s
    assert "self.send_error(405" in s
    assert "do_PUT = do_DELETE = do_PATCH = do_POST" in s
    assert "container" in s
    assert "docker.sock" in s
    assert "MAP.read_text()" in s
    assert "DOCKER_HOST" in (D / "compose.yaml").read_text()


def test_rejected_sensitive_read_routes():
    spec = importlib.util.spec_from_file_location("candidate_proxy", D / "proxy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.is_allowed_route("/containers/json") is True
    assert module.is_allowed_route("/v1.45/containers/abc/logs") is True
    assert module.is_allowed_route("/events") is True
    for forbidden in ("/containers/abc/exec", "/containers/abc/archive",
                      "/containers/abc/attach", "/secrets", "/volumes",
                      "/plugins", "/build", "/containers/create"):
        assert module.is_allowed_route(forbidden) is False
