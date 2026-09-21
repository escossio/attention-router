"""HTTP transport for credentials bound to one explicitly configured endpoint."""

from urllib import request as urllib_request


class _RejectRedirects(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib otherwise forwards Authorization even across origins. OAuth
        # form bodies and integration credentials must stay at their endpoint.
        return None


def urlopen_without_redirects(request, *, timeout):
    return urllib_request.build_opener(_RejectRedirects()).open(
        request,
        timeout=timeout,
    )
