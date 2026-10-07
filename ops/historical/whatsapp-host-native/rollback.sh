#!/bin/bash
# RETIRED_HISTORICAL: rollback only during the reviewed WhatsApp cutover window.
set -euo pipefail

mode=${1:-}
backup=${2:-}
private_env=${3:-}
if [[ "$mode" != --dry-run && "$mode" != --execute ]]; then
  echo 'usage: rollback.sh --dry-run|--execute PRIVATE_BACKUP_DIR PRIVATE_COMPOSE_ENV' >&2
  exit 2
fi
test -d "$backup/units"
test -f "$backup/transport-status.json"
test -f "$private_env"

repo_root=$(realpath "$(dirname "$0")/../../..")
compose="$repo_root/ops/whatsapp-container/compose.yaml"
test -f "$compose"
units=(
  andy-browser-netns.service
  andy-transport-netns.service
  attention-whatsapp-xvfb.service
  andy-browser-cdp-edge.service
  attention-whatsapp-browser.service
  andy-transport-cdp-proxy.service
  attention-whatsapp-observer.service
  attention-whatsapp-transport.service
)
for unit in "${units[@]}"; do
  test -f "$backup/units/$unit"
  systemctl cat "$unit" >/dev/null
done

if [[ "$mode" == --dry-run ]]; then
  echo 'ROLLBACK_DRY_RUN=PASS'
  echo '1. Stop observer, transport and browser candidate containers.'
  echo '2. Prove candidate Chrome exited and profile has no writer.'
  echo '3. Restore profile snapshot/ownership only when a demonstrated mutation requires it.'
  echo '4. Unmask and start the saved host units in dependency order.'
  printf 'UNIT_ORDER=%s\n' "${units[*]}"
  echo '5. Verify CONNECTED, ready, QR absent and spool/inbound baseline privately.'
  exit 0
fi

if [[ "$EUID" -ne 0 || "${WHATSAPP_CUTOVER_APPROVED:-}" != YES ]]; then
  echo 'execution requires root and WHATSAPP_CUTOVER_APPROVED=YES' >&2
  exit 2
fi

docker compose --env-file "$private_env" -f "$compose" --profile production \
  stop observer transport browser
if [[ $(docker inspect --format '{{.State.Running}}' andy-whatsapp-browser 2>/dev/null || echo false) == true ]]; then
  echo 'browser container still running; rollback stopped' >&2
  exit 1
fi
echo 'Confirm there is no other Chrome writer before continuing.' >&2
if [[ "${WHATSAPP_PROFILE_WRITER_CLEARED:-}" != YES ]]; then
  echo 'set WHATSAPP_PROFILE_WRITER_CLEARED=YES after private process/lock proof' >&2
  exit 2
fi

systemctl unmask "${units[@]}"
for unit in "${units[@]}"; do systemctl start "$unit"; done
echo 'Host units started. Verify CONNECTED, no QR, spool and inbound baseline.'
