"""Preparation only: no imports/startup of productive composition, no listener.

The caller supplies the exclusive experimental SQLAlchemy session dependency.
Enrollment and scope delivery authority must be implemented/validated before mounting.
"""


def session_router(get_session, settings):
    from attention_router.application.client_session import ClientSessionService
    from attention_router.api.v1.client_session import build_client_session_router

    return build_client_session_router(
        get_session=get_session, service=ClientSessionService(settings=settings)
    )


class MemoryDelivery:
    """StagedDelivery-shaped adapter; does not invent authentication.

    authority.guard must authenticate ClientSession, require OWNER, resolve its
    persisted scope, and lock authority + memory revisions through delivery.
    Only suitable for the bounded deterministic V0; no model/network calls inside.
    No default authority and no permissive fallback are provided.
    """

    def __init__(self, authority, memory):
        self.authority = authority
        self.memory = memory

    def query_and_deliver(self, *, bearer, scope_ref, question, deliver):
        import hashlib
        import json

        if (
            not callable(deliver)
            or not isinstance(question, str)
            or not 1 <= len(question.strip()) <= 1000
        ):
            raise ValueError("INVALID_QUERY")
        with self.authority.guard(bearer=bearer, scope_ref=scope_ref) as authorized:
            # An authority-bound memory interface is required: the CLI's
            # single-dataset answer() must not be wired directly here.
            response = self.memory.answer_authorized(authorized, question)
            body = json.dumps(response, ensure_ascii=False, allow_nan=False).encode()
            if len(body) > 16384:
                raise ValueError("RESPONSE_TOO_LARGE")
            authorized.revalidate(response)
            deliver(body)
            return {"body_sha256": hashlib.sha256(body).hexdigest(), "body_size": len(body)}
