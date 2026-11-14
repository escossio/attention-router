"""Reproduce a narrowly scoped, SHA-pinned host WhatsApp bootstrap auth repair.

The host-native bootstrap was installed outside the public repository. This
offline utility never imports that host file or invokes Docker. It requires
the exact known source hash and emits a candidate at a new destination only.
Deployment and service activation remain separately authorized operations.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import stat

SOURCE_SHA256 = "97fb2f441f56713041f718e7a473d2f042223e94413d16c1cd65499330f1e1b8"
RESULT_SHA256 = "bd77cc8253ecca7b7bf631adf4ddbb135745e431468587d5204a67283a204878"

SOURCE_ANCHOR = """                require(value.get('appState') == 'CONNECTED' and value.get('nativeState') == 'CONNECTED',
                        'BROWSER_AUTH_NOT_CONNECTED')"""

REPAIRED_ANCHOR = """                # AuthStore/AppState is no longer exposed by some WhatsApp Web
                # builds. Socket CONNECTED is necessary but not sufficient:
                # transport_gate() still independently requires authenticated
                # identity, CONNECTED transport, and no QR before convergence.
                require(
                    isinstance(value, dict)
                    and value.get('nativeState') == 'CONNECTED'
                    and value.get('appState') in (None, 'CONNECTED'),
                    'BROWSER_AUTH_NOT_CONNECTED',
                )"""


class BootstrapPatchRejected(ValueError):
    """Fail closed when the source, anchor or resulting bytes drift."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def browser_auth_state_eligible(value: object) -> bool:
    """Socket is connected and AppState, if exposed, is explicitly connected.

    This predicate is only an intermediate browser gate. Independent Transport
    authentication, identity matching, QR absence and delivery policy remain
    mandatory before the bootstrap can report overall convergence.
    """
    return (
        isinstance(value, dict)
        and value.get("nativeState") == "CONNECTED"
        and value.get("appState") in (None, "CONNECTED")
    )


def patch_bytes(
    original: bytes,
    *,
    source_sha256: str = SOURCE_SHA256,
    result_sha256: str = RESULT_SHA256,
) -> bytes:
    if _sha256(original) != source_sha256:
        raise BootstrapPatchRejected("SOURCE_SHA256_MISMATCH")
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BootstrapPatchRejected("SOURCE_ENCODING_INVALID") from exc
    if text.count(SOURCE_ANCHOR) != 1 or REPAIRED_ANCHOR in text:
        raise BootstrapPatchRejected("SOURCE_ANCHOR_MISMATCH")
    result = text.replace(SOURCE_ANCHOR, REPAIRED_ANCHOR, 1).encode()
    if _sha256(result) != result_sha256:
        raise BootstrapPatchRejected("RESULT_SHA256_MISMATCH")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Known original host bootstrap")
    parser.add_argument("--output", type=Path, help="New candidate path; existing files are never overwritten")
    args = parser.parse_args()
    try:
        info = args.input.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
            raise BootstrapPatchRejected("SOURCE_PERMISSIONS_INVALID")
        candidate = patch_bytes(args.input.read_bytes())
        if args.output is not None:
            # Creating only a new path protects the installed unit and backup.
            mode = stat.S_IMODE(info.st_mode)
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
            try:
                with os.fdopen(fd, "wb") as dst:
                    dst.write(candidate)
                    dst.flush()
                    os.fsync(dst.fileno())
                    os.fchmod(dst.fileno(), mode)
            except BaseException:
                args.output.unlink(missing_ok=True)
                raise
    except (OSError, BootstrapPatchRejected) as exc:
        parser.exit(status=1, message=f"BOOTSTRAP_PATCH_REJECTED:{type(exc).__name__}\n")
    print("BOOTSTRAP_PATCH_VERIFIED " + _sha256(candidate))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
