#!/usr/bin/env bash
# Install (or re-install) the displayathon Stream Deck plugin. Copies the
# sdPlugin bundle into the user's Stream Deck plugins dir and restarts
# Stream Deck via AppleScript so the new bundle is picked up.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${DIR}/com.displayathon.controller.sdPlugin"
DEST_DIR="${HOME}/Library/Application Support/com.elgato.StreamDeck/Plugins"
DEST="${DEST_DIR}/com.displayathon.controller.sdPlugin"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "✗ This installer is macOS-only. On Windows, drop the folder into %APPDATA%\\Elgato\\StreamDeck\\Plugins\\ and restart Stream Deck." >&2
  exit 1
fi

if [[ ! -d "${SRC}" ]]; then
  echo "✗ Plugin source not found: ${SRC}" >&2
  exit 1
fi

# Regenerate icons before installing so a fresh checkout has them too.
if [[ -f "${DIR}/make-icons.js" && "${SKIP_ICONS:-0}" != "1" ]]; then
  if command -v node >/dev/null 2>&1; then
    echo "→ Regenerating icons via make-icons.js…"
    (cd "${DIR}" && node make-icons.js)
  else
    echo "ℹ node not on PATH — skipping icon regen."
  fi
fi

if [[ ! -d "${DEST_DIR}" ]]; then
  echo "→ Creating Stream Deck plugin dir: ${DEST_DIR}"
  mkdir -p "${DEST_DIR}"
fi

# Quit Stream Deck first — it locks the plugin dir while running.
if pgrep -x "Stream Deck" >/dev/null; then
  echo "→ Quitting Stream Deck app…"
  osascript -e 'tell application "Stream Deck" to quit' || true
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if ! pgrep -x "Stream Deck" >/dev/null; then break; fi
    sleep 0.5
  done
  if pgrep -x "Stream Deck" >/dev/null; then
    echo "→ Stream Deck didn't quit gracefully; forcing."
    pkill -x "Stream Deck" || true
    sleep 1
  fi
fi

# Replace the bundle.
if [[ -e "${DEST}" ]]; then
  echo "→ Removing old plugin at ${DEST}"
  rm -rf "${DEST}"
fi

echo "→ Installing plugin → ${DEST}"
cp -R "${SRC}" "${DEST}"

# Relaunch Stream Deck.
if [[ -d "/Applications/Stream Deck.app" ]]; then
  echo "→ Relaunching Stream Deck app."
  open -a "Stream Deck"
elif [[ -d "/Applications/Elgato Stream Deck.app" ]]; then
  echo "→ Relaunching Stream Deck app."
  open -a "Elgato Stream Deck"
else
  echo "ℹ Stream Deck.app not found in /Applications/. Start it manually."
fi

cat <<EOF

────────────────────────────────────────────
✓ displayathon plugin installed.

  Bundle:   ${DEST}
  Service:  http://127.0.0.1:49696 — single global setting. Edit it inside
            any action's Property Inspector and every displayathon key
            uses the new URL.

  Drop any of these "displayathon" actions onto a key:
    • displayathon · Solid       — fill display with one color
    • displayathon · Fade        — ping-pong fade between two colors
    • displayathon · Text        — scrolling text (font, weight, fg/bg…)
    • displayathon · GIF         — send a .gif file from disk
    • displayathon · Health      — ping service; title shows uptime
    • displayathon · Rewarm BLE  — re-warm the BLE backend on the service

  To re-install after edits: ./install-plugin.sh
  Logs: ~/Library/Logs/ElgatoStreamDeck/
────────────────────────────────────────────
EOF
