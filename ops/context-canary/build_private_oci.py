"""Build the local OCI archive from a Git object and immutable dependency image."""
import argparse
from pathlib import Path
import re
import subprocess
import tempfile


def build(*, root, sha, base, output):
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("full commit SHA required")
    if not re.fullmatch(r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}", base):
        raise ValueError("immutable dependency image required")
    if output.exists() or "," in str(output):
        raise ValueError("new OCI output path required")
    commit = subprocess.check_output(["git", "-C", root, "rev-parse", "--verify", sha + "^{commit}"], text=True).strip()
    if commit != sha:
        raise ValueError("commit required")
    stamp = subprocess.check_output(["git", "-C", root, "show", "-s", "--format=%ct", sha], text=True).strip()
    with tempfile.TemporaryDirectory(dir=output.parent) as temp:
        archive = Path(temp) / "source.tar"
        image = Path(temp) / "image.tar"
        subprocess.run(["git", "-C", root, "archive", "--format=tar", "-o", archive, sha,
                        "attention_router", "ops/reconciliation/model-manifest.json",
                        "ops/context-canary/Dockerfile.private", "pyproject.toml"], check=True)
        with archive.open("rb") as source:
            subprocess.run(["docker", "buildx", "build", "--platform", "linux/amd64", "--network=none",
                            "--provenance=false", "--sbom=false", "-f", "ops/context-canary/Dockerfile.private",
                            "--build-arg", "BUILD_SHA=" + sha, "--build-arg", "VECTOR_BASE=" + base,
                            "--build-arg", "SOURCE_DATE_EPOCH=" + stamp,
                            "--output", f"type=oci,rewrite-timestamp=true,dest={image}", "-"], stdin=source, check=True)
        # Do not replace an existing operator artifact when concurrent builds race.
        output.hardlink_to(image)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sha")
    parser.add_argument("base")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(root=Path(__file__).resolve().parents[2], sha=args.sha, base=args.base, output=args.output.absolute())
