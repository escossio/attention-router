"""Fetch hash-locked wheels or build a local private OCI archive from an exact Git commit."""

import argparse
import contextlib
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

OPS = "ops/context-canary"
LOCK = f"{OPS}/requirements.linux-amd64.lock"
BASE = f"{OPS}/python-base.txt"


def git(root, *args):
    return subprocess.check_output(["git", "-C", root, *args], text=True).strip()


def pinned_base(root, sha):
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or git(root, "rev-parse", "--verify", sha + "^{commit}") != sha:
        raise ValueError("full commit SHA required")
    base = git(root, "show", f"{sha}:{BASE}")
    if not re.fullmatch(r"docker\.io/library/python@sha256:[0-9a-f]{64}", base):
        raise ValueError("immutable official Python base required")
    return base


def fetch_wheels(*, root, sha, wheelhouse):
    """Download only binary Python distributions explicitly sealed by the reviewed lock."""
    base = pinned_base(root, sha)
    wheelhouse.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        lock = Path(temp) / "requirements.lock"
        lock.write_text(git(root, "show", f"{sha}:{LOCK}") + "\n")
        subprocess.run(["docker", "run", "--rm", "--platform", "linux/amd64",
                        "--user", f"{os.getuid()}:{os.getgid()}", "--cap-drop=ALL",
                        "--read-only", "--security-opt=no-new-privileges", "--tmpfs", "/tmp:size=256m",
                        "-v", f"{lock}:/requirements.lock:ro", "-v", f"{wheelhouse.resolve()}:/wheels",
                        base, "python", "-m", "pip", "--isolated", "download", "--disable-pip-version-check",
                        "--no-cache-dir", "--only-binary=:all:", "--no-deps", "--require-hashes",
                        "--index-url=https://pypi.org/simple", "-r", "/requirements.lock", "-d", "/wheels"],
                       check=True)


def inspect_image(archive, sha):
    with tarfile.open(archive) as tar:
        def read(name):
            member = tar.getmember(name)
            if not member.isreg():
                raise ValueError("invalid OCI member")
            return tar.extractfile(member)

        def blob(descriptor, *, metadata=False):
            match = re.fullmatch(r"sha256:([0-9a-f]{64})", descriptor["digest"])
            if not match:
                raise ValueError("invalid OCI digest")
            path = "blobs/sha256/" + match[1]
            if tar.getmember(path).size != descriptor["size"]:
                raise ValueError("incorrect OCI blob size")
            with contextlib.closing(read(path)) as stream:
                digest = sha256()
                payload = bytearray()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                    if metadata:
                        payload.extend(chunk)
                if digest.hexdigest() != match[1]:
                    raise ValueError("OCI digest mismatch")
                if metadata:
                    return json.loads(payload)

        with contextlib.closing(read("index.json")) as index:
            descriptor, = json.load(index)["manifests"]
        manifest = blob(descriptor, metadata=True)
        image = blob(manifest["config"], metadata=True)
        if not (image["os"] == "linux" and image["architecture"] == "amd64"
                and image["config"]["Labels"]["org.opencontainers.image.revision"] == sha
                and image["config"]["Entrypoint"] == ["python", "-m", "attention_router.private_context_entrypoint"]
                and image["config"]["User"] == "65532:65532"
                and "STAGED_CONTEXT_PRIVATE_CANARY_ENABLED=false" in image["config"]["Env"]):
            raise ValueError("unexpected private runtime image")
        for layer in manifest["layers"]:
            blob(layer)
        return descriptor["digest"]


def build(*, root, sha, wheelhouse, output, no_cache=False):
    base = pinned_base(root, sha)
    if output.exists() or "," in str(output) or not wheelhouse.is_dir():
        raise ValueError("existing wheelhouse and new OCI output path required")
    stamp = git(root, "show", "-s", "--format=%ct", sha)
    with tempfile.TemporaryDirectory(dir=output.parent) as temp:
        source = Path(temp) / "source.tar"
        image = Path(temp) / "image.tar"
        subprocess.run(["git", "-C", root, "archive", "-o", source, sha, "attention_router",
                        "ops/reconciliation/model-manifest.json", f"{OPS}/Dockerfile.private", LOCK],
                       check=True)
        with source.open("rb") as stream:
            subprocess.run(["docker", "buildx", "build", "--platform", "linux/amd64", "--network=none",
                            "--provenance=false", "--sbom=false", *(["--no-cache"] if no_cache else []),
                            "-f", f"{OPS}/Dockerfile.private",
                            "--build-context", f"wheels={wheelhouse.resolve()}", "--tag", "andy-private-review:" + sha,
                            "--build-arg", "BUILD_SHA=" + sha, "--build-arg", "PYTHON_BASE=" + base,
                            "--build-arg", "SOURCE_DATE_EPOCH=" + stamp,
                            "--output", f"type=oci,rewrite-timestamp=true,dest={image}", "-"],
                           stdin=stream, check=True)
        digest = inspect_image(image, sha)
        output.hardlink_to(image)  # Fail instead of overwriting another build.
        return digest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("fetch-wheels", "build"):
        command = commands.add_parser(action)
        command.add_argument("sha")
        command.add_argument("wheelhouse", type=Path)
        if action == "build":
            command.add_argument("output", type=Path)
            command.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if args.action == "fetch-wheels":
        fetch_wheels(root=root, sha=args.sha, wheelhouse=args.wheelhouse)
    else:
        print(build(root=root, sha=args.sha, wheelhouse=args.wheelhouse, output=args.output.absolute(),
                    no_cache=args.no_cache))
