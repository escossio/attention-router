from pathlib import Path


JAVASCRIPT = Path("attention_router/web/static/control-plane.js")


def test_capability_lab_renders_minimized_semantic_assertion_evidence():
    javascript = JAVASCRIPT.read_text(encoding="utf-8")

    for token in (
        "semantic_assertions",
        "assertion_summary",
        "assertion_id",
        "expected_property",
        "observed_summary",
        "evaluator",
        "evidence_refs",
        "evidence_type",
        "source_sha",
        "unresolved_evidence_ref_count",
    ):
        assert token in javascript

    assert "assertion:" in javascript
    assert "resultado:" in javascript
    assert "expected:" in javascript
    assert "observed:" in javascript
    assert "evidence:" in javascript
    assert "unresolved:" in javascript


def test_semantic_evidence_ui_remains_read_only_and_minimized():
    javascript = JAVASCRIPT.read_text(encoding="utf-8")

    assert 'method: "POST"' not in javascript
    assert 'method: "PATCH"' not in javascript
    assert 'method: "DELETE"' not in javascript
    assert "run.correlation_id" not in javascript
    assert "run.terminal_reason" not in javascript
    assert "sanitized_metadata" not in javascript
    assert "assertion.provenance" not in javascript
    assert "run.requester_actor_key" not in javascript
    assert "assertion.requester_actor_key" not in javascript
    assert "run.request_text" not in javascript
    assert "assertion.request_text" not in javascript
