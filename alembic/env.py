from logging.config import fileConfig
import os

from alembic import context

from attention_router.config import settings
from attention_router.infrastructure.db import Base
from attention_router.infrastructure import artifact_models  # noqa: F401
from attention_router.infrastructure import models  # noqa: F401

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


def run_migrations_online() -> None:
    from sqlalchemy import engine_from_config, pool

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        hide_parameters=True,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
