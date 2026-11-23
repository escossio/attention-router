"""Gate one scoped OCI operation against the exact #428 authorization and local image."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


PROFILES = {"runtime_apply": ("dark", "up"), "runtime_rollback": ("dark", "stop"),
            "canary_enable": ("owner", "up"), "canary_disable": ("owner", "stop")}


def run(operation, comment, image, *, root):
    if not re.fullmatch(r"ghcr\.io/escossio/attention-router@sha256:[0-9a-f]{64}", image):
        raise ValueError("immutable repository manifest digest required")
    ops = root / "ops/context-canary"
    verified = subprocess.check_output([sys.executable, ops / "verify_github_promotion.py", operation,
                                       str(comment), "--root", root, "--artifact", image], text=True)
    sha = verified.strip().split()[-1]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("invalid promotion SHA")
    actual = json.loads(subprocess.check_output(["docker", "image", "inspect", image]))
    if (len(actual) != 1 or image not in (actual[0].get("RepoDigests") or [])
            or actual[0]["Config"]["Labels"].get("org.opencontainers.image.revision") != sha
            or actual[0]["Config"]["Entrypoint"] != ["python", "-m", "attention_router.private_context_entrypoint"]):
        raise ValueError("local OCI provenance mismatch")
    profile, action = PROFILES[operation]
    env = dict(os.environ, CONTEXT_IMAGE=image)
    compose = ["docker", "compose", "--project-name", "andy-private-context", "--env-file", os.devnull,
               "-f", ops / "compose.private.yaml", "--profile", profile]
    # Refuse replacement/stopping of an instance running a different authorized digest.
    containers = subprocess.check_output([*compose, "ps", "--all", "-q", profile], env=env, text=True).split()
    for cid in containers:
        container = json.loads(subprocess.check_output(["docker", "inspect", cid]))[0]
        if container["Image"] != actual[0]["Id"]:
            raise ValueError("different running artifact requires its own rollback approval")
    args = ["up", "-d", "--wait", "--no-build", "--pull", "never", "--no-deps"] if action == "up" else ["stop"]
    subprocess.run([*compose, *args, profile], env=env, check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=tuple(PROFILES))
    parser.add_argument("comment_id", type=int)
    parser.add_argument("artifact")
    args = parser.parse_args()
    run(args.operation, args.comment_id, args.artifact, root=Path(__file__).resolve().parents[2])
