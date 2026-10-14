#!/usr/bin/env bash
set -Eeuo pipefail
[[ $(id -u) == 0 ]] || { echo 'BOOT_ROLLBACK=FAIL reason=root_required' >&2; exit 1; }
# ExecStop sees this marker and leaves healthy containers untouched.
install -o root -g root -m 0600 /dev/null /run/andy-whatsapp-container-runtime.skip-stop
systemctl disable --now andy-whatsapp-container-runtime.service
for name in andy-whatsapp-browser andy-whatsapp-transport andy-whatsapp-observer; do
  if docker inspect "$name" >/dev/null 2>&1; then
    docker update --restart=unless-stopped "$name" >/dev/null
  fi
done
# AppArmor stays installed: the current Browser still requires this profile.
echo 'BOOT_ROLLBACK=PASS'
