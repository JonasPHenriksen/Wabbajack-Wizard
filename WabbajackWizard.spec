# -*- mode: python ; coding: utf-8 -*-
"""
The build recipe: one bundle, two binaries
==========================================

Run it with build.py, not by hand - that script checks the version, cleans
the output, and lays the finished folder out. This file only says what goes
in.

Two things here are deliberate and worth not undoing:

ONE FOLDER, NOT ONE FILE. A --onefile build unpacks itself into a temp
directory on every start, and this tool starts itself: a run launches the
downloader and then the sorter as child processes, so a single-file build
would unpack the whole of Python three times for one run. It would also put
__file__ in a temp folder that vanishes, when the entire suite is built
around living next to your archives.

TWO BINARIES, ONE ANALYSIS. The window must have no console or it drags a
black rectangle around behind it; the console side must have one or it has
nowhere to speak. That is the only difference between them - the same code is
inside both, and they share the one _internal folder beside them.
"""

import os
import sys

from PyInstaller.utils.win32 import versioninfo as VI

# SPECPATH is injected by PyInstaller and is this file's folder, which is the
# project. Importing our own modules for the version number and the binary
# names beats writing either of them down twice.
HERE = os.path.abspath(SPECPATH)
sys.path.insert(0, HERE)

import wj_config as CFG
import wj_paths

# --- Version resource, so Windows' own Properties dialog knows the build ---
# Also the cheapest way to look like a real program rather than an anonymous
# exe, which matters when the audience downloads a zip and their antivirus
# has opinions.
_v = tuple(int(p) for p in CFG.__version__.split(".")[:3]) + (0,)
_strings = VI.StringTable("040904B0", [
    VI.StringStruct("CompanyName", ""),
    VI.StringStruct("FileDescription", "Wabbajack Wizard"),
    VI.StringStruct("FileVersion", CFG.__version__),
    VI.StringStruct("InternalName", CFG.APP_NAME),
    VI.StringStruct("LegalCopyright", "MIT licensed. See LICENSE."),
    VI.StringStruct("OriginalFilename", wj_paths.GUI_EXE),
    VI.StringStruct("ProductName", "Wabbajack Wizard"),
    VI.StringStruct("ProductVersion", CFG.__version__),
])
_info = VI.VSVersionInfo(
    ffi=VI.FixedFileInfo(filevers=_v, prodvers=_v, mask=0x3F, flags=0x0,
                         OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
    kids=[VI.StringFileInfo([_strings]), VI.VarFileInfo([VI.VarStruct("Translation", [1033, 1200])])],
)
os.makedirs(os.path.join(HERE, "build"), exist_ok=True)
VERSION_FILE = os.path.join(HERE, "build", "wj_version_info.txt")
with open(VERSION_FILE, "w", encoding="utf-8") as _f:
    _f.write(str(_info))


a = Analysis(
    ["wj_launcher.py"],
    pathex=[HERE],
    binaries=[],
    datas=[],
    # The launcher reaches these by name, at run time, so nothing static
    # points at them for the analyser to follow.
    hiddenimports=[
        "wj_autoupdate", "wj_cdn", "wj_config", "wj_downloader", "wj_gallery",
        "wj_gui", "wj_merge_pool", "wj_paths", "wj_sorter", "wj_wabbajack",
        "wj_wizard",
        # Optional at run time, mandatory in a build: without it every
        # download silently falls back to being checked by size alone.
        "xxhash",
    ],
    hookspath=[],
    runtime_hooks=[],
    # Nothing here imports these; naming them keeps a stray dependency in the
    # build environment from doubling the size of the download.
    excludes=["numpy", "pandas", "matplotlib", "PIL", "pytest",
              "setuptools", "pip", "PyInstaller"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# UPX stays off. It saves a few MB and costs far more than that in antivirus
# false positives, which is not a trade worth making for something people
# download from a mod site.
_common = dict(
    exclude_binaries=True,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    version=VERSION_FILE,
)

exe_gui = EXE(pyz, a.scripts, [],
              name=os.path.splitext(wj_paths.GUI_EXE)[0], console=False, **_common)
exe_console = EXE(pyz, a.scripts, [],
                  name=os.path.splitext(wj_paths.CONSOLE_EXE)[0], console=True, **_common)

coll = COLLECT(exe_gui, exe_console, a.binaries, a.datas,
               strip=False, upx=False, name="WabbajackWizard")
