#!/bin/bash
set -euo pipefail
# Reuse the existing environment-restricted SSH key; no GHCR or DB credentials.
: "${PREFLIGHT_SSH_KEY:?Missing PREFLIGHT_SSH_KEY}"
: "${PREFLIGHT_KNOWN_HOSTS:?Missing PREFLIGHT_KNOWN_HOSTS}"
: "${PREFLIGHT_TARGET:?Missing PREFLIGHT_TARGET}"
: "${PREFLIGHT_ENVIRONMENT:?Missing PREFLIGHT_ENVIRONMENT}"
private_dir=$(mktemp -d)
trap 'rm -rf "$private_dir"' EXIT
chmod 700 "$private_dir"
printf '%s\n' "$PREFLIGHT_SSH_KEY" > "$private_dir/key"
printf '%s\n' "$PREFLIGHT_KNOWN_HOSTS" > "$private_dir/known_hosts"
chmod 600 "$private_dir/key"
export RELAY_VERIFY_SSH_KEY="$private_dir/key"
export RELAY_VERIFY_KNOWN_HOSTS="$private_dir/known_hosts"
python3 "$(dirname "$0")/coolify_verify.py" "$@"
