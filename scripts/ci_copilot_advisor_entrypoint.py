from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request

from scripts import ci_copilot_advisor as advisor


_SENSITIVE_REDIRECT_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "x-github-api-version",
    }
)


class CredentialStrippingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow HTTPS redirects without forwarding GitHub credentials cross-origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None

        target = urllib.parse.urlsplit(redirected.full_url)
        if target.scheme.lower() != "https":
            raise urllib.error.HTTPError(
                redirected.full_url,
                code,
                "CI advisor refuses non-HTTPS redirects",
                headers,
                fp,
            )

        source = urllib.parse.urlsplit(req.full_url)
        source_origin = (source.scheme.lower(), source.hostname, source.port)
        target_origin = (target.scheme.lower(), target.hostname, target.port)
        if source_origin != target_origin:
            for mapping in (redirected.headers, redirected.unredirected_hdrs):
                for header in list(mapping):
                    if header.lower() in _SENSITIVE_REDIRECT_HEADERS:
                        del mapping[header]
        return redirected


def safe_request_text(method: str, url: str, *, token: str) -> str:
    request = urllib.request.Request(
        url,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "attention-router-ci-copilot-advisor-v1",
        },
    )
    opener = urllib.request.build_opener(CredentialStrippingRedirectHandler())
    try:
        with opener.open(request, timeout=20) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise advisor.AdvisorError(f"GitHub text API failed: {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise advisor.AdvisorError(f"GitHub text API failed: {exc.reason}") from exc


def main(argv: list[str] | None = None) -> int:
    # Transport injection is intentionally limited to the job-log reader. The
    # deterministic advisor contract, policy, sanitization and Copilot boundary
    # remain owned by scripts.ci_copilot_advisor.
    advisor._request_text = safe_request_text
    return advisor.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
