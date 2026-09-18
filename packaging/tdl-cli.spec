# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the terminal-only tdl binary (CLI + TUI).

The Qt GUI is deliberately excluded: this build targets headless servers,
ARM boards, and Termux-adjacent environments where PySide6 would dominate
the archive size for no benefit.
"""

import os


project_root = os.path.abspath(os.path.join(SPECPATH, ".."))


a = Analysis(
    [os.path.join(project_root, "packaging", "tdl-cli-entry.py")],
    pathex=[project_root],
    binaries=[],
    datas=[],
    hiddenimports=["tdl.tui", "tdl.downloader", "tdl.stream", "tdl.crypto"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PySide6",
        "tkinter",
        "matplotlib",
        "numpy",
        "pytest",
        "setuptools",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="tdl",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
