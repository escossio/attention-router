from __future__ import annotations

import os
import stat

from attention_router.infrastructure.media_store import MediaStore


def test_media_store_creates_shared_group_readable_artifacts(tmp_path):
    root = tmp_path / "media"
    root.mkdir(mode=0o2770)
    root.chmod(0o2770)

    store = MediaStore(root, 1024)

    _reference, _digest, size, path = store.put_bytes(
        b"andy voice artifact",
        mime_type="audio/mpeg",
    )

    assert size > 0

    directory_mode = stat.S_IMODE(path.parent.stat().st_mode)
    file_mode = stat.S_IMODE(path.stat().st_mode)

    assert directory_mode == 0o2770
    assert file_mode == 0o640

    # A setgid shard is the group-inheritance boundary for files created
    # later by the worker.
    assert path.parent.stat().st_mode & stat.S_ISGID
    assert directory_mode & stat.S_IWGRP
    assert path.stat().st_gid == path.parent.stat().st_gid


def test_media_store_round_trip_preserves_integrity(tmp_path):
    root = tmp_path / "media"
    root.mkdir(mode=0o2770)
    root.chmod(0o2770)

    data = b"andy voice artifact integrity"

    store = MediaStore(root, 1024)

    reference, digest, size, path = store.put_bytes(
        data,
        mime_type="audio/mpeg",
    )

    validated = store.validate(
        reference,
        expected_sha256=digest,
        expected_size=size,
        mime_type="audio/mpeg",
    )

    assert validated == path
    assert path.read_bytes() == data


def test_media_store_restores_shared_directory_mode_after_umask(tmp_path):
    root = tmp_path / "media"
    root.mkdir(mode=0o2770)
    root.chmod(0o2770)

    store = MediaStore(root, 1024)

    previous_umask = os.umask(0o027)
    try:
        _reference, _digest, _size, path = store.put_bytes(
            b"andy voice artifact under restrictive umask",
            mime_type="audio/mpeg",
        )
    finally:
        os.umask(previous_umask)

    directory_mode = stat.S_IMODE(path.parent.stat().st_mode)
    file_mode = stat.S_IMODE(path.stat().st_mode)

    assert directory_mode == 0o2770
    assert directory_mode & stat.S_ISGID
    assert directory_mode & stat.S_IWGRP
    assert file_mode == 0o640
