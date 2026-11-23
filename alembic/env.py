from logging.config import fileConfig
import os

from alembic import context

from attention_router.config import settings
from attention_router.application.context_schema_release import context_schema_release_authorized
from attention_router.infrastructure.db import Base
from attention_router.infrastructure import context_consent_models  # noqa: F401
from attention_router.infrastructure import artifact_models  # noqa: F401
from attention_router.infrastructure import human_identity_models  # noqa: F401
from attention_router.infrastructure import client_location_models  # noqa: F401
from attention_router.infrastructure import location_history_models  # noqa: F401
from attention_router.infrastructure import google_contacts_models  # noqa: F401
from attention_router.infrastructure import models  # noqa: F401
from attention_router.infrastructure import provider_authorization_models  # noqa: F401
from attention_router.infrastructure import personal_context_bootstrap_models  # noqa: F401
from attention_router.infrastructure import entity_resolution_models  # noqa: F401
from attention_router.infrastructure import semantic_episode_models  # noqa: F401
from attention_router.infrastructure import candidate_insight_models  # noqa: F401
from attention_router.infrastructure import obligation_models  # noqa: F401
from attention_router.infrastructure import attention_models  # noqa: F401

config = context.config
config.set_main_option("sqlalchemy.url", os.environ.get("DATABASE_URL", settings.database_url))
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=os.environ.get("DATABASE_URL", settings.database_url),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def guard_context_revision(*, ctx, step, **_):
    # Alembic invokes this before the migration transaction commits. A caller
    # selecting an intermediate revision must pass the same production gate.
    if (settings.app_env == "production"
            and set(step.up_revision_ids) & {
                "0069_cognitive_revocation_v0", "0070_context_integrity",
                "0071_context_source_consent", "0072_context_index_fence",
            } and not context_schema_release_authorized(ctx.connection)):
        raise RuntimeError("CONTEXT_INDEX_FENCE_PROMOTION_NOT_AUTHORIZED")


def run_migrations_online() -> None:
    from sqlalchemy import engine_from_config, pool

    # The controlled release runner supplies an already-validated connection
    # and an explicit outer transaction. No second connection is opened.
    provided = config.attributes.get("connection")
    if provided is not None:
        context.configure(connection=provided, target_metadata=target_metadata, on_version_apply=guard_context_revision)
        with context.begin_transaction():
            context.run_migrations()
        return

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        hide_parameters=True,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, on_version_apply=guard_context_revision)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
