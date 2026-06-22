#!/usr/bin/env bash
# Build a self-contained PyInstaller binary + Anna manifest, packaged as a
# per-platform tar.gz for Binary distribution. PyInstaller cannot cross-compile,
# so each platform's artifact must be built on its own runner (see the GitHub
# Actions workflow). Run locally only to test the macOS/Linux build you are on.
set -euo pipefail
cd "$(dirname "$0")"

ENTRY_FILE="tidy_engine_plugin.py"
OUT_DIR="dist-anna"

TOOL_ID="$(python3 -c 'import json;print(json.load(open("executa.json"))["tool_id"])')"
VERSION="$(python3 -c 'import json;print(json.load(open("executa.json")).get("version") or "0.0.0")')"

OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|amd64) ARCH="x86_64" ;;
  arm64|aarch64) ARCH="arm64" ;;
esac
PLATFORM="$OS-$ARCH"

rm -rf build dist "$OUT_DIR/staging-$PLATFORM"
mkdir -p "$OUT_DIR/staging-$PLATFORM/bin"

uv run --with pyinstaller python -m PyInstaller \
  --onefile --clean --noupx \
  --name "$TOOL_ID" \
  "$ENTRY_FILE"

cp "dist/$TOOL_ID" "$OUT_DIR/staging-$PLATFORM/bin/$TOOL_ID"
chmod 0755 "$OUT_DIR/staging-$PLATFORM/bin/$TOOL_ID"

python3 - "$OUT_DIR/staging-$PLATFORM/manifest.json" "$TOOL_ID" "$VERSION" <<'PY'
import json, sys
from pathlib import Path
path, tool_id, version = sys.argv[1], sys.argv[2], sys.argv[3]
entrypoint = f"bin/{tool_id}"
Path(path).write_text(json.dumps({
    "name": tool_id,
    "version": version,
    "runtime": {"binary": {"entrypoint": {"default": entrypoint},
                           "permissions": {entrypoint: "0o755"}}},
}, indent=2) + "\n")
PY

ARCHIVE="$OUT_DIR/$TOOL_ID-$PLATFORM.tar.gz"
(cd "$OUT_DIR/staging-$PLATFORM" && tar czf "../$TOOL_ID-$PLATFORM.tar.gz" .)
echo "Built: $ARCHIVE"
