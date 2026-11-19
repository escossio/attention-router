"""Download the public, exact-revision embedding model after SHA-256 verification.

This is a build-time tool. Never runs in a production service.
It does not read WhatsApp data, API keys, or application credentials.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent
MANIFEST = json.loads((ROOT / "model-manifest.json").read_text())
REVISION = MANIFEST["revision"]
assert MANIFEST["model"] == "intfloat/multilingual-e5-small"
assert len(REVISION) == 40 and all(c in "0123456789abcdef" for c in REVISION)
SOURCES = {
    "model.onnx": "onnx/model_qint8_avx512_vnni.onnx",
    "tokenizer.json": "tokenizer.json",
    "config.json": "config.json",
    "tokenizer_config.json": "tokenizer_config.json",
}


def verify(path: Path, expected: dict) -> bool:
    if not path.is_file() or path.is_symlink() or path.stat().st_size != expected["bytes"]:
        return False
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest() == expected["sha256"]


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python download_model.py <local-build-model-directory>")
    dest = Path(sys.argv[1]).resolve()
    if dest == ROOT or ROOT in dest.parents:
        if dest != ROOT / "model":
            raise SystemExit("Only ops/reconciliation/model may reside within the repository.")
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    if dest.is_symlink():
        raise SystemExit("model destination cannot be a symlink")
    for name, source in SOURCES.items():
        expected = MANIFEST["files"][name]
        path = dest / name
        if verify(path, expected):
            print(name, "verified existing")
            continue
        url = f"https://huggingface.co/intfloat/multilingual-e5-small/resolve/{REVISION}/{source}"
        with tempfile.NamedTemporaryFile(dir=dest, prefix=".fetch-", delete=False) as tmp:
            temporary = Path(tmp.name)
            try:
                with urllib.request.urlopen(url, timeout=60) as response:
                    while chunk := response.read(1024 * 1024):
                        tmp.write(chunk)
                        if tmp.tell() > expected["bytes"]:
                            raise ValueError("MODEL_BYTE_LIMIT_EXCEEDED")
                tmp.flush()
                os.fsync(tmp.fileno())
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        if not verify(temporary, expected):
            temporary.unlink(missing_ok=True)
            raise ValueError(f"MODEL_SHA256_MISMATCH:{name}")
        os.replace(temporary, path)
        print(name, "downloaded and verified")
    (dest / "manifest.json").write_text(json.dumps(MANIFEST, indent=2) + "\n")
    print("LOCAL_MODEL_COMPLETE; no transcripts or user data processed")


if __name__ == "__main__":
    main()
