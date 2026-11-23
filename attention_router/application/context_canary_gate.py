"""Private one-OWNER context canary gate. Test runs are unchanged.

Production activation requires explicit settings for the exact tenant, OWNER,
binding and source account. No setting or API handler enrolls an owner, grants
source consent, creates an epoch, mounts a route, or migrates a database.
"""

from attention_router.config import settings


def canary_configured() -> bool:
    if settings.app_env == "test":
        return True
    return bool(
        settings.app_env == "production"
        and settings.staged_context_private_canary_enabled is True
        and settings.client_session_enabled is True
        and settings.context_epoch_authority_canary_enabled is True
        and settings.cognitive_runtime_canary_tenant_id
        and settings.persistent_memory_canary_binding_id
        and settings.staged_context_canary_owner_id
        and settings.staged_context_canary_source_account
        and settings.staged_context_canary_source_account != "default"
    )


def canary_scope(
    *, tenant_id: str, binding_id: str | None = None,
    owner_id: str | None = None, source_account: str | None = None,
) -> bool:
    if not canary_configured():
        return False
    if settings.app_env == "test":
        return True
    return bool(
        tenant_id == settings.cognitive_runtime_canary_tenant_id
        and (binding_id is None or binding_id == settings.persistent_memory_canary_binding_id)
        and (owner_id is None or owner_id == settings.staged_context_canary_owner_id)
        and (
            source_account is None
            or source_account == settings.staged_context_canary_source_account
        )
    )
