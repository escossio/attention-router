"""Historic current-location samples are not proof of tracking consent."""
from pathlib import Path

BASE=Path(__file__).resolve().parents[2]
MIGRATION=BASE/"alembic/versions/0063_client_location_observations_v1.py"


def test_migration_does_not_backfill_without_explicit_optin():
    content=MIGRATION.read_text()
    assert "INSERT INTO client_location_observations" not in content
    assert "SELECT 'clh_' || md5(id)" not in content
    assert "ROLLBACK_REQUIRES_EXPLICIT_EXPORT_OR_PURGE" in content

def test_alembic_revision_fits_postgres_version_column():
    import re

    content=MIGRATION.read_text()
    match=re.search(r'^revision = "([^"]+)"$',content,re.MULTILINE)
    assert match
    assert len(match.group(1)) <= 32
