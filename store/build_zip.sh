#!/usr/bin/env bash
# Build the Chrome Web Store upload zip for the ffbot extension.
#
# Zips the contents of extension/ (manifest.json at the zip root, as CWS
# requires), excluding docs and dev files. Output: store/ffbot-extension.zip
#
# Usage: bash store/build_zip.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXT_DIR="$ROOT/extension"
OUT_DIR="$ROOT/store"
ZIP_PATH="$OUT_DIR/ffbot-extension.zip"

if [[ ! -f "$EXT_DIR/manifest.json" ]]; then
  echo "error: $EXT_DIR/manifest.json not found" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
rm -f "$ZIP_PATH"

# cd into extension/ so archived paths are relative to it -> manifest.json at
# the zip root. zip's wildcards span '/' by default, so "*.md" also excludes
# markdown in subdirectories.
(
  cd "$EXT_DIR"
  zip -r -X "$ZIP_PATH" . \
    -x "README*" \
    -x "*.md" \
    -x "*.py" \
    -x "*.pyc" \
    -x "__pycache__/*" \
    -x ".*" \
    -x "*/.*" \
    -x "*.zip" \
    -x "*~" \
    -x "*.swp" \
    -x "*.bak" \
    -x "*.log" \
    -x "node_modules/*" \
    -x "tests/*"
)

echo
echo "Built $ZIP_PATH:"
unzip -l "$ZIP_PATH"

# Sanity check: manifest.json must sit at the archive root.
if unzip -l "$ZIP_PATH" | awk '{print $4}' | grep -qx "manifest.json"; then
  echo "OK: manifest.json is at the zip root."
else
  echo "error: manifest.json is not at the zip root" >&2
  exit 1
fi
