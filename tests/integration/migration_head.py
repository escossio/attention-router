"""Single Alembic tip used when verifying migrations against the current schema."""
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def current_schema_head() -> str:
    root = Path(__file__).resolve().parents[2]
    heads = ScriptDirectory.from_config(Config(str(root / "alembic.ini"))).get_heads()
    if len(heads) != 1:
        raise AssertionError("EXPECTED_SINGLE_POSTGRES_MIGRATION_HEAD")
    return heads[0]
