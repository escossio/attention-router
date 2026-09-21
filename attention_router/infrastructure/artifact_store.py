"""Provider-neutral immutable byte storage for Artifact Plane."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from typing import Protocol


ARTIFACT_REF_PREFIX = "sha256:"
LOCAL_ARTIFACT_STORAGE_PROVIDER = "local-fs-v1"
ARTIFACT_DIRECTORY_MODE = 0o750
ARTIFACT_FILE_MODE = 0o640


class ArtifactStoreError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StoredArtifactObject:
    storage_provider: str
    storage_reference: str
    content_sha256: str
    size_bytes: int
class ArtifactObjectStore(Protocol):
    storage_provider: str

    def put_bytes(
        self,
        tenant_id: str,
        data: bytes,
    ) -> StoredArtifactObject: ...

    def read_bytes(
        self,
        tenant_id: str,
        reference: str,
        *,
        expected_sha256: str,
        expected_size: int,
    ) -> bytes: ...


def artifact_ref(content_sha256: str) -> str:
    digest = content_sha256.strip().casefold()
    if len(digest) != 64 or any(
        char not in "0123456789abcdef" for char in digest
    ):
        raise ArtifactStoreError("ARTIFACT_STORE_HASH_INVALID")
    return f"{ARTIFACT_REF_PREFIX}{digest}"


def _tenant_namespace(tenant_id: str) -> str:
    normalized = tenant_id.strip()
    if not normalized or len(normalized) > 64:
        raise ArtifactStoreError("ARTIFACT_STORE_TENANT_INVALID")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
class LocalArtifactStore:
    storage_provider = LOCAL_ARTIFACT_STORAGE_PROVIDER

    def __init__(
        self,
        root: str | Path,
        *,
        max_bytes: int,
    ) -> None:
        if (
            type(max_bytes) is not int
            or max_bytes < 1
            or max_bytes > 1024 * 1024 * 1024
        ):
            raise ArtifactStoreError("ARTIFACT_STORE_MAX_BYTES_INVALID")
        self.root = Path(root)
        self.max_bytes = max_bytes

    def digest_from_ref(self, reference: str) -> str:
        if not isinstance(reference, str):
            raise ArtifactStoreError("ARTIFACT_STORE_REF_INVALID")
        if not reference.startswith(ARTIFACT_REF_PREFIX):
            raise ArtifactStoreError("ARTIFACT_STORE_REF_INVALID")
        digest = reference[len(ARTIFACT_REF_PREFIX) :]
        artifact_ref(digest)
        return digest

    def path_for_ref(
        self,
        tenant_id: str,
        reference: str,
    ) -> Path:
        namespace = _tenant_namespace(tenant_id)
        digest = self.digest_from_ref(reference)
        return (
            self.root
            / namespace[:2]
            / namespace
            / digest[:2]
            / digest
        )
    def _ensure_directory(self, path: Path) -> None:
        path.mkdir(mode=ARTIFACT_DIRECTORY_MODE, exist_ok=True)
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ArtifactStoreError("ARTIFACT_STORE_DIRECTORY_INVALID")
        path.chmod(ARTIFACT_DIRECTORY_MODE)

    def _ensure_parent(
        self,
        tenant_id: str,
        digest: str,
    ) -> Path:
        namespace = _tenant_namespace(tenant_id)
        self.root.mkdir(parents=True, exist_ok=True)
        root_info = self.root.lstat()
        if (
            stat.S_ISLNK(root_info.st_mode)
            or not stat.S_ISDIR(root_info.st_mode)
        ):
            raise ArtifactStoreError("ARTIFACT_STORE_ROOT_INVALID")
        self.root.chmod(ARTIFACT_DIRECTORY_MODE)

        current = self.root
        for component in (
            namespace[:2],
            namespace,
            digest[:2],
        ):
            current = current / component
            self._ensure_directory(current)
        return current

    def _expected_digest(
        self,
        reference: str,
        expected_sha256: str,
    ) -> str:
        digest = self.digest_from_ref(reference)
        expected = artifact_ref(expected_sha256)[
            len(ARTIFACT_REF_PREFIX) :
        ]
        if digest != expected:
            raise ArtifactStoreError(
                "ARTIFACT_STORE_REF_HASH_MISMATCH"
            )
        return digest
    def _read_validated(
        self,
        tenant_id: str,
        reference: str,
        *,
        expected_sha256: str,
        expected_size: int,
        return_bytes: bool,
    ) -> bytes | None:
        if (
            type(expected_size) is not int
            or expected_size < 0
            or expected_size > self.max_bytes
        ):
            raise ArtifactStoreError(
                "ARTIFACT_STORE_SIZE_INVALID"
            )
        digest = self._expected_digest(
            reference,
            expected_sha256,
        )
        path = self.path_for_ref(tenant_id, reference)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags)
        except FileNotFoundError as exc:
            raise ArtifactStoreError(
                "ARTIFACT_STORE_NOT_FOUND"
            ) from exc
        except OSError as exc:
            raise ArtifactStoreError(
                "ARTIFACT_STORE_FILE_INVALID"
            ) from exc

        output = bytearray() if return_bytes else None
        hasher = hashlib.sha256()
        total = 0
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ArtifactStoreError(
                    "ARTIFACT_STORE_FILE_INVALID"
                )
            if info.st_size != expected_size:
                raise ArtifactStoreError(
                    "ARTIFACT_STORE_SIZE_MISMATCH"
                )
            while True:
                chunk = os.read(fd, 64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > self.max_bytes:
                    raise ArtifactStoreError(
                        "ARTIFACT_STORE_TOO_LARGE"
                    )
                hasher.update(chunk)
                if output is not None:
                    output.extend(chunk)
        finally:
            os.close(fd)

        if total != expected_size:
            raise ArtifactStoreError(
                "ARTIFACT_STORE_SIZE_MISMATCH"
            )
        if hasher.hexdigest() != digest:
            raise ArtifactStoreError(
                "ARTIFACT_STORE_HASH_MISMATCH"
            )
        return bytes(output) if output is not None else None

    def validate(
        self,
        tenant_id: str,
        reference: str,
        *,
        expected_sha256: str,
        expected_size: int,
    ) -> Path:
        self._read_validated(
            tenant_id,
            reference,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
            return_bytes=False,
        )
        return self.path_for_ref(tenant_id, reference)
    def put_bytes(
        self,
        tenant_id: str,
        data: bytes,
    ) -> StoredArtifactObject:
        if not isinstance(data, bytes):
            raise ArtifactStoreError(
                "ARTIFACT_STORE_BYTES_REQUIRED"
            )
        if len(data) > self.max_bytes:
            raise ArtifactStoreError(
                "ARTIFACT_STORE_TOO_LARGE"
            )

        digest = hashlib.sha256(data).hexdigest()
        reference = artifact_ref(digest)
        parent = self._ensure_parent(tenant_id, digest)
        target = parent / digest

        if target.exists() or target.is_symlink():
            self.validate(
                tenant_id,
                reference,
                expected_sha256=digest,
                expected_size=len(data),
            )
            return StoredArtifactObject(
                self.storage_provider,
                reference,
                digest,
                len(data),
            )

        fd, raw_temp = tempfile.mkstemp(
            prefix=".artifact-",
            dir=parent,
        )
        temporary = Path(raw_temp)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            temporary.chmod(ARTIFACT_FILE_MODE)
            os.replace(temporary, target)
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
        self.validate(
            tenant_id,
            reference,
            expected_sha256=digest,
            expected_size=len(data),
        )
        return StoredArtifactObject(
            self.storage_provider,
            reference,
            digest,
            len(data),
        )

    def read_bytes(
        self,
        tenant_id: str,
        reference: str,
        *,
        expected_sha256: str,
        expected_size: int,
    ) -> bytes:
        result = self._read_validated(
            tenant_id,
            reference,
            expected_sha256=expected_sha256,
            expected_size=expected_size,
            return_bytes=True,
        )
        assert result is not None
        return result


__all__ = [
    "ARTIFACT_DIRECTORY_MODE",
    "ARTIFACT_FILE_MODE",
    "ARTIFACT_REF_PREFIX",
    "ArtifactObjectStore",
    "ArtifactStoreError",
    "LOCAL_ARTIFACT_STORAGE_PROVIDER",
    "LocalArtifactStore",
    "StoredArtifactObject",
    "artifact_ref",
]
