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


def verified_local_image(config_id, manifest_id, sha):
    # Docker's classic store identifies the config blob. Containerd can
    # identify the same verified image by the single-platform OCI manifest.
    for image_id in (config_id, manifest_id):
        try:
            rows = json.loads(subprocess.check_output(
                ["docker", "image", "inspect", image_id], stderr=subprocess.DEVNULL,
            ))
        except subprocess.CalledProcessError:
            continue
        if len(rows) != 1 or rows[0].get("Id") != image_id:
            continue
        image = rows[0]
        if image_id != config_id and (image.get("Descriptor") or {}).get("digest") != manifest_id:
            continue
        cfg = image.get("Config") or {}
        if ((cfg.get("Labels") or {}).get("org.opencontainers.image.revision") == sha
                and cfg.get("Entrypoint") == ["python", "-m", "attention_router.private_context_entrypoint"]):
            return image
    raise ValueError("local OCI provenance mismatch")


def run(operation, comment, image, *, root, archive=None):
    if not re.fullmatch(r"ghcr\.io/escossio/attention-router@sha256:[0-9a-f]{64}", image):
        raise ValueError("immutable repository manifest digest required")
    if archive is None:
        raise ValueError("attested OCI archive required")
    ops = root / "ops/context-canary"
    verified = subprocess.check_output([sys.executable, ops / "verify_github_promotion.py", operation,
                                       str(comment), "--root", root, "--artifact", image, "--archive", archive],
                                       text=True).splitlines()
    if len(verified) != 2:
        raise ValueError("invalid promotion receipt")
    promotion = re.fullmatch(rf"PREFLIGHT_APROVADO {operation} #428 PR #[1-9][0-9]* ([0-9a-f]{{40}})", verified[0])
    config = re.fullmatch(r"OCI_IMAGE_ID=(sha256:[0-9a-f]{64})", verified[1])
    if promotion is None or config is None:
        raise ValueError("invalid promotion receipt")
    actual = verified_local_image(config[1], image.rsplit("@", 1)[1], promotion[1])
    profile, action = PROFILES[operation]
    env = dict(os.environ, CONTEXT_IMAGE=actual["Id"])
    compose = ["docker", "compose", "--project-name", "andy-private-context", "--env-file", os.devnull,
               "-f", ops / "compose.private.yaml", "--profile", profile]
    # Refuse replacement/stopping of an instance running a different authorized digest.
    if operation == "runtime_rollback":
        owner = subprocess.check_output([*compose, "--profile", "owner", "ps", "--status", "running",
                                         "-q", "owner"], env=env, text=True).strip()
        if owner:
            raise ValueError("active OWNER requires approved G6 disable before G5 rollback")
    containers = subprocess.check_output([*compose, "ps", "--all", "-q", profile], env=env, text=True).split()
    for cid in containers:
        container = json.loads(subprocess.check_output(["docker", "inspect", cid]))[0]
        if container["Image"] != actual["Id"]:
            raise ValueError("different running artifact requires its own rollback approval")
    args = ["up", "-d", "--wait", "--no-build", "--pull", "never", "--no-deps"] if action == "up" else ["stop"]
    subprocess.run([*compose, *args, profile], env=env, check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=tuple(PROFILES))
    parser.add_argument("comment_id", type=int)
    parser.add_argument("artifact")
    parser.add_argument("--archive", required=True, type=Path)
    args = parser.parse_args()
    run(args.operation, args.comment_id, args.artifact, root=Path(__file__).resolve().parents[2], archive=args.archive)
