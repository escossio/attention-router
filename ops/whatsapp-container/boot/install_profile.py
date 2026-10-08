#!/usr/bin/env python3
"""Install and reload the versioned Browser AppArmor profile without unloading it."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def install_profile(source: Path, destination: Path, *, load: bool = True, owner: int = 0) -> str:
    content = source.read_bytes()
    expected = hashlib.sha256(content).hexdigest()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.is_file() or destination.read_bytes() != content:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.andy-whatsapp-browser.', delete=False) as temp:
            temporary = Path(temp.name)
            temp.write(content)
        try:
            os.chown(temporary, owner, owner)
            os.chmod(temporary, 0o644)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    os.chown(destination, owner, owner)
    os.chmod(destination, 0o644)
    if hashlib.sha256(destination.read_bytes()).hexdigest() != expected:
        raise RuntimeError("APPARMOR_PROFILE_CHECKSUM_MISMATCH")
    if load:
        subprocess.run(['apparmor_parser', '-Kr', str(destination)], check=True)
        status = json.loads(subprocess.run(['aa-status', '--json'], capture_output=True, text=True, check=True).stdout)
        if status.get('profiles', {}).get('andy-whatsapp-browser') != 'enforce':
            raise RuntimeError('APPARMOR_PROFILE_NOT_LOADED')
    return expected


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('usage: install_profile.py SOURCE DESTINATION')
    print(f'APPARMOR_SHA256={install_profile(Path(sys.argv[1]), Path(sys.argv[2]))}')
