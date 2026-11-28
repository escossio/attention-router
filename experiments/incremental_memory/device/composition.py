"""Unbound M2 composition. No simulated verifier or implicit runtime configuration."""

from datetime import UTC, datetime
import re

from sqlalchemy import text

from attention_router.api.v1.human_identity import build_human_identity_router
from attention_router.api.v1.client_bootstrap import build_client_bootstrap_router
from attention_router.application.human_identity import HumanIdentityService
from attention_router.application.client_bootstrap import DeviceBootstrapService
from attention_router.core.human_identity import HumanAuthCredentialRejected
from attention_router.integrations.google_identity import GoogleIdentityVerifier
from attention_router.infrastructure.human_identity_models import (
    ExternalIdentityBindingRow,
    HumanAuthTransactionRow,
    HumanAuthContinuationGrantRow,
)
from attention_router.infrastructure.client_bootstrap_models import DeviceBootstrapChallengeRow
from experiments.incremental_memory.authenticated.app import create_app, require_experimental
from experiments.incremental_memory.authenticated.schema import IDENTITY_TABLES

TABLES = [
    ExternalIdentityBindingRow.__table__,
    HumanAuthTransactionRow.__table__,
    HumanAuthContinuationGrantRow.__table__,
    DeviceBootstrapChallengeRow.__table__,
]


def provision_onboarding(engine, *, issuer_role, reader_role):
    """Explicit provisioning in an exclusive M1-shaped experimental DB, never startup."""
    if not re.fullmatch(r"andy_memory_m1_[0-9a-f]{32}", engine.url.database or ""):
        raise ValueError("EXCLUSIVE_DATABASE_REQUIRED")
    for role in (issuer_role, reader_role):
        if not re.fullmatch(r"m1_[a-z0-9_]{1,48}", role):
            raise ValueError("EXPERIMENTAL_ROLE_REQUIRED")
    with engine.begin() as conn:
        if (
            conn.execute(text("SELECT classification FROM memory_environment")).scalar_one()
            != "SYNTHETIC_M1"
        ):
            raise ValueError("SYNTHETIC_DATABASE_REQUIRED")
        for table in TABLES:
            table.create(conn)
            conn.exec_driver_sql(
                f"CREATE TRIGGER memory_m1_write_fence BEFORE INSERT OR UPDATE OR DELETE ON {table.name} FOR EACH STATEMENT EXECUTE FUNCTION memory_m1_write_fence()"
            )
        names = ",".join(t.name for t in TABLES)
        conn.exec_driver_sql(f"GRANT SELECT ON {names} TO {reader_role}")
        names = ",".join(t.name for t in TABLES + IDENTITY_TABLES)
        conn.exec_driver_sql(f"GRANT SELECT,INSERT,UPDATE ON {names} TO {issuer_role}")


class RestrictedGoogleIdentity:
    def __init__(self, *, audience, allowed_subjects):
        if not audience or not audience.endswith(".apps.googleusercontent.com"):
            raise ValueError("DEDICATED_GOOGLE_AUDIENCE_REQUIRED")
        if not allowed_subjects or any(
            not isinstance(s, str) or not s.strip() for s in allowed_subjects
        ):
            raise ValueError("EXPLICIT_TEST_IDENTITIES_REQUIRED")
        self._verifier = GoogleIdentityVerifier(audience=audience)
        self._subjects = frozenset(allowed_subjects)

    def verify(self, id_token):
        verified = self._verifier.verify(id_token)
        now = datetime.now(UTC)
        if (
            verified.provider != "google"
            or verified.subject not in self._subjects
            or verified.issued_at > now
            or verified.expires_at <= now
        ):
            raise HumanAuthCredentialRejected()
        return verified


def create_device_app(
    *, issuer_factory, reader_factory, settings, google_audience, allowed_subjects
):
    """Always real cryptographic provider verification; tests patch only that external boundary."""
    verifier = RestrictedGoogleIdentity(audience=google_audience, allowed_subjects=allowed_subjects)
    app = create_app(
        issuer_factory=issuer_factory,
        reader_factory=reader_factory,
        settings=settings,
        onboarding=True,
    )
    require_experimental(issuer_factory, reader=False, onboarding=True)

    def get_session():
        with issuer_factory.begin() as session:
            session.execute(text("SET LOCAL lock_timeout='1s'"))
            session.execute(text("SET LOCAL statement_timeout='3s'"))
            yield session

    app.include_router(
        build_human_identity_router(
            get_session=get_session,
            service=HumanIdentityService(settings=settings, verifier=verifier),
        )
    )
    app.include_router(
        build_client_bootstrap_router(
            get_session=get_session, service=DeviceBootstrapService(settings=settings)
        )
    )
    return app
