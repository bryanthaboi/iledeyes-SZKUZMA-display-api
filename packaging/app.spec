# PyInstaller spec for the displayathon.app desktop bundle.
# Built by ../build.sh.
# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

# NiceGUI ships a lot of static assets (Quasar, Tailwind, fonts) that
# collect_all picks up.
ng_datas, ng_binaries, ng_hidden = collect_all("nicegui")

# pywebview bundles its own JS bridge; collect those.
wv_datas, wv_binaries, wv_hidden = collect_all("webview")

hidden = (
    ng_hidden
    + wv_hidden
    + collect_submodules("nicegui")
    + collect_submodules("uvicorn")
    + collect_submodules("anyio")
    + collect_submodules("httpx")
)

a = Analysis(
    ["../app.py"],
    pathex=["."],
    binaries=ng_binaries + wv_binaries,
    datas=ng_datas + wv_datas,
    hiddenimports=hidden + ["display_client"],
    hookspath=[],
    runtime_hooks=[],
    # The UI doesn't need bleak/PIL; service does.
    excludes=["tkinter", "bleak", "CoreBluetooth"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="displayathon",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="displayathon",
)

app = BUNDLE(
    coll,
    name="displayathon.app",
    icon=None,
    bundle_identifier="com.boisclubgames.displayathon",
    info_plist={
        "CFBundleName": "displayathon",
        "CFBundleDisplayName": "displayathon",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "LSBackgroundOnly": False,
        "NSRequiresAquaSystemAppearance": False,
    },
)
