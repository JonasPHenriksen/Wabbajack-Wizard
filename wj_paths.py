#!/usr/bin/env python3
"""
Where the tool's own folder is, from source and from a build
============================================================

Every script here anchors its work to `os.path.dirname(__file__)`: the
settings file, the manifests, the logs, the state file, the lock, the pool
root when it is written as a relative path. That is exactly right while the
scripts are scripts.

Inside a PyInstaller build it is exactly wrong. `__file__` then points into
the bundle's `_internal` folder, which is read-only in spirit, replaced on
every upgrade, and nowhere near the folder the user unzipped and keeps their
archives beside. So one question - "which folder is ours?" - gets one answer
here, and every script asks it instead of computing it.

The other half of the problem is that this suite runs parts of itself: the
updater launches the downloader and the sorter as child processes, by name.
Frozen, there is no `wj_sorter.py` to hand an interpreter, and `sys.executable`
is our own exe rather than a Python. `stage_argv()` answers that too, with a
`--wj-stage=` token the launcher intercepts before argparse ever sees it.

Nothing here imports anything else of ours. It is the bottom of the stack on
purpose, so that any module can rely on it.
"""

import os
import sys

# The token that tells a frozen exe to be one of the sibling scripts instead
# of itself. Consumed by wj_launcher.py and never reaches an argument parser.
STAGE_FLAG = "--wj-stage="

# The two binaries a build produces. Same code, same bundle; they differ only
# in whether Windows gives them a console, exactly as python.exe and
# pythonw.exe differ.
GUI_EXE = "WabbajackWizard.exe"
CONSOLE_EXE = "WabbajackWizard-console.exe"


def frozen():
    """True when running from a build rather than from the .py files."""
    return bool(getattr(sys, "frozen", False))


def app_dir(module_file):
    """The folder this tool calls its own.

    Pass `__file__`. From source that is the folder the scripts are in, which
    is what every caller meant all along. Frozen, the scripts live inside the
    bundle and the answer is the folder holding the exe - where the user's
    wj_settings.json, manifests, logs and pools actually are."""
    if frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(module_file))


def console_exe():
    """The binary to hand work that talks: it has a console and a real stdout.

    The window is built with no console at all, so a stage launched from it
    would have nowhere to write. Its console twin sits beside it. If that twin
    has been deleted, ourselves is still better than failing outright."""
    twin = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), CONSOLE_EXE)
    return twin if os.path.isfile(twin) else sys.executable


def stage_argv(script, script_dir):
    """The command that runs one of the sibling scripts as a child process.

    From source: this interpreter, then the script next to us. Frozen: the
    console binary, then the token that makes it become that script."""
    if frozen():
        return [console_exe(), STAGE_FLAG + os.path.splitext(script)[0]]
    return [sys.executable or "python", os.path.join(script_dir, script)]


def own_names():
    """Filenames in our folder that are ours and must never be treated as
    archives. The sorter shields these when a pool folder and the tool's
    folder are the same directory."""
    if frozen():
        return {GUI_EXE.lower(), CONSOLE_EXE.lower(),
                os.path.basename(sys.executable).lower()}
    return set()
