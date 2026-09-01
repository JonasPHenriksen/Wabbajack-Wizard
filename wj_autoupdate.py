#!/usr/bin/env python3
"""
Wabbajack Modlist Auto-Updater
==============================
Front-end for wabbajack_archive_downloader.py and wabbajack_archive_sorter_v2.py.
Runs on Windows against the mapped share, or headless on Linux against a local
path. You launch it; it does the rest in one pass.

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

Windows, from the share (the usual setup)
-----------------------------------------
Put this file next to the two scripts and your .wabbajack files on the mapped
drive. Leave ARCHIVE_ROOT as your Windows path, the r"Z:\\Mod Archive" you
already had. Then
either double-click run.bat or:

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

Linux, headless
---------------
Same script. Set ARCHIVE_ROOT to a POSIX path in both of the other scripts and
leave NEXUS_MODE as "skip": --nxm-assist needs winreg and a browser, so the
script refuses it off Windows. Pair with the systemd timer if you want it on a
schedule.

Requirements
------------
  * Python 3.8+
  * The two existing scripts, in the same folder as this one.
  * Optional but strongly recommended: xxhash, so manifests and archives get
    real hash verification instead of a size check.
        Windows:  py -3 -m pip install xxhash
        Debian:   sudo apt install python3-xxhash
"""

import os
import re
import sys
import json
import time
import shutil
import base64
import struct
import logging
import argparse
import importlib.util
import subprocess
import urllib.error
import urllib.request
import concurrent.futures
from datetime import datetime, timezone

# ============================ CONFIGURATION ============================

# The two existing scripts. They must sit next to this file, which is also
# where the .wabbajack manifests live.
DOWNLOADER_SCRIPT = "wabbajack_archive_downloader.py"
SORTER_SCRIPT     = "wabbajack_archive_sorter_v2.py"

# ARCHIVE_ROOT is NOT configured here. It is read from the downloader script so
# there is exactly one place to set it. Edit ARCHIVE_ROOT in
# wabbajack_archive_downloader.py and wabbajack_archive_sorter_v2.py to your
# server-side POSIX path, e.g.:
#     ARCHIVE_ROOT = "/mnt/dietpi_userdata/mod-archive"
# Use the local path, not the //server/share SMB path.

# Which lists to follow.
#
# AUTO_TRACK = True is the zero-config default: every .wabbajack in this folder
# is matched against the published feeds automatically. The resolved mapping is
# cached in the state file, so the full feed scan only happens when a file turns
# up that has not been resolved before. Drop a new .wabbajack in the folder and
# it gets picked up on the next run; no config edit needed.
AUTO_TRACK = True

# Where the .wabbajack manifests live. This is deliberately separate from where
# the scripts live, so the tooling can sit in its own folder and keep its
# bookkeeping out of Wabbajack's directories.
#
# Anything that is not an absolute path is resolved against THIS SCRIPT's
# folder, never the working directory, so the whole tree can be copied around
# or reached through a different drive letter or a UNC path and still work.
#
#   None                                 -> the folder this script is in
#   "modlists"                           -> a subfolder of it       (recommended)
#   r"..\Wabbajack\*\downloaded_mod_lists" -> sibling of the tools folder, globbed
#   r"Z:\Wabbajack_Tools\modlists"       -> absolute, if you prefer to pin it
#
# A relative folder that lives inside the script's own folder is created if
# missing. Anything outside it must already exist, so a typo is reported rather
# than silently turned into an empty directory.
#
# The glob form follows Wabbajack's versioned folder. Note that a Wabbajack
# upgrade creates a fresh, EMPTY downloaded_mod_lists, at which point every
# manifest looks missing and gets re-downloaded. Owning your own folder avoids
# that entirely.
MANIFEST_DIR = "modlists"

# Lists to leave alone, matched against either the feed key or the .wabbajack
# filename (without extension), case-insensitively. Useful when the folder holds
# more lists than you actually install: a .wabbajack can be several GB on its
# own, so every list you follow costs real bandwidth.
#     EXCLUDE = ["LoreRim/LoreRim", "ForgottenGlory_@@_masterstroke"]
EXCLUDE = []

# Optional pins, as "<repo>/<machineURL>". Only needed for a list that
# auto-matching cannot resolve, which it will tell you about. Anything listed
# here is followed regardless.
TRACK = []

# Which list the sorter should stage at the end of a run.
#   None -> stage whichever tracked list was updated most recently this run.
#   "Name" -> always stage this one (the .wabbajack filename without extension).
# Only one list can be staged per run: the sorter gathers everything that list
# needs into its folder, so staging a second list would immediately undo the
# first. Pick the list you actually install from.
STAGE_LIST = None

# How to handle the Nexus archives.
#   "skip"   -> do not touch Nexus at all. Wabbajack fetches them itself later.
#               The only option on Linux, and the simplest one on Windows.
#   "assist" -> hand them to your downloader's --nxm-assist mode, which opens
#               each mod page in turn and captures your click. Windows only,
#               needs a browser and a real console. Still one click per file:
#               this is the same manual step Wabbajack would ask of you, just
#               driven by your script instead.
NEXUS_MODE = "skip"

# Set to True only AFTER applying the one-line .part fix to the sorter (see
# README). Until then, a leftover .part file from a failed download makes the
# sorter unsafe to run unattended and this script will skip it.
SORTER_IGNORES_PART = False

# Refuse to start a download batch that would leave less than this much free.
FREE_SPACE_MARGIN_GB = 20

# Notification target. ntfy topic URL or a Discord webhook URL. None disables.
#   NOTIFY_URL = "https://ntfy.sh/some-long-random-topic-name"
#   NOTIFY_URL = "https://discord.com/api/webhooks/..."
NOTIFY_URL = None

# Wabbajack's registry of metadata feeds.
REPOSITORIES_URL = ("https://raw.githubusercontent.com/wabbajack-tools/"
                    "mod-lists/master/repositories.json")

HTTP_TIMEOUT = 30
USER_AGENT   = "WJAutoUpdate/1.0"
STATE_FILE   = "wj_autoupdate_state.json"
LOCK_FILE    = ".wj_autoupdate.lock"
# =======================================================================

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = logging.getLogger("wj_autoupdate")

# Filled in by resolve_dirs() during startup.
MANIFESTS = HERE          # where the .wabbajack files are
BACKUPS = HERE            # where superseded/rejected manifests are parked

# Wabbajack CDN multi-part support. Manifests on authored-files.wabbajack.org
# are served as a definition plus numbered parts, not as a plain file, so a
# straight GET returns a ~1 KB error page.
try:
    import wabbajack_cdn as CDN
    _HAVE_CDN = True
except Exception:
    CDN = None
    _HAVE_CDN = False


# ----------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------
def setup_logging(verbose=False):
    LOG.setLevel(logging.DEBUG)
    con = logging.StreamHandler(sys.stdout)
    con.setLevel(logging.DEBUG if verbose else logging.INFO)
    con.setFormatter(logging.Formatter("%(message)s"))
    LOG.addHandler(con)
    try:
        fh = logging.FileHandler(os.path.join(HERE, "wj_autoupdate.log"),
                                 encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        LOG.addHandler(fh)
    except OSError as e:
        LOG.warning("[WARN] Could not open log file: %s", e)


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
def load_downloader_module():
    path = os.path.join(HERE, DOWNLOADER_SCRIPT)
    if not os.path.isfile(path):
        LOG.error("[TERMINATED] %s not found next to this script.", DOWNLOADER_SCRIPT)
        sys.exit(1)
    spec = importlib.util.spec_from_file_location("wj_dl", path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        LOG.error("[TERMINATED] Could not import %s: %s", DOWNLOADER_SCRIPT, e)
        sys.exit(1)
    # Keep its chatter out of our log except for real problems.
    mod.LOG.setLevel(logging.WARNING)
    mod.LOG.addHandler(logging.NullHandler())
    return mod


WJ = None  # populated in main()


# ----------------------------------------------------------------------
# Single-instance lock, portable across Windows and Linux
# ----------------------------------------------------------------------
HELD = object()          # someone else has it
UNAVAILABLE = object()   # this filesystem cannot lock; caller decides


def acquire_lock(path):
    """Best-effort exclusive lock. Returns a file object, HELD, or UNAVAILABLE.

    flock does not exist on Windows and msvcrt.locking is unreliable over SMB,
    so a failure to lock is reported as UNAVAILABLE rather than treated as
    contention. With a single person launching runs by hand that is fine; the
    lock only exists to stop an accidental double-click."""
    try:
        fd = open(path, "a+")
    except OSError:
        return UNAVAILABLE
    try:
        if os.name == "nt":
            import msvcrt
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        fd.close()
        return HELD
    except Exception:
        fd.close()
        return UNAVAILABLE
    try:
        fd.seek(0)
        fd.truncate(0) if os.name != "nt" else None
        fd.write(f"{os.getpid()} {datetime.now().isoformat()}\n")
        fd.flush()
    except OSError:
        pass
    return fd


def release_lock(fd):
    if fd is None or fd is HELD or fd is UNAVAILABLE:
        return
    try:
        if os.name == "nt":
            import msvcrt
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        fd.close()
    except OSError:
        pass


# ----------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------
def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        raw = r.read()
    return json.loads(raw.decode("utf-8-sig"))


def notify(title, body):
    if not NOTIFY_URL:
        return
    try:
        if "discord.com" in NOTIFY_URL or "discordapp.com" in NOTIFY_URL:
            payload = json.dumps({"content": f"**{title}**\n{body}"[:1900]}).encode()
            headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        else:  # ntfy
            payload = body.encode("utf-8")
            headers = {"Title": title, "User-Agent": USER_AGENT}
        req = urllib.request.Request(NOTIFY_URL, data=payload, headers=headers)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT):
            pass
        LOG.debug("Notification sent.")
    except Exception as e:
        LOG.warning("[WARN] Notification failed: %s", e)


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
    mu = entry_field(entry, "machineURL")
    if not mu:
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
    }


def load_feeds(repo_names=None):
    """Fetch repositories.json, then the named feeds (or all of them).
    Returns {key: normalised_entry}."""
    LOG.info("Fetching feed registry...")
    try:
        repos = http_json(REPOSITORIES_URL)
    except Exception as e:
        LOG.error("[ERROR] Could not read repositories.json: %s", e)
        return {}
    if not isinstance(repos, dict):
        LOG.error("[ERROR] repositories.json was not an object.")
        return {}

    wanted = {r: u for r, u in repos.items()
              if repo_names is None or r in repo_names}
    missing = set(repo_names or []) - set(wanted)
    if missing:
        LOG.warning("[WARN] Unknown repo(s) in TRACK, ignoring: %s",
                    ", ".join(sorted(missing)))
    LOG.info("Fetching %d feed(s)...", len(wanted))

    out = {}

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
                continue
            n = 0
            for raw in iter_entries(data):
                ent = normalise(raw, repo)
                if ent:
                    out[ent["key"]] = ent
                    n += 1
            LOG.debug("  %-28s %d list(s)", repo, n)
    LOG.info("Indexed %d list(s) across %d feed(s).", len(out), len(wanted))
    return out


# ----------------------------------------------------------------------
# Local manifests and state
# ----------------------------------------------------------------------
def local_manifests():
    """{filename: record} for every .wabbajack next to this script."""
    out = {}
    for f in sorted(os.listdir(MANIFESTS)):
        if f.lower().endswith(".wabbajack"):
            out[f] = WJ.parse_manifest(os.path.join(MANIFESTS, f))
    return out


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


def load_state():
    p = os.path.join(HERE, STATE_FILE)
    try:
        with open(p, encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {"lists": {}}
    except (OSError, json.JSONDecodeError):
        return {"lists": {}}


def save_state(state):
    p = os.path.join(HERE, STATE_FILE)
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, p)
    except OSError as e:
        LOG.error("[ERROR] Could not write state file: %s", e)


def safe_filename(title):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")
    return (name or "modlist") + ".wabbajack"


# ----------------------------------------------------------------------
# Manifest download
# ----------------------------------------------------------------------
def CDN_LOOKS_NEEDED(url):
    """Cheap host check used only to give a clear error when wabbajack_cdn.py
    is absent."""
    return isinstance(url, str) and (
        "wabbajack.org" in url or "b-cdn.net" in url)


def verify_manifest(path, size, hash_b64):
    try:
        actual = os.path.getsize(path)
    except OSError as e:
        return False, f"cannot stat ({e})"
    if size is not None and actual != size:
        return False, f"size {actual} != feed's {size}"
    if hash_b64 and getattr(WJ, "_HAVE_XXHASH", False):
        got = WJ.wabbajack_hash(path)
        if got and got != hash_b64:
            return False, "hash mismatch"
        if got is None:
            return True, "ok (hash unavailable)"
    elif hash_b64:
        return True, "ok (size only; install xxhash for hash checks)"
    return True, "ok"


def fetch_manifest(entry, filename):
    """Download entry's .wabbajack into the manifest folder, atomically."""
    dest = os.path.join(MANIFESTS, filename)
    part = dest + ".part"
    url = entry["download"]
    if not url:
        LOG.error("  [ERROR] '%s' has no download link in its feed.", entry["title"])
        return False

    LOG.info("  Downloading %s v%s (%s)", filename, entry["version"],
             WJ.human_bytes(entry["size"]) if entry["size"] else "size unknown")

    # Nearly every published manifest lives on the Wabbajack CDN, which needs
    # the multi-part protocol rather than a plain GET.
    if _HAVE_CDN and (CDN.is_cdn_url(url) or CDN.looks_multipart(url)):
        ok = CDN.download(url, dest, expected_size=entry["size"],
                          expected_hash=entry["hash"])
    else:
        if not _HAVE_CDN and CDN_LOOKS_NEEDED(url):
            LOG.error("  [ERROR] %s is a Wabbajack CDN URL but wabbajack_cdn.py "
                      "is missing from %s. Put it next to this script.",
                      filename, HERE)
            return False
        ok = WJ.stream_download(WJ.sanitize_url(url), dest,
                                expected_size=entry["size"])
    if not ok:
        LOG.error("  [ERROR] Manifest download failed for %s", filename)
        return False

    ok, why = verify_manifest(dest, entry["size"], entry["hash"])
    if not ok:
        LOG.error("  [ERROR] %s failed verification: %s - discarding.", filename, why)
        quarantine = os.path.join(BACKUPS, os.path.basename(dest) + ".rejected")
        try:
            os.replace(dest, quarantine)
            LOG.error("  Rejected copy kept at %s for inspection.",
                      os.path.basename(quarantine))
        except OSError:
            pass
        return False

    LOG.info("  Verified: %s", why)
    for stray in (part,):
        if os.path.exists(stray):
            try:
                os.remove(stray)
            except OSError:
                pass
    return True


def backup_existing(filename):
    src = os.path.join(MANIFESTS, filename)
    if not os.path.isfile(src):
        return
    bak = os.path.join(BACKUPS, filename + ".bak")
    try:
        if os.path.exists(bak):
            os.remove(bak)
        os.replace(src, bak)
        LOG.info("  Previous manifest kept as %s", os.path.basename(bak))
    except OSError as e:
        LOG.warning("  [WARN] Could not back up %s: %s", filename, e)


# ----------------------------------------------------------------------
# Work planning against the pools
# ----------------------------------------------------------------------
def plan_for(record):
    """Split a parsed manifest's archives into what we can fetch, what only
    Wabbajack can fetch, and what is already on disk."""
    game_base, _, _ = WJ.resolve_game_dirs(record["game"])
    existing = WJ.index_existing(game_base)
    direct, nexus, other, present = [], [], [], 0
    for a in record["archives"]:
        if a["name"].lower() in existing:
            present += 1
            continue
        kind = WJ.state_type(a["state"])
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
    """CSV of the Nexus download pages still outstanding, for the desktop."""
    if not nexus_items:
        return None
    import csv
    path = os.path.join(HERE, f"nexus_todo_{record['name']}.csv")
    try:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["archive", "size_bytes", "download_page"])
            for a in nexus_items:
                dom = WJ.nexus_domain(a)
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
def list_number(game, name):
    """Reproduce the menu number both scripts assign to a list.

    Both build their menu from sorted(*.wabbajack) filtered to the chosen
    game, numbered from 1, and both derive the display name from the filename
    stem. Computing it here means neither script needs modifying."""
    files = sorted(f for f in os.listdir(MANIFESTS) if f.lower().endswith(".wabbajack"))
    in_scope = []
    for f in files:
        rec = WJ.parse_manifest(os.path.join(MANIFESTS, f))
        if rec["game"] == game:
            in_scope.append(os.path.splitext(f)[0])
    for i, nm in enumerate(in_scope, 1):
        if nm == name:
            return str(i)
    return None


def run_script(script, args, label, interactive=False):
    """Run one of the existing scripts.

    interactive=True hands the child our real console and stdin, which
    --nxm-assist needs: it prints per-file instructions, opens a browser, and
    waits on your clicks. Everything else is captured to a file.

    Their stdout carries \\r-based progress bars that would otherwise pile up
    in the systemd journal as enormous single lines, so it goes to a file
    instead. Both scripts already write their own timestamped logs next to
    themselves, so nothing is lost; we tail this capture only on failure."""
    cmd = [sys.executable, os.path.join(HERE, script)] + args
    LOG.info("\n$ %s", " ".join(cmd))
    # The child scripts default to scanning their own folder for manifests;
    # this tells them to look where we actually keep them.
    env = dict(os.environ, PYTHONUNBUFFERED="1", WJ_MANIFEST_DIR=MANIFESTS)
    cap = os.path.join(HERE, f"wj_autoupdate_{label}.out")
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
# Commands
# ----------------------------------------------------------------------
def cmd_discover():
    banner("DISCOVER: MATCHING LOCAL MANIFESTS TO FEEDS")
    feeds = load_feeds(None)
    if not feeds:
        return 1

    by_machine, by_title = {}, {}
    for k, e in feeds.items():
        by_machine.setdefault(e["machine"].lower(), []).append(e)
        by_title.setdefault(e["title"].strip().lower(), []).append(e)

    files = [f for f in sorted(os.listdir(MANIFESTS)) if f.lower().endswith(".wabbajack")]
    if not files:
        LOG.warning("No .wabbajack files found in %s", MANIFESTS)

    matched, unmatched = [], []
    for f in files:
        stem = os.path.splitext(f)[0]
        inner = manifest_name(os.path.join(MANIFESTS, f))
        cands = []
        for probe in filter(None, (inner, stem)):
            cands += by_title.get(probe.strip().lower(), [])
            cands += by_machine.get(probe.strip().lower(), [])
        seen, uniq = set(), []
        for c in cands:
            if c["key"] not in seen:
                seen.add(c["key"])
                uniq.append(c)
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
    by_machine, by_title = {}, {}
    for e in feeds.values():
        by_machine.setdefault(e["machine"].lower(), []).append(e)
        by_title.setdefault(e["title"].strip().lower(), []).append(e)
    return by_machine, by_title


def match_candidates(by_machine, by_title, filename, inner_name):
    """Feed entries whose title or machineURL matches this file's name or the
    Name recorded inside its manifest."""
    stem = os.path.splitext(filename)[0]
    cands, seen = [], set()
    for probe in filter(None, (inner_name, stem)):
        p = probe.strip().lower()
        for c in by_title.get(p, []) + by_machine.get(p, []):
            if c["key"] not in seen:
                seen.add(c["key"])
                cands.append(c)
    return cands


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
    list from triggering a full scan on every single run."""
    files = [f for f in sorted(os.listdir(MANIFESTS)) if f.lower().endswith(".wabbajack")]
    cache = state.setdefault("map", {})
    if rediscover:
        LOG.info("--rediscover: clearing the cached list mapping.")
        cache.clear()

    tracked, unresolved = {}, []
    for f in files:
        cached = cache.get(f.lower())
        if cached:
            tracked[cached] = f
        elif cached == "":
            LOG.debug("  %s previously matched nothing; skipping.", f)
        else:
            unresolved.append(f)

    if unresolved and not AUTO_TRACK:
        LOG.warning("[WARN] AUTO_TRACK is off and %d file(s) are unpinned: %s",
                    len(unresolved), ", ".join(unresolved))
    elif unresolved:
        LOG.info("Resolving %d new manifest(s) against the published feeds. This "
                 "pulls every feed once and takes a moment.", len(unresolved))
        by_machine, by_title = index_feeds(load_feeds(None))
        for f in unresolved:
            path = os.path.join(MANIFESTS, f)
            rec = WJ.parse_manifest(path)
            cands = match_candidates(by_machine, by_title, f, manifest_name(path))
            if not cands:
                LOG.warning("[WARN] %s matches no published list, so it will be "
                            "left alone. Usually means private, renamed, or from "
                            "a repo outside repositories.json.", f)
                cache[f.lower()] = ""
                continue
            if len(cands) == 1:
                pick, why = cands[0], "unique match"
            else:
                pick, why = disambiguate(cands, path, rec.get("game"))
            if pick is None:
                LOG.warning("[WARN] %s matches %d published lists and they could "
                            "not be told apart. Add the right one to TRACK by "
                            "hand: %s", f, len(cands),
                            ", ".join(c["key"] for c in cands))
                cache[f.lower()] = ""
                continue
            LOG.info("[TRACK] %s  ->  %s  (%s)", f, pick["key"], why)
            cache[f.lower()] = pick["key"]
            tracked[pick["key"]] = f
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


def pending_updates(state, tracked):
    if not tracked:
        LOG.error("[TERMINATED] No lists to follow. Put your .wabbajack files in "
                  "%s, or pin entries in TRACK.", MANIFESTS)
        return None, None, None
    repos = {k.split("/", 1)[0] for k in tracked}
    feeds = load_feeds(repos)
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
        filename = local_file or prev.get("file") or safe_filename(ent["title"])
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


def cmd_run(args, state, tracked):
    seen, pending, resume = pending_updates(state, tracked)
    if seen is None:
        return 1

    if not pending and not resume and not args.force:
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
        if stem.lower() in ("master", "shared"):
            LOG.error("  [ERROR] '%s' clashes with a reserved pool folder name. "
                      "Rename it before tracking.", stem)
            continue
        if args.dry_run:
            LOG.info("  [DRY-RUN] would fetch %s", filename)
            continue
        backup_existing(filename)
        if fetch_manifest(ent, filename):
            updated.append((key, ent, filename))
        else:
            # Roll the old manifest back so the pools stay consistent.
            bak = os.path.join(BACKUPS, filename + ".bak")
            if os.path.isfile(bak):
                try:
                    os.replace(bak, os.path.join(MANIFESTS, filename))
                    LOG.info("  Restored the previous manifest.")
                except OSError:
                    pass

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
                complete[key] = False
                continue
            num = list_number(record["game"], record["name"])
            if num is None:
                LOG.error("  [ERROR] Could not resolve a menu number for %s.",
                          record["name"])
                summary.append(f"{record['name']}: could not resolve list number")
                complete[key] = False
                continue
            assist = (NEXUS_MODE == "assist")
            mode_flag = ["--nxm-assist"] if assist else ["--skip-nexus"]
            rc = run_script(DOWNLOADER_SCRIPT,
                            ["--game", record["game"], "--list", num,
                             "--yes"] + mode_flag
                            + (["-v"] if args.verbose else []),
                            "downloader", interactive=assist)
            # rc 1 means some individual files failed, not that the run aborted.
            if rc not in (0, 1):
                summary.append(f"{record['name']}: downloader exited {rc}")
            # Re-plan: anything still missing that we could have fetched means
            # the job is unfinished and must be retried on the next run.
            after = plan_for(record)
            left = after["direct"] + (after["nexus"] if NEXUS_MODE == "assist" else [])
            complete[key] = not left
            if left:
                LOG.warning("  [INCOMPLETE] %d fetchable archive(s) still "
                            "missing; will retry next run.", len(left))
                summary.append(f"{record['name']}: {len(left)} archive(s) still to fetch")

        todo = write_nexus_todo(record, p["nexus"])
        if todo:
            LOG.info("  Nexus to-do list: %s", os.path.basename(todo))
        summary.append(
            f"{record['name']} v{ent['version']}: "
            f"{len(p['direct'])} fetched here, "
            f"{len(p['nexus'])} Nexus file(s) ({WJ.human_bytes(total_size(p['nexus']))}) "
            f"left for Wabbajack")

    # ---- Sort ---------------------------------------------------------
    if not args.no_sort and not args.dry_run and staged_candidates:
        banner("STEP 3: SORTING POOLS")
        target = STAGE_LIST
        if target is None:
            target = staged_candidates[-1][0]["name"]
            LOG.info("STAGE_LIST is unset; staging the most recently updated list.")
        rec = next((r for r, _ in staged_candidates if r["name"] == target), None)
        if rec is None:
            rec = staged_candidates[-1][0]
            LOG.warning("[WARN] STAGE_LIST '%s' was not updated this run; "
                        "staging '%s' instead.", target, rec["name"])
            target = rec["name"]

        parts = find_part_files(rec and WJ.resolve_game_dirs(rec["game"])[0])
        if parts and not SORTER_IGNORES_PART:
            LOG.error("[SKIPPED] %d leftover .part file(s) are in the pools. The "
                      "sorter does not skip .part files, so it would park them in "
                      "Master and destroy your resume progress. Apply the one-line "
                      ".part fix from the README and set SORTER_IGNORES_PART = True, "
                      "or remove these by hand:", len(parts))
            for p_ in parts[:10]:
                LOG.error("  ? %s", p_)
            summary.append(f"sorter skipped: {len(parts)} .part file(s) present")
        else:
            num = list_number(rec["game"], target)
            if num is None:
                LOG.error("[ERROR] Could not resolve a menu number for '%s'.", target)
            else:
                rc = run_script(SORTER_SCRIPT,
                                ["--game", rec["game"], "--list", num, "--yes"]
                                + (["-v"] if args.verbose else []),
                                "sorter")
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

    banner("RUN COMPLETE")
    for line in summary:
        LOG.info("-> %s", line)
    LOG.info("")
    notify(f"Wabbajack: {len(work)} list(s) processed", "\n".join(summary) or "no detail")
    return 0


# ----------------------------------------------------------------------
def main():
    global WJ
    ap = argparse.ArgumentParser(
        description="Watch Wabbajack modlists for updates and drive the "
                    "downloader and sorter unattended.")
    ap.add_argument("--discover", action="store_true",
                    help="Show how every local .wabbajack matches up against the "
                         "published feeds, including ambiguous ones. Purely "
                         "informational; tracking is automatic.")
    ap.add_argument("--check", action="store_true",
                    help="Report which tracked lists have updates. Changes nothing.")
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
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)
    WJ = load_downloader_module()

    LOG.info("Wabbajack auto-updater  %s",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if not resolve_dirs():
        return 1
    LOG.info("Script dir:     %s", HERE)
    LOG.info("Manifest dir:   %s", MANIFESTS)
    LOG.info("Archive root:   %s", WJ.ARCHIVE_ROOT)
    LOG.info("Hash verify:    %s",
             "xxHash64" if getattr(WJ, "_HAVE_XXHASH", False)
             else "size only (install python3-xxhash)")

    LOG.info("Nexus mode:     %s", NEXUS_MODE)
    if not os.path.isdir(WJ.ARCHIVE_ROOT):
        LOG.error("[TERMINATED] ARCHIVE_ROOT does not exist or is not reachable: "
                  "%s\nIf that is a mapped drive, make sure the share is "
                  "connected before running.", WJ.ARCHIVE_ROOT)
        return 1
    if NEXUS_MODE == "assist" and os.name != "nt":
        LOG.error("[TERMINATED] NEXUS_MODE='assist' needs Windows: the downloader "
                  "registers an nxm:// handler via winreg and opens a browser. "
                  "Use 'skip' on Linux.")
        return 1

    if args.discover:
        return cmd_discover()

    # Everything past here touches the pools, so serialise it.
    lock_fd = None
    if not args.no_lock:
        lock_fd = acquire_lock(os.path.join(HERE, LOCK_FILE))
        if lock_fd is HELD:
            LOG.info("[SKIPPED] Another run holds the lock. Exiting cleanly. "
                     "(If nothing else is running, use --no-lock.)")
            return 0
        if lock_fd is UNAVAILABLE:
            LOG.warning("[WARN] Could not lock on this filesystem; continuing "
                        "without one. Do not start a second run by hand.")
            lock_fd = None

    try:
        state = load_state()
        tracked = resolve_tracking(state, rediscover=args.rediscover)
        if args.check:
            return cmd_check(state, tracked)
        return cmd_run(args, state, tracked)
    finally:
        release_lock(lock_fd)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        LOG.info("\n[ABORT] Interrupted.")
        sys.exit(130)
