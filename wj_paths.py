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
purpose, so that any module can rely on it - which is also why the rules
here that are not about folders live here: which addresses the tools may
fetch at all (web_url_problem, urlopen), and how a file is put in place
without losing the one already there (move_file, publish).
"""

import errno
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

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


def window_exe():
    """The binary with no console, beside ourselves: the one to hand work
    that has nothing to say, so that Windows opens no console window for it.
    Ourselves when it has been deleted."""
    twin = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), GUI_EXE)
    return twin if os.path.isfile(twin) else sys.executable


def has_console():
    """Does this process have a console of its own? The console side does;
    the window, pythonw, and a stage the window started (no_window) do not."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:                   # no kernel32 to ask: assume the old way
        return True


def no_window():
    """Keyword arguments for subprocess that keep a console program - tasklist,
    wabbajack-cli, our own console binary - from opening a console window.

    Only for a process with no console to share. One started from the window
    got a console window of its own from Windows, flashing up for every
    tasklist and staying up for a whole install, though everything it printed
    went down a pipe to the log pane. A process that has a console hands it
    down as before, so the console side's children still print into it, and
    still read from it."""
    if has_console() or not hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {}
    return {"creationflags": subprocess.CREATE_NO_WINDOW}


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


# Checking the address a fetch starts from is not enough: urllib follows a
# server's redirect wherever it points, ftp: included, so an http address in
# a manifest could still have the downloader fetch from an FTP server on the
# network and count the file downloaded. And urllib copies every header onto
# the redirected request, so the Nexus API key would follow a redirect to
# another host. Every fetch goes through urlopen() below instead, whose
# opener knows no scheme but http and https and refuses to be redirected to
# anything else.
_PRIVATE_HEADERS = ("apikey", "authorization", "proxy-authorization", "cookie")


def _origin(url):
    try:
        parts = urllib.parse.urlsplit(url)
        return parts.scheme.lower(), (parts.hostname or "").lower(), parts.port
    except ValueError:
        return None


class _WebOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to another web address, and leaves a request's
    credentials behind when the redirect leaves the host it was sent to."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        why = web_url_problem(newurl)
        if why:
            raise urllib.error.HTTPError(req.full_url, code,
                                         f"refused a redirect to {newurl!r}: {why}",
                                         headers, fp)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _origin(newurl) != _origin(req.full_url):
            for name in list(new.headers):
                if name.lower() in _PRIVATE_HEADERS:
                    del new.headers[name]
        return new


def _web_opener():
    """urllib's opener with the web handlers only: no file:, ftp: or data:
    handler is there to be reached, whatever a caller or a server asks."""
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.ProxyHandler(), urllib.request.UnknownHandler(),
                    urllib.request.HTTPHandler(), urllib.request.HTTPSHandler(),
                    urllib.request.HTTPDefaultErrorHandler(), _WebOnlyRedirects(),
                    urllib.request.HTTPErrorProcessor()):
        opener.add_handler(handler)
    return opener


_OPENER = None


def urlopen(request, timeout=None):
    """urllib.request.urlopen for the web only: the address is checked here,
    and a redirect is followed only to another web address. Takes a URL or a
    Request; raises URLError for an address that is not a web one."""
    global _OPENER
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    why = web_url_problem(url)
    if why:
        raise urllib.error.URLError(f"refusing {url!r}: {why}")
    if _OPENER is None:
        _OPENER = _web_opener()
    if timeout is None:
        return _OPENER.open(request)
    return _OPENER.open(request, timeout=timeout)


# ----------------------------------------------------------------------
# Putting a file in place without losing the one already there
# ----------------------------------------------------------------------
# A file with a given name in a pool folder may be the build another modlist
# pins: a filename does not identify content, and Synthesis.zip sits in five
# folders on the share at five different sizes. So nothing that moves or
# publishes a file may overwrite one. The sorter always parked a different
# build under a '~N' name rather than replace it; the downloader deleted
# whatever stood at its destination, and that is how a build another list
# still needed went, with no trash and no record. These are the one
# implementation every tool now uses for it.

def unique_path(path):
    """`path` if neither it nor its .meta sidecar is there, else the first
    'Name~1.ext', 'Name~2.ext' ... beside it for which neither is. The '~N'
    is the disambiguator every tool reads through (wj_config.canonical_name),
    and checking the sidecar too means one moved along with its archive never
    lands on somebody else's."""
    def free(p):
        return not os.path.lexists(p) and not os.path.lexists(p + ".meta")
    if free(path):
        return path
    root, ext = os.path.splitext(path)
    i = 1
    while not free(f"{root}~{i}{ext}"):
        i += 1
    return f"{root}~{i}{ext}"


def _other_volume(err):
    return err.errno == errno.EXDEV or getattr(err, "winerror", None) == 17


def move_file(src, dst):
    """Move one file to `dst`, never over another: FileExistsError when
    `dst` is taken. On one volume this is a rename, and atomic. Across
    volumes the bytes go to a temporary '.part' name beside `dst` first, which
    no index counts, and are renamed into place before `src` is removed. The
    move happens or it does not: a copy that fails, or is interrupted, leaves
    no temporary file behind, and when `src` will not go once its copy is in
    place, the copy goes again - a caller told the move failed must not find
    the file in both places. Returns `dst`."""
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "there is already a file there", dst)
    parent = os.path.dirname(dst)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        os.rename(src, dst)
        return dst
    except OSError as e:
        if not _other_volume(e):
            raise
    tmp = f"{dst}.{os.getpid()}.part"
    try:
        shutil.copy2(src, tmp)
        if os.path.lexists(dst):
            raise FileExistsError(errno.EEXIST, "there is already a file there", dst)
        os.rename(tmp, dst)
    except BaseException:               # a full disk, a share gone, Ctrl+C
        try:
            os.remove(tmp)              # our own half of the copy, nothing else
        except OSError:
            pass
        raise
    try:
        os.remove(src)
    except OSError:
        try:
            os.remove(dst)              # the copy just made, so nothing is doubled
        except OSError:
            pass
        raise
    return dst


def move_folder(src, dst):
    """Move a whole folder by renaming it, never over another one. There is
    no copy-and-delete fallback: a folder that will not rename - a file in it
    held open by a running install, or `dst` on another volume - stays
    exactly where it was, rather than half copied and half deleted."""
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "there is already a folder there", dst)
    parent = os.path.dirname(dst)
    if parent:
        os.makedirs(parent, exist_ok=True)
    os.rename(src, dst)
    return dst


def set_aside(path):
    """Move the file at `path`, and its .meta sidecar, to the first free
    'Name~N.ext' beside it, and return where it went. This is how a file
    makes room for a different build under the same name: nothing goes, and
    every tool still finds it under the name its manifests use.

    The two move together or not at all - OSError, and both where they were.
    A sidecar left behind used to be written over by the new file's, and the
    build set aside lost the record of where it came from."""
    aside = unique_path(path)
    meta = path + ".meta"
    has_meta = os.path.isfile(meta)
    if has_meta:
        os.rename(meta, aside + ".meta")
    try:
        os.rename(path, aside)
    except OSError:
        if has_meta:
            try:
                os.rename(aside + ".meta", meta)
            except OSError:
                pass
        raise
    return aside


def publish(part, dest, same=None):
    """Give a finished, verified file its real name. Whatever already has
    that name is set aside under a '~N' name rather than replaced - unless
    `same(dest)` says it already holds these very bytes, when there is
    nothing to keep. Returns where the earlier file went, or None."""
    aside = None
    if os.path.lexists(dest) and not (same is not None and same(dest)):
        aside = set_aside(dest)
    os.replace(part, dest)
    return aside


def create_stamped(folder, prefix, suffix=".csv", when=None, tries=120):
    """A new file '<prefix><YYYYmmdd_HHMMSS><suffix>' in `folder`, opened for
    writing as UTF-8 text: (path, file). The stamp is `when`, or now - or,
    when a file of that name is already there, the next second's that is
    free. A record is never opened over another: one written in the same
    second as an earlier one used to replace it, and the files that earlier
    one accounted for could then be neither put back nor purged. Raises
    OSError."""
    when = when or datetime.now()
    for i in range(tries):
        stamp = (when + timedelta(seconds=i)).strftime("%Y%m%d_%H%M%S")
        path = os.path.join(folder, f"{prefix}{stamp}{suffix}")
        try:
            return path, open(path, "x", newline="", encoding="utf-8")
        except FileExistsError:
            continue
    raise FileExistsError(errno.EEXIST, "every name for a new record is taken",
                          os.path.join(folder, prefix + "*" + suffix))
