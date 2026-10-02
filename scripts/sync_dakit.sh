#!/usr/bin/env bash
# Vendor the dakit design system bundle into relay's static tree.
#
# Relay has no Node build step, so the generated dakit bundle is checked in.
# Run this after changing dakit (cd ../dakit && node build.mjs), then commit
# both repos. The dakit commit the bundle came from is recorded in
# static/dakit/PROVENANCE.
#
# Usage: scripts/sync_dakit.sh [path-to-dakit]   (default: ../dakit)
set -euo pipefail

cd "$(dirname "$0")/.."
DAKIT_DIR="${1:-../dakit}"
DAKIT_DIR="${DAKIT_DIR%/}"

if [[ ! -f "$DAKIT_DIR/dist/dakit.css" ]]; then
  echo "error: $DAKIT_DIR/dist/dakit.css not found — build dakit first:" >&2
  echo "  cd $DAKIT_DIR && node build.mjs" >&2
  exit 1
fi

mkdir -p static/dakit/fonts
cp "$DAKIT_DIR/dist/dakit.css" static/dakit/dakit.css
cp "$DAKIT_DIR"/fonts/inter-var.woff2 \
   "$DAKIT_DIR"/fonts/ibm-plex-mono-400.woff2 \
   "$DAKIT_DIR"/fonts/ibm-plex-mono-500.woff2 \
   "$DAKIT_DIR"/fonts/LICENSE.md \
   static/dakit/fonts/

{
  echo "Vendored from dakit ($(git -C "$DAKIT_DIR" rev-parse --short HEAD) $(git -C "$DAKIT_DIR" log -1 --format=%cs))."
  echo "Do not edit; regenerate with scripts/sync_dakit.sh after rebuilding dakit."
} > static/dakit/PROVENANCE

echo "Synced dakit into static/dakit/ (dakit $(git -C "$DAKIT_DIR" rev-parse --short HEAD))"
