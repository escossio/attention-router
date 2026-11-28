"""Explicit offline-runtime preparation. No production resources or credentials.

Run after exact-SHA validation and operational authorization. Dependencies are
downloaded on the host only; no IP connectivity is added to either container.
"""

import argparse
import pathlib
import secrets
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent


def dependencies():
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--require-hashes",
            "--only-binary=:all:",
            "--platform",
            "manylinux2014_x86_64",
            "--python-version",
            "313",
            "--implementation",
            "cp",
            "--abi",
            "cp313",
            "--dest",
            str(ROOT / "vendor"),
            "-r",
            str(ROOT / "requirements.lock"),
        ],
        check=True,
    )


def credentials():
    folder = ROOT / "secrets"
    folder.mkdir(mode=0o700, exist_ok=True)
    for name in ("admin", "reader", "writer"):
        path = folder / name
        # Never rotate or overwrite an existing credential implicitly.
        if not path.exists():
            with path.open("x") as stream:
                stream.write(secrets.token_urlsafe(36))
            path.chmod(0o400 if name == "admin" else 0o444)


def initialize():
    # Dedicated container name only. Password is read inside it, never in argv.
    def sql(statement):
        command = [
            "docker",
            "exec",
            "-i",
            "andy-memory-lab-db",
            "sh",
            "-c",
            'export PGPASSWORD="$(cat /run/secrets/admin)"; '
            "exec psql -h /var/run/postgresql -U postgres -d memory_lab -v ON_ERROR_STOP=1",
        ]
        result = subprocess.run(command, input=statement, text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError(
                "LAB_INITIALIZATION_REFUSED: output suppressed; do not retry blindly"
            )

    roles = []
    for name in ("reader", "writer"):
        value = (ROOT / "secrets" / name).read_text()
        if not value or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for c in value
        ):
            raise ValueError("INVALID_EXPERIMENTAL_CREDENTIAL")
        roles.append("CREATE ROLE lab_" + name + " LOGIN PASSWORD '" + value + "';")
    sql(
        "BEGIN; " + "".join(roles) + "REVOKE ALL ON DATABASE memory_lab FROM PUBLIC; "
        "REVOKE CREATE ON SCHEMA public FROM PUBLIC; "
        + (ROOT / "schema.sql").read_text()
        + " COMMIT;"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=["dependencies", "credentials", "initialize"])
    args = parser.parse_args()
    {"dependencies": dependencies, "credentials": credentials, "initialize": initialize}[
        args.step
    ]()
