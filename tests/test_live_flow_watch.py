from scripts.live_flow_watch import (
    _matches_binding,
    _matches_inbound_event,
    _trace_service_names,
    _trace_start_unix_nano,
)


def _span(name: str, **attributes):
    return {
        "name": name,
        "attributes": [
            {"key": key, "value": {"stringValue": value}}
            for key, value in attributes.items()
        ],
    }


def test_watcher_matches_binding_and_inbound_on_any_span():
    spans = [_span("attention.message"), _span(
        "actor.resolve",
        **{
            "attention.binding_id": "23fa59eedff686fe",
            "attention.inbound_event_id": "event-real",
        },
    )]

    assert _matches_binding(spans, "23fa59eedff686fe")
    assert _matches_inbound_event(spans, "event-real")


def test_watcher_rejects_wrong_binding_or_inbound():
    spans = [_span(
        "actor.resolve",
        **{
            "attention.binding_id": "23fa59eedff686fe",
            "attention.inbound_event_id": "event-real",
        },
    )]

    assert not _matches_binding(spans, "wrong-binding")
    assert not _matches_inbound_event(spans, "event-other")


def test_watcher_allows_trace_replay_without_filters():
    assert _matches_binding([], None)
    assert _matches_inbound_event([], None)


def test_candidate_trace_service_is_rejected():
    payload = {"batches": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "attention-router-binding-candidate"}}]}, "scopeSpans": []}]}
    assert "attention-router-worker" not in _trace_service_names(payload)


def test_historical_live_trace_is_older_than_arm_time():
    payload = {"batches": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "attention-router-worker"}}]}, "scopeSpans": [{"spans": [{"name": "inbound.receive", "startTimeUnixNano": "1000000000", "attributes": []}]}]}]}
    assert "attention-router-worker" in _trace_service_names(payload)
    assert _trace_start_unix_nano(payload) < 2_000_000_000
