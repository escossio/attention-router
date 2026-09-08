import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


# Test-only settings are established before test modules import application settings.
# Never inherit provider credentials or a live database URL from a developer's .env.
os.environ.update({
    "APP_ENV": "test",
    "DATABASE_URL": "sqlite+pysqlite:///:memory:",
    "ADMIN_AUTH_ENABLED": "true",
    "ADMIN_TOKEN": "synthetic-admin-token",
    "INTERNAL_INGRESS_HMAC_SECRET": "unit-test-internal-ingress-secret-32-bytes",
    "OPENAI_API_KEY": "",
    "TTS_INTERNAL_TOKEN": "",
    "STT_INTERNAL_TOKEN": "",
    "ANDY_AGENT_ENABLED": "false",
    "AGENT_BUILDER_INTERVIEWER_PROVIDER": "deterministic",
    "AGENT_DECISION_PIPELINE_ENABLED": "true",
    "LEGACY_EXTERNAL_FALLBACK_ENABLED": "false",
    "TTS_ENABLED": "false",
    "STT_ENABLED": "false",
    "EXTERNAL_DELIVERY_ENABLED": "false",
    "AUTONOMOUS_EXECUTION_ENABLED": "false",
    "META_WHATSAPP_ENABLED": "false",
    "META_WEBHOOK_DISPATCH_ENABLED": "false",
    "OTEL_TRACING_ENABLED": "false",
})


@pytest.fixture(autouse=True)
def legacy_inline_test_context(request, monkeypatch):
    """Select the historical inline path only for tests of that explicit contract."""
    legacy_module = request.node.path.name in {"test_engine.py", "test_attention_logic.py"}
    legacy_outbound = request.node.path.name == "test_wwebjs_outbound.py" and request.node.name in {
        "test_cell_phone_does_not_use_wwebjs", "test_soft_ping_does_not_use_wwebjs",
    }
    if legacy_module or legacy_outbound:
        from attention_router.config import settings

        monkeypatch.setattr(settings, "agent_decision_pipeline_enabled", False)


@pytest.fixture()
def session():
    from attention_router.infrastructure.db import Base
    from attention_router.infrastructure.repository import seed_policies

    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with Session() as db:
        seed_policies(db)
        db.commit()
        yield db
    engine.dispose()
