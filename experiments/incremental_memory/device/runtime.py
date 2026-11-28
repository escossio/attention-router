"""Explicit protected-file configuration for a future, separately authorized listener."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def load_app():
    path = Path(os.environ["ANDY_LAB_CONFIG"])
    if path.stat().st_mode & 0o077:
        raise ValueError("PRIVATE_CONFIGURATION_REQUIRED")
    config = json.loads(path.read_text())
    if config.get("environment") != "M2_SYNTHETIC_DEVICE":
        raise ValueError("EXPLICIT_EXPERIMENT_REQUIRED")
    # Existing ORM/service imports instantiate global Settings. Preserve its guards
    # using dedicated protected lab secrets; no admin/internal router is mounted.
    for key, variable in (
        ("lab_admin_secret", "ADMIN_TOKEN"),
        ("lab_ingress_secret", "INTERNAL_INGRESS_HMAC_SECRET"),
    ):
        value = config.get(key)
        if not isinstance(value, str) or len(value) < 32:
            raise ValueError("EXPERIMENTAL_BOOTSTRAP_SECRETS_REQUIRED")
        os.environ[variable] = value
    os.environ["ADMIN_AUTH_ENABLED"] = "true"
    os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
    from .composition import create_device_app

    factories = []
    for key in ("issuer_dsn", "reader_dsn"):
        engine = create_engine(
            config[key],
            hide_parameters=True,
            pool_size=2,
            max_overflow=0,
            connect_args={"connect_timeout": 3},
        )
        factories.append(sessionmaker(bind=engine, expire_on_commit=False))
    settings = SimpleNamespace(
        client_session_enabled=True,
        client_session_ttl_seconds=300,
        client_session_challenge_ttl_seconds=60,
        human_identity_enabled=True,
        human_auth_challenge_ttl_seconds=60,
        human_auth_continuation_grant_ttl_seconds=120,
        device_bootstrap_enabled=True,
        device_bootstrap_challenge_ttl_seconds=60,
    )
    return create_device_app(
        issuer_factory=factories[0],
        reader_factory=factories[1],
        settings=settings,
        google_audience=config["google_audience"],
        allowed_subjects=config["allowed_subjects"],
    )
