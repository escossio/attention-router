from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from scripts import ci_copilot_advisor_entrypoint as entrypoint


def _headers_lower(request: urllib.request.Request) -> dict[str, str]:
    combined = dict(request.headers)
    combined.update(request.unredirected_hdrs)
    return {key.lower(): value for key, value in combined.items()}


def test_cross_origin_https_redirect_strips_github_credentials():
    request = urllib.request.Request(
        "https://api.github.com/repos/escossio/attention-router/actions/jobs/123/logs",
        method="GET",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer synthetic-token",
            "Cookie": "session=synthetic",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "attention-router-ci-copilot-advisor-v1",
        },
    )

    redirected = entrypoint.CredentialStrippingRedirectHandler().redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://pipelines.actions.githubusercontent.com/job-log.txt?sig=synthetic",
    )

    assert redirected is not None
    headers = _headers_lower(redirected)
    assert "authorization" not in headers
    assert "proxy-authorization" not in headers
    assert "cookie" not in headers
    assert "x-github-api-version" not in headers
    assert headers["accept"] == "application/vnd.github+json"
    assert headers["user-agent"] == "attention-router-ci-copilot-advisor-v1"


def test_same_origin_https_redirect_keeps_github_authorization():
    request = urllib.request.Request(
        "https://api.github.com/repos/escossio/attention-router/actions/jobs/123/logs",
        method="GET",
        headers={"Authorization": "Bearer synthetic-token"},
    )

    redirected = entrypoint.CredentialStrippingRedirectHandler().redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://api.github.com/repos/escossio/attention-router/actions/jobs/123/logs?attempt=2",
    )

    assert redirected is not None
    assert _headers_lower(redirected)["authorization"] == "Bearer synthetic-token"


def test_redirect_to_non_https_is_rejected():
    request = urllib.request.Request(
        "https://api.github.com/repos/escossio/attention-router/actions/jobs/123/logs",
        method="GET",
        headers={"Authorization": "Bearer synthetic-token"},
    )

    with pytest.raises(urllib.error.HTTPError, match="refuses non-HTTPS redirects"):
        entrypoint.CredentialStrippingRedirectHandler().redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "http://example.test/job-log.txt",
        )
