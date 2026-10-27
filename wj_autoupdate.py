#!/usr/bin/env python3
"""
Wabbajack Modlist Auto-Updater
==============================
Front-end for wj_downloader.py and wj_sorter.py.
Runs on Windows against the mapped share. You launch it; it does the rest in
one pass.

What one run does
-----------------
  1. Takes a single-instance lock so a double-click cannot start two runs.
  2. Reads Wabbajack's public metadata feeds and compares each tracked list's
     published version and hash against a local state file.
  3. For every list that moved: downloads the new .wabbajack to a .part file,
     verifies it against the feed's Size and Hash, keeps the old one as .bak,
     then atomically replaces it. The filename never changes, so your archive
     folder layout stays stable.
  4. Runs the downloader for each changed list, in whichever NEXUS_MODE you
     configured.
  5. Runs the sorter once to rebuild the pools and stage one list.
  6. Writes a report plus nexus_todo_<list>.csv, and optionally notifies.

From the share (the usual setup)
--------------------------------
Put this file next to the two scripts and your .wabbajack files on the mapped
drive. Leave ARCHIVE_ROOT in wj_config.py as your mapped path, the
r"Z:\\Mod Archive" you already had. Then either double-click run.bat or:

    py -3 wj_autoupdate.py --check        # what has updates? changes nothing
    py -3 wj_autoupdate.py                # a real run

Tracking needs no configuration. Every .wabbajack in the folder is matched
against the published feeds on first sight and the result is cached, so adding
a list is just a matter of dropping the file in. --discover shows the matching
if you want to inspect it; --rediscover forces a re-match.

Set NEXUS_MODE to decide what happens to the Nexus archives. "skip" leaves
them to Wabbajack, which is simplest. "assist" hands them to your downloader's
--nxm-assist mode, which opens each mod page and captures your click; it is
still one click per file, the same manual step Wabbajack asks of you.

Requirements
------------
  * Python 3.8+ (setup_env.bat builds the environment run.bat uses)
  * The two existing scripts, in the same folder as this one.
  * xxhash, so manifests and archives get real hash verification instead of
    a size check. Refused without it unless --allow-size-only is passed.
"""

import os
import re
import csv
import sys
import json
import time
import shutil
import base64
import struct
import zipfile
import logging
import logging.handlers
import argparse
import itertools
import importlib.util
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import concurrent.futures
from datetime import datetime, timedelta, timezone

import wj_paths

try:
    import wj_config as CFG
except ImportError:
    sys.stderr.write("wj_config.py is missing from the folder holding these scripts. "
                     "It defines the archive layout and every setting, and is not "
                     "optional." + chr(10))
    raise SystemExit(2)


# ============================ CONFIGURATION ============================

# The two existing scripts. They must sit next to this file, which is also
# where the .wabbajack manifests live.
DOWNLOADER_SCRIPT = "wj_downloader.py"
SORTER_SCRIPT     = "wj_sorter.py"

# ARCHIVE_ROOT is NOT configured here. The archive layout - ARCHIVE_ROOT, the
# game folders, the pool names, the excluded folders - lives in wj_config.py,
# which the downloader, the sorter and the wizard all import. Edit it there.
# On Linux use the local mount path, not the //server/share SMB path.

# Every user setting lives in wj_config.py, with its default, and in
# wj_settings.json beside it with your overrides. They are copied here so the
# rest of this module, and the wizard, keep reading them as module attributes;
# reload_settings() refreshes the copies after the GUI saves the file.
AUTO_TRACK = CFG.AUTO_TRACK
MANIFEST_DIR = CFG.MANIFEST_DIR
EXCLUDE = list(CFG.EXCLUDE)
TRACK = list(CFG.TRACK)
STAGE_LIST = CFG.STAGE_LIST
NEXUS_MODE = CFG.NEXUS_MODE
KEEP_BACKUP_DAYS = CFG.KEEP_BACKUP_DAYS
REQUIRE_HASH = CFG.REQUIRE_HASH
DOWNLOAD_ORDER = CFG.DOWNLOAD_ORDER
FREE_SPACE_MARGIN_GB = CFG.FREE_SPACE_MARGIN_GB
NOTIFY_URL = CFG.NOTIFY_URL

# The sorter skips "*.part" when indexing, so a leftover part file from an
# interrupted download is left where it is and its resume progress survives.
# This flag stays only as an escape hatch for an older sorter.
SORTER_IGNORES_PART = True

# How long to remember that a local .wabbajack matched no published list before
# trying again. A negative result is never cached at all when any feed failed to
# load during that run - otherwise one flaky request from GitHub silently drops
# a modlist from tracking for good.
NEGATIVE_MATCH_TTL_DAYS = 14

# Wabbajack's registry of metadata feeds. WJ_FEED_REGISTRY names another one:
# the test suites serve their own on localhost, so a whole unattended run can
# be checked without reaching GitHub. Fetched like every feed, so it too has
# to be a web address.
REPOSITORIES_URL = os.environ.get("WJ_FEED_REGISTRY") or (
    "https://raw.githubusercontent.com/wabbajack-tools/mod-lists/master/repositories.json")

HTTP_TIMEOUT = 30
USER_AGENT   = CFG.USER_AGENT
STATE_FILE   = "wj_autoupdate_state.json"
LOCK_FILE    = ".wj_autoupdate.lock"
# =======================================================================

NEWLINE = chr(10)
HERE = wj_paths.app_dir(__file__)
LOG = logging.getLogger("wj_autoupdate")

# --dry-run: plan everything, change nothing. Set by main(); while it is
# set, nothing is written - not the state file, not a to-do CSV - nothing
# is pruned or purged, and no notification is sent. A dry run used to
# delete expired backups and send a "fetched" report like a real one.
DRY_RUN = False


def runtime_path(name=None, make=True):
    """Where one of the files this tooling writes as it runs lives - the
    runtime folder beside the scripts, see wj_paths.RUNTIME_DIR - or that
    folder itself. Worked out from HERE at the moment of asking, so that
    moving HERE, as every sandbox test does, moves these with it. The folder
    is made if it is not there, unless `make` is off, for a caller that only
    reads."""
    d = os.path.join(HERE, wj_paths.RUNTIME_DIR)
    if make:
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass        # the caller's own open() then says what is wrong
    return os.path.join(d, name) if name else d

# Filled in by resolve_dirs() during startup.
MANIFESTS = HERE          # where the .wabbajack files are
BACKUPS = HERE            # where superseded/rejected manifests are parked

# Wabbajack CDN multi-part support. Manifests on authored-files.wabbajack.org
# are served as a definition plus numbered parts, not as a plain file, so a
# straight GET returns a ~1 KB error page.
try:
    import wj_cdn as CDN
    _HAVE_CDN = True
except Exception:
    CDN = None
    _HAVE_CDN = False


# ----------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------
_HANDLERS = []


def setup_logging(verbose=False):
    LOG.setLevel(logging.DEBUG)
    con = logging.StreamHandler(sys.stdout)
    con.setLevel(logging.DEBUG if verbose else logging.INFO)
    con.setFormatter(logging.Formatter("%(message)s"))
    LOG.addHandler(con)
    _HANDLERS.append(con)
    try:
        # Rotated, because nothing else ever trims this file: the two child
        # scripts keep only their previous and current logs, while this one
        # had grown without limit. 5 MB is a few hundred runs.
        fh = logging.handlers.RotatingFileHandler(
            runtime_path("wj_autoupdate.log"), encoding="utf-8",
            maxBytes=5 * 1024 * 1024, backupCount=2)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        LOG.addHandler(fh)
        _HANDLERS.append(fh)
    except OSError as e:
        LOG.warning("[WARN] Could not open log file: %s", e)

    # wj_cdn logs to its own logger, which has no handler of its own.
    # Without this its messages reach nothing but logging.lastResort on stderr,
    # so the log file records "Manifest download failed" with no cause - the
    # single most useful line (an HTTP 404 on the CDN definition, say) is the
    # one that goes missing. Same treatment for the downloader in adopt_logger().
    if CDN is not None:
        adopt_logger(CDN.LOG)


def adopt_logger(child):
    """Route another module's logger into our handlers.

    Keeps the child at INFO so its progress chatter does not drown the run, but
    its warnings and errors land in wj_autoupdate.log where they belong."""
    child.setLevel(logging.INFO)
    child.propagate = False
    for h in _HANDLERS:
        if h not in child.handlers:
            child.addHandler(h)


def banner(title):
    LOG.info("\n%s\n%s\n%s", "=" * 58, title, "=" * 58)


# ----------------------------------------------------------------------
# Working out where things live
# ----------------------------------------------------------------------
def _version_key(name):
    """Sort key that orders 4.9 before 4.10 rather than after it."""
    return [int(n) for n in re.findall(r"\d+", name)] or [-1]


def looks_absolute(path):
    """Absolute-path test that recognises Windows forms even when this is
    running on Linux, where posixpath would call 'Z:\\x' relative."""
    if not path:
        return False
    return (os.path.isabs(path)
            or path.startswith("\\\\") or path.startswith("//")
            or (len(path) > 1 and path[1] == ":"))


def resolve_manifest_dir(spec):
    """Turn MANIFEST_DIR into a concrete folder.

    A spec containing '*' is globbed and the highest version-numbered match
    wins, so Wabbajack's versioned folder can be followed without hard-coding
    the version. If the newest match holds no manifests but an older one does,
    the populated one is used and the situation is reported, because that is
    exactly what a fresh Wabbajack upgrade looks like."""
    if not spec:
        return HERE
    spec = os.path.expandvars(os.path.expanduser(str(spec)))
    if not looks_absolute(spec):
        spec = os.path.join(HERE, spec)
    if "*" not in spec and "?" not in spec:
        return os.path.normpath(spec)

    import glob as _glob
    matches = [m for m in _glob.glob(spec) if os.path.isdir(m)]
    if not matches:
        LOG.error("[TERMINATED] MANIFEST_DIR pattern matched nothing: %s", spec)
        return None
    matches.sort(key=lambda m: _version_key(os.path.basename(os.path.dirname(m.rstrip("/\\")))
                                            or os.path.basename(m)))
    newest = matches[-1]
    populated = [m for m in matches
                 if any(f.lower().endswith(".wabbajack") for f in os.listdir(m))]
    if newest not in populated and populated:
        LOG.warning("[WARN] %s holds no manifests (a fresh Wabbajack install "
                    "does this). Using %s instead.", newest, populated[-1])
        return populated[-1]
    if len(matches) > 1:
        LOG.info("MANIFEST_DIR matched %d folder(s); using the newest.", len(matches))
    return newest


def resolve_dirs():
    """Set MANIFESTS and BACKUPS. Returns False on a fatal path problem."""
    global MANIFESTS, BACKUPS
    d = resolve_manifest_dir(MANIFEST_DIR)
    if d is None:
        return False
    d = os.path.normpath(d)
    if not os.path.isdir(d):
        # Only create folders that sit inside our own, so a mistyped path
        # elsewhere is reported instead of quietly conjured up.
        inside = os.path.normcase(os.path.abspath(d)).startswith(
            os.path.normcase(os.path.abspath(HERE)) + os.sep)
        if inside:
            try:
                os.makedirs(d, exist_ok=True)
                LOG.info("Created manifest folder %s", d)
            except OSError as e:
                LOG.error("[TERMINATED] Cannot create %s: %s", d, e)
                return False
        else:
            LOG.error("[TERMINATED] Manifest folder does not exist: %s\n"
                      "Create it, or fix MANIFEST_DIR at the top of this script.", d)
            return False
    MANIFESTS = os.path.abspath(d)
    # Everything that imports the downloader shares one idea of which pool
    # subfolders are modlist folders. Without this the wizard would index a
    # stray folder that the sorter never scans, and call its contents present.
    if WJ is not None:
        WJ.set_known_list_dirs(WJ.known_list_dirs_from(MANIFESTS))
    BACKUPS = os.path.join(HERE, "manifest_backups")
    try:
        os.makedirs(BACKUPS, exist_ok=True)
    except OSError as e:
        LOG.warning("[WARN] Cannot create %s (%s); backups will sit beside the "
                    "manifests instead.", BACKUPS, e)
        BACKUPS = MANIFESTS
    return True


# ----------------------------------------------------------------------
# Import the downloader as a module so manifest parsing, the existing-file
# index, hashing and the pool layout all come from one implementation. The
# downloader guards its Windows-only imports and only runs main() under
# __main__, so importing it is side-effect free.
# ----------------------------------------------------------------------
def sibling_module(script, alias):
    """Load one of our own scripts as a module object.

    From source it is read from the .py file next to us, deliberately as a
    fresh module under its own name: the point is to see what that file says
    right now, on this disk, rather than what an earlier import cached.

    Frozen there is no .py file to read and no local edit to catch - the code
    is sealed in the bundle - so the module is imported the ordinary way.
    Raises FileNotFoundError if the script is missing; the callers each have
    their own thing to say about that."""
    if wj_paths.frozen():
        return importlib.import_module(os.path.splitext(script)[0])
    path = os.path.join(HERE, script)
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_downloader_module():
    try:
        mod = sibling_module(DOWNLOADER_SCRIPT, "wj_dl")
    except FileNotFoundError:
        LOG.error("[TERMINATED] %s not found next to this script.", DOWNLOADER_SCRIPT)
        sys.exit(1)
    except Exception as e:
        LOG.error("[TERMINATED] Could not import %s: %s", DOWNLOADER_SCRIPT, e)
        sys.exit(1)
    # Keep its chatter out of our log except for real problems, but make sure
    # those problems actually reach the log file rather than vanishing into
    # logging.lastResort on stderr.
    adopt_logger(mod.LOG)
    mod.LOG.setLevel(logging.WARNING)
    return mod


WJ = None  # populated in main()


def check_script_consistency():
    """Confirm the downloader and the sorter really do share one layout.

    Both now take ARCHIVE_ROOT, GAME_FOLDERS and EXCLUDE_POOL_DIRS from
    wj_config.py, so this should be true by construction. It is still checked
    on every run, cheaply, because the failure it guards against is silent and
    expensive: the downloader decides where an archive is WRITTEN and the
    sorter decides where it is MOVED TO, so a local override re-added to one of
    them sends every run across two different directory trees while both report
    success.

    Frozen there is nothing left to catch - one bundle holds one copy of each
    module, so the two cannot disagree - and the check costs a comparison of
    values already in memory. It is left in rather than made conditional: one
    code path, and it stays honest if the build ever gains a second source of
    settings."""
    if not wj_paths.frozen() and not os.path.isfile(os.path.join(HERE, "wj_config.py")):
        LOG.error("[TERMINATED] wj_config.py is missing from %s. It defines the "
                  "archive layout and is not optional.", HERE)
        return False
    try:
        mod = sibling_module(SORTER_SCRIPT, "wj_sort_cfg")
    except FileNotFoundError:
        LOG.error("[TERMINATED] %s not found next to this script.", SORTER_SCRIPT)
        return False
    except Exception as e:
        LOG.error("[TERMINATED] Could not read the sorter's configuration: %s", e)
        return False

    ok = True
    if os.path.normcase(str(mod.ARCHIVE_ROOT)) != os.path.normcase(str(WJ.ARCHIVE_ROOT)):
        LOG.error("[TERMINATED] ARCHIVE_ROOT differs between the two scripts:")
        LOG.error("    %-40s %s", DOWNLOADER_SCRIPT, WJ.ARCHIVE_ROOT)
        LOG.error("    %-40s %s", SORTER_SCRIPT, mod.ARCHIVE_ROOT)
        ok = False
    if dict(mod.GAME_FOLDERS) != dict(WJ.GAME_FOLDERS):
        LOG.error("[TERMINATED] GAME_FOLDERS differs between the two scripts, so "
                  "they would use different pool folders for the same game.")
        for g in sorted(set(mod.GAME_FOLDERS) | set(WJ.GAME_FOLDERS)):
            a, b = WJ.GAME_FOLDERS.get(g), mod.GAME_FOLDERS.get(g)
            if a != b:
                LOG.error("    %-28s downloader=%-18s sorter=%s", g, a, b)
        ok = False
    a = set(getattr(WJ, "EXCLUDE_POOL_DIRS", ()) or ())
    b = set(getattr(mod, "EXCLUDE_POOL_DIRS", ()) or ())
    if a != b:
        LOG.error("[TERMINATED] EXCLUDE_POOL_DIRS differs: downloader=%s sorter=%s. "
                  "A folder excluded from one but not the other is worse than "
                  "excluding it from neither.", sorted(a), sorted(b))
        ok = False
    return ok


# ----------------------------------------------------------------------
# Single-instance lock, portable across Windows and Linux
# ----------------------------------------------------------------------
HELD = object()          # someone else has it
UNAVAILABLE = object()   # this filesystem cannot lock; caller decides


def acquire_lock(path):
    """Best-effort exclusive lock. Returns a file object, HELD, or UNAVAILABLE.

    msvcrt.locking is unreliable over SMB, so a failure to lock at all is
    reported as UNAVAILABLE rather than treated as contention. With a single
    person launching runs by hand that is fine; the lock only exists to stop
    an accidental double-click."""
    try:
        fd = open(path, "a+")
    except OSError:
        return UNAVAILABLE
    try:
        import msvcrt
        fd.seek(0)
        msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
    except (OSError, BlockingIOError):
        fd.close()
        return HELD
    except Exception:
        fd.close()
        return UNAVAILABLE
    try:
        fd.write(f"{os.getpid()} {datetime.now().isoformat()}\n")
        fd.flush()
    except OSError:
        pass
    return fd


def release_lock(fd):
    if fd is None or fd is HELD or fd is UNAVAILABLE:
        return
    try:
        import msvcrt
        fd.seek(0)
        msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception:
        pass
    try:
        fd.close()
    except OSError:
        pass


# ----------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------
def http_json(url):
    # The registry names each author's feed, and the feeds are fetched from
    # wherever it says: only web addresses (wj_paths.web_url_problem).
    why = wj_paths.web_url_problem(url)
    if why:
        raise ValueError(f"refusing {url!r}: {why}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    with wj_paths.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        raw = r.read()
    return json.loads(raw.decode("utf-8-sig"))


def _without_credentials(text, url):
    """`text` with the user and password `url` carries, if any, masked: an
    error about the URL quotes it, and the log sits on a share."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return text
    secret = parts.netloc.rpartition("@")[0]
    return text.replace(secret, "***") if secret else text


# Set when a run has sent its own report, so main() does not send a second.
_NOTIFIED = False


def notify(title, body):
    global _NOTIFIED
    if not NOTIFY_URL or DRY_RUN:
        return
    why = wj_paths.web_url_problem(NOTIFY_URL)
    if why:
        LOG.warning("[WARN] NOTIFY_URL is not used: %s.", why)
        return
    try:
        if "discord.com" in NOTIFY_URL or "discordapp.com" in NOTIFY_URL:
            payload = json.dumps({"content": f"**{title}**\n{body}"[:1900]}).encode()
            headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        else:  # ntfy
            payload = body.encode("utf-8")
            headers = {"Title": title, "User-Agent": USER_AGENT}
        req = urllib.request.Request(NOTIFY_URL, data=payload, headers=headers)
        with wj_paths.urlopen(req, timeout=HTTP_TIMEOUT):
            pass
        _NOTIFIED = True
        LOG.debug("Notification sent.")
    except Exception as e:
        LOG.warning("[WARN] Notification failed: %s",
                    _without_credentials(f"{type(e).__name__}: {e}", NOTIFY_URL))


# ----------------------------------------------------------------------
# Feed handling
# ----------------------------------------------------------------------
def iter_entries(data):
    """Yield modlist entry dicts from a feed. Author-run feeds are not all
    shaped identically, so accept a bare array or a dict wrapping one."""
    if isinstance(data, list):
        for e in data:
            if isinstance(e, dict):
                yield e
    elif isinstance(data, dict):
        if any(k in data for k in ("links", "download_metadata", "machineURL")):
            yield data
            return
        for v in data.values():
            if isinstance(v, list):
                for e in v:
                    if isinstance(e, dict):
                        yield e


def entry_field(entry, *names):
    """Look a field up at the top level and inside `links`."""
    links = entry.get("links") if isinstance(entry.get("links"), dict) else {}
    for n in names:
        if entry.get(n) not in (None, ""):
            return entry[n]
        if links.get(n) not in (None, ""):
            return links[n]
    return None


def normalise(entry, repo):
    """One feed entry in the shape every tool here uses, or None.

    The feeds are their authors' own files, and the key made from repo and
    machineURL becomes a path: the gallery keeps each list's report at
    reports/<repo>/<machineURL> in its cache. A machineURL that is not a
    plain name is refused here, with the entry."""
    mu = entry_field(entry, "machineURL")
    if not mu:
        return None
    why = CFG.plain_name_problem(str(mu))
    if why:
        LOG.warning("[WARN] Feed '%s' lists a modlist whose machineURL %r is not a plain "
                    "name (%s); it is left out.", repo, str(mu), why)
        return None
    dm = entry.get("download_metadata")
    dm = dm if isinstance(dm, dict) else {}
    return {
        "repo":     repo,
        "key":      f"{repo}/{mu}",
        "machine":  str(mu),
        "title":    str(entry.get("title") or mu),
        "game":     str(entry.get("game") or ""),
        "version":  str(entry.get("version") or ""),
        "updated":  str(entry.get("dateUpdated") or ""),
        "download": entry_field(entry, "download"),
        "hash":     dm.get("Hash"),
        "size":     dm.get("Size") if isinstance(dm.get("Size"), int) else None,
        "archives": dm.get("NumberOfArchives"),
        "arch_size": dm.get("SizeOfArchives") if isinstance(dm.get("SizeOfArchives"), int) else None,
        # Presentation flags. The updater ignores all four, but the browse
        # stage needs them and re-reading 149 feeds to get them would be
        # absurd. force_down in particular is half of Wabbajack's own
        # "Unavailable" test - dropping it here silently disarmed that check.
        "force_down": bool(entry.get("force_down")),
        "nsfw": bool(entry.get("nsfw")),
        "official": bool(entry.get("official")),
        "tags": [str(t) for t in (entry.get("tags") or [])
                 if isinstance(t, (str, int))],
    }


# One complete all-feeds fetch per process, reused for later subset requests.
# The browse stage reads all 149 feeds to show the gallery, and the update
# stage then asks for the handful of repos it tracks; without this the second
# ask re-fetches feeds the first one already has, and 149 requests is the part
# of a run that visibly takes time.
#
# Reused for FEED_SNAPSHOT_TTL at most. The window can stay open all day, and
# a snapshot that never expired meant "Check the feeds" never saw a version
# published after its first look - nor did the "any known modlist" rule. A
# snapshot with a feed missing is reused only FEED_RETRY_S, so the next ask
# tries that feed again instead of refusing until a restart. The window's
# refresh buttons start a fresh one (reset_feed_cache).
FEED_SNAPSHOT_TTL = 6 * 3600
FEED_RETRY_S = 300
_FEED_CACHE = {"all": None, "complete": False, "at": None, "failed": None}


def reset_feed_cache():
    _FEED_CACHE["all"] = None
    _FEED_CACHE["complete"] = False
    _FEED_CACHE["at"] = None
    _FEED_CACHE["failed"] = None


def _snapshot_fresh():
    at = _FEED_CACHE.get("at")
    if at is None:          # put there by hand - as the test suites do - and kept
        return True
    limit = FEED_SNAPSHOT_TTL if _FEED_CACHE["complete"] else FEED_RETRY_S
    return time.time() - at < limit


def _feed_shape_ok(data):
    """Does a feed have a shape iter_entries() reads in full? A list of
    entries - an empty one included, since an author can publish nothing -
    one bare entry, or an object wrapping lists of them. Anything else, or a
    list holding something that is not an entry, is a feed whose lists could
    not all be read, which the "any known modlist" rule must not take for a
    feed with nothing in it."""
    if isinstance(data, list):
        return all(isinstance(e, dict) for e in data)
    if isinstance(data, dict):
        if any(k in data for k in ("links", "download_metadata", "machineURL")):
            return True
        lists = [v for v in data.values() if isinstance(v, list)]
        return bool(lists) and all(isinstance(e, dict) for v in lists for e in v)
    return False


def load_feeds(repo_names=None):
    """Fetch repositories.json, then the named feeds (or all of them).
    Returns ({key: normalised_entry}, complete) where `complete` is False if
    any feed failed to load. A caller must not draw a negative conclusion - "no
    published list matches this file" - from an incomplete picture, because one
    flaky request would then untrack a modlist for good."""
    if _FEED_CACHE["all"] is not None and _snapshot_fresh():
        cached, complete = _FEED_CACHE["all"], _FEED_CACHE["complete"]
        if repo_names is None:
            return dict(cached), complete
        want = set(repo_names)
        subset = {k: v for k, v in cached.items() if v["repo"] in want}
        missing = want - {v["repo"] for v in cached.values()}
        if missing:
            LOG.warning("[WARN] Unknown repo(s) in TRACK, ignoring: %s",
                        ", ".join(sorted(missing)))
        LOG.info("Reusing this run's feed snapshot (%d of %d list(s)).",
                 len(subset), len(cached))
        # Complete for these feeds, when it is known which ones failed: one
        # of the other 149 failing used to make a run whose every tracked
        # feed answered say that not all of them had, and exit 1.
        failed = _FEED_CACHE.get("failed")
        if not complete and failed is not None:
            complete = not (want & failed)
        return subset, complete

    LOG.info("Fetching feed registry...")
    try:
        repos = http_json(REPOSITORIES_URL)
    except Exception as e:
        LOG.error("[ERROR] Could not read repositories.json: %s", e)
        return {}, False
    if not isinstance(repos, dict):
        LOG.error("[ERROR] repositories.json was not an object.")
        return {}, False

    wanted = {r: u for r, u in repos.items()
              if repo_names is None or r in repo_names}
    # A repository's name is the first half of every key it publishes, and
    # so of a path in the gallery cache; see normalise(). One left out is a
    # gap in the picture exactly as a feed that did not answer is, so it
    # counts as failed: the "any known modlist" rule must not delete on it.
    refused = [r for r in wanted if CFG.plain_name_problem(str(r))]
    for r in refused:
        LOG.warning("[WARN] repositories.json names a feed %r that is not a plain "
                    "name; it is left out.", r)
        del wanted[r]
    missing = set(repo_names or []) - set(wanted) - set(refused)
    if missing:
        LOG.warning("[WARN] Unknown repo(s) in TRACK, ignoring: %s",
                    ", ".join(sorted(missing)))
    LOG.info("Fetching %d feed(s)...", len(wanted))

    out = {}
    failed = [str(r) for r in refused]

    def grab(item):
        repo, url = item
        try:
            return repo, http_json(url), None
        except Exception as e:
            return repo, None, e

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for repo, data, err in pool.map(grab, wanted.items()):
            if err is not None:
                LOG.warning("[WARN] Feed '%s' unavailable: %s", repo, err)
                failed.append(repo)
                continue
            n = unread = 0
            # A feed that answered is not a feed that was read. Its shape, or
            # an entry in it, may be one this version cannot read - a format
            # that moved on drops the machineURL, say - and then its lists are
            # missing from the picture exactly as a feed that timed out is.
            # Counted as a feed that did not answer, so the "any known
            # modlist" rule refuses rather than deleting on the gap.
            short = not _feed_shape_ok(data)
            for raw in iter_entries(data):
                ent = normalise(raw, repo)
                if ent:
                    out[ent["key"]] = ent
                    n += 1
                else:
                    unread += 1
            if unread:
                LOG.warning("[WARN] Feed '%s' has %d entr%s this version cannot read; it "
                            "counts as a feed that did not answer.", repo, unread,
                            "y" if unread == 1 else "ies")
                short = True
            elif short:
                LOG.warning("[WARN] Feed '%s' is not shaped as a feed this version reads; it "
                            "counts as a feed that did not answer.", repo)
            if short:
                failed.append(repo)
            LOG.debug("  %-28s %d list(s)", repo, n)
    LOG.info("Indexed %d list(s) across %d feed(s).", len(out), len(wanted))
    if failed:
        LOG.warning("[WARN] %d feed(s) could not be read this run, or were left out "
                    "(%s). The picture is incomplete, so nothing will be recorded as "
                    "permanently unmatched.", len(failed), ", ".join(sorted(failed)))
    # Only a request for EVERY feed produces a snapshot later subset requests
    # can be served from. Caching a subset would make the next caller think a
    # repo it never asked about is genuinely absent.
    if repo_names is None:
        _FEED_CACHE["all"] = dict(out)
        _FEED_CACHE["complete"] = not failed
        _FEED_CACHE["at"] = time.time()
        _FEED_CACHE["failed"] = {str(r) for r in failed}
    return out, not failed


# ----------------------------------------------------------------------
# Local manifests and state
# ----------------------------------------------------------------------
def manifest_name(path):
    """The modlist's own Name field, for matching against feed titles."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            t = next((m for m in z.namelist()
                      if os.path.basename(m).lower() == "modlist"), None)
            if not t:
                return None
            with z.open(t) as fh:
                data = json.load(fh)
        return str(data.get("Name")) if isinstance(data, dict) else None
    except Exception:
        return None


def manifest_version(path):
    """The modlist's own Version field, for a list no feed describes."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as z:
            t = next((m for m in z.namelist()
                      if os.path.basename(m).lower() == "modlist"), None)
            if not t:
                return None
            with z.open(t) as fh:
                data = json.load(fh)
        v = data.get("Version") if isinstance(data, dict) else None
        return str(v) if v else None
    except Exception:
        return None


def load_state():
    p = runtime_path(STATE_FILE)
    try:
        with open(p, encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {"lists": {}}
    except (OSError, json.JSONDecodeError):
        return {"lists": {}}


def save_state(state):
    if DRY_RUN:
        LOG.debug("State not saved: a dry run changes nothing.")
        return
    p = runtime_path(STATE_FILE)
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, p)
    except OSError as e:
        LOG.error("[ERROR] Could not write state file: %s", e)


def manifest_filename_for(key, entry):
    """What to call a manifest we do not already hold a copy of.

    Follows the '<repo>_@@_<machineURL>.wabbajack' convention the rest of the
    folder uses, so the new file lines up with its pool folder and resolves
    straight back to the same feed key on the next run. Naming it after the
    feed title instead would produce 'LoreRim.wabbajack' next to a
    'LoreRim_@@_LoreRim' pool folder, and the two would drift apart."""
    stem = key.replace("/", "_@@_")
    cleaned = safe_filename(stem)
    if cleaned[:-len(".wabbajack")] == stem:
        return cleaned
    return safe_filename(entry["title"])


def safe_filename(title):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")
    return (name or "modlist") + ".wabbajack"


# ----------------------------------------------------------------------
# Manifest download
# ----------------------------------------------------------------------
def CDN_LOOKS_NEEDED(url):
    """Cheap host check used only to give a clear error when wj_cdn.py
    is absent."""
    return isinstance(url, str) and (
        "wabbajack.org" in url or "b-cdn.net" in url)


def verify_manifest(path, size, hash_b64, known_hash=None):
    """Check a manifest against the feed's Size and Hash. (ok, why).

    known_hash is the file's hash if something already computed it while
    writing the file - the CDN downloader hashes every assembled file before
    it renames it. Reusing that saves re-reading a multi-GB file off the share
    for no new information.

    Nothing to compare is not a match, and neither is a hash that could not
    be read: both used to pass. A feed entry with neither size nor hash then
    had any local file adopted as its published build - an old version
    included, which was never updated again - and one the file could not be
    hashed for was taken on trust."""
    if size is None and not hash_b64:
        return False, "the feed gives no size or hash to compare it with"
    try:
        actual = os.path.getsize(path)
    except OSError as e:
        return False, f"cannot stat ({e})"
    if size is not None and actual != size:
        return False, f"size {actual} != feed's {size}"
    if hash_b64 and known_hash:
        if known_hash != hash_b64:
            return False, "hash mismatch"
        return True, "ok (hashed while downloading)"
    if hash_b64 and getattr(WJ, "_HAVE_XXHASH", False):
        got = WJ.wabbajack_hash(path)
        if got is None:
            return False, "the file could not be read to hash it"
        if got != hash_b64:
            return False, "hash mismatch"
    elif hash_b64:
        return True, "ok (size only; install xxhash for hash checks)"
    return True, "ok"


def manifest_problem(path):
    """Why the file at `path` is not a readable .wabbajack, or None.

    A manifest is a zip holding a 'modlist' member. A host that answers with
    an error page and a 200, or a download that stopped short, is neither -
    and a feed that states no size or hash leaves this the only check there
    is before it replaces the manifest you have."""
    try:
        with zipfile.ZipFile(path) as z:
            if not any(os.path.basename(m).lower() == "modlist" for m in z.namelist()):
                return "it holds no 'modlist' manifest"
    except (zipfile.BadZipFile, OSError, ValueError) as e:
        return f"it is not a readable .wabbajack ({e})"
    return None


def fetch_manifest(entry, filename):
    """Fetch entry's .wabbajack and put it in place without ever leaving a gap.

    The order matters. The old code moved your existing manifest aside FIRST
    and downloaded second, so anything that stopped the process mid-transfer -
    Ctrl+C, a dropped share, a reboot - left you with no manifest at all and
    only a half-written .part file. A multi-GB list then had to be fetched
    again from zero.

    Here the new copy is downloaded to a staging name and fully verified before
    anything touches the file you already have. Until the final rename, an
    interruption costs you nothing but the partial download, which resumes."""
    dest = os.path.join(MANIFESTS, filename)
    staging = dest + ".new"
    url = entry["download"]
    if not url:
        LOG.error("  [ERROR] '%s' has no download link in its feed.", entry["title"])
        return False

    LOG.info("  Downloading %s v%s (%s)", filename, entry["version"],
             WJ.human_bytes(entry["size"]) if entry["size"] else "size unknown")

    # A staging copy an interrupted run left behind is ours, and was never
    # verified: it goes, rather than being kept beside the new download.
    if os.path.isfile(staging):
        try:
            os.remove(staging)
        except OSError as e:
            LOG.error("  [ERROR] Could not clear %s left by an earlier run: %s",
                      os.path.basename(staging), e)
            return False

    # Nearly every published manifest lives on the Wabbajack CDN, which needs
    # the multi-part protocol rather than a plain GET.
    cdn_hash = None
    if _HAVE_CDN and (CDN.is_cdn_url(url) or CDN.looks_multipart(url)):
        # strict_hash: a CDN serving another build than the feed describes is
        # refused before the transfer, not after several GB of it.
        ok, cdn_hash = CDN.download(url, staging, expected_size=entry["size"],
                                    expected_hash=entry["hash"], detail=True,
                                    strict_hash=bool(entry["hash"]))
    else:
        if not _HAVE_CDN and CDN_LOOKS_NEEDED(url):
            LOG.error("  [ERROR] %s is a Wabbajack CDN URL but wj_cdn.py "
                      "is missing from %s. Put it next to this script.",
                      filename, HERE)
            return False
        ok = WJ.stream_download(WJ.sanitize_url(url), staging,
                                expected_size=entry["size"])
    if not ok:
        LOG.error("  [ERROR] Manifest download failed for %s "
                  "(your existing copy is untouched)", filename)
        return False

    if entry["size"] is None and not entry["hash"]:
        ok, why = True, "the feed states no size or hash; the file is checked as a manifest only"
    else:
        ok, why = verify_manifest(staging, entry["size"], entry["hash"],
                                  known_hash=cdn_hash)
    if ok:
        bad = manifest_problem(staging)
        if bad:
            ok, why = False, bad
    if not ok:
        LOG.error("  [ERROR] %s failed verification: %s - discarding.", filename, why)
        quarantine = os.path.join(BACKUPS, filename + ".rejected")
        try:
            if os.path.exists(quarantine):
                os.remove(quarantine)
            os.replace(staging, quarantine)
            LOG.error("  Rejected copy kept at %s for inspection.",
                      os.path.basename(quarantine))
        except OSError:
            try:
                os.remove(staging)
            except OSError:
                pass
        return False

    LOG.info("  Verified: %s", why)

    # Only now is the existing manifest touched, and only once it is kept: a
    # manifest whose backup cannot be made is not replaced. It used to be
    # replaced all the same, with no backup at all - every time, wherever the
    # manifests and manifest_backups sat on different volumes.
    try:
        kept = backup_existing(filename)
    except OSError as e:
        LOG.error("  [ERROR] %s is not replaced: the version you have could not be kept as "
                  "a backup first (%s). The download is discarded; the next run tries "
                  "again.", filename, e)
        try:
            os.remove(staging)
        except OSError:
            pass
        return False
    try:
        os.replace(staging, dest)
    except OSError as e:
        LOG.error("  [ERROR] Could not put %s in place: %s", filename, e)
        if kept and not os.path.lexists(dest):
            try:
                wj_paths.move_file(kept, dest)
                LOG.info("  Restored the previous manifest.")
            except OSError:
                pass
        return False
    return True


def prune_backups():
    """Drop superseded manifests once they are too old to be a useful rollback.

    These are whole modlist manifests - several GB each - so keeping one per
    list forever quietly eats more space than the archives some lists need.
    A .bak's age is time since its manifest was replaced, which
    backup_existing stamps on it; a .rejected one's is time since it was
    downloaded, which is also when it was rejected."""
    if BACKUPS == MANIFESTS or DRY_RUN:
        return
    cutoff = time.time() - KEEP_BACKUP_DAYS * 86400
    freed = 0
    try:
        names = os.listdir(BACKUPS)
    except OSError:
        return
    for n in names:
        if not n.lower().endswith((".bak", ".rejected")):
            continue
        p = os.path.join(BACKUPS, n)
        try:
            if os.path.getmtime(p) >= cutoff:
                continue
            size = os.path.getsize(p)
            os.remove(p)
            freed += size
        except OSError:
            continue
    if freed:
        LOG.info("Pruned old manifest backups, freeing %s.", WJ.human_bytes(freed))


def backup_existing(filename):
    """Park the manifest about to be replaced as <name>.bak, stamped with
    the moment it was superseded. Returns where it was kept, or None when
    there was none to keep; raises OSError when it could not be kept, and
    then nothing has changed.

    It moves by wj_paths.move_file, which copies across volumes: MANIFEST_DIR
    may point into Wabbajack's own folder under %LOCALAPPDATA% while
    manifest_backups sits beside the tools, on another drive or a share, and
    a rename alone failed there every time. It goes to a name of its own
    first, so the previous .bak gives way only once this one is safe.

    A move keeps the file's own mtime, which says when the manifest was
    downloaded - for a list that went months without an update, months ago.
    prune_backups judges by mtime, so without the stamp the prune that ends
    the run could delete a backup made minutes before."""
    src = os.path.join(MANIFESTS, filename)
    if not os.path.isfile(src):
        return None
    bak = os.path.join(BACKUPS, filename + ".bak")
    kept = wj_paths.move_file(src, wj_paths.unique_path(bak + ".new"))
    try:
        os.replace(kept, bak)              # one folder: only the previous .bak gives way
        kept = bak
    except OSError as e:
        LOG.warning("  [WARN] Could not replace the previous backup of %s (%s).", filename, e)
    LOG.info("  Previous manifest kept as %s", os.path.basename(kept))
    now = time.time()
    try:
        os.utime(kept, (now, now))
    except OSError:
        pass
    return kept


# ----------------------------------------------------------------------
# Work planning against the pools
# ----------------------------------------------------------------------
MANIFEST_CACHE_FILE = "wj_manifest_cache.json"


def archive_kind(a):
    """Source kind for an archive, from either a full or a cached record."""
    return a.get("kind") or WJ.state_type(a.get("state"))


def load_plan_records(progress=None, files=None):
    """Lightweight per-list records, cached on disk.

    The cache itself lives in the downloader module now, so the sorter reads
    the same file instead of opening every manifest zip on each run; see
    load_light_records() there for what is cached and how it is invalidated."""
    return WJ.load_light_records(MANIFESTS, runtime_path(MANIFEST_CACHE_FILE),
                                 progress=progress, files=files)


# ----------------------------------------------------------------------
# The trash: what stage 5c reclaimed, waiting out its purge date
# ----------------------------------------------------------------------
def trash_name_problem():
    """Why the trash folder must not be acted on right now, or None.

    wj_config refuses a bad TRASH_FOLDER on the way in. This asks again at
    the two places that act on it - clearing Master into it, and the purge
    that deletes from it for good - because both act on whatever folder the
    name reaches, and a name that slipped past would cost every file in it.
    It also asks what the settings alone cannot know: a modlist's own folder
    is never a trash, and the manifests held here decide which names those
    are."""
    cfg = WJ.CFG
    name = cfg.TRASH_FOLDER
    why = (cfg.folder_setting_problem(name) if isinstance(name, str)
           else "it is not a folder name")
    if why:
        return why
    low = name.lower()
    if low == str(cfg.UNMANAGED_FOLDER).lower():
        return "it is also the unmanaged store"
    if low in cfg.EXCLUDE_POOL_DIRS:
        return "it is listed as another installer's folder"
    if low in WJ.known_list_dirs_from(MANIFESTS):
        return "it is the folder of a modlist held here"
    return None


# The record stage 5c writes of each file it sends to a trash.
_DELETED_RECORD = re.compile(r"^deleted_(.+)_(\d{8}_\d{6})\.csv$", re.I)


def trash_records():
    """{normcased path in a trash: (arrived, size)} from every deleted_*.csv
    record in the runtime folder: where each file stage 5c reclaimed went,
    at what size, and when. The trash is found through the layout as it
    stands, not the path the record wrote, so a share mapped to another
    letter since still finds its files. The latest record for a path wins: a
    file put back and reclaimed again counts from the second time."""
    out = {}
    folder = runtime_path(make=False)
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    trash_name = WJ.CFG.TRASH_FOLDER
    for n in names:
        m = _DELETED_RECORD.match(n)
        if not m:
            continue
        try:
            fallback = datetime.strptime(m.group(2), "%Y%m%d_%H%M%S").timestamp()
        except ValueError:
            continue
        trash = os.path.join(WJ.ARCHIVE_ROOT, m.group(1), trash_name)
        try:
            with open(os.path.join(folder, n), newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        except (OSError, csv.Error, UnicodeDecodeError):
            continue
        for r in rows:
            to = os.path.basename(str(r.get("trashed_to") or ""))
            if not to:
                continue
            try:
                size = int(r.get("size_bytes") or -1)
            except ValueError:
                continue
            arrived = fallback                  # a record from before trashed_at
            try:
                if r.get("trashed_at"):
                    arrived = datetime.fromisoformat(r["trashed_at"]).timestamp()
            except ValueError:
                pass
            key = os.path.normcase(os.path.join(trash, to))
            if key not in out or arrived > out[key][0]:
                out[key] = (arrived, size)
    return out


def trash_contents():
    """{game_folder: [(name, path, size, arrived)]} for every file in every
    game's trash. `arrived` is when a deletion record says the file went
    there - matched by name and size - or None for a file no record accounts
    for, which nothing here deletes: put there by hand, or by a folder that
    merely shares the trash's name."""
    records = trash_records()
    root, trash_name = WJ.ARCHIVE_ROOT, WJ.CFG.TRASH_FOLDER
    out = {}
    try:
        games = sorted(os.listdir(root))
    except OSError:
        return out
    for g in games:
        trash = os.path.join(root, g, trash_name)
        if not os.path.isdir(trash):
            continue
        items = []
        try:
            with os.scandir(trash) as it:
                for e in it:
                    try:
                        if not e.is_file(follow_symlinks=False):
                            continue
                        size = e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
                    hit = records.get(os.path.normcase(e.path))
                    items.append((e.name, e.path, size,
                                  hit[0] if hit and hit[1] == size else None))
        except OSError:
            continue
        if items:
            out[g] = sorted(items, key=lambda t: t[0].lower())
    return out


def purge_trash(days=None):
    """Delete from every game's trash folder what has been there long enough.

    Only what a cleanup put there: a file is purged when a deletion record
    says stage 5c sent it to this trash, at this size, and its age is
    counted from that record. Nothing else in a trash folder is ever deleted
    by this tooling, whatever its name - a folder the user keeps files in
    can share the trash's name - and a file whose arrival stamp could not be
    set is not purged on the strength of its build date. Run at the start of
    every run, so the undo window is exactly TRASH_KEEP_DAYS and no longer.
    days=0 empties the trash of everything a cleanup put there, which is what
    the wizard's "empty now" does.

    Returns (rows, record_path): one (game_folder, name, size, arrived) per
    file removed, and the CSV they were written to as it went. Nothing at
    all is deleted when trash_name_problem() objects to the folder."""
    # The layout module is reached through the downloader, like everything
    # else here.
    cfg = WJ.CFG
    days = cfg.TRASH_KEEP_DAYS if days is None else days
    cutoff = time.time() - days * 86400
    trash_name = cfg.TRASH_FOLDER
    rows, record, writer = [], None, None
    why = trash_name_problem()
    if why:
        LOG.error("[ERROR] Not emptying the trash: TRASH_FOLDER %r cannot be used (%s). "
                  "Nothing was deleted.", trash_name, why)
        return rows, None
    if DRY_RUN:
        return rows, None
    unrecorded = 0
    for g, items in trash_contents().items():
        for name, path, size, when in items:
            if when is None:
                unrecorded += 1
                continue
            if days and when > cutoff:
                continue
            arrived = datetime.fromtimestamp(when).isoformat(timespec="seconds")
            try:
                os.remove(path)
            except OSError as ex:
                LOG.warning("[WARN] Could not purge %s: %s", path, ex)
                continue
            if record is None:
                try:
                    # Never over an earlier purge's record (wj_paths.create_stamped).
                    _rec_path, record = wj_paths.create_stamped(runtime_path(), "purged_trash_")
                    writer = csv.writer(record)
                    writer.writerow(["game_folder", "archive", "size_bytes", "arrived"])
                except OSError as ex:
                    LOG.warning("[WARN] Could not open the purge record: %s", ex)
                    record, writer = False, None
            row = (g, name, size, arrived)
            rows.append(row)
            if writer is not None:
                try:
                    writer.writerow(row)
                    record.flush()
                except OSError as ex:
                    LOG.warning("[WARN] Could not write to the purge record: %s", ex)
                    writer = None
    record_path = None
    if record:
        record_path = record.name
        record.close()
    if rows:
        LOG.info("Trash: purged %d file(s) older than %s day(s), %s.", len(rows),
                 days, WJ.human_bytes(sum(r[2] for r in rows)))
    if unrecorded:
        LOG.info("Trash: %d file(s) in a trash folder are in no deletion record, so "
                 "nothing here deletes them; they are yours to keep or remove.", unrecorded)
    return rows, record_path


def plan_for(record):
    """Split a parsed manifest's archives into what we can fetch, what only
    Wabbajack can fetch, and what is already on disk."""
    game_base, _, _ = WJ.resolve_game_dirs(record["game"])
    existing = WJ.index_existing(game_base)
    direct, nexus, other, present = [], [], [], 0
    for a in record["archives"]:
        # present_match compares the manifest's Size, not just the filename: a
        # truncated or wrong-version file with the right name used to count as
        # done forever and only surface when Wabbajack hash-checked it.
        if WJ.present_match(existing, a)[0]:
            present += 1
            continue
        kind = archive_kind(a)
        if kind == "nexus":
            nexus.append(a)
        elif kind in ("http", "wabbajackcdn", "mediafire"):
            direct.append(a)
        elif kind == "gamefile":
            continue
        else:
            other.append((kind, a))
    return {"base": game_base, "direct": direct, "nexus": nexus,
            "other": other, "present": present}


def total_size(items):
    return sum(a["size"] or 0 for a in items)


def free_space_ok(path, need_bytes):
    try:
        free = shutil.disk_usage(path).free
    except OSError as e:
        LOG.warning("[WARN] Could not check free space on %s: %s", path, e)
        return True
    margin = FREE_SPACE_MARGIN_GB * (1024 ** 3)
    if free - need_bytes < margin:
        LOG.error("[ERROR] Not enough space: %s free, need %s plus a %d GB margin.",
                  WJ.human_bytes(free), WJ.human_bytes(need_bytes),
                  FREE_SPACE_MARGIN_GB)
        return False
    LOG.info("Free space:     %s (need ~%s)", WJ.human_bytes(free),
             WJ.human_bytes(need_bytes))
    return True


def find_part_files(game_base):
    out = []
    try:
        dirs = [game_base] + [os.path.join(game_base, d)
                              for d in os.listdir(game_base)
                              if os.path.isdir(os.path.join(game_base, d))]
    except OSError:
        return out
    for d in dirs:
        try:
            out += [os.path.join(d, n) for n in os.listdir(d)
                    if n.lower().endswith(".part")]
        except OSError:
            continue
    return out


def write_nexus_todo(record, nexus_items):
    """CSV of the Nexus download pages still outstanding, for the desktop.

    Removed again once nothing is outstanding: a to-do list for a finished
    list is clutter, and ten of them had accumulated here. A dry run writes
    and removes nothing."""
    if DRY_RUN:
        return None
    path = runtime_path(f"nexus_todo_{record['name']}.csv")
    if not nexus_items:
        try:
            os.remove(path)
        except OSError:
            pass
        return None
    try:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["archive", "size_bytes", "download_page"])
            # Same order the downloader will work in, so the CSV can be
            # followed top to bottom alongside a run.
            for a in sorted(nexus_items, key=WJ.size_key):
                # The site slug, not the raw manifest value: these links are
                # meant to be clicked, and nexusmods.com/skyrimse/... 404s.
                dom = WJ.nexus_url_domain(WJ.nexus_domain(a))
                mid, fid = WJ.nexus_ids(a)
                page = (WJ.nexus_file_page(dom, mid, fid)
                        if dom and mid and fid else "")
                w.writerow([a["name"], a["size"] or "", page])
        return path
    except OSError as e:
        LOG.warning("[WARN] Could not write Nexus to-do CSV: %s", e)
        return None


# ----------------------------------------------------------------------
# Driving the two existing scripts
# ----------------------------------------------------------------------
def reload_settings():
    """Re-copy every user setting from wj_config after wj_settings.json changed.

    The GUI's Settings tab saves the file and calls this, so the change takes
    effect in the running process rather than after a restart. The downloader
    module keeps copies of its own - the pool layout among them, which this
    process plans with - so it is asked to refresh them as well: the copy
    loaded as WJ, and the ordinary import, where anything made one."""
    global AUTO_TRACK, MANIFEST_DIR, EXCLUDE, TRACK, STAGE_LIST, NEXUS_MODE
    global KEEP_BACKUP_DAYS, REQUIRE_HASH, DOWNLOAD_ORDER, FREE_SPACE_MARGIN_GB
    global NOTIFY_URL
    copies = {id(m): m for m in (WJ, sys.modules.get("wj_downloader")) if m is not None}
    for mod in copies.values():
        refresh = getattr(mod, "refresh_settings", None)
        if refresh is not None:
            refresh()
    AUTO_TRACK = CFG.AUTO_TRACK
    MANIFEST_DIR = CFG.MANIFEST_DIR
    EXCLUDE = list(CFG.EXCLUDE)
    TRACK = list(CFG.TRACK)
    STAGE_LIST = CFG.STAGE_LIST
    NEXUS_MODE = CFG.NEXUS_MODE
    KEEP_BACKUP_DAYS = CFG.KEEP_BACKUP_DAYS
    REQUIRE_HASH = CFG.REQUIRE_HASH
    DOWNLOAD_ORDER = CFG.DOWNLOAD_ORDER
    FREE_SPACE_MARGIN_GB = CFG.FREE_SPACE_MARGIN_GB
    NOTIFY_URL = CFG.NOTIFY_URL


def wabbajack_busy():
    """Is a Wabbajack process running? True, False, or None when that cannot
    be checked (tasklist did not answer). An unattended run has nobody to
    ask, and treats None as yes: it used to read it as no, and move archives
    under whatever might be running."""
    try:
        import wj_wabbajack as WB
    except ImportError:
        return None
    procs = WB.running_processes()
    return None if procs is None else bool(procs)


def child_flags(args):
    """Flags that must reach the child scripts so they behave like the parent."""
    flags = ["--allow-size-only"] if args.allow_size_only else []
    order = getattr(args, "order", None) or DOWNLOAD_ORDER
    if order:
        flags += ["--order", order]
    return flags


# Set by the GUI: a callable handed each line a child script prints, so its
# progress shows in the window as it happens. None means the console
# behaviour below.
OUTPUT_HOOK = None


def set_output_hook(fn):
    global OUTPUT_HOOK
    OUTPUT_HOOK = fn


# The file that asks the child script running now to stop: the downloader and
# the sorter watch for it (WJ_STOP_FILE) and stop at their next safe point, the
# way Ctrl+C stops them in a console. It is how the window stops a job it
# started: the window has no console to send Ctrl+C to, and closing it used
# to leave the child running on, unseen and unlocked.
_CHILD = {"stop": None, "stopping": False}
_STOP_SEQ = itertools.count(1)

# What run_script() returns for a child it did not start because the job had
# been asked to stop: Ctrl+C's code, which every caller already reads as "did
# not finish".
STOPPED = 130


def request_child_stop():
    """Ask the child script running now, if there is one, to stop at its
    next safe point - and every child after it, until allow_children(), not
    to start at all. A job that runs several in a row, a download per list or
    a rebuild per game, used to go straight on to the next one. True unless
    the child running now could not be asked."""
    _CHILD["stopping"] = True
    path = _CHILD.get("stop")
    if not path:
        return True
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("stop\n")
        return True
    except OSError:
        return False


def allow_children():
    """A new job: children may start again after an earlier stop."""
    _CHILD["stopping"] = False


def stop_requested():
    """Has the job running now been asked to stop (request_child_stop)? For
    long work done in this process rather than in a child script, which
    reads its stop file instead."""
    return bool(_CHILD.get("stopping"))


def archive_root_for_children():
    """The archive root a child script has to be told to use, or None when
    it works it out the same way itself.

    A new ARCHIVE_ROOT saved in the window takes effect at the next start
    (wj_config.RESTART_SETTINGS), so until then this process plans, measures
    and clears Master under the root it started with - and a download or a
    sort it starts would read the new one from the file, and work under the
    other. While a different root waits in the file, the child is handed
    this one (WJ_ARCHIVE_ROOT)."""
    running = getattr(WJ, "ARCHIVE_ROOT", None)
    if not running:
        return None
    saved = CFG.resolve_root(CFG.next_start_settings()["ARCHIVE_ROOT"])
    same = (os.path.normcase(os.path.abspath(saved))
            == os.path.normcase(os.path.abspath(running)))
    return None if same else running


def child_running():
    """Is a child script running under run_script() now?"""
    return bool(_CHILD.get("stop"))


def run_script(script, args, label, interactive=False):
    """Run one of the existing scripts.

    interactive=True hands the child our real console and stdin, which
    --nxm-assist needs: it prints per-file instructions, opens a browser, and
    waits on your clicks. Everything else is captured to a file.

    Their stdout carries \\r-based progress bars that would otherwise pile up
    in a scheduler's journal as enormous single lines, so it goes to a file
    instead. Both scripts already write their own timestamped logs next to
    themselves, so nothing is lost; we tail this capture only on failure.

    With OUTPUT_HOOK set, the child's output is streamed line by line to the
    hook instead, carriage returns included as line breaks, and stdin is
    closed: the GUI has no console to hand over, and neither child needs one
    when its flags and the API key file answer every question.

    After request_child_stop() nothing is started: STOPPED is returned."""
    if _CHILD.get("stopping"):
        LOG.info("[STOP] Not starting %s: asked to stop.", label)
        return STOPPED
    stop = runtime_path(f".wj_stop_{os.getpid()}_{next(_STOP_SEQ)}")
    _CHILD["stop"] = stop
    try:
        return _run_script(script, args, label, interactive, stop)
    finally:
        _CHILD["stop"] = None
        try:
            os.remove(stop)
        except OSError:
            pass


def _run_script(script, args, label, interactive, stop):
    cmd = wj_paths.stage_argv(script, HERE) + args
    LOG.info("\n$ %s", " ".join(cmd))
    # The child scripts default to scanning their own folder for manifests;
    # this tells them to look where we actually keep them.
    env = dict(os.environ, PYTHONUNBUFFERED="1", WJ_MANIFEST_DIR=MANIFESTS,
               WJ_LOCK_HELD="1", WJ_STOP_FILE=stop)
    root = archive_root_for_children()
    if root:
        env["WJ_ARCHIVE_ROOT"] = root
    cap = runtime_path(f"wj_autoupdate_{label}.out")
    if OUTPUT_HOOK is not None:
        # The window has no console to give away, and frozen the child is the
        # console binary, which Windows would otherwise hand a console window
        # of its own - one flashing up per stage. Everything it says is on the
        # pipe we are about to read, so it needs no window to say it in.
        quiet = ({"creationflags": subprocess.CREATE_NO_WINDOW}
                 if hasattr(subprocess, "CREATE_NO_WINDOW") else {})
        try:
            # UTF-8: the children set their stdout to it (setup_logging), and
            # read as the ANSI code page their non-ASCII names came out garbled.
            proc = subprocess.Popen(cmd, cwd=HERE, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace",
                                    bufsize=1, **quiet)
        except OSError as e:
            LOG.error("[ERROR] Could not launch %s: %s", script, e)
            return 127
        with proc:
            for raw in proc.stdout:
                for piece in raw.replace("\r", "\n").split("\n"):
                    if piece.strip():
                        OUTPUT_HOOK(piece)
        LOG.info("%s exited with %d", label, proc.returncode)
        return proc.returncode
    try:
        if interactive:
            rc = subprocess.run(cmd, cwd=HERE, env=env, check=False).returncode
            LOG.info("%s exited with %d", label, rc)
            return rc
        with open(cap, "w", encoding="utf-8", errors="replace") as out:
            rc = subprocess.run(cmd, cwd=HERE, env=env, check=False,
                                stdin=subprocess.DEVNULL,
                                stdout=out, stderr=subprocess.STDOUT).returncode
    except OSError as e:
        LOG.error("[ERROR] Could not launch %s: %s", script, e)
        return 127
    LOG.info("%s exited with %d (output: %s)", label, rc, os.path.basename(cap))
    if rc not in (0, 1):
        try:
            with open(cap, encoding="utf-8", errors="replace") as f:
                tail = f.read()[-4000:]
            LOG.error("--- tail of %s ---\n%s", label, tail)
        except OSError:
            pass
    return rc


# ----------------------------------------------------------------------
# Start-up shared by the flag interface and the interactive wizard
# ----------------------------------------------------------------------
def bootstrap(verbose=False, nexus_mode=None, allow_size_only=False, recover=True):
    """Prepare the environment. Returns 0 on success, else an exit code.

    `recover` hands back an nxm:// handler a killed run left behind; a run
    that only reports - --check, --dry-run, --gallery - passes False, and
    changes nothing.

    Both entry points need exactly this: logging, the downloader module, the
    cross-script configuration check, the manifest folder, and the refusal to
    run without hash verification. Keeping it in one place means the wizard
    cannot quietly start up under weaker checks than the flag interface."""
    global WJ, NEXUS_MODE
    # Before the log opens: the log is one of the files that moves.
    moved = wj_paths.adopt_runtime(HERE)
    setup_logging(verbose)
    WJ = load_downloader_module()
    if nexus_mode:
        NEXUS_MODE = nexus_mode

    LOG.info("Wabbajack Wizard %s  %s", CFG.__version__,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if moved:
        LOG.info("Moved %d runtime file(s) from beside the scripts into %s: %s",
                 len(moved), runtime_path(), ", ".join(sorted(moved)))
    LOG.info("Settings:       %s",
             ("%s (%d override(s))" % (CFG.SETTINGS_FILE, len(CFG._LOADED)))
             if CFG._LOADED else "defaults (no %s)" % CFG.SETTINGS_FILE)
    for problem in CFG.SETTINGS_PROBLEMS:
        LOG.warning("[WARN] %s: %s", CFG.SETTINGS_FILE, problem)
    # Every refusal below is "could not start", which is exit 2 (README):
    # some used to return 1, which a scheduler reads as "ran, with problems".
    if not check_script_consistency():
        return 2
    if not resolve_dirs():
        return 2
    LOG.info("Script dir:     %s", HERE)
    LOG.info("Runtime dir:    %s", runtime_path())
    LOG.info("Manifest dir:   %s", MANIFESTS)
    LOG.info("Archive root:   %s", WJ.ARCHIVE_ROOT)
    have_hash = bool(getattr(WJ, "_HAVE_XXHASH", False))
    LOG.info("Hash verify:    %s",
             "xxHash64" if have_hash else "size only (install python3-xxhash)")
    if not have_hash and REQUIRE_HASH and not allow_size_only:
        LOG.error("[TERMINATED] 'xxhash' is not importable by this interpreter "
                  "(%s), so manifests and archives could only be checked by "
                  "size.", sys.executable)
        LOG.error("             Run setup_env.bat and launch through run.bat, or "
                  "pass --allow-size-only to accept size-only checks.")
        return 2

    LOG.info("Nexus mode:     %s", NEXUS_MODE)
    # A run killed while it held the nxm:// handler - Task Manager, a closed
    # console, a power cut - could not hand it back; this start does.
    if recover:
        try:
            WJ.recover_nxm_handler(runtime_path())
        except Exception as e:              # never a reason not to start
            LOG.debug("Could not check the nxm:// handler: %s", e)
    if not os.path.isdir(WJ.ARCHIVE_ROOT):
        LOG.error("[TERMINATED] ARCHIVE_ROOT does not exist or is not reachable: "
                  "%s\nIf that is a mapped drive, make sure the share is "
                  "connected before running.", WJ.ARCHIVE_ROOT)
        return 2
    if NEXUS_MODE == "assist" and not getattr(WJ, "_HAVE_WINREG", False):
        LOG.error("[TERMINATED] NEXUS_MODE='assist' needs the Windows registry: "
                  "the downloader registers an nxm:// handler via winreg. "
                  "Use 'skip' here.")
        return 2
    return 0


def cmd_gallery(sync=False, force=False):
    """Report the published gallery, and optionally refresh its cache.

    The interactive browse stage does this for you. The flag exists so a
    scheduled run can keep the report cache warm - the sweep is 224 files and
    about 45 MB, which is nine seconds on a decent line, but it is nine
    seconds nobody wants to wait for at the start of a browse."""
    try:
        import wj_gallery as GAL
    except ImportError:
        LOG.error("[TERMINATED] wj_gallery.py is missing from %s.", HERE)
        return 2

    banner("PUBLISHED GALLERY")
    try:
        summary = GAL.load_summary()
        featured = GAL.load_featured()
        disabled = GAL.load_disabled()
    except GAL.GalleryError as e:
        LOG.error("[TERMINATED] The gallery index could not be read: %s", e)
        return 2

    feeds, complete = load_feeds()
    if not feeds:
        LOG.error("[TERMINATED] No feed could be read.")
        return 2

    state = load_state()
    tracked = resolve_tracking(state)
    pooled = GAL.pooled_games({e.get("game") for e in feeds.values()})
    rows = GAL.build_rows(feeds, summary, featured, disabled, set(tracked),
                          games=set(pooled))
    # A hidden list is left out of everything below, its report sweep
    # included: not fetching what will never be looked at is the point.
    hidden = [r for r in rows if r["hidden"]]
    rows = [r for r in rows if not r["hidden"]]

    LOG.info("%d published list(s); %d for the %d game(s) with a pool folder%s.",
             len(feeds), len(rows), len(pooled),
             (" (%d hidden, left out)" % len(hidden)) if hidden else "")
    LOG.info("Tracked here: %d. Featured: %d. Unavailable: %d. Retired: %d.",
             sum(1 for r in rows if r["have"]),
             sum(1 for r in rows if r["featured"]),
             sum(1 for r in rows if GAL.unavailable(r)),
             sum(1 for r in rows if r["retired"]))
    if not complete:
        LOG.warning("[WARN] Some feeds did not respond, so this is an "
                    "incomplete picture. Nothing is concluded from an "
                    "absence here.")

    if sync:
        keys = [r["key"] for r in rows if not r["retired"]]
        LOG.info("Fetching %d validation report(s)...", len(keys))
        last = [0.0]

        def progress(done, total, cached, key):
            now = time.time()
            if total and (done == total or now - last[0] > 2.0):
                last[0] = now
                LOG.info("  [%d/%d] %s", done, total, (key or "")[:56])

        fetched, cached, failed = GAL.sync_reports(keys, progress=progress,
                                                   force=force)
        LOG.info("%d fetched, %d already cached, %d without a published "
                 "report.", fetched, cached, len(failed))
        for k in failed:
            LOG.debug("  no report: %s", k)

    size, files = GAL.cache_size()
    covered = sum(1 for r in rows if GAL.have_report(r["key"]))
    LOG.info("Report cache: %s across %d file(s); %d of %d rows covered.",
             WJ.human_bytes(size), files, covered, len(rows))
    LOG.info("")
    LOG.info("Run the wizard to browse them with how much of each you "
             "already hold.")
    return 0


# ----------------------------------------------------------------------
# Format assumptions
# ----------------------------------------------------------------------
def cmd_selftest():
    """Check that the formats this tooling reads still look the way it expects.

    Everything here is Wabbajack's own undocumented internals - the shape of
    repositories.json, of a feed entry, of the JSON inside a .wabbajack, of a
    CDN definition, and the set of downloader type names. None of it is a
    published contract, so any of it can change without notice.

    The point is not to prevent that. It is to make it ANNOUNCE itself. Without
    this, a feed that stops carrying machineURL shows up as "Following 0
    list(s)", and an archive type that gets renamed shows up as files quietly
    reclassified as unsupported - both of which look like your problem rather
    than an upstream change."""
    banner("SELF TEST: FORMAT ASSUMPTIONS")
    ok_count = bad_count = 0

    def verdict(label, ok, detail=""):
        nonlocal ok_count, bad_count
        if ok:
            ok_count += 1
            LOG.info("  [OK]      %s", label)
        else:
            bad_count += 1
            LOG.error("  [CHANGED] %s", label)
        if detail:
            LOG.info("            %s", detail)

    # -- 1. the registry -----------------------------------------------
    try:
        repos = http_json(REPOSITORIES_URL)
    except Exception as e:
        verdict("repositories.json is reachable", False, str(e))
        repos = None
    if repos is not None:
        verdict("repositories.json is an object of name -> feed URL",
                isinstance(repos, dict) and repos
                and all(isinstance(v, str) for v in repos.values()),
                "%d repo(s) listed" % (len(repos) if isinstance(repos, dict) else 0))

    # -- 2. a feed entry ------------------------------------------------
    feeds, complete = load_feeds(None)
    verdict("every feed loaded", complete,
            "a feed that fails is a warning, not a format change")
    verdict("feeds yield modlist entries", bool(feeds),
            "%d list(s) indexed" % len(feeds))
    if feeds:
        with_machine = sum(1 for e in feeds.values() if e["machine"])
        with_dl = sum(1 for e in feeds.values() if e["download"])
        with_size = sum(1 for e in feeds.values() if e["size"])
        with_hash = sum(1 for e in feeds.values() if e["hash"])
        n = len(feeds)
        verdict("entries carry machineURL", with_machine == n,
                "%d of %d" % (with_machine, n))
        verdict("entries carry a download link", with_dl > n * 0.9,
                "%d of %d" % (with_dl, n))
        verdict("entries carry download_metadata Size", with_size > n * 0.9,
                "%d of %d - Size is what makes the cheap 'is this already the "
                "published build' check possible" % (with_size, n))
        verdict("entries carry download_metadata Hash", with_hash > n * 0.9,
                "%d of %d" % (with_hash, n))

    # -- 3. the inside of a .wabbajack ----------------------------------
    files = WJ.list_manifest_files(MANIFESTS)
    if not files:
        verdict("a local manifest is available to inspect", False,
                "no .wabbajack files in %s" % MANIFESTS)
    else:
        records = load_plan_records()
        readable = [f for f, e in records.items() if e.get("readable")]
        verdict("manifests open and contain a 'modlist' member",
                len(readable) == len(records),
                "%d of %d readable" % (len(readable), len(records)))
        with_game = [f for f in readable if records[f].get("game")]
        verdict("manifests declare a GameType", len(with_game) == len(readable),
                "%d of %d" % (len(with_game), len(readable)))
        total = sum(len(records[f]["archives"]) for f in readable)
        sized = sum(1 for f in readable for a in records[f]["archives"]
                    if a.get("size") is not None)
        verdict("archive entries carry a Size", total and sized > total * 0.95,
                "%d of %d entries" % (sized, total))

        # -- 4. downloader type names ----------------------------------
        kinds = {}
        for f in readable:
            for a in records[f]["archives"]:
                kinds[a["kind"]] = kinds.get(a["kind"], 0) + 1
        unknown = kinds.get("unknown", 0)
        verdict("every archive's State type is one we recognise", unknown == 0,
                "; ".join("%s:%d" % kv for kv in sorted(kinds.items())))
        if unknown:
            raws = {}
            for f in readable:
                for a in records[f]["archives"]:
                    if a["kind"] == "unknown":
                        raws[a.get("raw", "(unrecorded)")] = \
                            raws.get(a.get("raw", "(unrecorded)"), 0) + 1
            LOG.error("            %d archive(s) use a State $type this tooling "
                      "does not know:", unknown)
            for raw, n in sorted(raws.items(), key=lambda kv: -kv[1]):
                LOG.error("              %-42s %d archive(s)", raw, n)
            LOG.error("            Wabbajack has added or renamed a downloader. "
                      "Add it to state_type() in the downloader: as a fetchable "
                      "kind if this tooling can handle it, or as a recognised "
                      "one (like 'mega') if it should just be reported.")

    # -- 5. a CDN definition -------------------------------------------
    if not _HAVE_CDN:
        verdict("wj_cdn.py is present", False, "manifests cannot be fetched")
    elif feeds:
        cdn_entry = next((e for e in feeds.values()
                          if e["download"] and CDN.is_cdn_url(e["download"])), None)
        if cdn_entry is None:
            verdict("a CDN-hosted manifest exists to probe", False,
                    "no feed entry points at the Wabbajack CDN")
        else:
            d = CDN.get_definition(cdn_entry["download"])
            if d is None:
                # A single 404 is an upstream publishing problem, not a format
                # change, so say which it is rather than guessing.
                verdict("the CDN definition for '%s' parses" % cdn_entry["title"],
                        False,
                        "this can be one dead upload rather than a format "
                        "change - try --selftest again later, or check another list")
            else:
                parts = CDN._field(d, "Parts", default=[])
                first = parts[0] if parts else {}
                fields = [CDN._field(first, f) is not None
                          for f in ("Index", "Offset", "Size", "Hash")]
                verdict("CDN definitions still list Parts", bool(parts),
                        "%d part(s) in '%s'" % (len(parts), cdn_entry["title"]))
                verdict("each part carries Index, Offset, Size and Hash",
                        all(fields),
                        "missing: %s" % ", ".join(
                            f for f, present in zip(
                                ("Index", "Offset", "Size", "Hash"), fields)
                            if not present) or "none")

    # -- 5. the gallery reports ----------------------------------------
    # These are what the browse stage answers "how much of this do I already
    # have?" from, without downloading a 5 GB .wabbajack to find out. They are
    # files in a GitHub repository, not an API, so their shape is exactly as
    # unpromised as everything else here.
    try:
        import wj_gallery as GAL
    except ImportError:
        verdict("wj_gallery.py is present", False,
                "the browse stage needs it; the rest of the tooling does not")
    else:
        try:
            summary = GAL.load_summary(force=True)
        except Exception as e:
            verdict("modListSummary.json loads", False, str(e))
            summary = {}
        else:
            verdict("modListSummary.json loads", bool(summary),
                    "%d list(s) with a validation row" % len(summary))
            need = ("failed", "passed", "modlist_missing", "has_failures")
            complete = sum(1 for r in summary.values()
                           if all(k in r for k in need))
            verdict("summary rows carry the health fields",
                    complete == len(summary),
                    "%d of %d carry %s" % (complete, len(summary),
                                           "/".join(need)))
        try:
            featured = GAL.load_featured(force=True)
            verdict("featured_lists.json loads", bool(featured),
                    "%d featured key(s) - note this is NOT the 'wj-featured' "
                    "repository, which is a different and smaller set"
                    % len(featured))
        except Exception as e:
            verdict("featured_lists.json loads", False, str(e))
        try:
            disabled = GAL.load_disabled(force=True)
            verdict("disabled_mod_lists.json parses despite its "
                    "trailing commas", True,
                    "%d retired list(s)" % len(disabled))
        except Exception as e:
            verdict("disabled_mod_lists.json parses despite its "
                    "trailing commas", False, str(e))

        # The report has to describe the same archives as the manifest, or the
        # gap it reports is fiction. Checked against a list actually held.
        recs = load_plan_records()
        probe = None
        for fn, rec in sorted(recs.items()):
            key = fn[:-len(".wabbajack")].replace("_@@_", "/")
            if key in summary and rec.get("archives"):
                probe = (key, rec)
                break
        if probe is None:
            verdict("a report can be matched to a manifest we hold", False,
                    "no tracked list has both a manifest and a summary row")
        else:
            key, rec = probe
            rep = GAL.load_report(key, force=True)
            if not rep:
                verdict("the per-list report is published", False, key)
            else:
                arch = GAL.report_archives(rep, WJ)
                verdict("the per-list report lists archives", bool(arch),
                        "%d in %s" % (len(arch), key))
                mine = {(a["name"], a["size"]) for a in rec["archives"]}
                theirs = {(a["name"], a["size"]) for a in arch}
                verdict("the report describes the SAME archives as the "
                        ".wabbajack", mine == theirs,
                        "%s: %d local vs %d published, %d only local, "
                        "%d only published"
                        % (key, len(mine), len(theirs),
                           len(mine - theirs), len(theirs - mine)))
                verdict("report archives carry a recognised State type",
                        all(a["kind"] != "unknown" for a in arch),
                        "unknown: %d" % sum(1 for a in arch
                                            if a["kind"] == "unknown"))

    banner("SELF TEST COMPLETE")
    LOG.info("  %d assumption(s) hold, %d changed.", ok_count, bad_count)
    if bad_count:
        LOG.error("")
        LOG.error("  Something upstream reads differently than this tooling "
                  "expects. Nothing is broken by itself - but until the lines "
                  "marked CHANGED are understood, treat the run's own reports "
                  "as unreliable rather than as facts about your collection.")
    return 1 if bad_count else 0


# ----------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------
def cmd_discover():
    banner("DISCOVER: MATCHING LOCAL MANIFESTS TO FEEDS")
    feeds, _complete = load_feeds(None)
    if not feeds:
        return 1
    idx = index_feeds(feeds)

    files = WJ.list_manifest_files(MANIFESTS)
    if not files:
        LOG.warning("No .wabbajack files found in %s", MANIFESTS)

    matched, unmatched = [], []
    for f in files:
        uniq = match_candidates(idx, f, None)
        inner = None
        if not uniq:
            inner = manifest_name(os.path.join(MANIFESTS, f))
            uniq = match_candidates(idx, f, inner)
        if uniq:
            matched.append((f, inner, uniq))
        else:
            unmatched.append((f, inner))

    LOG.info("")
    for f, inner, cands in matched:
        LOG.info("%s   (manifest Name: %s)", f, inner or "?")
        for c in cands:
            LOG.info("    -> %-45s v%-10s %s", c["key"], c["version"], c["title"])
    for f, inner in unmatched:
        LOG.info("%s   (manifest Name: %s)  -> NO MATCH", f, inner or "?")

    if unmatched:
        LOG.info("\nUnmatched lists are usually private, renamed, or from a repo not "
                 "in repositories.json. Search the output above for the title by "
                 "hand, or leave them untracked.")

    banner("PASTE THIS INTO THE CONFIG BLOCK")
    single = [c[0]["key"] for _, _, c in matched if len(c) == 1]
    LOG.info("TRACK = [\n%s\n]", "\n".join(f'    "{k}",' for k in single) or "")
    ambiguous = [(f, c) for f, _, c in matched if len(c) > 1]
    for f, c in ambiguous:
        LOG.info("# %s matched %d feeds - pick one: %s",
                 f, len(c), ", ".join(x["key"] for x in c))
    return 0


# ----------------------------------------------------------------------
# Working out which published list each local .wabbajack corresponds to
# ----------------------------------------------------------------------
def index_feeds(feeds):
    """Lookup tables for matching a local file to a published list."""
    idx = {"machine": {}, "title": {}, "key": {}}
    for e in feeds.values():
        idx["machine"].setdefault(e["machine"].lower(), []).append(e)
        idx["title"].setdefault(e["title"].strip().lower(), []).append(e)
        idx["key"][e["key"].lower()] = e
    return idx


def feed_key_from_filename(filename):
    """The published feed key a '<repo>_@@_<machineURL>.wabbajack' file names.

    This is the convention Wabbajack's own downloads follow, and it is exact:
    the stem IS the feed key with '/' written as '_@@_'. Matching on it first
    means the common case resolves with no guessing at all - no full feed scan,
    no reliance on a title that the author may have since renamed, and no
    ambiguity between two lists that happen to share a display name."""
    stem = os.path.splitext(filename)[0]
    repo, sep, machine = stem.partition("_@@_")
    if not sep or not repo or not machine:
        return None
    return "%s/%s" % (repo, machine)


def match_candidates(idx, filename, inner_name):
    """Feed entries that could be this local file.

    Tried in order of how much we trust them:
      1. the feed key encoded in the filename  (exact, unambiguous)
      2. the Name recorded inside the manifest (against titles and machineURLs)
      3. the filename stem                     (same)
    """
    cands, seen = [], set()

    def add(c):
        if c and c["key"] not in seen:
            seen.add(c["key"])
            cands.append(c)

    key = feed_key_from_filename(filename)
    if key:
        add(idx["key"].get(key.lower()))
    if cands:
        return cands

    stem = os.path.splitext(filename)[0]
    for probe in filter(None, (inner_name, stem)):
        p = probe.strip().lower()
        for c in idx["title"].get(p, []) + idx["machine"].get(p, []):
            add(c)
    return cands


def negative_expired(entry):
    """Should a remembered "matches nothing" be retried?

    A bare "" is the old format, written before negatives had an expiry and
    written even when a feed had failed to load. Those are never trustworthy,
    so they are always retried."""
    if entry == "":
        return True
    if isinstance(entry, dict):
        try:
            return datetime.now(timezone.utc) >= datetime.fromisoformat(
                entry.get("retry_after"))
        except (TypeError, ValueError):
            return True
    return True


def remember_no_match(cache, filename, feeds_complete):
    """Cache a no-match, but only when we actually saw every feed."""
    if not feeds_complete:
        LOG.warning("      Not recording this as unmatched: at least one feed "
                    "failed to load, so it may simply not have been visible.")
        return
    until = datetime.now(timezone.utc) + timedelta(days=NEGATIVE_MATCH_TTL_DAYS)
    cache[filename.lower()] = {"retry_after": until.isoformat(timespec="seconds")}


def disambiguate(cands, path, local_game):
    """Two lists can share a title across repos. Prefer hard evidence."""
    strong = [c for c in cands if verify_manifest(path, c["size"], c["hash"])[0]]
    if len(strong) == 1:
        return strong[0], "matches the published build"
    if local_game:
        g = local_game.lower()
        narrowed = [c for c in cands if c["game"].lower() == g]
        if len(narrowed) == 1:
            return narrowed[0], "only candidate for this game"
    return None, "ambiguous"


def resolve_tracking(state, rediscover=False):
    """Return {feed_key: local_filename} for every list we should follow.

    Results are cached in state["map"], keyed by filename, so the expensive
    all-feeds scan runs only when something new shows up. An empty string in
    the cache is a remembered "no match", which stops a private or renamed
    list from triggering a full scan on every single run.

    A file is followed only when its filename IS the feed key, or when the
    key it matches is pinned in TRACK. A title, or the Name inside the
    manifest, is a guess - an author's beta of a list carries the list's own
    Name - and a file matched by a guess was replaced by the published
    manifest at the next update, its own kept only for KEEP_BACKUP_DAYS. Such
    a match is now said, as a suggestion, and followed once it is pinned."""
    files = WJ.list_manifest_files(MANIFESTS)
    cache = state.setdefault("map", {})
    dirty = False
    if rediscover:
        LOG.info("--rediscover: clearing the cached list mapping.")
        cache.clear()
        dirty = True
    pinned = {str(k).strip().lower() for k in TRACK if str(k).strip()}
    # A pin with no file of its own yet may be for one of the files that
    # matched nothing on its own, so those are tried again while there is one.
    unclaimed_pins = pinned - {str(v).lower() for v in cache.values() if isinstance(v, str)}

    tracked, unresolved = {}, []

    def named_after(fn, key):
        return (feed_key_from_filename(fn) or "").lower() == str(key).lower()

    def claim(key, fn):
        """Follow `key` with the file `fn` - unless another file follows it
        already with the better claim, its name being the key. Two files can
        stand for one pinned list, the published one and a beta matched by
        its title, and whichever was met last used to win: the next update
        then replaced the beta with the published manifest."""
        cur = tracked.get(key)
        if cur and cur != fn:
            if named_after(cur, key) or not named_after(fn, key):
                LOG.warning("[WARN] %s and %s both stand for %s; following %s, and leaving "
                            "%s alone.", cur, fn, key, cur, fn)
                return
            LOG.warning("[WARN] %s and %s both stand for %s; following %s, which is named "
                        "after it, and leaving %s alone.", cur, fn, key, fn, cur)
        tracked[key] = fn

    for f in files:
        cached = cache.get(f.lower())
        if isinstance(cached, str) and cached:
            exact = named_after(f, cached)
            if exact or cached.lower() in pinned:
                claim(cached, f)
                continue
            # Matched by its title, by a version that still did so.
            LOG.warning("[UNTRACK] %s was followed as %s because of its title, which is "
                        "not proof that it is that list, and is no longer followed. Pin "
                        "%s in TRACK to keep following it.", f, cached, cached)
            del cache[f.lower()]
            dirty = True
            unresolved.append(f)
        elif cached is None:
            unresolved.append(f)
        elif negative_expired(cached) or unclaimed_pins:
            LOG.info("  %s was recorded as unmatched; retrying it.", f)
            unresolved.append(f)
        else:
            LOG.debug("  %s matched nothing recently; skipping until %s.",
                      f, cached.get("retry_after"))

    if unresolved and not AUTO_TRACK:
        LOG.warning("[WARN] AUTO_TRACK is off and %d file(s) are unpinned: %s",
                    len(unresolved), ", ".join(unresolved))
    elif unresolved:
        LOG.info("Resolving %d manifest(s) against the published feeds.",
                 len(unresolved))
        feeds, complete = load_feeds(None)
        idx = index_feeds(feeds)
        for f in unresolved:
            path = os.path.join(MANIFESTS, f)

            # Cheap probes first: the feed key encoded in the filename, then
            # the stem. Only if both miss do we crack the manifest open to read
            # its own Name, which on a 4 GB list means pulling the whole
            # modlist member across the share.
            cands = match_candidates(idx, f, None)
            key = (feed_key_from_filename(f) or "").lower()
            if cands and cands[0]["key"].lower() == key:
                pick, why = cands[0], "its filename is the feed key"
            else:
                if not cands:
                    cands = match_candidates(idx, f, manifest_name(path))
                chosen = [c for c in cands if c["key"].lower() in pinned]
                if not chosen:
                    if cands:
                        LOG.warning("[SUGGEST] %s is not named after a feed key, but looks "
                                    "like %s by its title or name. A title is no proof, so "
                                    "it is not followed; to follow it, add %s to TRACK.",
                                    f, " or ".join(c["key"] for c in cands),
                                    cands[0]["key"] if len(cands) == 1 else "the right one")
                    else:
                        LOG.warning("[WARN] %s matches no published list, so it will be "
                                    "left alone. Usually means private, renamed, or from "
                                    "a repo outside repositories.json.", f)
                    remember_no_match(cache, f, complete)
                    dirty = True
                    continue
                if len(chosen) == 1:
                    pick, why = chosen[0], "pinned in TRACK, and matched by its title"
                else:
                    rec = WJ.parse_manifest(path)
                    pick, why = disambiguate(chosen, path, rec.get("game"))
            if pick is None:
                LOG.warning("[WARN] %s matches %d published lists and they could "
                            "not be told apart. Add the right one to TRACK by "
                            "hand: %s", f, len(cands),
                            ", ".join(c["key"] for c in cands))
                remember_no_match(cache, f, complete)
                dirty = True
                continue
            LOG.info("[TRACK] %s  ->  %s  (%s)", f, pick["key"], why)
            cache[f.lower()] = pick["key"]
            claim(pick["key"], f)
            dirty = True
    if dirty:
        save_state(state)

    # Explicit pins always count. Reuse the real local filename when we can,
    # so pinning does not invent a second file alongside the one you have.
    realname = {f.lower(): f for f in files}
    for key in TRACK:
        if key in tracked:
            continue
        local = next((realname[fl] for fl, k in cache.items()
                      if k == key and fl in realname), None)
        tracked[key] = local

    # Drop anything excluded, by feed key or by filename stem.
    if EXCLUDE:
        drop = {e.strip().lower() for e in EXCLUDE if e and e.strip()}
        kept = {}
        for key, fn in tracked.items():
            stem = os.path.splitext(fn)[0].lower() if fn else ""
            if key.lower() in drop or (stem and stem in drop):
                LOG.info("[SKIP]  %s (excluded)", fn or key)
                continue
            kept[key] = fn
        tracked = kept

    if tracked:
        LOG.info("Following %d list(s).", len(tracked))
    return tracked


# Did every feed the last pending_updates() asked answer? A run that says
# "all current" when one did not has said something it does not know.
FEEDS_ANSWERED = True


def pending_updates(state, tracked):
    global FEEDS_ANSWERED
    if not tracked:
        LOG.error("[TERMINATED] No lists to follow. Put your .wabbajack files in "
                  "%s, or pin entries in TRACK.", MANIFESTS)
        return None, None, None
    repos = {k.split("/", 1)[0] for k in tracked}
    feeds, complete = load_feeds(repos)
    FEEDS_ANSWERED = bool(complete)
    if not complete:
        LOG.warning("[WARN] Not every feed answered; a list in one that did not is "
                    "not checked for an update this time.")
    if not feeds:
        return None, None, None

    known = state.get("lists", {})
    pending, seen, adopted, resume = [], [], [], []
    for key, local_file in tracked.items():
        ent = feeds.get(key)
        if not ent:
            LOG.warning("[WARN] Tracked list '%s' not present in its feed.", key)
            continue
        prev = known.get(key, {})
        filename = (local_file or prev.get("file")
                    or manifest_filename_for(key, ent))
        path = os.path.join(MANIFESTS, filename)
        on_disk = os.path.isfile(path)

        # Fast path: our state already says we hold this exact version.
        if (prev.get("version") == ent["version"]
                and prev.get("hash") == ent["hash"] and on_disk):
            # The manifest is current, but a previous run may have been unable
            # to finish fetching its archives (low disk, network drop, an
            # exhausted retry budget). Those need another pass even though the
            # version has not moved.
            if prev.get("needs_archives"):
                LOG.info("[RESUME] %s is current at v%s but its archives are "
                         "incomplete; retrying the fetch.", filename, ent["version"])
                resume.append((key, ent, prev, filename))
            seen.append((key, ent, prev, filename, False, on_disk))
            continue

        # Slow path. Before assuming we need a 700 MB manifest download, check
        # whether the copy on disk already IS the published one. This is what
        # makes the first run cheap: your existing manifests get adopted into
        # the state file instead of re-fetched.
        changed = True
        if on_disk:
            ok, why = verify_manifest(path, ent["size"], ent["hash"])
            if ok:
                LOG.info("[ADOPT] %s already matches feed v%s (%s)",
                         filename, ent["version"], why)
                adopted.append((key, ent, filename))
                changed = False
            else:
                LOG.debug("  %s differs from feed v%s: %s",
                          filename, ent["version"], why)
        seen.append((key, ent, prev, filename, changed, on_disk))
        if changed:
            pending.append((key, ent, prev, filename))

    # Record adoptions immediately so a later crash doesn't lose the cheap win.
    if adopted:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for key, ent, filename in adopted:
            state.setdefault("lists", {})[key] = {
                "version": ent["version"], "hash": ent["hash"],
                "file": filename, "title": ent["title"],
                "updated": ent["updated"], "applied": now, "adopted": True,
            }
        save_state(state)
    return seen, pending, resume


def cmd_check(state, tracked):
    banner("CHECK: TRACKED LISTS")
    seen, pending, resume = pending_updates(state, tracked)
    if seen is None:
        return 1
    for key, ent, prev, filename, changed, on_disk in seen:
        old = prev.get("version") or ("absent" if not on_disk else "unknown")
        flag = "UPDATE" if changed else "  ok  "
        LOG.info("[%s] %-40s %s -> %s", flag, key, old, ent["version"])
        if changed and ent["arch_size"]:
            LOG.info("           full list is %s of archives across %s file(s)",
                     WJ.human_bytes(ent["arch_size"]), ent["archives"])
    LOG.info("\n%d of %d tracked list(s) have updates.", len(pending), len(seen))
    return 0


def at_a_console():
    """Is somebody able to answer this run? A scheduled task has no console,
    and stdin is then not one.

    On Windows isatty() is no answer: it is true of NUL as well, which is
    what stdin is for a run started with it redirected there - by a
    scheduler, or `< nul`. Only a real console has a console mode."""
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return False
        if os.name != "nt":
            return True
        import ctypes
        import msvcrt
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetConsoleMode.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
        kernel32.GetConsoleMode.restype = ctypes.c_int
        mode = ctypes.c_uint32()
        return bool(kernel32.GetConsoleMode(msvcrt.get_osfhandle(sys.stdin.fileno()),
                                            ctypes.byref(mode)))
    except (AttributeError, ValueError, OSError):
        return False


def cmd_run(args, state, tracked):
    global NEXUS_MODE
    if NEXUS_MODE == "assist" and not getattr(args, "nexus_mode", None) and not at_a_console():
        # Assist mode opens a page per Nexus file and waits three minutes for
        # a click on each. Unattended, that is hours of waiting for nobody;
        # the settings' NEXUS_MODE is for the window and the console, and an
        # unattended run asked for assist mode by name would still get it.
        LOG.warning("[NOTE] Nobody is at a console to click Nexus pages, so this run "
                    "leaves Nexus files to Wabbajack (NEXUS_MODE skip). Pass "
                    "--nexus-mode assist to insist.")
        NEXUS_MODE = "skip"
    if not args.dry_run:
        purge_trash()
    seen, pending, resume = pending_updates(state, tracked)
    if seen is None:
        return 1

    only_stem = None
    if args.only:
        # Restrict the archive work to one list. Without this, a run downloads
        # archives for every list whose manifest moved - which in assist mode
        # means clicking through thousands of Nexus files across lists you were
        # not planning to install. Unlike --force this does not re-fetch a
        # manifest that is already current.
        want = args.only.strip().lower()
        sel = [(k, e, p, f) for k, e, p, f, _c, _d in seen
               if want in (k.lower(), os.path.splitext(f)[0].lower())]
        if not sel:
            LOG.error("[ERROR] --only '%s' matched no tracked list. Tracked: %s",
                      args.only, ", ".join(sorted(os.path.splitext(f)[0]
                                                  for _k, _e, _p, f, _c, _d in seen)))
            return 2
        keys = {k for k, _e, _p, _f in sel}
        pending = [p_ for p_ in pending if p_[0] in keys]
        resume = [r for r in resume if r[0] in keys]
        if not pending and not resume:
            # Manifest already current: still process its archives.
            resume = sel
        only_stem = os.path.splitext(sel[0][3])[0]
        LOG.info("--only: this run is limited to %s.", ", ".join(sorted(keys)))

    if not pending and not resume and not args.force:
        if not FEEDS_ANSWERED:
            LOG.warning("\n[DONE] Every tracked list that could be checked is current, "
                        "but not every feed answered, so not every list was checked.")
            return 1
        LOG.info("\n[DONE] All %d tracked list(s) are current. Nothing to do.",
                 len(seen))
        return 0

    if args.force:
        pending = [(k, e, p, f) for k, e, p, f, _c, _d in seen
                   if args.force in (k, os.path.splitext(f)[0])]
        if not pending:
            LOG.error("[ERROR] --force '%s' matched no tracked list.", args.force)
            return 2
        LOG.info("--force: reprocessing %s regardless of version.",
                 pending[0][0])

    failures = []

    # Manifests are not small - a big list's .wabbajack runs to several GB, and
    # a run can queue a dozen of them. Step 2 checked free space before
    # downloading archives; step 1 checked nothing at all and would happily
    # fill the volume.
    if pending and not args.dry_run:
        need = sum(e["size"] or 0 for _k, e, _p, _f in pending)
        if need and not free_space_ok(MANIFESTS, need):
            LOG.error("[TERMINATED] Not enough room for the manifests this run "
                      "would fetch. Free some space, or narrow the run with "
                      "--force <list>.")
            return 2

    banner(f"STEP 1: FETCHING {len(pending)} UPDATED MANIFEST(S)")
    if not pending:
        LOG.info("No manifest needs fetching.")
    updated = []
    # Lists whose manifest is already correct but whose archives are unfinished
    # go straight into step 2 without a manifest download.
    carried = [(k, e, f) for k, e, _p, f in resume]
    for key, ent, prev, filename in pending:
        LOG.info("\n%s  %s -> %s", key, prev.get("version") or "absent", ent["version"])
        stem = os.path.splitext(filename)[0]
        why = CFG.list_name_problem(stem)
        if why:
            LOG.error("  [ERROR] '%s' cannot be a modlist here: %s. Rename it before "
                      "tracking.", stem, why)
            failures.append(f"{filename}: {why}")
            continue
        if args.dry_run:
            LOG.info("  [DRY-RUN] would fetch %s", filename)
            continue
        # fetch_manifest only disturbs the existing file once the replacement
        # is downloaded and verified, so there is nothing to roll back here.
        if fetch_manifest(ent, filename):
            updated.append((key, ent, filename))
        else:
            failures.append("%s: the manifest was not updated (see the log for why)" % filename)

    work = updated + [c for c in carried
                      if c[0] not in {k for k, _e, _f in updated}]
    if not work:
        if args.dry_run:
            return 0
        LOG.warning("\n[DONE] No manifest was successfully updated.")
        return 1

    # ---- Plan and download the archives we are allowed to fetch --------
    banner("STEP 2: FETCHING NON-NEXUS ARCHIVES")
    summary, staged_candidates = [], []
    complete = {}          # key -> did we finish fetching everything we can?
    for key, ent, filename in work:
        record = WJ.parse_manifest(os.path.join(MANIFESTS, filename))
        record["name"] = os.path.splitext(filename)[0]
        if not record["game"]:
            LOG.warning("[WARN] %s declares no GameType; skipping downloads.", filename)
            complete[key] = False
            continue

        p = plan_for(record)
        need = total_size(p["direct"])
        LOG.info("\n%s (%s)", record["name"], WJ.game_label(record["game"]))
        LOG.info("  already on disk: %d", p["present"])
        LOG.info("  we can fetch:    %d  (%s)", len(p["direct"]), WJ.human_bytes(need))
        LOG.info("  Nexus:           %d  (%s) %s", len(p["nexus"]),
                 WJ.human_bytes(total_size(p["nexus"])),
                 "- you will click these" if NEXUS_MODE == "assist"
                 else "- left for Wabbajack")
        if p["other"]:
            LOG.info("  unsupported:     %d", len(p["other"]))

        staged_candidates.append((record, ent))

        fetchable = p["direct"] + (p["nexus"] if NEXUS_MODE == "assist" else [])
        complete.setdefault(key, not fetchable)
        if fetchable and not args.no_download and not args.dry_run:
            if not free_space_ok(p["base"], total_size(fetchable)):
                summary.append(f"{record['name']}: SKIPPED, low disk space")
                failures.append(f"{record['name']}: not downloaded, low disk space")
                complete[key] = False
                continue
            assist = (NEXUS_MODE == "assist")
            mode_flag = ["--nxm-assist"] if assist else ["--skip-nexus"]
            # By name, never by menu number: the number is only the list's
            # position in a menu built from whichever manifests happened to
            # parse, so one unreadable file used to shift it and send the whole
            # download at a different modlist.
            rc = run_script(DOWNLOADER_SCRIPT,
                            ["--game", record["game"], "--list", record["name"],
                             "--yes"] + mode_flag + child_flags(args)
                            + (["-v"] if args.verbose else []),
                            "downloader", interactive=assist)
            # rc 1 means some individual files failed, not that the run aborted;
            # anything else is the run itself failing, and the exit code says so.
            if rc not in (0, 1):
                summary.append(f"{record['name']}: downloader exited {rc}")
                failures.append(f"{record['name']}: the downloader exited {rc}")
            # The child just wrote into the pools, so the cached index of what
            # is on disk is stale. Everything else the parent does is read-only,
            # which is what makes caching the scan safe at all: over SMB it walks
            # thousands of files, and it used to run twice per list.
            WJ.invalidate_existing_cache()
            # Re-plan: anything still missing that we could have fetched means
            # the job is unfinished and must be retried on the next run.
            after = plan_for(record)
            left = after["direct"] + (after["nexus"] if NEXUS_MODE == "assist" else [])
            complete[key] = not left
            if left:
                LOG.warning("  [INCOMPLETE] %d fetchable archive(s) still "
                            "missing; will retry next run.", len(left))
                summary.append(f"{record['name']}: {len(left)} archive(s) still to fetch")
                failures.append(f"{record['name']}: {len(left)} archive(s) this run could "
                                f"fetch are still missing")

        todo = write_nexus_todo(record, p["nexus"])
        if todo:
            LOG.info("  Nexus to-do list: %s", os.path.basename(todo))
        planned = args.dry_run or args.no_download
        summary.append(
            f"{record['name']} v{ent['version']}: "
            f"{len(p['direct'])} {'to fetch here' if planned else 'fetched here'}, "
            f"{len(p['nexus'])} Nexus file(s) ({WJ.human_bytes(total_size(p['nexus']))}) "
            f"{'for clicks' if NEXUS_MODE == 'assist' else 'left for Wabbajack'}")

    # ---- Sort ---------------------------------------------------------
    if not args.no_sort and not args.dry_run and staged_candidates:
        banner("STEP 3: SORTING POOLS")
        # --only names the list this run is about, so it also decides what gets
        # staged. Lists span several games here, and each game has its own
        # pools: downloading one list's archives and then gathering a different
        # list - possibly for a different game entirely - is never the intent.
        target = only_stem or STAGE_LIST
        rec = None
        if target is None:
            rec = staged_candidates[-1][0]
            target = rec["name"]
            LOG.info("STAGE_LIST is unset; staging the most recently updated list.")
        else:
            rec = next((r for r, _ in staged_candidates if r["name"] == target), None)
            if rec is None:
                # A pinned list that did not happen to update this run must
                # STILL be the one staged. Staging something else gathers a
                # different list's archives into a different folder and undoes
                # the preparation for the list you actually install - which is
                # the entire reason for pinning one.
                pinned = os.path.join(MANIFESTS, target + ".wabbajack")
                if os.path.isfile(pinned):
                    rec = WJ.parse_manifest(pinned)
                    rec["name"] = target
                    LOG.info("STAGE_LIST '%s' did not update this run; staging "
                             "it anyway, as pinned.", target)
                else:
                    LOG.error("[ERROR] STAGE_LIST '%s' has no manifest in %s. "
                              "Skipping the sort rather than staging some other "
                              "list.", target, MANIFESTS)
                    failures.append(f"STAGE_LIST '{target}' not found")

        busy = wabbajack_busy() if rec is not None and rec.get("game") else False
        if rec is None or not rec.get("game"):
            LOG.warning("[SKIPPED] Nothing safe to stage this run.")
        elif busy is None:
            # Unattended, nobody to ask, and no way to know: not moving.
            LOG.error("[SKIPPED] Whether Wabbajack is running could not be checked; "
                      "not moving archives on a guess. The sort will happen on the "
                      "next run.")
            failures.append("sorter skipped: could not check whether Wabbajack is running")
        elif busy:
            # Unattended, so there is nobody to ask to close it. Moving
            # archives under a running install would break the install.
            LOG.error("[SKIPPED] Wabbajack is running; not moving archives "
                      "under it. The sort will happen on the next run.")
            failures.append("sorter skipped: Wabbajack is running")
        else:
            parts = find_part_files(WJ.resolve_game_dirs(rec["game"])[0])
            if parts and not SORTER_IGNORES_PART:
                LOG.error("[SKIPPED] %d leftover .part file(s) are in the pools "
                          "and this sorter is configured as not skipping them:",
                          len(parts))
                for p_ in parts[:10]:
                    LOG.error("  ? %s", p_)
                summary.append(f"sorter skipped: {len(parts)} .part file(s) present")
            else:
                if parts:
                    LOG.info("%d .part file(s) in the pools; the sorter leaves "
                             "them alone and their resume progress is safe.",
                             len(parts))
                rc = run_script(SORTER_SCRIPT,
                                ["--game", rec["game"], "--list", target, "--yes"]
                                + (["-v"] if args.verbose else []),
                                "sorter")
                if rc != 0:
                    failures.append(f"sorter exited {rc}")
                else:
                    # The wizard's stage 5 warns before a rebuild undoes this.
                    state.setdefault("staged", {})[WJ.folder_key(rec["game"])] = target
                summary.append(f"sorter staged '{target}' (exit {rc})")

    # ---- Record state and report --------------------------------------
    if not args.dry_run:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for key, ent, filename in work:
            state.setdefault("lists", {})[key] = {
                "version": ent["version"],
                "hash":    ent["hash"],
                "file":    filename,
                "title":   ent["title"],
                "updated": ent["updated"],
                "applied": now,
                "needs_archives": not complete.get(key, False),
            }
        save_state(state)

    prune_backups()

    banner("RUN COMPLETE")
    for line in summary:
        LOG.info("-> %s", line)
    if failures:
        LOG.error("")
        LOG.error("%d problem(s) this run:", len(failures))
        for f_ in failures:
            LOG.error("  ! %s", f_)
    LOG.info("")

    body = summary + ((["", "PROBLEMS:"] + failures) if failures else [])
    notify("Wabbajack: %d list(s) processed%s"
           % (len(work), " (%d problem(s))" % len(failures) if failures else ""),
           NEWLINE.join(body) or "no detail")
    # A scheduler has to be able to tell "clean" from "finished, but something
    # failed" - every path that reached here used to return 0 regardless.
    return 1 if failures else 0


def cmd_nexus_key(args):
    """--store-nexus-key, --forget-nexus-key and --nexus-key-status.

    Storing moves the key from nexus_api_key.txt into Windows Credential
    Manager when the file holds one, and asks for it otherwise, without
    echoing it. Nothing here needs the share, so it runs before bootstrap.
    The key itself is never printed; only where it is."""
    import wj_keystore as KS
    if (args.store_nexus_key or args.forget_nexus_key) and not KS.available():
        print("There is no Windows Credential Manager here, so the key stays in "
              f"{KS.ENV_VAR} or {KS.KEY_FILE}.")
        return 2
    if args.forget_nexus_key:
        print("The stored Nexus API key was removed." if KS.forget()
              else "No Nexus API key was stored.")
    elif args.store_nexus_key:
        if KS.read_file(HERE):
            try:
                print(KS.move_file_in(HERE))
            except (OSError, ValueError) as e:
                print(f"Not moved: {e}. {KS.KEY_FILE} is untouched.")
                return 1
        else:
            import getpass
            try:
                key = getpass.getpass("Your Nexus Personal API key (not shown as you type): ")
            except (EOFError, KeyboardInterrupt):
                print()
                return 1
            try:
                KS.store(key)
            except (OSError, ValueError) as e:
                print(f"Not stored: {e}")
                return 1
            print(f"Stored in Windows Credential Manager as {KS.TARGET}.")
    print(f"The Nexus API key is {KS.describe(HERE)}.")
    return 0


# ----------------------------------------------------------------------
def main():
    global WJ, NEXUS_MODE, DOWNLOAD_ORDER, DRY_RUN
    ap = argparse.ArgumentParser(
        description="Watch Wabbajack modlists for updates and drive the "
                    "downloader and sorter unattended.")
    ap.add_argument("--discover", action="store_true",
                    help="Show how every local .wabbajack matches up against the "
                         "published feeds, including ambiguous ones. Purely "
                         "informational; tracking is automatic.")
    ap.add_argument("--selftest", action="store_true",
                    help="Check that Wabbajack's feed, manifest and CDN formats "
                         "still look the way this tooling reads them, and that "
                         "every archive type is one it recognises. Use it when "
                         "results look implausible: it separates 'upstream "
                         "changed' from 'your collection is odd'.")
    ap.add_argument("--gallery", action="store_true",
                    help="Summarise the published gallery: how many lists "
                         "exist for the games you keep pools for, how many you "
                         "already track, and how much of the validation-report "
                         "cache is present. Changes nothing on the pools.")
    ap.add_argument("--sync-reports", action="store_true",
                    help="With --gallery, fetch the per-list validation "
                         "reports into the cache (224 files, about 45 MB). "
                         "These are what the browse stage uses to say how much "
                         "of a list you already hold without downloading its "
                         ".wabbajack first.")
    ap.add_argument("--check", action="store_true",
                    help="Report which tracked lists have updates. Changes nothing.")
    ap.add_argument("--run", action="store_true",
                    help="A full unattended run: fetch every updated manifest, the "
                         "archives they are missing, and stage STAGE_LIST (or the most "
                         "recently updated list). What a scheduled task runs. With "
                         "nobody at a console, Nexus files are left to Wabbajack "
                         "unless --nexus-mode assist insists.")
    ap.add_argument("--only", metavar="LIST",
                    help="Do the archive work for just this list (by TRACK key "
                         "or filename stem), leaving other updated lists' "
                         "manifests alone. Use this for a Nexus click session: "
                         "without it a run walks every updated list in turn.")
    ap.add_argument("--force", metavar="LIST",
                    help="Reprocess one list (by TRACK key or filename stem) even "
                         "if its version has not moved.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Plan everything, download and move nothing.")
    ap.add_argument("--no-download", action="store_true",
                    help="Update manifests only; do not fetch archives.")
    ap.add_argument("--no-sort", action="store_true",
                    help="Skip the sorter at the end of the run.")
    ap.add_argument("--rediscover", action="store_true",
                    help="Forget the cached list mapping and match every local "
                         ".wabbajack against the feeds again. Use after "
                         "renaming a file, or to retry an unmatched one.")
    ap.add_argument("--no-lock", action="store_true",
                    help="Skip the single-instance lock. Use if a stale lock on "
                         "a network share is blocking you.")
    ap.add_argument("--gui", action="store_true",
                    help="Open the desktop window. This is what run.bat does "
                         "with no arguments.")
    ap.add_argument("--wizard", action="store_true",
                    help="Interactive console mode: walk the five stages "
                         "(browse the gallery, updates, incomplete lists, "
                         "prepare one for install, disk space) showing status "
                         "and offering skip / all / some at each. This is what "
                         "a bare launch of this script does.")
    ap.add_argument("--nexus-mode", choices=("skip", "assist"),
                    help="Override NEXUS_MODE for this run. 'assist' opens each "
                         "Nexus page and waits for your click (needs you at the "
                         "keyboard); 'skip' leaves Nexus files to Wabbajack and "
                         "is the only mode that works unattended.")
    ap.add_argument("--order", choices=("smallest", "largest", "manifest"),
                    help="Order archives are fetched in. 'smallest' finishes "
                         "the most files soonest and loses the least to an "
                         "interruption.")
    ap.add_argument("--allow-size-only", action="store_true",
                    help="Proceed without xxhash, checking sizes only.")
    ap.add_argument("--store-nexus-key", action="store_true",
                    help="Keep the Nexus API key in Windows Credential Manager: the one in "
                         "nexus_api_key.txt, which is then deleted, or one you type.")
    ap.add_argument("--forget-nexus-key", action="store_true",
                    help="Remove the Nexus API key from Windows Credential Manager.")
    ap.add_argument("--nexus-key-status", action="store_true",
                    help="Say where the Nexus API key is found, never what it is.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.store_nexus_key or args.forget_nexus_key or args.nexus_key_status:
        return cmd_nexus_key(args)

    # A bare launch is the interactive wizard; the flags stay for scripted and
    # scheduled use. Keeping one entry point means run.bat needs no arguments
    # and the two interfaces cannot drift apart - the wizard drives exactly the
    # same functions the flags do.
    # The wizard is the default unless an action flag asks for something
    # specific. Testing len(sys.argv) instead would send "--nexus-mode assist"
    # - a modifier, not an action - down the non-interactive path, which is
    # exactly the sort of surprise a scheduled run should never spring.
    # --order and --nexus-mode are modifiers too; --run is the action that
    # asks for a full unattended run, which before it existed could only be
    # had by adding a flag with side effects of its own.
    actions = (args.run, args.check, args.discover, args.only, args.force, args.dry_run,
               args.no_download, args.no_sort, args.rediscover, args.selftest,
               args.gallery, args.sync_reports)
    if args.order:
        # One run's order, for the window and the wizard as for a flag run.
        DOWNLOAD_ORDER = args.order
    if args.gui:
        import wj_gui
        return wj_gui.main(nexus_mode=args.nexus_mode,
                           allow_size_only=args.allow_size_only,
                           no_lock=args.no_lock, verbose=args.verbose)
    if args.wizard or not any(actions):
        import wj_wizard
        return wj_wizard.run(verbose=args.verbose, nexus_mode=args.nexus_mode,
                             allow_size_only=args.allow_size_only,
                             no_lock=args.no_lock)

    DRY_RUN = bool(args.dry_run)
    # A run that only reports changes nothing, the nxm:// handler included.
    reports_only = (args.check or args.dry_run or args.gallery or args.sync_reports
                    or args.selftest or args.discover)
    rc = bootstrap(verbose=args.verbose, nexus_mode=args.nexus_mode,
                   allow_size_only=args.allow_size_only, recover=not reports_only)
    if rc:
        if not args.check and not args.dry_run:
            # A run that could not start is one a scheduler most needs to hear of.
            notify("Wabbajack updater could not start (exit %d)" % rc,
                   "See runtime\\wj_autoupdate.log beside the tools.")
        return rc

    # Read-only, and outside the lock; but an error in one of them is still
    # an unhandled error, exit 4 with its traceback in the log, rather than
    # Python's own exit 1, which reads as "finished with problems".
    try:
        if args.selftest:
            return cmd_selftest()
        if args.gallery or args.sync_reports:
            return cmd_gallery(sync=args.sync_reports)
        if args.discover:
            return cmd_discover()
    except Exception:
        LOG.exception("[TERMINATED] Unhandled error; see the traceback above.")
        return 4

    # Everything past here touches the pools, so serialise it.
    lock_fd = None
    if not args.no_lock:
        lock_fd = acquire_lock(runtime_path(LOCK_FILE))
        if lock_fd is HELD:
            LOG.info("[SKIPPED] Another run holds the lock. Exiting without "
                     "doing anything. (If nothing else is running, use "
                     "--no-lock.)")
            # Exit 3, not 0: "did nothing because something else was running"
            # is not the same as "ran and everything was current", and a
            # scheduler that cannot tell them apart reports a healthy run
            # forever while nothing actually happens.
            return 3
        if lock_fd is UNAVAILABLE:
            LOG.warning("[WARN] Could not lock on this filesystem; continuing "
                        "without one. Do not start a second run by hand.")
            lock_fd = None

    rc = 2
    try:
        state = load_state()
        tracked = resolve_tracking(state, rediscover=args.rediscover)
        rc = cmd_check(state, tracked) if args.check else cmd_run(args, state, tracked)
        return rc
    except KeyboardInterrupt:
        rc = 130                    # stopped by the person running it: not a failure to report
        raise
    except Exception:
        LOG.exception("[TERMINATED] Unhandled error; see the traceback above.")
        rc = 4
        return rc
    finally:
        release_lock(lock_fd)
        # cmd_run reports its own outcome when it gets to the end. This
        # covers every path that did not - a dead feed, no space, an unhandled
        # error, an early "nothing could be checked" - which previously sent
        # nothing at all unless the code was 2 or more, exactly backwards for
        # a run you are not watching.
        if rc not in (0, 130) and not _NOTIFIED and not args.check and not args.dry_run:
            notify("Wabbajack updater finished with problems (exit %d)" % rc,
                   "See runtime\\wj_autoupdate.log beside the tools.")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        LOG.info("\n[ABORT] Interrupted.")
        sys.exit(130)
