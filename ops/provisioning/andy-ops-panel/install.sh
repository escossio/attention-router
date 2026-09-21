#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${ANDY_OPS_INSTALL_DIR:-/srv/andy-ops-panel}"
SERVICE_PATH="/etc/systemd/system/andy-ops-panel.service"
ENV_PATH="/etc/default/andy-ops-panel"

[[ "$(id -u)" -eq 0 ]] || {
  echo "andy-ops-panel install requires root/operator privileges" >&2
  exit 1
}

install -d -m 0755 "$INSTALL_DIR"
install -m 0755 "$ROOT_DIR/server.py" "$INSTALL_DIR/server.py"
install -m 0644 "$ROOT_DIR/index.html" "$INSTALL_DIR/index.html"
install -m 0644 "$ROOT_DIR/app.js" "$INSTALL_DIR/app.js"
install -m 0644 "$ROOT_DIR/styles.css" "$INSTALL_DIR/styles.css"
install -m 0644 "$ROOT_DIR/README.md" "$INSTALL_DIR/README.md"
install -m 0644 "$ROOT_DIR/andy-ops-panel.service" "$SERVICE_PATH"

if [[ ! -e "$ENV_PATH" ]]; then
  install -m 0600 "$ROOT_DIR/andy-ops-panel.env.example" "$ENV_PATH"
  echo "Created $ENV_PATH from the safe example; set the private LAN bind before remote use."
fi

python3 -m py_compile "$INSTALL_DIR/server.py"
node --check "$INSTALL_DIR/app.js" >/dev/null

systemctl daemon-reload
systemctl enable --now andy-ops-panel.service

echo "ANDY_OPS_PANEL_INSTALL=PASS"
echo "INSTALL_DIR=$INSTALL_DIR"
echo "SERVICE=andy-ops-panel.service"
