#!/usr/bin/env python3
"""Render the Grafana-only Apache vhost; never read authentication secrets."""

import argparse
from pathlib import Path
import re


def render(server_name: str, auth_user_file: str, upstream_port: int) -> str:
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", server_name):
        raise ValueError("invalid server name")
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", auth_user_file):
        raise ValueError("invalid absolute authentication file path")
    if not 1 <= upstream_port <= 65535:
        raise ValueError("invalid upstream port")
    content = Path(__file__).with_name("apache-site.conf.template").read_text()
    for key, value in {
        "SERVER_NAME": server_name,
        "AUTH_USER_FILE": auth_user_file,
        "UPSTREAM_PORT": str(upstream_port),
    }.items():
        content = content.replace(f"@{key}@", value)
    return content


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-name", required=True)
    parser.add_argument("--auth-user-file", required=True)
    parser.add_argument("--upstream-port", required=True, type=int)
    args = parser.parse_args()
    print(render(args.server_name, args.auth_user_file, args.upstream_port), end="")
