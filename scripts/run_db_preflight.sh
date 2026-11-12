#!/bin/bash
set -euo pipefail
# All inputs come from the environment; credentials never appear in argv/logs.
: "${PREFLIGHT_SSH_KEY:?Missing PREFLIGHT_SSH_KEY}"
: "${PREFLIGHT_KNOWN_HOSTS:?Missing PREFLIGHT_KNOWN_HOSTS}"
: "${PREFLIGHT_TARGET:?Missing PREFLIGHT_TARGET}"
: "${PREFLIGHT_ENVIRONMENT:?Missing PREFLIGHT_ENVIRONMENT}"
: "${IMAGE_TAG:?Missing IMAGE_TAG}"
: "${GH_TOKEN:?Missing GH_TOKEN}"
: "${GITHUB_ACTOR:?Missing GITHUB_ACTOR}"
private_dir=$(mktemp -d)
trap 'rm -rf "$private_dir"' EXIT
chmod 700 "$private_dir"
printf '%s\n' "$PREFLIGHT_SSH_KEY" > "$private_dir/key"
printf '%s\n' "$PREFLIGHT_KNOWN_HOSTS" > "$private_dir/known_hosts"
chmod 600 "$private_dir/key"
python3 - <<'PY' | ssh -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=4 \
  -o "UserKnownHostsFile=$private_dir/known_hosts" -i "$private_dir/key" \
  "$PREFLIGHT_TARGET" "$PREFLIGHT_ENVIRONMENT $IMAGE_TAG"
import json, os
print(json.dumps({"username": os.environ["GITHUB_ACTOR"], "token": os.environ["GH_TOKEN"]}))
PY
