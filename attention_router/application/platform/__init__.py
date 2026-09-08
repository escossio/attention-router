"""Application services for Platform Matrix V1."""

from attention_router.application.platform.registry import (
    matrix_status,
    resolve_capability_request,
    sync_platform_registry,
)

__all__ = ["matrix_status", "resolve_capability_request", "sync_platform_registry"]
