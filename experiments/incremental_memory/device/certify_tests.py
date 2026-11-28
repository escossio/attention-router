"""Discardable image-only runner; preserves global guards without APP_ENV=test."""

import os
import secrets
import sys

import pytest

os.environ["ADMIN_TOKEN"] = secrets.token_urlsafe(32)
os.environ["INTERNAL_INGRESS_HMAC_SECRET"] = secrets.token_urlsafe(32)

sys.exit(pytest.main([
    "-q", "-p", "no:cacheprovider", "--confcutdir=/checks",
    "test_incremental_memory_m1.py", "test_incremental_memory_m2.py",
]))
