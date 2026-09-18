#!/data/data/com.termux/files/usr/bin/env bash
# Termux installer for the tdl zipapp.
#
# Usage (from the extracted release archive):
#   bash install.sh
#
# The script is idempotent: re-running it upgrades an existing install.

set -euo pipefail

PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
BIN_DIR="$PREFIX/bin"
APP_DIR="$PREFIX/share/tdl"
PYZ_URL_DEFAULT=""

here="$(cd "$(dirname "$0")" && pwd)"

if [[ ! -f "$here/tdl.pyz" ]]; then
    echo "error: tdl.pyz not found next to install.sh" >&2
    echo "Extract the release archive first, e.g.:" >&2
    echo "  tar -xzf tdl-termux-aarch64.tar.gz && cd tdl-termux-aarch64" >&2
    exit 1
fi

echo "==> Installing Termux packages"
pkg install -y python openssl ffmpeg >/dev/null

echo "==> Installing Python dependencies"
python -m pip install --upgrade pip >/dev/null
python -m pip install requests pycryptodome

echo "==> Installing tdl"
mkdir -p "$APP_DIR"
install -m 0755 "$here/tdl.pyz" "$APP_DIR/tdl.pyz"
ln -sf "$APP_DIR/tdl.pyz" "$BIN_DIR/tdl"

echo "==> FFmpeg path"
FFMPEG_PATH="$(command -v ffmpeg || true)"
if [[ -z "$FFMPEG_PATH" ]]; then
    echo "warning: ffmpeg not found; FLAC extraction and embedded tags will not work" >&2
fi

cat <<'EOF'

tdl installed.

  tdl login --pkce   # required once for Hi-Res Lossless
  tdl tui            # terminal interface
  tdl <TIDAL-URL>    # direct download

If ffmpeg was installed now, set its path once:
  python - <<'PY'
from tdl.storage import load_settings, save_settings
s = load_settings(); s.ffmpeg_path = "ffmpeg"; save_settings(s)
PY
EOF
