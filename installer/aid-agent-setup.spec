# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for AID Helpdesk Agent Setup
#
# Produces:  dist/aid-agent-setup.exe
#   - Single-file Windows executable
#   - No console window (wizard is the UI)
#   - Bundles: setup_wizard.py, ad_bridge.py, all dependencies
#
# Build:  pyinstaller aid-agent-setup.spec
#

import os
import sys

block_cipher = None

# Path to repo root (one level up from installer/)
REPO_ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

a = Analysis(
    [os.path.join(SPECPATH, "setup_wizard.py")],
    pathex=[REPO_ROOT],            # makes ad_bridge importable
    binaries=[],
    datas=[],
    hiddenimports=[
        # pywin32 service internals - must be explicit
        "win32serviceutil",
        "win32service",
        "win32event",
        "win32api",
        "win32con",
        "servicemanager",
        "pywintypes",
        "win32timezone",
        # pywinrm
        "winrm",
        "winrm.protocol",
        "winrm.exceptions",
        "requests_kerberos",
        "requests_ntlm",
        # other
        "dotenv",
        "pkg_resources.py2_compat",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["matplotlib", "numpy", "pandas", "PIL", "PyQt5", "wx"],
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
    name="aid-agent-setup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,         # request UAC elevation (needed to install Windows Service)
    icon=None,              # set to "aid.ico" once you have an icon file
)
