"""Isolated staged-read boundary. No route registration, no production switch.

A short admission guard captures detached, immutable evidence. Pure computation
runs after admission closes. A fresh authority guard must validate dependencies
and enclose the delivery callback. No result is returned to the caller as a
disclosable payload before the final guard has authorized its delivery.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from hashlib import sha256
from time import monotonic
from typing import Callable, Protocol

from attention_router.observability.context_performance import Stage, phase


class StagedReadRejected(ValueError):
    """A request failed closed before disclosure."""


@dataclass(frozen=True, slots=True)
class SourceVersion:
    source_ref: str
    revision: str
    receipt_ref: str
    receipt_revision: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class DetachedEvidence:
    """Private request-local material: never persist or reuse across requests."""

    content: bytes
    sources: tuple[SourceVersion, ...]

    def __post_init__(self):
        if type(self.content) is not bytes or len(self.content) > 2_000_000:
            raise StagedReadRejected("STAGED_EVIDENCE_SIZE_INVALID")
        if type(self.sources) is not tuple or any(
            not isinstance(source, SourceVersion) for source in self.sources
        ):
            raise StagedReadRejected("STAGED_EVIDENCE_DEPS_INVALID")
        if len({s.source_ref for s in self.sources}) != len(self.sources):
            raise StagedReadRejected("STAGED_EVIDENCE_DEPS_DUPLICATE")
        if any(
            not all((
                s.source_ref, s.revision, s.receipt_ref, s.receipt_revision, s.content_hash
            ))
            for s in self.sources
        ):
            raise StagedReadRejected("STAGED_EVIDENCE_DEPS_INCOMPLETE")


@dataclass(frozen=True, slots=True)
class AuthorizedSnapshot:
    """A snapshot whose authority stamp comes from the admission adapter."""

    scope_ref: str
    question_digest: str
    consent_revision: str
    epoch_token: object
    evidence: DetachedEvidence


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """Metadata only; no protected response body escapes this API."""

    body_sha256: str
    body_size: int


class StagedAuthority(Protocol):
    def capture(
        self, *, bearer: str, scope_ref: str, question: str
    ) -> AbstractContextManager[AuthorizedSnapshot]:
        """Authenticate and capture in a short, held authority boundary."""
        ...

    def delivery_gate(
        self, *, bearer: str, scope_ref: str, question: str,
        snapshot: AuthorizedSnapshot,
    ) -> AbstractContextManager[None]:
        """Reauthenticate, validate every dependency, and fence disclosure."""
        ...


class StagedRead:
    """Transport-neutral orchestration for a synchronous, bounded delivery.

    The authority adapter MUST use the same locks as revocation writers.
    The delivery callback MUST represent the actual disclosable boundary.
    Merely creating and returning an HTTP Response is NOT a valid delivery.
    """

    def __init__(
        self,
        authority: StagedAuthority,
        compute: Callable[[DetachedEvidence, str], bytes],
        *,
        compute_budget_seconds: float = 15.0,
        max_body_bytes: int = 262_144,
    ):
        if not 0 < compute_budget_seconds <= 60 or not 0 < max_body_bytes <= 1_000_000:
            raise ValueError("STAGED_BUDGET_INVALID")
        self._authority = authority
        self._compute = compute
        self._budget = compute_budget_seconds
        self._max_body = max_body_bytes

    def query_and_deliver(
        self, *, bearer: str, scope_ref: str, question: str,
        deliver: Callable[[bytes], None],
    ) -> DeliveryReceipt:
        if not bearer or not scope_ref or not isinstance(question, str) or not 0 < len(question.strip()) <= 1000:
            raise StagedReadRejected("STAGED_QUERY_INVALID")
        if not callable(deliver):
            raise ValueError("STAGED_DELIVERY_CALLBACK_REQUIRED")

        with phase(Stage.CAPTURE), self._authority.capture(
            bearer=bearer, scope_ref=scope_ref, question=question,
        ) as snapshot:
            if not isinstance(snapshot, AuthorizedSnapshot):
                raise StagedReadRejected("STAGED_CAPTURE_INVALID")
            if snapshot.scope_ref != scope_ref or snapshot.question_digest != sha256(question.encode()).hexdigest():
                raise StagedReadRejected("STAGED_CAPTURE_SCOPE_MISMATCH")
            if not snapshot.consent_revision or snapshot.epoch_token is None:
                raise StagedReadRejected("STAGED_CAPTURE_AUTHORITY_MISSING")

        # No consent, row, session, or database lock should be held here.
        started = monotonic()
        with phase(Stage.COMPILE):
            body = self._compute(snapshot.evidence, question)
        if monotonic() - started > self._budget:
            raise StagedReadRejected("STAGED_COMPUTE_EXPIRED")
        if type(body) is not bytes or len(body) > self._max_body:
            raise StagedReadRejected("STAGED_RESPONSE_INVALID")

        # The delivery callback executes *within* final authorization.
        # Final source/receipt/version/epoch/expiry verification belongs to the adapter.
        with phase(Stage.REVALIDATE), self._authority.delivery_gate(
            bearer=bearer, scope_ref=scope_ref, question=question,
            snapshot=snapshot,
        ):
            deliver(body)

        return DeliveryReceipt(body_sha256=sha256(body).hexdigest(), body_size=len(body))
