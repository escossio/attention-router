"""The operator stage entrypoint must register its FK targets on its own."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap


def test_selection_stage_import_resolves_foreign_keys_in_clean_process():
    code = textwrap.dedent(
        """
        import sys
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import create_engine, func, select
        from sqlalchemy.orm import Session

        from scripts.stage_personal_context_bootstrap_selection import stage_selection
        from attention_router.infrastructure.db import Base

        assert callable(stage_selection)
        assert "attention_router.web.app" not in sys.modules
        selection = Base.metadata.tables["personal_context_bootstrap_selections"]
        assert {fk.target_fullname for fk in selection.foreign_keys} == {
            "tenants.id",
            "human_identities.id",
            "personal_context_bootstrap_runs.id",
        }
        for foreign_key in selection.foreign_keys:
            assert foreign_key.column.table is Base.metadata.tables[
                foreign_key.target_fullname.split(".", 1)[0]
            ]
        assert {
            "tenants",
            "human_identities",
            "personal_context_bootstrap_runs",
            "personal_context_bootstrap_selections",
        } <= set(Base.metadata.tables)

        from attention_router.infrastructure.human_identity_models import HumanIdentityRow
        from attention_router.infrastructure.models import TenantRow
        from attention_router.infrastructure.personal_context_bootstrap_selection_models import (
            PersonalContextBootstrapSelectionRow,
        )

        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine, tables=[
            Base.metadata.tables[name] for name in (
                "tenants", "human_identities",
                "personal_context_bootstrap_runs",
                "personal_context_bootstrap_selections",
            )
        ])
        now = datetime.now(UTC)
        with Session(engine) as session:
            session.add(TenantRow(
                id="tenant-standalone", slug="standalone", name="Standalone",
                status="ACTIVE", created_at=now, updated_at=now,
            ))
            session.add(HumanIdentityRow(id="human-standalone", created_at=now))
            session.flush()
            session.add(PersonalContextBootstrapSelectionRow(
                id="pbs_standalone", tenant_id="tenant-standalone",
                owner_human_identity_id="human-standalone",
                chat_keys=[f"synthetic-chat-{i}" for i in range(5)],
                display_chats=[{"index": i, "display_name": f"Chat {i}",
                                "thread_type": "GROUP" if i == 0 else "DIRECT"}
                               for i in range(5)],
                expected_consent_ref="synthetic-consent",
                processing_budget={"page_size": 50},
                created_at=now, expires_at=now + timedelta(hours=1),
            ))
            session.flush()
            assert session.scalar(select(func.count()).select_from(
                PersonalContextBootstrapSelectionRow
            )) == 1
            assert session.scalar(select(func.count()).select_from(
                Base.metadata.tables["personal_context_bootstrap_runs"]
            )) == 0
            session.rollback()
        engine.dispose()
        """
    )
    env = os.environ.copy()
    env.setdefault("APP_ENV", "test")
    env["ADMIN_AUTH_ENABLED"] = "false"
    env.setdefault("INTERNAL_INGRESS_HMAC_SECRET", "x" * 32)

    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
