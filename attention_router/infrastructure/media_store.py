from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import tempfile


MEDIA_REF_PREFIX = "sha256:"

# Media artifacts are written by the container worker and consumed by the
# host-local WhatsApp transport. Keep them private from other users while
# allowing the shared service group to traverse/read them.
MEDIA_DIRECTORY_MODE = 0o2770
MEDIA_FILE_MODE = 0o640
# Ogg is a container accepted by the current contract, including Ogg/Opus.
# Raw audio/opus remains unsupported until a real provider fixture proves it.
ALLOWED_MIME_TYPES = {"audio/ogg", "audio/mpeg", "audio/mp4"}


class MediaStoreError(ValueError):
    pass


def media_ref(content_sha256: str) -> str:
    digest = content_sha256.casefold()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise MediaStoreError("INVALID_MEDIA_HASH")
    return f"{MEDIA_REF_PREFIX}{digest}"


class MediaStore:
    def __init__(self, root: str | Path, max_bytes: int) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes

    def digest_from_ref(self, reference: str) -> str:
        if not isinstance(reference, str) or not reference.startswith(MEDIA_REF_PREFIX):
            raise MediaStoreError("INVALID_MEDIA_REF")
        digest = reference[len(MEDIA_REF_PREFIX) :]
        media_ref(digest)
        return digest

    def path_for_digest(self, digest: str) -> Path:
        normalized = media_ref(digest)[len(MEDIA_REF_PREFIX) :]
        return self.root / normalized[:2] / normalized

    def path_for_ref(self, reference: str) -> Path:
        return self.path_for_digest(self.digest_from_ref(reference))

    def validate(
        self,
        reference: str,
        *,
        expected_sha256: str,
        expected_size: int,
        mime_type: str,
    ) -> Path:
        if mime_type not in ALLOWED_MIME_TYPES:
            raise MediaStoreError("UNSUPPORTED_MEDIA_MIME")
        if expected_size < 1 or expected_size > self.max_bytes:
            raise MediaStoreError("INVALID_MEDIA_SIZE")
        digest = self.digest_from_ref(reference)
        if digest != expected_sha256:
            raise MediaStoreError("MEDIA_REF_HASH_MISMATCH")
        path = self.path_for_digest(digest)
        try:
            info = path.lstat()
        except FileNotFoundError as exc:
            raise MediaStoreError("MEDIA_NOT_FOUND") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise MediaStoreError("MEDIA_NOT_REGULAR")
        if info.st_size != expected_size:
            raise MediaStoreError("MEDIA_SIZE_MISMATCH")
        hasher = hashlib.sha256()
        total = 0
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(64 * 1024), b""):
                total += len(chunk)
                if total > self.max_bytes:
                    raise MediaStoreError("MEDIA_TOO_LARGE")
                hasher.update(chunk)
        if hasher.hexdigest() != digest:
            raise MediaStoreError("MEDIA_HASH_MISMATCH")
        return path

    def put_bytes(self, data: bytes, *, mime_type: str) -> tuple[str, str, int, Path]:
        if mime_type not in ALLOWED_MIME_TYPES:
            raise MediaStoreError("UNSUPPORTED_MEDIA_MIME")
        if not data or len(data) > self.max_bytes:
            raise MediaStoreError("INVALID_MEDIA_SIZE")
        digest = hashlib.sha256(data).hexdigest()
        target = self.path_for_digest(digest)
        target.parent.mkdir(
            parents=True,
            exist_ok=True,
            mode=MEDIA_DIRECTORY_MODE,
        )
        # mkdir mode is filtered by the process umask. Restore the exact
        # shared-service contract so the host transport can also create
        # artifacts inside an existing worker-created shard.
        target.parent.chmod(MEDIA_DIRECTORY_MODE)
        if target.exists():
            self.validate(media_ref(digest), expected_sha256=digest, expected_size=len(data), mime_type=mime_type)
            return media_ref(digest), digest, len(data), target
        fd, raw_temp = tempfile.mkstemp(prefix=".media-", dir=target.parent)
        temporary = Path(raw_temp)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            temporary.chmod(MEDIA_FILE_MODE)
            os.replace(temporary, target)
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            temporary.unlink(missing_ok=True)
        return media_ref(digest), digest, len(data), target

    def delete(self, reference: str) -> bool:
        path = self.path_for_ref(reference)
        if path.is_symlink():
            raise MediaStoreError("MEDIA_NOT_REGULAR")
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True
