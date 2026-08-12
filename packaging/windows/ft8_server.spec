# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the MRRC_FT8 server (onedir). Build on Windows with
# packaging/windows/build.ps1 after: tests, desktop/client npm build, and the
# vendor DLLs under vendor/wsjtx-runtime + vendor/hamlib are in place.
from pathlib import Path
import sys

ROOT = Path(SPECPATH).parents[1]
DIST_ROOT = ROOT / "dist" / "windows" / "_pyinstaller"

_extra_data = []
_dll_root = ROOT / "vendor" / "wsjtx-runtime" / "windows" / "x64"
if _dll_root.exists():
    _extra_data.append((str(_dll_root), "."))  # wsjt_core.dll + libgfortran/libgomp/libfftw3f...
_hamlib_root = ROOT / "vendor" / "hamlib" / "windows" / "x64"
if _hamlib_root.exists():
    _extra_data.append((str(_hamlib_root), "hamlib"))  # rigctld.exe + libhamlib

a = Analysis(
    [str(ROOT / "packaging" / "windows" / "server_entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "server" / "web" / "static"), "server/web/static"),
        (str(ROOT / "desktop" / "ft8web" / "dist"), "desktop/ft8web/dist"),
        (str(ROOT / "cty.dat"), "."),
        (str(ROOT / "windows" / "restart.ps1"), "."),
        *_extra_data,
    ],
    hiddenimports=[
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
        "dotenv",
        "serial",
        "serial.tools.list_ports",
        "argon2",
        "argon2.low_level",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="ft8-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=True, upx_exclude=[],
    name="ft8-server",
)
