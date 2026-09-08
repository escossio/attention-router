from __future__ import annotations


DEFAULT_TENANT_ID = "00000000-0000-4000-8000-000000000001"
DEFAULT_TENANT_SLUG = "tenant_alex"


class TenantScopeError(PermissionError):
    """Raised when an object is addressed outside the active tenant boundary."""


def require_same_tenant(expected: str, actual: str, *, subject: str = "entity") -> None:
    if expected != actual:
        raise TenantScopeError(f"TENANT_SCOPE_MISMATCH:{subject}")
