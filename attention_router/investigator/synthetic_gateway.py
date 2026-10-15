"""Offline composition: signed synthetic admission before any investigation.

This wrapper is NOT a server, provider connector or production entry point.
"""

from __future__ import annotations

from .model import Investigation
from .service import Investigator
from .signed_admission import SignedEnvelope, SyntheticAdmission


class SyntheticInvestigationGateway:
    def __init__(
        self, admission: SyntheticAdmission, investigator: Investigator,
    ) -> None:
        self.admission = admission
        self.investigator = investigator

    def investigate_signed(self, envelope: SignedEnvelope) -> Investigation:
        # An untrusted request MUST pass admission before any runtime,
        # query budget allocation, audit session or tool invocation.
        event = self.admission.admit(envelope)
        return self.investigator.investigate(event)
