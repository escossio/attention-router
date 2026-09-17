from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class HumanAuthState(str, Enum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class VerifiedProviderIdentity:
    provider: str
    subject: str
    nonce: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class IssuedHumanAuthChallenge:
    challenge_id: str
    nonce: str
    expires_at: datetime


@dataclass(frozen=True)
class HumanIdentityValidated:
    human_identity_id: str
    status: str = "HUMAN_IDENTITY_VALIDATED"


@dataclass(frozen=True)
class HumanAuthContinuationGrant:
    token: str = field(repr=False)
    purpose: str
    expires_at: datetime


@dataclass(frozen=True)
class HumanIdentityContinued:
    human_identity_id: str
    continuation_grant: HumanAuthContinuationGrant = field(repr=False)
    status: str = "HUMAN_IDENTITY_VALIDATED"


class HumanAuthContinuationGrantRejected(Exception):
    code = "HUMAN_AUTH_CONTINUATION_GRANT_REJECTED"


class HumanAuthDisabled(Exception):
    code = "HUMAN_AUTH_DISABLED"


class HumanAuthChallengeNotFound(Exception):
    code = "HUMAN_AUTH_CHALLENGE_NOT_FOUND"


class HumanAuthChallengeExpired(Exception):
    code = "HUMAN_AUTH_CHALLENGE_EXPIRED"


class HumanAuthChallengeConsumed(Exception):
    code = "HUMAN_AUTH_CHALLENGE_CONSUMED"


class HumanAuthCredentialRejected(Exception):
    code = "HUMAN_AUTH_CREDENTIAL_REJECTED"


class HumanAuthNonceMismatch(Exception):
    code = "HUMAN_AUTH_NONCE_MISMATCH"


class HumanAuthProviderUnavailable(Exception):
    code = "HUMAN_AUTH_PROVIDER_UNAVAILABLE"
