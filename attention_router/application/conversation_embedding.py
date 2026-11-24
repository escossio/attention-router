"""Offline, pinned local provider. Never downloads a model or transmits text."""

import hashlib
import json
import os
from pathlib import Path
import threading

from attention_router.observability.context_performance import Stage, inference_lock, phase

MODEL = "intfloat/multilingual-e5-small"
REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3:onnx-qint8-v1"
DIMENSIONS = 384


class LocalEmbeddingProvider:
    model = MODEL
    revision = REVISION
    dimensions = DIMENSIONS

    def __init__(self, root=None):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        root = Path(
            root or os.getenv("CONVERSATION_VECTOR_MODEL_ROOT", "/opt/andy-embedding-model")
        )
        manifest = json.loads((root / "manifest.json").read_text())
        if manifest["model"] != MODEL or manifest["revision"] != REVISION.split(":")[0]:
            raise ValueError("MODEL_VERSION_MISMATCH")
        for name, spec in manifest["files"].items():
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != spec["sha256"]:
                raise ValueError("MODEL_HASH_MISMATCH")
        self.tokenizer = Tokenizer.from_file(str(root / "tokenizer.json"))
        self.tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.log_severity_level = 3
        self.session = ort.InferenceSession(
            str(root / "model.onnx"), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self.lock = threading.RLock()

    def count(self, value):
        with self.lock:
            return len(self.tokenizer.encode(value, add_special_tokens=False).ids)

    def fragments(self, value, size=350):
        with self.lock:
            offsets = self.tokenizer.encode(value, add_special_tokens=False).offsets
        return [
            (offsets[i][0], offsets[min(i + size, len(offsets)) - 1][1])
            for i in range(0, len(offsets), size)
        ]

    def encode(self, texts, *, query=False):
        import numpy as np

        if not texts or len(texts) > 8:
            raise ValueError("EMBEDDING_BATCH_INVALID")
        with inference_lock(self.lock):
            batch = self.tokenizer.encode_batch(
                [("query: " if query else "passage: ") + t for t in texts]
            )
            if any(len(e.ids) > 512 for e in batch):
                raise ValueError("EMBEDDING_TEXT_TOO_LONG")
            inputs = {
                "input_ids": np.array([e.ids for e in batch], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in batch], dtype=np.int64),
                "token_type_ids": np.array([e.type_ids for e in batch], dtype=np.int64),
            }
            with phase(Stage.INFERENCE):
                outputs = self.session.run(
                    None, {i.name: inputs[i.name] for i in self.session.get_inputs()}
                )[0]
            mask = inputs["attention_mask"][..., None]
            vectors = (
                (outputs * mask).sum(axis=1) / mask.sum(axis=1) if outputs.ndim == 3 else outputs
            )
            norms = np.linalg.norm(vectors, axis=1, keepdims=True)
            if (
                vectors.shape != (len(texts), DIMENSIONS)
                or not np.isfinite(vectors).all()
                or (norms <= 0).any()
            ):
                raise ValueError("EMBEDDING_OUTPUT_INVALID")
            return (vectors / norms).astype(np.float32).tolist()


_provider = None
_provider_lock = threading.Lock()


def get_provider():
    global _provider
    with _provider_lock:
        if _provider is None:
            _provider = LocalEmbeddingProvider()
        return _provider
