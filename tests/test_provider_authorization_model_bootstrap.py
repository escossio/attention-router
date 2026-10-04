from __future__ import annotations

import os
import subprocess
import sys
import textwrap


def test_provider_authorization_models_register_fk_targets_in_clean_process():
    code = textwrap.dedent(
        """
        from attention_router.infrastructure.provider_authorization_models import (
            ProviderAuthorizationRow,
        )

        targets = {
            foreign_key.target_fullname
            for foreign_key in ProviderAuthorizationRow.__table__.foreign_keys
        }
        assert "tenants.id" in targets
        assert "human_identities.id" in targets
        assert "integration_bindings.id" in targets
        assert "integration_credentials.id" in targets

        for foreign_key in ProviderAuthorizationRow.__table__.foreign_keys:
            _ = foreign_key.column
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
