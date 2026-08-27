# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the portable tdl Qt6 GUI."""

import os


project_root = os.path.abspath(os.path.join(SPECPATH, ".."))


analysis = Analysis(
    [os.path.join(project_root, "run.py")],
    pathex=[project_root],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)


pyz = PYZ(analysis.pure)


executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="tdl-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)


coll = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    analysis.zipfiles,
    strip=False,
    upx=True,
    name="tdl-gui",
)
