#!/usr/bin/env bash
# Install displayathon-service as a user-level launchd agent.
# Auto-detects between binary mode (preferred, no Python needed) and dev mode
# (Python + script via .venv). No sudo required either way.

set -euo pipefail

LABEL="com.boisclubgames.displayathon"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET="$TARGET_DIR/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/displayathon"
# Avoid "Application Support" because the space in the path makes launchd
# unhappy ("Bootstrap failed: 5: Input/output error" on some macOS versions).
INSTALL_BIN_DIR="$HOME/Library/displayathon/bin"
USER_FONTS_DIR="$HOME/Library/displayathon/fonts"

# ---------------------------------------------------------------------------
# pick mode: binary (preferred) or dev (.venv + script)
# ---------------------------------------------------------------------------

if [[ -x "$REPO/dist/displayathon-service" ]]; then
    MODE="binary"
elif [[ -x "$REPO/.venv/bin/python" ]]; then
    MODE="dev"
else
    cat >&2 <<EOF
error: neither a built binary nor a dev venv was found.

Either:
    ./build.sh                                 # produce dist/displayathon-service
or:
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
EOF
    exit 1
fi

echo "→ mode:    $MODE"

# ---------------------------------------------------------------------------
# resolve ProgramArguments
# ---------------------------------------------------------------------------

if [[ "$MODE" == "binary" ]]; then
    mkdir -p "$INSTALL_BIN_DIR"
    INSTALLED_BIN="$INSTALL_BIN_DIR/displayathon-service"
    echo "→ copying:  $REPO/dist/displayathon-service → $INSTALLED_BIN"
    cp "$REPO/dist/displayathon-service" "$INSTALLED_BIN"
    chmod +x "$INSTALLED_BIN"
    # ad-hoc re-sign after copy in case xattrs or codesign metadata get lost
    codesign --force --sign - "$INSTALLED_BIN" 2>/dev/null || true
    PROGRAM_ARGS_XML="        <string>$INSTALLED_BIN</string>"
else
    INSTALLED_BIN=""
    PYTHON="$REPO/.venv/bin/python"
    SCRIPT="$REPO/display_service.py"
    echo "→ python:   $PYTHON"
    echo "→ script:   $SCRIPT"
    PROGRAM_ARGS_XML="        <string>$PYTHON</string>
        <string>$SCRIPT</string>"
fi

mkdir -p "$TARGET_DIR" "$LOG_DIR" "$USER_FONTS_DIR"

# Sync repo fonts → user fonts dir so the service picks them up at runtime,
# regardless of which build is installed. Re-running this is idempotent.
if [[ -d "$REPO/fonts" ]]; then
    for src in "$REPO/fonts"/*.ttf "$REPO/fonts"/*.otf "$REPO/fonts"/*.ttc; do
        [[ -f "$src" ]] || continue
        cp -f "$src" "$USER_FONTS_DIR/"
        echo "→ font:     $(basename "$src") → $USER_FONTS_DIR/"
    done
fi

# ---------------------------------------------------------------------------
# write the plist
# ---------------------------------------------------------------------------

cat > "$TARGET" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
$PROGRAM_ARGS_XML
    </array>

    <key>WorkingDirectory</key>
    <string>$REPO</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>DISPLAYATHON_PORT</key>
        <string>49696</string>
        <key>DISPLAYATHON_HOST</key>
        <string>0.0.0.0</string>
        <key>DISPLAYATHON_LOG</key>
        <string>INFO</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>ThrottleInterval</key>
    <integer>5</integer>

    <key>ProcessType</key>
    <string>Background</string>

    <key>StandardOutPath</key>
    <string>$LOG_DIR/out.log</string>

    <key>StandardErrorPath</key>
    <string>$LOG_DIR/err.log</string>
</dict>
</plist>
EOF

echo "→ plist:    $TARGET"

# ---------------------------------------------------------------------------
# (re)bootstrap launchd
# ---------------------------------------------------------------------------

DOMAIN="gui/$(id -u)"
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "→ already loaded — booting out for refresh"
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    # Give launchd a moment to release the label; otherwise the bootstrap
    # below can return "5: Input/output error" on a still-transitioning job.
    for _ in 1 2 3 4 5; do
        launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
        sleep 0.5
    done
fi

# Strip any stray quarantine xattr from the installed binary so Gatekeeper
# doesn't reject the launchd exec on first run.
xattr -dr com.apple.quarantine "$INSTALLED_BIN" 2>/dev/null || true

echo "→ bootstrapping launchd"
if ! launchctl bootstrap "$DOMAIN" "$TARGET" 2>&1; then
    echo
    echo "Bootstrap failed. Try once more after this:" >&2
    echo "    launchctl bootout $DOMAIN/$LABEL 2>/dev/null; ./service/install.sh" >&2
    exit 1
fi
launchctl enable "$DOMAIN/$LABEL"
launchctl kickstart -k "$DOMAIN/$LABEL"

sleep 1

echo
echo "✓ installed ($MODE mode)"
echo
echo "Status:"
launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E '^\s+(state|last exit code|pid)' | sed 's/^/    /' || true
echo
echo "Logs:"
echo "    tail -f $LOG_DIR/out.log"
echo "    tail -f $LOG_DIR/err.log"
echo
echo "Health check:"
echo "    curl -s http://127.0.0.1:49696/api/health | python3 -m json.tool"
