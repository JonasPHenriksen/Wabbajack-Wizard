#!/usr/bin/env python3
"""
Where the tool's own folder is, from source and from a build
============================================================

Every script here anchors its work to `os.path.dirname(__file__)`: the
settings file, the manifests, the pool root when it is written as a relative
path, and the runtime folder beside them holding the logs, the state file and
the lock. That is exactly right while the scripts are scripts.

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
purpose, so that any module can rely on it - which is also why the one rule
here that is not about folders lives here: which addresses the tools may
fetch at all (web_url_problem).
"""

import os
import sys
import urllib.parse

# The token that tells a frozen exe to be one of the sibling scripts instead
# of itself. Consumed by wj_launcher.py and never reaches an argument parser.
STAGE_FLAG = "--wj-stage="

# The two binaries a build produces. Same code, same bundle; they differ only
# in whether Windows gives them a console, exactly as python.exe and
# pythonw.exe differ.
GUI_EXE = "WabbajackWizard.exe"
CONSOLE_EXE = "WabbajackWizard-console.exe"

# The folder beside the scripts, or beside the exe, holding what the tools
# write as they run: the logs, the records of what stage 5 took, the state
# and the manifest cache, the lock, and the files a running assist session
# owns. They used to sit in the folder itself, a dozen or so beside the
# source, and they move here the first time a tool starts (adopt_runtime).
# The settings, the API key file, the manifests and the caches that are
# folders of their own stay where they are.
RUNTIME_DIR = "runtime"

# What adopt_runtime() moves: these names, and names with these prefix and
# suffix pairs - every file the tools write beside themselves as they run.
RUNTIME_NAMES = ("wj_autoupdate.log", "wj_autoupdate.log.1", "wj_autoupdate.log.2",
                 "wj_autoupdate_state.json", "wj_manifest_cache.json", "wj_overview.json",
                 ".wj_autoupdate.lock", ".nxm_listener_port", ".nxm_prev_handler",
                 "nxm_captured.txt")
RUNTIME_PATTERNS = (("wj_sorter_", ".log"), ("wj_downloader_", ".log"),
                    ("wj_autoupdate_", ".out"), ("deleted_", ".csv"),
                    ("purged_trash_", ".csv"), ("merged_", ".csv"),
                    ("download_failures_", ".csv"), ("nexus_todo_", ".csv"),
                    ("nexus_pages_", ".csv"))


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


def runtime_dir(module_file):
    """The folder this tool writes its runtime files in - RUNTIME_DIR in the
    folder app_dir() names - made if it is not there yet. Pass `__file__`."""
    d = os.path.join(app_dir(module_file), RUNTIME_DIR)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass            # the caller's own open() then says what is wrong
    return d


def is_runtime_file(name):
    """Is `name` one of the runtime files adopt_runtime() moves?"""
    low = name.lower()
    return (low in RUNTIME_NAMES
            or any(low.startswith(p) and low.endswith(s) for p, s in RUNTIME_PATTERNS))


def adopt_runtime(here):
    """Move the runtime files an older version left in the folder `here`
    (the tool's own, as app_dir() says) into its runtime folder. Returns the
    names moved.

    Cheap enough to run at every start - one listing of the folder - and a
    no-op once the move is done. A file that is already in the runtime
    folder is left where it is rather than overwritten, and so is one that
    will not move, being open in another process: the lock held by a run of
    the older version, say. Nothing is ever deleted."""
    dest = os.path.join(here, RUNTIME_DIR)
    moved = []
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError:
        return moved
    try:
        names = os.listdir(here)
    except OSError:
        return moved
    for n in names:
        if not is_runtime_file(n):
            continue
        src, dst = os.path.join(here, n), os.path.join(dest, n)
        if not os.path.isfile(src) or os.path.exists(dst):
            continue
        try:
            os.replace(src, dst)
            moved.append(n)
        except OSError:
            pass
    return moved


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


# The only kinds of address the tools fetch. Every address fetched comes from
# someone else - a manifest, an author's feed, the gallery - and urllib opens
# more than the web: a file: address in a manifest had the downloader copy any
# file this Windows account can read into the pool, on the share, and count it
# downloaded. ftp: and data: have no business here either.
WEB_SCHEMES = ("http", "https")


def web_url_problem(url):
    """Why `url` is not an address the tools may fetch, or None."""
    if not isinstance(url, str) or not url.strip():
        return "there is no address"
    try:
        scheme = urllib.parse.urlsplit(url.strip()).scheme.lower()
    except ValueError as e:
        return f"it is not an address ({e})"
    if scheme not in WEB_SCHEMES:
        return f"{scheme}: is not a web address" if scheme else "it is not a web address"
    return None
