"""Offline runtime-image checks; no listener, provider request or database connection."""

import json
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile

from .runtime import load_app


def main():
    assert sys.version_info[:2] == (3, 13)
    assert os.getuid() == 10001
    # Even loopback has no running peer; prohibit any connection during this smoke.
    def forbidden(*args, **kwargs):
        raise AssertionError("SMOKE_MUST_NOT_CONNECT")

    socket.socket.connect = forbidden
    os.environ.pop("ANDY_LAB_CONFIG", None)
    try:
        load_app()
    except KeyError:
        pass
    else:
        raise AssertionError("MISSING_CONFIG_ACCEPTED")
    with tempfile.TemporaryDirectory() as directory:
        config = Path(directory) / "config.json"
        config.write_text(json.dumps({"environment": "WRONG"}))
        os.environ["ANDY_LAB_CONFIG"] = str(config)
        config.chmod(0o644)
        try:
            load_app()
        except ValueError as error:
            assert str(error) == "PRIVATE_CONFIGURATION_REQUIRED"
        else:
            raise AssertionError("PUBLIC_CONFIG_ACCEPTED")
        config.chmod(0o600)
        try:
            load_app()
        except ValueError as error:
            assert str(error) == "EXPLICIT_EXPERIMENT_REQUIRED"
        else:
            raise AssertionError("WRONG_ENVIRONMENT_ACCEPTED")
    # Disposable smoke secrets satisfy the inherited Settings guards, never a
    # ClientSession or provider credential. No productive environment is imported.
    os.environ["ADMIN_TOKEN"] = secrets.token_urlsafe(32)
    os.environ["INTERNAL_INGRESS_HMAC_SECRET"] = secrets.token_urlsafe(32)
    from .composition import RestrictedGoogleIdentity

    verifier = RestrictedGoogleIdentity(
        audience="synthetic.apps.googleusercontent.com", allowed_subjects=["synthetic"]
    )
    assert type(verifier._verifier).__name__ == "GoogleIdentityVerifier"
    print(json.dumps({"status": "PASS", "python": sys.version.split()[0],
                      "uid": os.getuid(), "imports": "PASS", "missing_config": "REJECTED",
                      "public_config": "REJECTED", "wrong_environment": "REJECTED",
                      "network": "none", "provider_calls": 0}))


if __name__ == "__main__":
    main()
