from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "ops" / "provisioning" / "native-otel-edge"
RENDERER_PATH = PACKAGE / "render.py"
TEMPLATE_PATH = PACKAGE / "apache-site.conf.template"
EXAMPLE_PATH = PACKAGE / "native-otel-edge.env.example"


spec = importlib.util.spec_from_file_location("native_otel_edge_render", RENDERER_PATH)
assert spec and spec.loader
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


def valid_values() -> dict[str, str]:
    return {
        "NATIVE_OTEL_EDGE_BIND_IP": "192.0.2.10",
        "NATIVE_OTEL_EDGE_BIND_PORT": "4318",
        "NATIVE_OTEL_TRANSPORT_SOURCE_IP": "198.51.100.10",
        "NATIVE_OTEL_INGRESS_SOURCE_IP": "203.0.113.18",
        "NATIVE_OTEL_COLLECTOR_LOOPBACK_PORT": "14318",
    }


def test_renderer_produces_closed_otlp_edge() -> None:
    rendered = renderer.render_template(
        TEMPLATE_PATH.read_text(encoding="utf-8"),
        valid_values(),
    )

    assert "Listen 192.0.2.10:4318" in rendered
    assert "Require all denied" in rendered
    assert "Require ip 198.51.100.10 203.0.113.18" in rendered
    assert "<Limit POST>" in rendered
    assert "<LimitExcept POST>" in rendered
    assert "http://127.0.0.1:14318/v1/traces" in rendered
    assert "%U" in rendered
    assert "@" not in rendered


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("NATIVE_OTEL_EDGE_BIND_IP", "not-an-ip"),
        ("NATIVE_OTEL_EDGE_BIND_PORT", "0"),
        ("NATIVE_OTEL_TRANSPORT_SOURCE_IP", "0.0.0.0"),
        ("NATIVE_OTEL_INGRESS_SOURCE_IP", "ff02::1"),
        ("NATIVE_OTEL_COLLECTOR_LOOPBACK_PORT", "70000"),
    ],
)
def test_renderer_rejects_invalid_network_values(key: str, value: str) -> None:
    values = valid_values()
    values[key] = value
    with pytest.raises(ValueError):
        renderer.render_template(
            TEMPLATE_PATH.read_text(encoding="utf-8"),
            values,
        )


def test_renderer_rejects_same_component_source_address() -> None:
    values = valid_values()
    values["NATIVE_OTEL_INGRESS_SOURCE_IP"] = values[
        "NATIVE_OTEL_TRANSPORT_SOURCE_IP"
    ]
    with pytest.raises(ValueError):
        renderer.validated_values(values)


def test_env_file_is_strict(tmp_path: Path) -> None:
    env = tmp_path / "edge.env"
    env.write_text(
        EXAMPLE_PATH.read_text(encoding="utf-8")
        + "\nUNEXPECTED_SECRET=must-not-be-accepted\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unexpected key"):
        renderer.read_env(env)


def test_public_example_contains_only_documentation_addresses() -> None:
    example = EXAMPLE_PATH.read_text(encoding="utf-8")
    assert "192.0.2.10" in example
    assert "198.51.100.10" in example
    assert "203.0.113.18" in example
    assert "10.77." not in example
    assert "192.168." not in example
