"""Offline checks for the pinned embedding build artifact manifest."""
import hashlib
import importlib.util
from pathlib import Path


def downloader():
    path = (
        Path(__file__).resolve().parents[1]
        / "ops" / "reconciliation" / "download_model.py"
    )
    spec = importlib.util.spec_from_file_location("pinned_embedding_download", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_model_manifest_is_pinned_and_has_expected_shape():
    m = downloader()
    assert m.MANIFEST["model"] == "intfloat/multilingual-e5-small"
    assert m.MANIFEST["dimensions"] == 384
    assert len(m.MANIFEST["revision"]) == 40
    assert set(m.MANIFEST["files"]) == set(m.SOURCES)
    for item in m.MANIFEST["files"].values():
        assert len(item["sha256"]) == 64 and item["bytes"] > 0


def test_model_hash_verifier_accepts_exact_bytes_only(tmp_path):
    m = downloader()
    path = tmp_path / "synthetic.onnx"
    blob = b"synthetic, no model weights"
    info = {"sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)}
    path.write_bytes(blob)
    assert m.verify(path, info)
    path.write_bytes(blob + b"x")
    assert not m.verify(path, info)
    path.write_bytes(blob)
    assert not m.verify(path, {"sha256": "0" * 64, "bytes": len(blob)})
    path.unlink()
    assert not m.verify(path, info)
