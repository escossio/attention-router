"""Small offline checks for the standalone process boundary."""
import os

import pytest

from attention_router.web import maestro_standalone_server as server


def test_database_url_must_be_protected_postgres_file(tmp_path, monkeypatch):
    path = tmp_path / "database-url"
    monkeypatch.setenv("MAESTRO_DATABASE_URL_FILE", str(path))
    with pytest.raises(ValueError, match="required"):
        server._load_database_url()
    path.write_text("sqlite:///local.db", encoding="utf-8")
    with pytest.raises(ValueError, match="PostgreSQL"):
        server._load_database_url()
    path.write_text("postgresql+psycopg://user:synthetic@db.invalid/synthetic", encoding="utf-8")
    server._load_database_url()
    assert os.environ["DATABASE_URL"].startswith("postgresql+psycopg://")


def test_listener_refuses_disabled_admission(monkeypatch):
    from attention_router.config import settings

    monkeypatch.setattr(settings, "maestro_relay_ingress_enabled", False)
    with pytest.raises(ValueError, match="explicitly enabled"):
        server._listener()
