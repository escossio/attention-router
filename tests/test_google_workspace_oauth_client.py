from __future__ import annotations

from io import BytesIO
import json
import logging
from urllib import error as urllib_error

import pytest

from attention_router.application.gmail_connection import (
    GMAIL_METADATA_SCOPE,
    GmailAuthorizationRejected,
    GmailProviderUnavailable,
    GoogleGmailProfile,
    GoogleTokenGrant,
    GoogleWorkspaceOAuthClient,
)


SECRET_MARKER = "synthetic-sensitive-marker"


@pytest.fixture
def oauth_client():
    return GoogleWorkspaceOAuthClient(
        client_id="synthetic-client-id",
        client_secret=SECRET_MARKER + "-client-secret",
    )


def _reject_profile(monkeypatch, *, body, http_status=403):
    response = BytesIO(body)
    error = urllib_error.HTTPError(
        GoogleWorkspaceOAuthClient.PROFILE_URL,
        http_status,
        SECRET_MARKER + "-http-message",
        {},
        response,
    )

    def urlopen(request, *, timeout):
        assert request.full_url == GoogleWorkspaceOAuthClient.PROFILE_URL
        assert request.get_method() == "GET"
        raise error

    monkeypatch.setattr(
        "attention_router.application.gmail_connection.urllib_request.urlopen",
        urlopen,
    )
    return response


def _assert_safe_record(caplog, *, reason, google_status, http_status=403):
    records = [record for record in caplog.records if record.name == "uvicorn.error"]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.event == "GMAIL_PROFILE_FAILED"
    assert record.http_status == http_status
    assert record.google_status == google_status
    assert record.reason == reason
    assert record.getMessage() == (
        f"GMAIL_PROFILE_FAILED http_status={http_status} "
        f"google_status={google_status} reason={reason}"
    )
    assert record.exc_info is None
    assert record.stack_info is None
    assert SECRET_MARKER not in caplog.text
    assert SECRET_MARKER not in repr(record.__dict__)


@pytest.mark.parametrize(
    ("error_fields", "expected_reason", "expected_error"),
    [
        (
            {"errors": [{"reason": "accessNotConfigured"}]},
            "accessNotConfigured", GmailAuthorizationRejected,
        ),
        (
            {"errors": [{"reason": "insufficientPermissions"}]},
            "insufficientPermissions", GmailAuthorizationRejected,
        ),
        (
            {"errors": [{"reason": "domainPolicy"}]},
            "domainPolicy", GmailAuthorizationRejected,
        ),
        (
            {"errors": [{"reason": "rateLimitExceeded"}]},
            "rateLimitExceeded", GmailAuthorizationRejected,
        ),
        (
            {"details": [{
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "SERVICE_DISABLED",
                "metadata": {"consumer": SECRET_MARKER + "-project-id"},
            }]},
            "SERVICE_DISABLED", GmailProviderUnavailable,
        ),
        (
            {"errors": [{"reason": "accessNotConfigured"}], "details": [{
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "SERVICE_DISABLED",
            }]},
            "SERVICE_DISABLED,accessNotConfigured", GmailProviderUnavailable,
        ),
        (
            {"errors": [{"reason": "insufficientPermissions"}], "details": [{
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
            }]},
            "ACCESS_TOKEN_SCOPE_INSUFFICIENT,insufficientPermissions",
            GmailAuthorizationRejected,
        ),
    ],
)
def test_profile_logs_only_canonical_error_metadata(
    oauth_client, monkeypatch, caplog, error_fields, expected_reason, expected_error,
):
    _reject_profile(monkeypatch, body=json.dumps({"error": {
        "status": "PERMISSION_DENIED",
        "message": SECRET_MARKER + "-provider-message",
        **error_fields,
    }}).encode())

    with pytest.raises(expected_error) as caught:
        oauth_client.gmail_profile(SECRET_MARKER + "-access-token")

    assert caught.value.code == expected_error.code
    assert caught.value.__cause__ is None
    _assert_safe_record(
        caplog, reason=expected_reason, google_status="PERMISSION_DENIED",
    )


def test_profile_service_disabled_does_not_reclassify_http_401(
    oauth_client, monkeypatch, caplog,
):
    _reject_profile(monkeypatch, http_status=401, body=json.dumps({"error": {
        "status": "UNAUTHENTICATED",
        "details": [{
            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
            "reason": "SERVICE_DISABLED",
        }],
    }}).encode())

    with pytest.raises(GmailAuthorizationRejected):
        oauth_client.gmail_profile(SECRET_MARKER + "-access-token")

    _assert_safe_record(
        caplog, reason="SERVICE_DISABLED", google_status="UNAUTHENTICATED", http_status=401,
    )


@pytest.mark.parametrize(
    ("http_status", "expected_error"),
    [(401, GmailAuthorizationRejected), (403, GmailAuthorizationRejected),
     (400, GmailProviderUnavailable), (429, GmailProviderUnavailable),
     (500, GmailProviderUnavailable), (503, GmailProviderUnavailable)],
)
def test_profile_preserves_http_error_mapping(
    oauth_client, monkeypatch, caplog, http_status, expected_error,
):
    _reject_profile(monkeypatch, body=b"{}", http_status=http_status)

    with pytest.raises(expected_error):
        oauth_client.gmail_profile(SECRET_MARKER + "-access-token")

    _assert_safe_record(
        caplog, reason="UNKNOWN", google_status="UNKNOWN", http_status=http_status,
    )


@pytest.mark.parametrize("body", [
    b"", b"invalid-json", b"\xff", b"[]", b'{"error":null}',
    b'{"error":{"status":[],"errors":{},"details":{}}}',
    b'{"error":{"errors":[null,7,{"reason":[]}],"details":[null]}}',
    b"[" * 2000,
    b"x" * (16 * 1024 + 100),
])
def test_profile_malformed_error_body_cannot_mask_rejection(
    oauth_client, monkeypatch, caplog, body,
):
    response = _reject_profile(monkeypatch, body=body)

    with pytest.raises(GmailAuthorizationRejected):
        oauth_client.gmail_profile(SECRET_MARKER + "-access-token")

    assert response.tell() <= 16 * 1024
    _assert_safe_record(caplog, reason="UNKNOWN", google_status="UNKNOWN")


def test_profile_failed_error_body_read_cannot_mask_rejection(
    oauth_client, monkeypatch, caplog,
):
    response = _reject_profile(monkeypatch, body=b"")

    def failed_read(size):
        assert size == 16 * 1024
        raise OSError(SECRET_MARKER + "-read-error")

    monkeypatch.setattr(response, "read", failed_read)
    with pytest.raises(GmailAuthorizationRejected):
        oauth_client.gmail_profile(SECRET_MARKER + "-access-token")

    _assert_safe_record(caplog, reason="UNKNOWN", google_status="UNKNOWN")


def test_profile_never_logs_unknown_provider_text(oauth_client, monkeypatch, caplog):
    _reject_profile(monkeypatch, body=json.dumps({"error": {
        "status": "PERMISSION_DENIED " + SECRET_MARKER,
        "message": SECRET_MARKER + "-email@example.invalid",
        "errors": [{"reason": "accessNotConfigured\n" + SECRET_MARKER}],
        "details": [{
            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
            "reason": "SERVICE_DISABLED " + SECRET_MARKER + "-refresh-token",
            "metadata": {"authorization_code": SECRET_MARKER + "-code"},
        }, {
            "@type": "untrusted-type",
            "reason": "SERVICE_DISABLED",
        }],
        "body": SECRET_MARKER + "-bearer-and-provider-key",
    }}).encode())

    with pytest.raises(GmailAuthorizationRejected):
        oauth_client.gmail_profile(SECRET_MARKER + "-access-token")

    _assert_safe_record(caplog, reason="UNKNOWN", google_status="UNKNOWN")


def test_token_exchange_and_profile_success_preserve_contract(
    oauth_client, monkeypatch, caplog,
):
    access_token = SECRET_MARKER + "-access-token"
    refresh_token = SECRET_MARKER + "-refresh-token"
    authorization_code = SECRET_MARKER + "-authorization-code"
    responses = [
        {"access_token": access_token, "refresh_token": refresh_token,
         "scope": GMAIL_METADATA_SCOPE},
        {"emailAddress": " Synthetic-Owner@Example.Invalid "},
    ]
    requests = []

    def urlopen(request, *, timeout):
        requests.append(request)
        return BytesIO(json.dumps(responses.pop(0)).encode())

    monkeypatch.setattr(
        "attention_router.application.gmail_connection.urllib_request.urlopen",
        urlopen,
    )
    grant = oauth_client.exchange_authorization_code(authorization_code)
    profile = oauth_client.gmail_profile(grant.access_token)

    assert grant == GoogleTokenGrant(access_token, refresh_token, (GMAIL_METADATA_SCOPE,))
    assert profile == GoogleGmailProfile("synthetic-owner@example.invalid")
    assert requests[0].get_method() == "POST"
    assert requests[0].full_url == oauth_client.TOKEN_URL
    assert requests[1].get_method() == "GET"
    assert requests[1].get_header("Authorization") == f"Bearer {access_token}"
    assert not responses
    assert not caplog.records
