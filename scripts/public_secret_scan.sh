#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMP="$(mktemp -d)"
trap 'rm -rf "$TEMP"' EXIT
VERSION=8.30.1
FILE="gitleaks_${VERSION}_linux_x64.tar.gz"
SHA256=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
curl --fail --silent --show-error --location \
  "https://github.com/gitleaks/gitleaks/releases/download/v${VERSION}/${FILE}" \
  --output "$TEMP/$FILE"
printf '%s  %s\n' "$SHA256" "$TEMP/$FILE" | sha256sum --check --status
tar -xzf "$TEMP/$FILE" -C "$TEMP" gitleaks
# HEAD limits scanning to this sanitized ancestry, even with other refs fetched.
# Redaction is mandatory: CI must never reproduce a secret in a finding.
"$TEMP/gitleaks" git "$ROOT" --log-opts=HEAD --redact --no-banner \
  --gitleaks-ignore-path "$ROOT/.gitleaksignore"
