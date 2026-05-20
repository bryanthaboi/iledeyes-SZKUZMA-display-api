# PyInstaller spec for the displayathon-service CLI binary.
# Built by ../build.sh. Don't run pyinstaller against this directly without
# also setting the working directory to the repo root.
# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs, collect_submodules

block_cipher = None

# bleak's CoreBluetooth backend pulls in pyobjc-framework-CoreBluetooth at
# runtime via importlib; tell PyInstaller about it explicitly so the bundled
# binary works on a fresh machine without a system Python.
hidden = (
    collect_submodules("bleak")
    + collect_submodules("CoreBluetooth")
    + collect_submodules("uvicorn")
    + collect_submodules("anyio")
    # AppKit / Foundation are needed for the CoreText-based text renderer
    # (so /api/text produces the same pixels as the .app's canvas path).
    + collect_submodules("AppKit")
    + collect_submodules("Foundation")
    + collect_submodules("Quartz")
    + collect_submodules("objc")
)

# pyobjc framework wrappers ship native `_<framework>.cpython-*.so` C
# extensions that collect_submodules does NOT pick up. Without these the
# bundled binary lazy-loads CoreBluetooth and dies with:
#     ImportError: cannot import name '_CoreBluetooth' from partially
#     initialized module 'CoreBluetooth' (most likely due to a circular import)
# Pull every relevant framework's dynamic libs in by hand.
pyobjc_binaries = (
    collect_dynamic_libs("objc")
    + collect_dynamic_libs("Foundation")
    + collect_dynamic_libs("AppKit")
    + collect_dynamic_libs("Quartz")
    + collect_dynamic_libs("CoreBluetooth")
    + collect_dynamic_libs("bleak")
)

# Pull PIL plugins (Image.open dispatches via these).
pil_datas, pil_binaries, pil_hidden = collect_all("PIL")

a = Analysis(
    ["../display_service.py"],
    pathex=["."],
    binaries=pil_binaries + pyobjc_binaries,
    datas=pil_datas + [("../fonts", "fonts")],
    hiddenimports=hidden + pil_hidden + [
        "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont",
        "displayathon",
    ],
    hookspath=[],
    # rt_preload_pyobjc.py runs before the user module imports, eagerly loading
    # CoreBluetooth so the lazy-load chain from bleak can't hit a partial init.
    # Path is resolved relative to build.sh's cwd (the repo root).
    runtime_hooks=["packaging/rt_preload_pyobjc.py"],
    excludes=["tkinter", "nicegui", "pywebview", "webview"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="displayathon-service",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
