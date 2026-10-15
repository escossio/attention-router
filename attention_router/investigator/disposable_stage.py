"""Signed synthetic admission followed by disposable PostgreSQL staging.

This object only stages and returns a receipt; it does not invoke a model,
dispatch CI work or read operational hosts.
"""

from __future__ import annotations

from dataclasses import dataclass

from .disposable_pg_proof import DisposablePgSeedStore, StagedSeed
from .signed_admission import SignedEnvelope, SyntheticAdmission


@dataclass(frozen=True)
class DurableStageReceipt:
    seed: StagedSeed

    @property
    def may_start_new_investigation(self) -> bool:
        # An idempotent resend must NEVER silently start a second session.
        return self.seed.created


class DisposableSignedStage:
    def __init__(
        self, admission: SyntheticAdmission, store: DisposablePgSeedStore,
    ) -> None:
        self.admission = admission
        self.store = store

    def stage_signed(self, envelope: SignedEnvelope) -> DurableStageReceipt:
        # Current SyntheticAdmission has an in-memory nonce set. The PG store
        # provides the authoritative cross-instance nonce transaction.
        # This proof can fail closed between verification and DB commit;
        # production admission needs unified retry/lease semantics.
        verified = self.admission.admit(envelope)
        staged = self.store.stage_verified(
            principal_id=envelope.principal_id,
            nonce=envelope.nonce,
            event=verified,
        )
        return DurableStageReceipt(staged)
