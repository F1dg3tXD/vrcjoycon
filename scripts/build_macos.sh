#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script must be run on macOS." >&2
  exit 1
fi

ARCH="$(uname -m)"
echo "Building VRCJoyCon for macOS ${ARCH}"

cd "$(dirname "$0")/.."
python3 scripts/build.py

APP="dist/VRCJoyCon.app"
if [[ -d "$APP" ]]; then
  ZIP="dist/VRCJoyCon-macos-${ARCH}.zip"
  rm -f "$ZIP"
  (cd dist && zip -r "VRCJoyCon-macos-${ARCH}.zip" "VRCJoyCon.app")
  echo "Created ${ZIP}"
else
  echo "Expected application bundle not found at ${APP}" >&2
  exit 1
fi