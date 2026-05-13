#!/usr/bin/env bash
# Stop and remove the displayathon-service launchd agent.

set -euo pipefail

LABEL="com.boisclubgames.displayathon"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    echo "→ booting out $DOMAIN/$LABEL"
    launchctl bootout "$DOMAIN/$LABEL" || true
else
    echo "→ not currently loaded"
fi

if [[ -f "$TARGET" ]]; then
    rm -f "$TARGET"
    echo "→ removed $TARGET"
else
    echo "→ no plist at $TARGET"
fi

for candidate in \
    "$HOME/Library/displayathon/bin/displayathon-service" \
    "$HOME/Library/Application Support/displayathon/bin/displayathon-service"; do
    if [[ -f "$candidate" ]]; then
        rm -f "$candidate"
        echo "→ removed $candidate"
    fi
done

echo
echo "✓ uninstalled"
echo
echo "Logs remain at ~/Library/Logs/displayathon/ — delete them if you want."
