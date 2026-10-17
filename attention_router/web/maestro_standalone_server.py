"""Dedicated MAESTRO receipt listener; never starts the legacy HTTP ingress."""
from __future__ import annotations

import os
import ssl
import sys
from pathlib import Path
from urllib.request import ProxyHandler, HTTPSHandler, build_opener


def _load_database_url() -> None:
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    path = Path(os.environ["MAESTRO_DATABASE_URL_FILE"])
    if not path.is_absolute() or not path.is_file():
        raise ValueError("MAESTRO database URL file is required")
    value = path.read_text(encoding="utf-8").strip()
    try:
        driver = make_url(value).drivername
    except ArgumentError:
        raise ValueError("MAESTRO database URL invalid") from None
    if driver != "postgresql+psycopg":
        raise ValueError("MAESTRO requires the existing PostgreSQL driver")
    os.environ["DATABASE_URL"] = value


def _listener():
    from attention_router.config import settings
    from attention_router.web.dual_ingress_server import tls_listener_from_env

    if not settings.maestro_relay_ingress_enabled:
        raise ValueError("MAESTRO relay admission must be explicitly enabled")
    listener = tls_listener_from_env(
        os.environ, relay_admission_enabled=True,
    )
    if listener is None:
        raise ValueError("MAESTRO TLS listener is required")
    return listener


def health() -> None:
    listener = _listener()
    context = ssl.create_default_context(cafile=listener.certificate)
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context))
    with opener.open(
        f"https://{listener.host}:{listener.port}/health/live", timeout=3,
    ) as response:
        if response.status != 200:
            raise RuntimeError("MAESTRO health failed")


def main() -> None:
    _load_database_url()
    if sys.argv[1:] == ["--health"]:
        health()
        return
    if sys.argv[1:]:
        raise ValueError("unsupported MAESTRO argument")
    listener = _listener()
    from attention_router.web.dual_ingress_server import HardenedTLSConfig
    from attention_router.web.maestro_relay_ingress import create_maestro_tls_app
    from uvicorn import Server

    Server(HardenedTLSConfig(
        create_maestro_tls_app(), host=listener.host, port=listener.port,
        ssl_certfile=listener.certificate, ssl_keyfile=listener.private_key,
        proxy_headers=False, access_log=False, lifespan="off",
    )).run()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError):
        raise SystemExit("MAESTRO_CONFIG_INVALID") from None
