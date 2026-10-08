"""Container WhatsApp observability regressions, with no runtime mutations."""
import importlib.util
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "ops/provisioning/andy-ops-panel/transport_observability.py"
spec = importlib.util.spec_from_file_location("andy_ops_container_observability", MODULE)
obs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(obs)


def states(health="healthy"):
    return {role: {"exists": True, "running": True, "health": health,
                   "restart_count": 0, "started_at": "now", "image": "sha256:test", "revision": "test"}
            for role in obs.CONTAINERS}


def inputs():
    docker = states()
    transport = {**docker["transport"], "probe_error": None, "ready": True,
                 "client_state": "CONNECTED", "browser_debug_reachable": True,
                 "authenticated_identity_match": True, "owner_identity_verification": "MATCH",
                 "qr_seen": False, "page_count": 1}
    observer = {**docker["observer"], "probe_error": None, "service_state": "READY",
                "browser_connected": True, "status_fresh": True, "observer_health": "HEALTHY",
                "whatsapp_page_count": 1, "connected_page_count": 1, "app_state": "CONNECTED"}
    return docker, transport, observer


def derive(docker=None, transport=None, observer=None):
    d, t, o = inputs()
    d.update(docker or {})
    t.update(transport or {})
    o.update(observer or {})
    return obs._derive_state(d["browser"], t, o, d, True)


def codes(result):
    return {entry["code"] for entry in result["divergences"]}


def test_healthy_container_and_masked_legacy_are_ok():
    result = derive()
    assert result["severity"] == "OK" and result["divergences"] == []
    assert result["transport_page_count"] == result["observer_page_count"] == 1
    assert "legacy" not in result


def test_host_cdp_is_never_probed(monkeypatch):
    monkeypatch.setattr(obs, "ENABLED", True)
    monkeypatch.setattr(obs, "_docker_states", states)
    monkeypatch.setattr(obs, "_observer_status", lambda: inputs()[2])
    monkeypatch.setattr(obs, "_timeline", lambda: [])
    monkeypatch.setattr(obs, "_http_json", lambda url: {"status": "ready"} if url == obs.API_READY_URL else inputs()[1])
    result = obs.sample_transport_observability()
    assert result["browser"]["page_count"] == 1
    assert result["derived"]["severity"] == "OK"
    assert result["legacy"]["state"] == "RETIRED / MASKED"


def test_page_source_divergence():
    result = derive(observer={"whatsapp_page_count": 0})
    assert {"PAGE_COUNT_SOURCE_DIVERGENCE", "WHATSAPP_PAGE_TOPOLOGY_INVALID"} <= codes(result)


def test_both_page_sources_unknown():
    result = derive(transport={"page_count": None, "probe_error": "timeout"},
                    observer={"whatsapp_page_count": None, "probe_error": "missing"})
    assert result["page_count"] is None and result["severity"] == "WARN"
    assert codes(result) == {"OBSERVABILITY_GAP"}


def test_partial_page_evidence():
    result = derive(observer={"whatsapp_page_count": None, "probe_error": "missing"})
    assert result["page_count"] == 1 and result["evidence_completeness"] == "PARTIAL"
    assert "WHATSAPP_PAGE_TOPOLOGY_INVALID" not in codes(result)


def test_browser_container_unhealthy():
    d = states()
    d["browser"]["health"] = "unhealthy"
    assert "BROWSER_CONTAINER_NOT_HEALTHY" in codes(derive(docker=d))


def test_transport_not_connected():
    assert "TRANSPORT_NOT_CONNECTED" in codes(derive(transport={"client_state": "DISCONNECTED"}))


def test_observer_not_ready():
    assert "OBSERVER_NOT_READY" in codes(derive(observer={"service_state": "STARTING"}))


def test_qr_seen():
    assert "QR_OBSERVED" in codes(derive(transport={"qr_seen": True}))


def test_stale_message_activity_does_not_degrade_observer():
    result = derive(observer={"message_activity_age_seconds": 3600})
    assert result["severity"] == "OK"


def test_sensitive_fields_are_not_projected():
    payload = {"ready": True, "body": "secret", "token": "secret", "hmac": "secret",
               "secret": "secret", "owner_phone": "secret"}
    assert obs._project(payload, obs.SAFE_TRANSPORT_FIELDS) == {"ready": True}
    assert obs._project(payload, obs.SAFE_OBSERVER_FIELDS) == {}


def test_old_journal_is_not_used(monkeypatch):
    monkeypatch.setattr(obs, "ENABLED", True)
    monkeypatch.setattr(obs, "_docker_states", states)
    monkeypatch.setattr(obs, "_observer_status", lambda: inputs()[2])
    monkeypatch.setattr(obs, "_timeline", lambda: [])
    monkeypatch.setattr(obs, "_http_json", lambda url: {"status": "ready"} if url == obs.API_READY_URL else inputs()[1])
    monkeypatch.setattr(obs.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy subprocess")))
    assert obs.sample_transport_observability()["derived"]["severity"] == "OK"
