from pathlib import Path


STATIC = Path("attention_router/web/static")


def test_control_plane_assets_are_self_contained():
    html = (STATIC / "control-plane.html").read_text(encoding="utf-8")
    css = (STATIC / "control-plane.css").read_text(encoding="utf-8")
    javascript = (STATIC / "control-plane.js").read_text(encoding="utf-8")

    assert '/static/control-plane.css' in html
    assert '/static/control-plane.js' in html
    assert "Andy Control Plane" in html
    assert css.strip()
    assert javascript.strip()


def test_control_plane_does_not_persist_admin_credential_in_browser_storage():
    javascript = (STATIC / "control-plane.js").read_text(encoding="utf-8")

    assert "localStorage.setItem" not in javascript
    assert "localStorage.getItem" not in javascript
    assert "window.localStorage" not in javascript
    assert "sessionStorage.setItem" not in javascript
    assert "sessionStorage.getItem" not in javascript
    assert "window.sessionStorage" not in javascript
    assert "Authorization" in javascript


def test_control_plane_reads_existing_protected_runtime_surfaces_only():
    javascript = (STATIC / "control-plane.js").read_text(encoding="utf-8")

    expected_paths = {
        "/api/v1/admin/platform/operations/snapshot",
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
