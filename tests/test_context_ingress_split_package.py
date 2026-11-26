"""Offline contract checks for the preparation-only legacy ingress split."""
import ast
import hashlib
import json
from pathlib import Path

import pytest
import yaml


PACKAGE = Path(__file__).resolve().parents[1] / "ops/context-canary/ingress-split"
PROVENANCE = json.loads((PACKAGE / "PROVENANCE.json").read_text())


def model(role):
    name = "compose.andy.yaml" if role == "andy" else "compose.maestro-standby.yaml"
    document = yaml.safe_load((PACKAGE / name).read_text())
    return document, document["services"][f"{role}-ingress"]


def test_preparation_only_and_exact_validated_files():
    assert PROVENANCE["status"] == "PREPARATION_ONLY"
    assert PROVENANCE["deployment_authorized"] is False
    assert PROVENANCE["schema_change_authorized"] is False
    assert PROVENANCE["maestro_standby_admission_enabled"] is False
    assert set(PROVENANCE["files_sha256"]) == {
        "compose.andy.yaml", "compose.maestro-standby.yaml", "maestro_standalone_server.py",
    }
    for name, digest in PROVENANCE["files_sha256"].items():
        assert hashlib.sha256((PACKAGE / name).read_bytes()).hexdigest() == digest


def test_legacy_dependency_boundary_is_explicit():
    assert PROVENANCE["historical_standalone_pr"] == 374
    assert PROVENANCE["tested_legacy_local_image_id"].startswith("sha256:")
    assert PROVENANCE["tested_database_revision"] == "0067_vector_lock_order"
    assert "attention_router/web/dual_ingress_server.py" in PROVENANCE[
        "legacy_image_dependencies"
    ]
    assert PROVENANCE["historical_launcher_sha256"] != PROVENANCE["files_sha256"][
        "maestro_standalone_server.py"
    ]
    ast.parse((PACKAGE / "maestro_standalone_server.py").read_text())


@pytest.mark.parametrize("role", ["andy", "maestro"])
def test_manifest_has_explicit_inputs_and_no_automatic_schema_change(role):
    document, service = model(role)
    assert list(document["services"]) == [f"{role}-ingress"]
    assert service["profiles"] == ["authorized-cutover"]
    assert ":?" in service["image"] and service["pull_policy"] == "never"
    assert not ({"build", "ports", "depends_on", "network_mode", "pid"} & service.keys())
    assert "alembic" not in " ".join(service["command"]).lower()
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["restart"] == "unless-stopped"
    assert set(service["networks"]) == {"operational"}
    assert document["networks"]["operational"]["external"] is True
    assert ":?" in document["networks"]["operational"]["name"]
    assert ":?" in service["networks"]["operational"]["ipv4_address"]
    assert all(v["bind"]["create_host_path"] is False for v in service["volumes"])


def test_andy_gate_and_certified_overlays():
    _, service = model("andy")
    assert service["command"][-2:] == ["--lifespan", "on"]
    assert service["environment"]["INTEGRATION_INGRESS_ENABLED"] == "true"
    assert service["environment"]["MAESTRO_RELAY_INGRESS_ENABLED"] == "false"
    assert service["environment"]["MAESTRO_HTTPS_LISTENER_ENABLED"] == "false"
    assert service["environment"]["ANDY_MAINTENANCE_GATE_DIR"] == "/run/andy-maintenance"
    gate = [v for v in service["volumes"] if not v["read_only"]]
    assert [v["target"] for v in gate] == ["/run/andy-maintenance"]
    paths = {v["target"].removeprefix("/app/") for v in service["volumes"] if v["read_only"]}
    assert paths == set(PROVENANCE["andy_overlay_sha256"])
    assert len(paths) == 4


def test_maestro_standby_authority_and_private_mounts():
    _, service = model("maestro")
    assert service["user"] == "10001:10001"
    assert service["environment"]["INTEGRATION_INGRESS_ENABLED"] == "false"
    assert service["environment"]["MAESTRO_RELAY_INGRESS_ENABLED"] == "false"
    assert service["environment"]["MAESTRO_HTTPS_LISTENER_ENABLED"] == "true"
    assert "ANDY_MAINTENANCE_GATE_DIR" not in service["environment"]
    assert service["environment"]["MAESTRO_HTTPS_BIND_HOST"] == service["networks"][
        "operational"
    ]["ipv4_address"]
    assert all(v["read_only"] for v in service["volumes"])
    assert {v["target"] for v in service["volumes"]} == {
        "/app/attention_router/web/maestro_standalone_server.py",
        "/run/secrets/database_url", "/run/tls/server.crt", "/run/tls/server.key",
    }


def test_independent_projects():
    a, _ = model("andy")
    b, _ = model("maestro")
    assert a["name"] != b["name"]
