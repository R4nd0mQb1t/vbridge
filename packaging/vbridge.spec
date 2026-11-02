# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the V Bridge service (one-folder, windowed, tray).
# Build from the repo root:  pyinstaller packaging/vbridge.spec --noconfirm
import os

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SERVICE = os.path.join(ROOT, "service")

block_cipher = None

a = Analysis(
    [os.path.join(SERVICE, "vbridge_launcher.py")],
    pathex=[SERVICE],
    binaries=[],
    datas=[(os.path.join(SERVICE, "bridge", "firms.json"), "bridge")],
    hiddenimports=["MetaTrader5", "pystray._win32", "PIL.Image", "PIL.ImageDraw", "yaml", "psutil",
                   "bridge.tray", "bridge.procs", "bridge.worker", "bridge.mt5_api", "bridge.symbols"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "numpy.testing"],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VBridgeService",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=os.path.join(SPECPATH, 'assets', 'vbridge.ico'),
    version=os.path.join(SPECPATH, 'version_info.txt'),
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, strip=False, upx=False, name="VBridgeService")
