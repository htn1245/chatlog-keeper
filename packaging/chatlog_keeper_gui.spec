# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — one-file windowed ``chatlog-keeper-gui`` executable.

Build from the repo root:
    pyinstaller packaging/chatlog_keeper_gui.spec --noconfirm --clean --distpath dist_exe

The bundled key helpers, runtime DLLs and Crypto/zstandard collection must be
exactly those of ``chatlog_keeper.spec``.  Rather than copy that logic, this
file runs the other spec's preamble (everything before its ``Analysis`` call),
then adds Tcl/Tk and swaps in the window entry point.
"""
import glob as _gui_glob
import os as _gui_os
import sys as _gui_sys

_BASE_SPEC = _gui_os.path.join(SPECPATH, "chatlog_keeper.spec")
with open(_BASE_SPEC, encoding="utf-8") as _gui_handle:
    _gui_base_text = _gui_handle.read()
_GUI_SPLIT = "\na = Analysis("
if _gui_base_text.count(_GUI_SPLIT) != 1:
    raise RuntimeError(
        "chatlog_keeper.spec no longer has exactly one Analysis call to split on"
    )
# Defines SPEC_DIR, ROOT, binaries, datas and hiddenimports.
exec(compile(_gui_base_text.split(_GUI_SPLIT)[0], _BASE_SPEC, "exec"), globals())

# The command-line spec deliberately leaves conda's Tcl/Tk DLLs out; the window
# needs them next to _tkinter.  No-op on a python.org build host.
_gui_lib_bin = _gui_os.path.join(
    _gui_os.path.dirname(_gui_os.path.abspath(_gui_sys.executable)), "Library", "bin"
)
if _gui_os.path.isdir(_gui_lib_bin):
    for _gui_dll in _gui_glob.glob(_gui_os.path.join(_gui_lib_bin, "*.dll")):
        if _gui_os.path.basename(_gui_dll).lower().startswith(("tcl", "tk")):
            binaries.append((_gui_dll, "."))

a = Analysis(
    [os.path.join(SPEC_DIR, "chatlog_keeper_gui_main.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["memexa", "torch", "transformers"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="chatlog-keeper-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
