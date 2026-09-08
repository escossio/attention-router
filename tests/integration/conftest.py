import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from attention_router.infrastructure.repository import seed_policies


def _db_url(db_name: str) -> str:
    base = os.environ["DATABASE_URL"]
    return base.rsplit("/", 1)[0] + f"/{db_name}"


@pytest.fixture(scope="session")
def pg_url():
    db_name = f"attention_router_test_{uuid.uuid4().hex[:10]}"
    admin = create_engine(_db_url("postgres"), isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as conn:
        conn.execute(text(f'create database "{db_name}"'))
    url = _db_url(db_name)
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    repo_root = Path(__file__).resolve().parents[2]
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=repo_root, env=env, check=True)
    yield url
    with admin.connect() as conn:
        conn.execute(text(f'drop database if exists "{db_name}" with (force)'))
    admin.dispose()


@pytest.fixture()
def Session(pg_url):
    engine = create_engine(pg_url, future=True)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with SessionLocal() as session:
        seed_policies(session)
        session.commit()
    yield SessionLocal
    engine.dispose()
