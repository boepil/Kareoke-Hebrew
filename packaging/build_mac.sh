#!/usr/bin/env bash
set -euo pipefail

# Build script for Karaoke Studio Core on macOS (Apple Silicon arm64).
# Usage:
#   ./packaging/build_mac.sh
#
# Environment variables (all optional):
#   KARAOKE_BUILD_PYTHON: Path to python executable (defaults to python3)
#   APPLE_SIGNING_IDENTITY: "Developer ID Application: Your Name (TEAMID)" or personal cert identity
#   APPLE_ID: Apple ID email for notarization
#   APPLE_APP_SPECIFIC_PASSWORD: App-specific password for notarization
#   APPLE_TEAM_ID: 10-character Apple Developer Team ID
#   KARAOKE_FFMPEG_PATH: Explicit path to ffmpeg binary
#   KARAOKE_DENO_PATH: Explicit path to deno binary

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo "=== Karaoke Studio Core macOS Build ==="
echo "Project root: $PROJECT_ROOT"
ARCH="$(uname -m)"
echo "Host architecture: $ARCH"

PYTHON="${KARAOKE_BUILD_PYTHON:-python3}"
if ! command -v "$PYTHON" &>/dev/null; then
    echo "Error: Python was not found ($PYTHON). Please install Python 3.10 or 3.11."
    exit 1
fi
echo "Using Python: $($PYTHON --version)"

# Verify / locate FFmpeg
FFMPEG="${KARAOKE_FFMPEG_PATH:-}"
if [ -z "$FFMPEG" ]; then
    if command -v ffmpeg &>/dev/null; then
        FFMPEG="$(command -v ffmpeg)"
    elif [ -f "/opt/homebrew/bin/ffmpeg" ]; then
        FFMPEG="/opt/homebrew/bin/ffmpeg"
    elif [ -f "/usr/local/bin/ffmpeg" ]; then
        FFMPEG="/usr/local/bin/ffmpeg"
    fi
fi
if [ -n "$FFMPEG" ] && [ -x "$FFMPEG" ]; then
    echo "Found FFmpeg: $FFMPEG"
    export KARAOKE_FFMPEG_PATH="$FFMPEG"
else
    echo "Warning: FFmpeg not found on PATH or Homebrew locations."
    echo "Install via: brew install ffmpeg"
fi

# Verify / locate Deno
DENO="${KARAOKE_DENO_PATH:-}"
if [ -z "$DENO" ]; then
    if command -v deno &>/dev/null; then
        DENO="$(command -v deno)"
    elif [ -f "/opt/homebrew/bin/deno" ]; then
        DENO="/opt/homebrew/bin/deno"
    elif [ -f "/usr/local/bin/deno" ]; then
        DENO="/usr/local/bin/deno"
    fi
fi
if [ -n "$DENO" ] && [ -x "$DENO" ]; then
    echo "Found Deno: $DENO"
    export KARAOKE_DENO_PATH="$DENO"
else
    echo "Notice: Deno not found. yt-dlp will fall back to Node.js or builtin extractor."
fi

# Build virtual environment if needed
VENV_DIR="$PROJECT_ROOT/.venv-mac-build"
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment at $VENV_DIR..."
    "$PYTHON" -m venv "$VENV_DIR"
fi
VENV_PYTHON="$VENV_DIR/bin/python"

echo "Installing/verifying build requirements..."
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -r "$PROJECT_ROOT/packaging/requirements-mac-build.txt"

echo "Building KaraokeStudioCore.app with PyInstaller..."
"$VENV_PYTHON" -m PyInstaller --clean --noconfirm "$PROJECT_ROOT/packaging/core_mac.spec"

APP_PATH="$PROJECT_ROOT/dist/KaraokeStudioCore.app"
if [ ! -d "$APP_PATH" ]; then
    echo "Error: PyInstaller build failed; $APP_PATH was not produced."
    exit 1
fi

echo "=== Code Signing ==="
SIGNING_IDENTITY="${APPLE_SIGNING_IDENTITY:-}"
if [ -n "$SIGNING_IDENTITY" ]; then
    echo "Signing with identity: $SIGNING_IDENTITY"
    codesign --force --deep --options runtime --sign "$SIGNING_IDENTITY" "$APP_PATH"
    echo "Verifying signature..."
    codesign --verify --deep --strict --verbose=2 "$APP_PATH"
else
    echo "No APPLE_SIGNING_IDENTITY provided; applying ad-hoc signature for personal installation..."
    codesign --force --deep -s - "$APP_PATH"
fi

echo "=== Packaging DMG ==="
DMG_NAME="KaraokeStudioCore-${ARCH}.dmg"
DMG_PATH="$PROJECT_ROOT/dist/$DMG_NAME"
STAGING_DIR="$PROJECT_ROOT/dist/dmg_staging"

rm -rf "$STAGING_DIR" "$DMG_PATH"
mkdir -p "$STAGING_DIR"
cp -R "$APP_PATH" "$STAGING_DIR/"
ln -s /Applications "$STAGING_DIR/Applications"

hdiutil create -volname "Karaoke Studio Core" \
    -srcfolder "$STAGING_DIR" \
    -ov \
    -format UDZO \
    "$DMG_PATH"

rm -rf "$STAGING_DIR"
echo "DMG created at: $DMG_PATH"

if [ -n "$SIGNING_IDENTITY" ]; then
    echo "Signing DMG..."
    codesign --force --sign "$SIGNING_IDENTITY" "$DMG_PATH"
fi

# Notarization (if Apple credentials provided)
APPLE_ID="${APPLE_ID:-}"
APPLE_PASS="${APPLE_APP_SPECIFIC_PASSWORD:-}"
TEAM_ID="${APPLE_TEAM_ID:-}"

if [ -n "$APPLE_ID" ] && [ -n "$APPLE_PASS" ] && [ -n "$TEAM_ID" ]; then
    echo "=== Submitting DMG for Apple Notarization ==="
    xcrun notarytool submit "$DMG_PATH" \
        --apple-id "$APPLE_ID" \
        --password "$APPLE_PASS" \
        --team-id "$TEAM_ID" \
        --wait
    echo "Stapling notarization ticket to DMG..."
    xcrun stapler staple "$DMG_PATH"
    echo "Notarization and stapling complete!"
else
    echo ""
    echo "Note: Notarization credentials not configured (APPLE_ID / APPLE_APP_SPECIFIC_PASSWORD / APPLE_TEAM_ID)."
    echo "For personal installation on your Mac:"
    echo "  1. Double click $DMG_NAME and drag KaraokeStudioCore into /Applications"
    echo "  2. On first launch, if Gatekeeper warns 'App cannot be opened':"
    echo "     Right-click (Control-click) KaraokeStudioCore in /Applications and select 'Open' -> 'Open'"
    echo "     Or run in Terminal: xattr -cr /Applications/KaraokeStudioCore.app"
fi

echo ""
echo "=== Build Complete ==="
echo "Artifact: $DMG_PATH"
