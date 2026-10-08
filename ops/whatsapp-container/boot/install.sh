#!/usr/bin/env bash
set -Eeuo pipefail
source_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
install_dir=/usr/local/lib/andy-whatsapp-container-runtime
profile_path=/etc/apparmor.d/andy-whatsapp-browser
unit_path=/etc/systemd/system/andy-whatsapp-container-runtime.service
[[ $(id -u) == 0 ]] || { echo 'BOOT_INSTALL=FAIL reason=root_required' >&2; exit 1; }
install -d -o root -g root -m 0755 "$install_dir"
install -o root -g root -m 0644 "$source_dir/andy-whatsapp-browser.apparmor" "$install_dir/andy-whatsapp-browser.apparmor"
install -o root -g root -m 0644 "$source_dir/compose.yaml" "$install_dir/compose.yaml"
install -o root -g root -m 0755 "$source_dir/boot/bootstrap.py" "$install_dir/bootstrap.py"
install -o root -g root -m 0755 "$source_dir/boot/install_profile.py" "$install_dir/install_profile.py"
install -o root -g root -m 0644 "$source_dir/boot/andy-whatsapp-container-runtime.service" "$unit_path"
python3 "$install_dir/install_profile.py" "$source_dir/andy-whatsapp-browser.apparmor" "$profile_path"
cmp "$source_dir/andy-whatsapp-browser.apparmor" "$profile_path"
sha256sum "$source_dir/andy-whatsapp-browser.apparmor" "$profile_path" "$install_dir/andy-whatsapp-browser.apparmor"
systemctl daemon-reload
echo 'BOOT_INSTALL=PASS'
