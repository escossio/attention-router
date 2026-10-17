"""Opt-in HTTP legacy + restricted HTTPS MAESTRO in the *same* ingress container.

No extra container, route, service identity or database. Safe when TLS is disabled.
The production Compose command must be changed only in a separately authorized rollout.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from uvicorn import Config, Server

from attention_router.config import settings
from attention_router.web.ingress_app import create_ingress_app
from attention_router.web.maestro_relay_ingress import create_maestro_tls_app


@dataclass(frozen=True)
class TLSListener:
    host: str
    port: int
    certificate: str
    private_key: str


def tls_listener_from_env(
    env: Mapping[str, str], *, relay_admission_enabled: bool,
) -> TLSListener | None:
    opt_in = env.get("MAESTRO_HTTPS_LISTENER_ENABLED", "false").lower()
    if opt_in not in {"true", "false"}:
        raise ValueError("MAESTRO_HTTPS_LISTENER_ENABLED must be true or false")
    if opt_in == "false":
        if relay_admission_enabled:
            raise ValueError("MAESTRO admission cannot be enabled without HTTPS listener")
        return None

    host = env.get("MAESTRO_HTTPS_BIND_HOST", "")
    port_text = env.get("MAESTRO_HTTPS_PORT", "")
    certificate = env.get("MAESTRO_HTTPS_CERTFILE", "")
    private_key = env.get("MAESTRO_HTTPS_KEYFILE", "")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("MAESTRO HTTPS listener requires explicit IP bind") from exc
    if (address.version != 4 or address.is_unspecified or address.is_multicast
            or not (address.is_private or address.is_loopback)):
        raise ValueError("MAESTRO HTTPS cannot listen on a public/wildcard IP")
    if not port_text.isascii() or not port_text.isdecimal():
        raise ValueError("MAESTRO HTTPS requires explicit TCP port")
    port = int(port_text)
    if not 1 <= port <= 65535 or port == settings.ingress_http_port:
        raise ValueError("MAESTRO HTTPS port invalid or conflicts with legacy HTTP")
    paths = [Path(certificate), Path(private_key)]
    if any(not path.is_absolute() or not path.is_file() for path in paths):
        raise ValueError("MAESTRO HTTPS requires existing absolute cert/key paths")
    if paths[1].stat().st_mode & 0o077:
        raise ValueError("MAESTRO HTTPS private key must not allow group/other access")
    return TLSListener(str(address), port, certificate, private_key)


class HardenedTLSConfig(Config):
    """Explicit TLS floor even when OpenSSL/Uvicorn defaults differ."""

    def load(self) -> None:
        super().load()
        if self.ssl is not None:
            self.ssl.minimum_version = ssl.TLSVersion.TLSv1_2


def build_servers(listener: TLSListener | None) -> tuple[Server, ...]:
    legacy = Server(Config(
        create_ingress_app(), host=settings.ingress_http_host,
        port=settings.ingress_http_port, lifespan="off",
    ))
    if listener is None:
        return (legacy,)
    secure = Server(HardenedTLSConfig(
        create_maestro_tls_app(), host=listener.host, port=listener.port,
        ssl_certfile=listener.certificate, ssl_keyfile=listener.private_key,
        proxy_headers=False, access_log=False, lifespan="off",
    ))
    return legacy, secure


async def _supervise(servers: tuple[Server, ...]) -> None:
    jobs = [asyncio.create_task(server.serve()) for server in servers]
    done, _pending = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
    for server in servers:
        server.should_exit = True
    completed = await asyncio.gather(*jobs, return_exceptions=True)
    failures = [result for result in completed if isinstance(result, BaseException)]
    if failures:
        raise RuntimeError("ingress listener exited abnormally") from failures[0]
    if not all(server.started for server in servers):
        raise RuntimeError("ingress listener did not start successfully")
    if not done:
        raise RuntimeError("ingress listeners stopped unexpectedly")


def main() -> None:
    listener = tls_listener_from_env(
        os.environ, relay_admission_enabled=settings.maestro_relay_ingress_enabled,
    )
    asyncio.run(_supervise(build_servers(listener)))


if __name__ == "__main__":
    main()
