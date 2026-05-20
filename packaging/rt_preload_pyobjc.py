# PyInstaller runtime hook — pre-loads pyobjc framework modules.
#
# Why: when the frozen binary's first BLE call goes through bleak, bleak
# lazy-imports the CoreBluetooth backend, which lazy-imports CoreBluetooth,
# which calls objc.loadBundle inside its package __init__. Under PyInstaller's
# import hook, that lazy chain hits a partial-init state and dies with:
#
#     ImportError: cannot import name '_CoreBluetooth' from partially
#     initialized module 'CoreBluetooth' (most likely due to a circular import)
#
# This hook runs before the user's display_service module is imported, so
# CoreBluetooth's __init__ completes in a clean state. Subsequent re-imports
# from bleak hit the module cache and succeed.
#
# This file is referenced by packaging/service.spec via `runtime_hooks=`.
# Failures here are swallowed — on non-mac platforms these modules don't
# exist, and a degraded binary that still serves /api/health is better than
# a binary that won't start.

import sys

_PREWARM = (
    "objc",
    "Foundation",
    "AppKit",
    "CoreBluetooth",
    "bleak.backends.corebluetooth",
    "bleak.backends.corebluetooth.scanner",
    "bleak.backends.corebluetooth.client",
)

for _mod in _PREWARM:
    try:
        __import__(_mod)
    except Exception as _e:
        print(f"[displayathon rt-hook] preload {_mod} failed: {_e}", file=sys.stderr)
