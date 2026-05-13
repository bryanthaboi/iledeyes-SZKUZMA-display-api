#!/usr/bin/env bash
# Build the two standalone artifacts.
#   dist/displayathon-service        — CLI binary; launchd runs this
#   dist/displayathon.app            — Mac app bundle; double-click to launch UI
#
# Once built, neither artifact requires a system Python or a venv to run.
# The build itself uses a Python venv (reused from .venv if present, else a
# throwaway .build-venv/) but that's a build-time detail.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# ---------------------------------------------------------------------------
# 1. pick a build venv
# ---------------------------------------------------------------------------

if [[ -x ".venv/bin/python" ]]; then
    VENV=".venv"
    echo "→ reusing $VENV"
elif [[ -x ".build-venv/bin/python" ]]; then
    VENV=".build-venv"
    echo "→ reusing $VENV"
else
    VENV=".build-venv"
    echo "→ creating $VENV"
    python3 -m venv "$VENV"
fi

PIP="$VENV/bin/pip"
PYTHON="$VENV/bin/python"
PYINSTALLER="$VENV/bin/pyinstaller"

# ---------------------------------------------------------------------------
# 2. install deps + pyinstaller
# ---------------------------------------------------------------------------

echo "→ installing runtime deps"
"$PIP" install --quiet --upgrade pip
"$PIP" install --quiet -r requirements.txt

if [[ ! -x "$PYINSTALLER" ]]; then
    echo "→ installing pyinstaller"
    "$PIP" install --quiet "pyinstaller>=6.10"
fi

# ---------------------------------------------------------------------------
# 3. clean previous build outputs
# ---------------------------------------------------------------------------

echo "→ cleaning previous build"
rm -rf build dist/displayathon-service dist/displayathon dist/displayathon.app

mkdir -p dist

# ---------------------------------------------------------------------------
# 4. build the service binary
# ---------------------------------------------------------------------------

echo "→ building displayathon-service"
"$PYINSTALLER" \
    --noconfirm \
    --workpath build/service \
    --distpath dist \
    packaging/service.spec

# ---------------------------------------------------------------------------
# 5. build the .app bundle
# ---------------------------------------------------------------------------

echo "→ building displayathon.app"
"$PYINSTALLER" \
    --noconfirm \
    --workpath build/app \
    --distpath dist \
    packaging/app.spec

# ---------------------------------------------------------------------------
# 6. report
# ---------------------------------------------------------------------------

echo
echo "✓ build complete"
echo
echo "Artifacts:"
[[ -x dist/displayathon-service ]] && ls -lh dist/displayathon-service | awk '{print "    " $0}'
[[ -d dist/displayathon.app    ]] && du -sh dist/displayathon.app | awk '{print "    " $0}'

cat <<EOF

Next steps:
    ./service/install.sh           # register the service binary with launchd
    open dist/displayathon.app     # launch the UI
                                   # (or drag dist/displayathon.app to /Applications)
EOF
