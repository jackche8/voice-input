#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

VERSION="$(python3 -c 'from _version import APP_VERSION; print(APP_VERSION)')"
if [[ -z "$VERSION" ]]; then
  echo "ERROR: failed to read APP_VERSION from _version.py" >&2
  exit 1
fi

ARCH="$(uname -m)"
if [[ "$ARCH" == "aarch64" ]]; then
  ARCH="arm64"
fi

echo "Building Ollama Voice Input v${VERSION} for macOS (${ARCH}, --onedir .app)..."
python3 -m PyInstaller --clean --noconfirm packaging/pyinstaller/ollama-voice-input.spec

APP_BUNDLE="$ROOT/dist/Ollama Voice Input.app"
MAIN_BIN="$APP_BUNDLE/Contents/MacOS/ollama-voice-input"
INFO_PLIST="$APP_BUNDLE/Contents/Info.plist"

if [[ ! -x "$MAIN_BIN" ]]; then
  echo "ERROR: missing executable inside bundle: $MAIN_BIN" >&2
  exit 1
fi

plutil -lint "$INFO_PLIST"

if [[ ! -f "$APP_BUNDLE/Contents/Frameworks/_asr_runtime/runtimes/asr/server.py" \
   && ! -f "$APP_BUNDLE/Contents/Resources/_asr_runtime/runtimes/asr/server.py" ]]; then
  echo "ERROR: missing isolated ASR runtime closure inside bundle: $APP_BUNDLE" >&2
  exit 1
fi

DMG_STAGE="$ROOT/build/dmg-stage"
rm -rf "$DMG_STAGE"
trap 'rm -rf "$DMG_STAGE"' EXIT
mkdir -p "$DMG_STAGE"

cp -R "$APP_BUNDLE" "$DMG_STAGE/"
ln -s /Applications "$DMG_STAGE/Applications"
cp "$ROOT/LICENSE" "$ROOT/COMMERCIAL.md" "$ROOT/THIRD-PARTY-NOTICES.md" "$DMG_STAGE/"

DMG_OUT="$ROOT/dist/Ollama-Voice-Input-${VERSION}-macos-${ARCH}.dmg"
rm -f "$DMG_OUT"

# Retry up to 3 times to handle transient Spotlight "Resource busy" errors on macOS runners
attempt=1
max_attempts=3
while true; do
  if hdiutil create -volname "Ollama Voice Input" -srcfolder "$DMG_STAGE" -ov -format UDZO "$DMG_OUT"; then
    break
  fi
  if [[ "$attempt" -ge "$max_attempts" ]]; then
    echo "ERROR: hdiutil create failed after ${max_attempts} attempts" >&2
    exit 1
  fi
  echo "hdiutil create failed (attempt ${attempt}/${max_attempts}), retrying in 5s..." >&2
  sleep 5
  attempt=$((attempt + 1))
done

echo "Built macOS DMG: $DMG_OUT"
