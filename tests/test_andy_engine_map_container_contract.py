"""Engine map container is independent of the AGT host, source bundled in OCI."""
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1] / "ops/provisioning/andy-engine-build-map/container"
CONFIG = yaml.safe_load((ROOT / "compose.yaml").read_text())


def test_static_server_in_a_restricted_container():
    s = CONFIG["services"]["engine-map"]
    assert s["read_only"] is True
    assert s["user"] == "10001:10001"
    assert s["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in s["security_opt"]
    assert s["restart"] == "unless-stopped"
    assert s["ports"][0].startswith("$" + "{ENGINE_MAP_BIND_HOST:-127.0.0.1}")
    assert s["ports"][0].endswith(":18120")
    assert "volumes" not in s and "privileged" not in s
    assert "healthcheck" in s


def test_all_runtime_assets_are_bundled_in_source():
    text = (ROOT / "Dockerfile").read_text()
    assert "COPY server.py index.html app.js styles.css engine-map.json ./" in text
    for f in ("server.py", "index.html", "app.js", "styles.css", "engine-map.json"):
        assert (ROOT / f).is_file()
    assert "10.45.0.2" not in text
    assert "192.168.88.2" not in (ROOT / "server.py").read_text()
    document = json.loads((ROOT / "engine-map.json").read_text())
    assert document["schema_version"] >= 1
    assert len(document["stages"]) > 0


def test_state_is_versioned_not_modified_at_runtime():
    s = (ROOT / "server.py").read_text()
    assert '"0.0.0.0"' in s
    assert '"/healthz"' in s
    assert "Path(__file__).resolve().parent" in s
    assert "write_text(" not in s
