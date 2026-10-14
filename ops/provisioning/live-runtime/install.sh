#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "install.sh must run as root" >&2
  exit 1
fi

SOURCE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ENV_FILE=${ATTENTION_LIVE_RUNTIME_ENV_FILE:-/etc/default/attention-router-live-runtime}

if [ ! -f "$ENV_FILE" ]; then
  echo "missing host-private runtime config: $ENV_FILE" >&2
  exit 1
fi

install -m 0755 "$SOURCE_DIR/reconcile.py" \
  /usr/local/sbin/attention-router-live-runtime-reconcile
install -m 0644 "$SOURCE_DIR/worker-production-execution.compose.yaml" \
  /etc/attention-router/worker-production-execution.compose.yaml
install -m 0644 "$SOURCE_DIR/attention-router-live-runtime.service" \
  /etc/systemd/system/attention-router-live-runtime.service

systemctl daemon-reload
systemctl enable --now attention-router-live-runtime.service
systemctl is-enabled --quiet attention-router-live-runtime.service
systemctl is-active --quiet attention-router-live-runtime.service

set -a
. "$ENV_FILE"
set +a
/usr/local/sbin/attention-router-live-runtime-reconcile --check
