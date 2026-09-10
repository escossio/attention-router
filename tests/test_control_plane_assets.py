from pathlib import Path


STATIC = Path("attention_router/web/static")


def test_capability_lab_assets_are_self_contained():
    html = (STATIC / "control-plane.html").read_text(encoding="utf-8")
    css = (STATIC / "control-plane.css").read_text(encoding="utf-8")
    javascript = (STATIC / "control-plane.js").read_text(encoding="utf-8")
    scenarios = (STATIC / "capability-lab-scenarios.json").read_text(encoding="utf-8")

    assert '/static/control-plane.css' in html
    assert '/static/control-plane.js' in html
    assert "/static/capability-lab-scenarios.json" in javascript
    assert "Andy Capability Lab" in html
    assert "Desenvolvimento e validação" in html
    assert "não define a UX comercial da Andy" in html
    assert 'data-view="scenarios"' in html
    assert 'id="scenario-list"' in html
    assert 'id="scenario-engine-metrics"' in html
    assert 'id="scenario-engine-list"' in html
    assert css.strip()
    assert javascript.strip()
    assert scenarios.strip()


def test_capability_lab_does_not_persist_admin_credential_in_browser_storage():
    javascript = (STATIC / "control-plane.js").read_text(encoding="utf-8")

    assert "localStorage.setItem" not in javascript
    assert "localStorage.getItem" not in javascript
    assert "window.localStorage" not in javascript
    assert "sessionStorage.setItem" not in javascript
    assert "sessionStorage.getItem" not in javascript
    assert "window.sessionStorage" not in javascript
    assert "Authorization" in javascript


def test_capability_lab_reads_existing_protected_runtime_surfaces_only():
    javascript = (STATIC / "control-plane.js").read_text(encoding="utf-8")

    expected_paths = {
        "/api/v1/admin/platform/operations/snapshot",
        "/api/v1/admin/platform/operations/capability-lab/scenario-engine",
        "/api/v1/admin/platform/operations/capability-lab/probe/",
        "/api/v1/admin/policies",
        "/api/v1/admin/platform/matrix",
        "/api/v1/private/response-reviews?review_status=PENDING",
        "/api/v1/private/execution-intents?intent_status=PENDING",
    }
    for path in expected_paths:
        assert path in javascript

    assert 'method: "POST"' not in javascript
    assert 'method: "PATCH"' not in javascript
    assert 'method: "DELETE"' not in javascript


def test_capability_lab_is_not_presented_as_customer_settings_ui():
    html = (STATIC / "control-plane.html").read_text(encoding="utf-8")
    normalized_html = " ".join(html.split())

    assert "bancada para provar features" in normalized_html
    assert "Laboratório não é autoridade" in normalized_html
    assert "não define a UX comercial da Andy" in normalized_html
    assert "Feature acceptance" in normalized_html
    assert "SEM EFEITO DE PRODUÇÃO" in normalized_html


def test_capability_lab_does_not_claim_unbound_feature_success():
    html = (STATIC / "control-plane.html").read_text(encoding="utf-8")
    javascript = (STATIC / "control-plane.js").read_text(encoding="utf-8")
    scenarios = (STATIC / "capability-lab-scenarios.json").read_text(encoding="utf-8")

    assert "INCOMPLETE" in html
    assert 'bindingState = "UNBOUND"' in javascript
    assert '"engine_scenario_key": null' in scenarios
    assert "sem binding/evidência durável, o resultado não é certificação" in javascript


def test_capability_lab_presents_existing_runtime_as_canonical():
    html = (STATIC / "control-plane.html").read_text(encoding="utf-8")
    javascript = (STATIC / "control-plane.js").read_text(encoding="utf-8")
    normalized_html = " ".join(html.split())

    assert "O Attention Router já possui manifest versionado, registry" in normalized_html
    assert "O laboratório exercita esse motor; não cria outro." in normalized_html
    assert "canonical runtime capability" in javascript
    assert "este é o registry existente que o Lab deve exercitar, não duplicar" in javascript


def test_capability_lab_hypotheses_show_registry_stages_and_t0_probe():
    javascript = (STATIC / "control-plane.js").read_text(encoding="utf-8")

    assert 'label: "UNREGISTERED"' in javascript
    assert "não existe no registry canônico observado" in javascript
    assert "T0 esperado:" in javascript
    assert "T1 simulado:" in javascript
    assert "T2 esperado:" in javascript
    assert "registry:" in javascript
    assert "probe T0:" in javascript
    assert "evidência durável:" in javascript
    assert "EPHEMERAL_ONLY" in javascript


def test_browser_probe_can_send_only_repository_scenario_id():
    javascript = (STATIC / "control-plane.js").read_text(encoding="utf-8")

    assert "encodeURIComponent(scenario.scenario_id)" in javascript
    assert "capability_key=" not in javascript
    assert "requester_actor_key=" not in javascript
    assert "owner_authorized" not in javascript
    assert "policy_allows" not in javascript


def test_capability_lab_frontend_does_not_expect_minimized_scenario_fields():
    javascript = (STATIC / "control-plane.js").read_text(encoding="utf-8")

    assert "run.correlation_id" not in javascript
    assert "run.terminal_reason" not in javascript
