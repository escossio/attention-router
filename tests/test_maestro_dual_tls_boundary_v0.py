"""Only synthetic/loopback probes. Never starts a production listener."""
import asyncio
import json
import socket
import ssl
import subprocess
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener, urlopen

import pytest
from fastapi.testclient import TestClient

from attention_router.config import settings
from attention_router.web.dual_ingress_server import (
    build_servers, tls_listener_from_env, _supervise,
)
from attention_router.web.ingress_app import create_ingress_app
from attention_router.web.maestro_relay_ingress import (
    MAESTRO_RELAY_PATH, create_maestro_tls_app,
)


@pytest.fixture
def synthetic_cert(tmp_path):
    cert = tmp_path / "synthetic-cert.pem"
    key = tmp_path / "synthetic-key.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-nodes", "-sha256", "-days", "1", "-subj", "/CN=localhost",
        "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
        "-keyout", str(key), "-out", str(cert),
    ], check=True, capture_output=True, timeout=15)
    key.chmod(0o600)
    return cert, key


def test_maestro_missing_on_legacy_even_when_feature_enabled(monkeypatch):
    monkeypatch.setattr(settings, "maestro_relay_ingress_enabled", True)
    with TestClient(create_ingress_app()) as client:
        assert client.get("/health/live").status_code == 200
        assert client.post(MAESTRO_RELAY_PATH, json={}).status_code == 404
    with TestClient(create_maestro_tls_app(), base_url="http://testserver") as client:
        response = client.post(MAESTRO_RELAY_PATH + "?x=y", json={},
                               headers={"X-Forwarded-Proto": "https"})
        assert response.status_code == 403


def test_optin_requires_complete_private_tls_configuration(tmp_path, synthetic_cert):
    cert, key = synthetic_cert
    assert tls_listener_from_env({}, relay_admission_enabled=False) is None
    with pytest.raises(ValueError):
        tls_listener_from_env({}, relay_admission_enabled=True)
    base = {
        "MAESTRO_HTTPS_LISTENER_ENABLED": "true",
        "MAESTRO_HTTPS_BIND_HOST": "127.0.0.1",
        "MAESTRO_HTTPS_PORT": "19443",
        "MAESTRO_HTTPS_CERTFILE": str(cert),
        "MAESTRO_HTTPS_KEYFILE": str(key),
    }
    good = tls_listener_from_env(base, relay_admission_enabled=True)
    assert good.host == "127.0.0.1"
    for key_name, bad in (
        ("MAESTRO_HTTPS_BIND_HOST", "0.0.0.0"),
        ("MAESTRO_HTTPS_BIND_HOST", "8.8.8.8"),
        ("MAESTRO_HTTPS_PORT", str(settings.ingress_http_port)),
        ("MAESTRO_HTTPS_CERTFILE", "/missing/not-present"),
        ("MAESTRO_HTTPS_LISTENER_ENABLED", "maybe"),
    ):
        candidate = {**base, key_name: bad}
        with pytest.raises(ValueError):
            tls_listener_from_env(candidate, relay_admission_enabled=True)
    key.chmod(0o644)
    with pytest.raises(ValueError):
        tls_listener_from_env(base, relay_admission_enabled=True)


def _allocate_free_local_port():
    with socket.socket(socket.AF_INET) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_legacy_http_and_dedicated_https_run_together(
    monkeypatch, synthetic_cert,
):
    cert, key = synthetic_cert
    legacy_port = _allocate_free_local_port()
    secure_port = _allocate_free_local_port()
    while secure_port == legacy_port:
        secure_port = _allocate_free_local_port()
    monkeypatch.setattr(settings, "ingress_http_host", "127.0.0.1")
    monkeypatch.setattr(settings, "ingress_http_port", legacy_port)
    monkeypatch.setattr(settings, "maestro_relay_ingress_enabled", False)
    listener = tls_listener_from_env({
        "MAESTRO_HTTPS_LISTENER_ENABLED": "true",
        "MAESTRO_HTTPS_BIND_HOST": "127.0.0.1",
        "MAESTRO_HTTPS_PORT": str(secure_port),
        "MAESTRO_HTTPS_CERTFILE": str(cert),
        "MAESTRO_HTTPS_KEYFILE": str(key),
    }, relay_admission_enabled=False)
    legacy_server, tls_server = build_servers(listener)

    async def probe():
        serving = asyncio.create_task(_supervise((legacy_server, tls_server)))
        try:
            for _ in range(150):
                if legacy_server.started and tls_server.started:
                    break
                if serving.done():
                    raise AssertionError("a listener exited unexpectedly")
                await asyncio.sleep(0.03)
            assert legacy_server.started and tls_server.started
            assert tls_server.config.ssl.minimum_version == ssl.TLSVersion.TLSv1_2
            def request():
                # The old HTTP port still responds, but cannot see the MAESTRO route.
                with urlopen(f"http://127.0.0.1:{legacy_port}/health/live", timeout=2) as r:
                    assert json.load(r)["listener"] == "ingress"
                try:
                    urlopen(Request(
                        f"http://127.0.0.1:{legacy_port}{MAESTRO_RELAY_PATH}",
                        data=b"{}", method="POST",
                    ), timeout=2)
                except HTTPError as error:
                    assert error.code == 404
                else:
                    raise AssertionError("plaintext listener exposed MAESTRO")
                # Certificate not trusted by default; must fail before request dispatch.
                try:
                    urlopen(f"https://127.0.0.1:{secure_port}/health/live", timeout=2)
                except URLError as error:
                    assert "CERTIFICATE_VERIFY_FAILED" in str(error)
                else:
                    raise AssertionError("temporary cert trusted without CA")
                opener = build_opener(ProxyHandler({}), HTTPSHandler(
                    context=ssl.create_default_context(cafile=str(cert)),
                ))
                with opener.open(
                    f"https://127.0.0.1:{secure_port}/health/live", timeout=2,
                ) as response:
                    assert json.load(response)["listener"] == "maestro-tls"
                try:
                    opener.open(Request(
                        f"https://127.0.0.1:{secure_port}/health/other",
                        method="GET",
                    ), timeout=2)
                except HTTPError as error:
                    assert error.code == 404
                else:
                    raise AssertionError("TLS listener exposed arbitrary path")
            await asyncio.to_thread(request)
        finally:
            for server in (legacy_server, tls_server):
                server.should_exit = True
            await asyncio.wait_for(serving, timeout=7)

    asyncio.run(probe())
