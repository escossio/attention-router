"""Experimental fence for the existing transactional context boundary."""

from dataclasses import asdict, dataclass, replace
from hmac import compare_digest, digest
from secrets import token_hex
from threading import Lock
from typing import Protocol

from sqlalchemy import select

from attention_router.application.context_validity import ContextGeneration, ContextValidity
from attention_router.application.context_canary_gate import canary_configured
from attention_router.config import settings
from attention_router.infrastructure.hashing import stable_hash, stable_json
from attention_router.infrastructure.models import AuditEventRow


@dataclass(frozen=True)
class EpochState:
    authority: str
    domain: str
    epoch: str
    revision: int
    checkpoint: str


class EpochAuthority(Protocol):
    def read(self, domain: str, challenge: str) -> tuple[EpochState, bytes]: ...
    def advance(self, expected: EpochState, proposed: EpochState) -> None: ...


def sign_state(key: bytes, state: EpochState, challenge: str) -> bytes:
    payload = {"version": 1, "challenge": challenge, "state": asdict(state)}
    return digest(key, stable_json(payload).encode(), "sha256")


@dataclass(frozen=True)
class AnchoredGeneration:
    epoch: str
    revision: int
    generation: ContextGeneration


class ContextEpoch:
    """Bind the existing generation and journal to an independent CAS authority.

    Uses a synthetic authority in tests and an explicitly gated durable
    authority for a production canary. Advancing the anchor before PostgreSQL
    commits may deny service after a crash. It cannot authorize an older
    checkpoint. An exact committed checkpoint can resume after restart.
    """

    def __init__(self, authority: EpochAuthority, *, authority_id: str, environment: str, key: bytes):
        if settings.app_env != "test":
            # A production canary must use an explicitly gated, independent
            # durable authority. Synthetic laboratory anchors remain test-only.
            from attention_router.infrastructure.context_epoch_durable import DurableEpochAuthority

            if not (
                canary_configured()
                and settings.context_epoch_authority_canary_enabled
                and isinstance(authority, DurableEpochAuthority)
            ):
                raise ValueError("CONTEXT_EPOCH_CONFIGURATION_DENIED")
        if not authority_id or not environment or len(key) < 32:
            raise ValueError("CONTEXT_EPOCH_CONFIGURATION_DENIED")
        self.authority = authority
        self.authority_id = authority_id
        self.environment = environment
        self._key = key
        self._seen: dict[str, EpochState] = {}
        self._lock = Lock()

    @staticmethod
    def domain_for(environment: str, context: ContextValidity) -> str:
        if not environment:
            raise ValueError("CONTEXT_EPOCH_ENVIRONMENT_REQUIRED")
        return stable_hash({"environment": environment, "scope": context.scope})

    def domain(self, context: ContextValidity) -> str:
        return self.domain_for(self.environment, context)

    @staticmethod
    def checkpoint_for(context: ContextValidity) -> str:
        token = context.token()
        journal = context.session.execute(select(AuditEventRow.id, AuditEventRow.payload).where(
            AuditEventRow.tenant_id == context.arguments["tenant_id"],
            AuditEventRow.origin == "context_validity",
            AuditEventRow.event_type == "CONTEXT_VALIDITY_CHANGED",
            AuditEventRow.payload["scope"].as_string() == context.scope,
        )).all()
        generations = {}
        for event_id, payload in journal:
            current = payload.get("generation")
            if type(current) is not int or current in generations or payload.get("previous_generation") != current - 1:
                raise ValueError("CONTEXT_EPOCH_JOURNAL_INVALID")
            generations[current] = (event_id, payload)
        if len(generations) != token.generation or any(not 1 <= n <= token.generation for n in generations):
            raise ValueError("CONTEXT_EPOCH_JOURNAL_INVALID")
        return stable_hash({
            "generation": asdict(token),
            "journal": [generations[n] for n in sorted(generations)],
        })

    def checkpoint(self, context: ContextValidity) -> str:
        return self.checkpoint_for(context)

    def _verified(self, context: ContextValidity) -> EpochState:
        domain = self.domain(context)
        challenge = token_hex(32)
        try:
            state, signature = self.authority.read(domain, challenge)
        except OSError as error:
            raise ValueError("CONTEXT_EPOCH_AUTHORITY_UNAVAILABLE") from error
        if (
            state.domain != domain or state.authority != self.authority_id or not state.epoch
            or type(state.revision) is not int or state.revision < 0
            or not compare_digest(signature, sign_state(self._key, state, challenge))
        ):
            raise ValueError("CONTEXT_EPOCH_PROOF_INVALID")
        with self._lock:
            previous = self._seen.get(domain)
            if previous and (
                state.epoch != previous.epoch or state.revision < previous.revision
                or (state.revision == previous.revision and state != previous)
            ):
                raise ValueError("CONTEXT_EPOCH_AUTHORITY_REGRESSED")
            self._seen[domain] = state
        if state.checkpoint != self.checkpoint(context):
            raise ValueError("CONTEXT_EPOCH_RECONCILIATION_REQUIRED")
        return state

    def token(self, context: ContextValidity) -> AnchoredGeneration:
        state = self._verified(context)
        return AnchoredGeneration(state.epoch, state.revision, context.token())

    def advance(self, context: ContextValidity, operation, *args, **kwargs):
        """Stage an existing generation-changing operation in the PG transaction."""
        expected = self._verified(context)
        with context.session.begin_nested():
            result = operation(*args, **kwargs)
            target = self.checkpoint(context)
            if target != expected.checkpoint:
                self.authority.advance(
                    expected, replace(expected, revision=expected.revision + 1, checkpoint=target),
                )
        return result

    def compile(self, context: ContextValidity, query: str):
        token = self.token(context)
        return token, context.compile(query)[1]

    def consume(self, context: ContextValidity, token: AnchoredGeneration, packet):
        if token != self.token(context):
            raise ValueError("CONTEXT_EPOCH_TOKEN_STALE")
        return context.consume_compiled(token.generation, packet)
