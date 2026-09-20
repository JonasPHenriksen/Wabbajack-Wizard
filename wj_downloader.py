#!/usr/bin/env python3
"""
Wabbajack Wizard - the downloader, and the shared library
=========================================================
Fetches the archives a chosen modlist needs and drops them straight into that
list's folder, ready for Wabbajack to verify and install. It is also the
module every other tool imports for what they all need to agree on: how a
.wabbajack manifest is parsed, how a file on disk is judged present, how the
pools are indexed, and the light manifest cache.

It supports the download sources that appear in manifests:

  * Nexus Mods   -> see "Premium vs. free" below.
  * Direct HTTP  -> downloaded straight from the manifest URL.  (no account)
  * WabbajackCDN -> downloaded straight from the manifest URL.  (no account)
  * MediaFire    -> best-effort: the file page is scraped for its link. (no account)
  * GameFile     -> skipped. These come from your local game install, not a
                    download, so Wabbajack copies them from the game folder.

Premium vs. free (Nexus only):
  Nexus gates *automated* download links behind Premium. There are two paths:

    PREMIUM  -> the API "download_link" endpoint returns a direct CDN URL with
                no extra steps. Fully automatic.

    FREE     -> the same endpoint returns HTTP 403. A valid link can only be
                minted by the Nexus website, one click per file, which the site
                hands to your PC as an "nxm://" link containing a short-lived
                key+expires. The API honours those for free accounts, so this
                script can capture/replay nxm:// links and download from them.
                This is unavoidably semi-manual (one click per file) - that is
                exactly the limitation Premium removes.

  The script auto-detects which kind of account your API key belongs to and
  picks the right path. Non-Nexus sources always download with no account.

Free-account Nexus workflows (pick one):
  * --nxm-assist   Register this script as the nxm:// handler (temporarily,
                   restoring your previous handler such as Wabbajack on
                   --unregister-nxm), open each pending mod's download page in
                   turn, and download each file as you click it.
  * --nxm-file F   Replay a file of nxm:// links you captured earlier (one per
                   line) and download them in a batch. Handy if you collect the
                   links first and download later.
  Use --register-nxm / --unregister-nxm to manage the handler registration on
  its own (Windows only).

Layout:
    <ARCHIVE_ROOT>/<GameFolder>/<Modlist name>/<archive>     (Windows: backslashes)
  A file already present anywhere in the game's pools (Master / Shared / any
  list folder) at the manifest's expected Size is NOT downloaded again. Each
  downloaded file gets its ".meta" sidecar written from the manifest.

API key:
  Account Settings -> API Keys -> "Personal API Key" on nexusmods.com. Supply by
  --apikey, the NEXUS_API_KEY env var, a nexus_api_key.txt file next to this
  script, or the interactive prompt. The key is sent only to api.nexusmods.com
  as the documented `apikey` header and is never written to the log.

Notes:
  * Every download is verified by xxHash64, the hash Wabbajack itself uses.
    Without the xxhash package only sizes can be compared, and the tool
    refuses to run unless --allow-size-only says that is acceptable.
  * Downloads stream to a .part file and resume when the server allows it.
    An archive that fails verification is discarded, never kept.
  * WabbajackCDN archives go through wj_cdn.py, which speaks the CDN's
    definition.json.gz + parts/N protocol; a plain GET on such a URL returns
    a ~1 KB error page.
  * WJ_MANIFEST_DIR, when set to a real directory, says where .wabbajack files
    are; the orchestrator sets it for the children it launches. Logs, the API
    key file and the CSV reports live beside this script regardless.
"""

import os
import re
import sys
import csv
import json
import time
import base64
import struct
import socket
import zipfile
import logging
import argparse
import platform
import http.client
import queue
import threading
import webbrowser
import concurrent.futures
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from collections import defaultdict

import wj_paths

# Wabbajack CDN multi-part support. Files on authored-files.wabbajack.org are
# served as a definition plus numbered parts; a plain GET on the URL returns a
# ~1 KB error page, so these must not go through stream_download().
try:
    import wj_cdn as _CDN
    _HAVE_CDN = True
except Exception:
    _CDN = None
    _HAVE_CDN = False

# winreg only exists on Windows; the nxm handler registration needs it.
try:
    import winreg  # type: ignore
    _HAVE_WINREG = True
except Exception:
    _HAVE_WINREG = False

# ============================ CONFIGURATION ============================
# ---------------------------------------------------------------------
# Layout settings come from wj_config.py, which both this script and
# wj_sorter.py import. They used to be duplicated here and
# there: editing one copy and forgetting the other left the downloader writing
# into one directory tree while the sorter moved files out of another, with
# both reporting success. Edit wj_config.py.
# ---------------------------------------------------------------------
try:
    import wj_config as CFG
except ImportError:
    sys.stderr.write(
        "wj_config.py is missing from the folder holding these scripts. It "
        "defines ARCHIVE_ROOT and the game/pool layout and is not optional.\n")
    raise SystemExit(2)

ARCHIVE_ROOT = CFG.resolved_archive_root()
GAME_FOLDERS = CFG.GAME_FOLDERS
GAME_DISPLAY = CFG.GAME_DISPLAY
POOL_FOLDERS = CFG.POOL_FOLDERS
# Includes CFG.UNMANAGED_FOLDER, so the user's own hand-managed store is as
# invisible to this tooling as another installer's folder is.
EXCLUDE_POOL_DIRS = CFG.excluded_dirs()
MANIFEST_SUBDIR = CFG.MANIFEST_SUBDIR

# Hash verification is the whole point of downloading a pinned revision. When
# the optional xxhash package is absent this script can only compare sizes,
# which passes a file that is the right length but the wrong content. Rather
# than quietly downgrading (the same command giving different guarantees
# depending on which Python picked it up), stop and say so. --allow-size-only
# is the deliberate opt-out. Both settings live in wj_config.py.
REQUIRE_HASH = CFG.REQUIRE_HASH
DOWNLOAD_ORDER = CFG.DOWNLOAD_ORDER or "smallest"

# Several small files at once, from direct sources only; see wj_config.py.
# 1 is one at a time, which is the default and the reliable arrangement.
PARALLEL_SMALL = max(1, int(getattr(CFG, "PARALLEL_SMALL_DOWNLOADS", 1) or 1))
SMALL_BYTES = int(getattr(CFG, "SMALL_DOWNLOAD_MB", 50) or 0) * (1 << 20)
# Assist mode: open the next Nexus page while the current file downloads.
CLICK_AHEAD = bool(getattr(CFG, "ASSIST_CLICK_AHEAD", False))
# Assist mode: show the pages in a window of our own rather than the browser.
ASSIST_WINDOW = bool(getattr(CFG, "ASSIST_WINDOW", True))
try:
    import wj_assistwin as _ASSISTWIN
except Exception:                       # the module is optional at run time
    _ASSISTWIN = None

# Nexus API tuning -----------------------------------------------------
NEXUS_API_BASE   = "https://api.nexusmods.com/v1"
# Sent as the Application-Name and Application-Version headers Nexus asks
# API clients to identify themselves with.
APP_NAME         = CFG.APP_NAME
APP_VERSION      = CFG.__version__
REQUEST_PAUSE_S  = 0.30          # polite gap between Nexus API calls
DOWNLOAD_CHUNK   = 1 << 20       # 1 MiB streaming chunks
NEXUS_DAILY_FLOOR = 5            # stop minting links when daily allowance hits this
DOWNLOAD_RETRIES = 4             # attempts per file when the stream drops mid-transfer
STREAM_TIMEOUT   = 120           # seconds a connection may go silent before it is a retry

# Many modlists pin a file to the *target* game's Nexus domain even though the
# file actually lives on a parent game's page. The classic case: an Enderal
# Special Edition list whose files are uploaded to skyrimspecialedition. When a
# download_link request 404s under the manifest's stated domain, we retry the
# same mod/file under these fallbacks (in order) before giving up. Keys and
# values are lowercase Nexus domain slugs.
#
# The two Enderal SE lists held here tag their files "enderalse" and
# "skyrimse", short forms the API does not serve; candidate_domains() maps
# those onto the real domain first (NEXUS_URL_ALIASES), and the entries here
# then say where else to look. Before "enderalse" had an entry, an API-path
# download of any of its 148 files tried that one bogus domain and gave up.
NEXUS_DOMAIN_FALLBACKS = {
    "enderalspecialedition": ["skyrimspecialedition", "skyrim"],
    "enderalse":             ["enderalspecialedition", "skyrimspecialedition", "skyrim"],
    "enderal":               ["skyrim", "enderalspecialedition"],
    "skyrimspecialedition":  ["skyrim"],
    "skyrim":                ["skyrimspecialedition"],
    "falloutnv":             ["fallout3", "falloutnewvegas", "newvegas"],
    "skyrimse":              ["skyrimspecialedition"],
}

# Free-account nxm capture --------------------------------------------
NXM_LISTEN_HOST   = "127.0.0.1"
NXM_PORT_FILE     = ".nxm_listener_port"   # written next to the script
NXM_PREV_HANDLER  = ".nxm_prev_handler"    # saved previous nxm:// command
NXM_FALLBACK_FILE = "nxm_captured.txt"     # forwarder appends here if no listener
# The download window's Skip button is an nxm:// link under this host, so it
# travels the same way a click does whichever path catches it.
NXM_SKIP_PREFIX   = "nxm://wabbajackwizard/"
SKIPPED           = object()               # _wait_click's verdict for that button
NXM_CLICK_TIMEOUT = 180          # seconds to wait per file for a click

# =======================================================================

LOG = logging.getLogger("wj_downloader")


# ----------------------------------------------------------------------
# Logging (mirrors the sorter, incl. "keep previous + current only")
# ----------------------------------------------------------------------
def prune_old_logs(log_dir, keep=1):
    try:
        logs = sorted(
            f for f in os.listdir(log_dir)
            if f.lower().startswith("wj_downloader_") and f.lower().endswith(".log")
        )
    except OSError:
        return
    for old in logs[:-keep] if keep else logs:
        try:
            os.remove(os.path.join(log_dir, old))
        except OSError:
            pass


def setup_logging(log_dir, verbose=False):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    LOG.setLevel(logging.DEBUG)
    LOG.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    LOG.addHandler(console)

    log_path = None
    try:
        os.makedirs(log_dir, exist_ok=True)
        prune_old_logs(log_dir, keep=1)
        log_path = os.path.join(log_dir, f"wj_downloader_{datetime.now():%Y%m%d_%H%M%S}.log")
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%H:%M:%S"))
        LOG.addHandler(fh)
    except OSError as e:
        LOG.warning("[WARN] Could not open a log file in %s: %s", log_dir, e)
    return log_path


def banner(title):
    LOG.info("\n=================== %s ===================", title)


# ----------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------
def game_folder(game):
    return GAME_FOLDERS.get(game, game)


def game_label(game):
    return GAME_DISPLAY.get(game, game)


def resolve_game_dirs(game):
    base = os.path.join(ARCHIVE_ROOT, game_folder(game))
    return base, os.path.join(base, "Master"), os.path.join(base, "Shared")


# Shared with the sorter and the wizard through wj_config, re-exported here
# because wj_autoupdate and wj_wizard reach everything through this module.
folder_key = CFG.folder_key
same_pool = CFG.same_pool
canonical_name = CFG.canonical_name
is_suffixed = CFG.is_suffixed
reserved_list_names = CFG.reserved_list_names


def safe_game_type(gt, base):
    """A manifest's GameType, or None if it could not name a folder.

    The GameType becomes a folder name under the archive root; see
    wj_config.valid_game_type() for why that has to be checked."""
    gt = str(gt or "")
    if not gt:
        return None
    if not CFG.valid_game_type(gt):
        LOG.error("  [ERROR] %s declares GameType %r, which is not a folder "
                  "name. Treating it as no game.", base, gt)
        return None
    return gt


def sanitize_url(url):
    """Percent-encode unsafe characters (notably spaces) in a URL's path and
    query without double-encoding existing %xx escapes.

    Nexus CDN links arrive with raw spaces in the filename; Python 3.14's
    http.client rejects any URL containing control characters/spaces, so we
    encode them here. `%` is kept "safe" so already-encoded URLs (e.g. the
    WabbajackCDN ones, which use %20) pass through unchanged."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except (ValueError, AttributeError):
        return url
    path = urllib.parse.quote(parts.path, safe="/%:@!$&'()*+,;=~-._")
    query = urllib.parse.quote(parts.query, safe="=&%:@!$'()*+,;/?~-._")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def is_junk_name(name):
    """OS litter that must never be mistaken for an archive or a manifest.

    Deliberately NOT "anything starting with a dot". Nexus filenames routinely
    begin with one - ".45 Auto Pistol Ha_ru anims-21142-1-2-1.zip", ".30 Carbine
    Pistol-71955-...", ".1130 _ResourcePack Fixes-..." - and excluding those made
    13 archives that are present on disk report as absent, and hid them from the
    sorter so they would never be staged. Only the AppleDouble prefix and a
    named list of OS files are junk."""
    low = name.lower()
    return low.startswith("._") or low in (
        ".ds_store", "thumbs.db", "desktop.ini", ".localized")


def list_manifest_files(directory):
    """Sorted .wabbajack filenames in `directory`, junk excluded."""
    try:
        names = os.listdir(directory)
    except OSError as e:
        LOG.error("[ERROR] Cannot read manifest folder %s: %s", directory, e)
        return []
    return sorted(f for f in names
                  if f.lower().endswith(".wabbajack") and not is_junk_name(f))


def resolve_manifest_dir(script_dir):
    """Where to look for .wabbajack files.

    WJ_MANIFEST_DIR wins when it points at a real directory. A value that does
    NOT resolve is reported loudly rather than silently falling back, because a
    silent fallback means operating on a different set of modlists than the
    caller intended."""
    env = os.environ.get("WJ_MANIFEST_DIR")
    if env:
        if os.path.isdir(env):
            return env
        LOG.error("[ERROR] WJ_MANIFEST_DIR points at %s, which is not a "
                  "directory. Refusing to guess.", env)
        sys.exit(2)
    sub_dir = os.path.join(script_dir, MANIFEST_SUBDIR)
    if os.path.isdir(sub_dir) and list_manifest_files(sub_dir):
        return sub_dir
    return script_dir


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def size_key(archive, largest_first=False):
    """Sort key for download order. Unknown sizes always sort last."""
    size = archive.get("size")
    if size is None:
        return (1, 0)
    return (0, -size if largest_first else size)


def order_downloads(direct_items, nexus_items, order):
    """Sort both work lists in place. Returns a human label, or None."""
    if order not in ("smallest", "largest"):
        return None
    largest = order == "largest"
    direct_items.sort(key=lambda pair: size_key(pair[1], largest))
    nexus_items.sort(key=lambda a: size_key(a, largest))
    return "largest first" if largest else "smallest first"


def parse_meta(meta_text):
    out = {}
    if not isinstance(meta_text, str):
        return out
    for line in meta_text.splitlines():
        line = line.strip()
        if not line or line.startswith("[") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip().lower()] = v.strip()
    return out


# ----------------------------------------------------------------------
# Manifest parsing
# ----------------------------------------------------------------------
def parse_manifest(wj_path):
    record = {"game": None, "name": os.path.splitext(os.path.basename(wj_path))[0],
              "archives": [], "readable": True}
    base = os.path.basename(wj_path)
    LOG.info("  [SCAN] %s", base)
    try:
        with zipfile.ZipFile(wj_path, "r") as z:
            target = next((m for m in z.namelist()
                           if os.path.basename(m).lower() == "modlist"), None)
            if target is None:
                LOG.warning("  [WARN] No 'modlist' manifest inside %s - skipping.", base)
                record["readable"] = False
                return record
            with z.open(target) as f:
                data = json.load(f)

        if isinstance(data, dict) and data.get("GameType"):
            record["game"] = safe_game_type(data["GameType"], base)

        archives = data.get("Archives") if isinstance(data, dict) else None
        if not isinstance(archives, list):
            LOG.warning("  [WARN] 'Archives' missing/malformed in %s.", base)
            return record

        for a in archives:
            if not isinstance(a, dict):
                continue
            name = a.get("Name") or a.get("NameLog")
            if not name:
                continue
            meta_text = a.get("Meta") or ""
            record["archives"].append({
                "name":      str(name),
                "size":      a.get("Size") if isinstance(a.get("Size"), int) else None,
                "hash":      a.get("Hash"),
                "meta_text": meta_text,
                "meta":      parse_meta(meta_text),
                "state":     a.get("State") or {},
            })
    except zipfile.BadZipFile:
        LOG.error("  [ERROR] %s is not a readable zip/wabbajack file.", base)
        record["readable"] = False
    except json.JSONDecodeError as e:
        LOG.error("  [ERROR] Manifest JSON in %s is corrupt: %s", base, e)
        record["readable"] = False
    except OSError as e:
        LOG.error("  [ERROR] Could not open %s: %s", base, e)
        record["readable"] = False
    except Exception as e:
        LOG.error("  [ERROR] Unexpected failure reading %s: %s", base, e)
        record["readable"] = False

    LOG.info("  [OK]   %s -> game=%s, %d archive(s).",
             base, record["game"] or "unknown", len(record["archives"]))
    return record


# Sources this tooling can fetch itself, and sources it recognises but cannot.
# The distinction matters in reporting: "Mega, download it yourself" is a fact
# about the file, while "unknown" means Wabbajack has a downloader this tooling
# has never heard of - which is a reason to look at state_type(), not at your
# collection.
def state_type(state):
    t = (state or {}).get("$type", "")
    if t.startswith("NexusDownloader"):           return "nexus"
    if t.startswith("HttpDownloader"):            return "http"
    if t.startswith("WabbajackCDNDownloader"):    return "wabbajackcdn"
    if t.startswith("MediaFireDownloader"):       return "mediafire"
    if t.startswith("GameFileSourceDownloader"):  return "gamefile"
    if t.startswith("GoogleDriveDownloader"):     return "googledrive"
    if t.startswith("ManualDownloader"):          return "manual"
    # Recognised, but not fetchable here: Mega needs its own client and key
    # handling, so these are reported for you to download by hand.
    if t.startswith("MegaDownloader"):            return "mega"
    return "unknown"


def state_type_raw(state):
    """The State's declared $type, for when 'unknown' needs explaining."""
    t = (state or {}).get("$type", "")
    return t.split(",", 1)[0].strip() or "(no $type)"


def nexus_domain(archive):
    """Nexus game domain slug (Meta gameName is authoritative)."""
    meta = archive.get("meta", {})
    if meta.get("gamename"):
        return meta["gamename"].lower()
    gn = (archive.get("state") or {}).get("GameName")
    return gn.lower() if isinstance(gn, str) else None


def nexus_ids(archive):
    """Return (mod_id, file_id) as ints, preferring the Meta values."""
    meta = archive.get("meta", {})
    st = archive.get("state") or {}
    def pick(*vals):
        for v in vals:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
        return None
    return pick(meta.get("modid"), st.get("ModID")), pick(meta.get("fileid"), st.get("FileID"))


def candidate_domains(archive, forced=None):
    """Ordered, de-duplicated list of Nexus domains to try for this archive.

    The manifest's own domain is authoritative when it's correct, so it goes
    first; the fallbacks (e.g. enderalspecialedition -> skyrimspecialedition)
    catch the very common case of a file that is hosted on a parent game's
    page. A --nexus-domain override, if given, is tried before anything else."""
    ordered = []
    def add(d):
        if d:
            d = d.lower()
            if d not in ordered:
                ordered.append(d)
    add(forced)
    primary = nexus_domain(archive)
    # A manifest slug the API does not serve ("skyrimse", "enderalse") is
    # tried under its real name first, then as written - the alias saves two
    # wasted requests per file, the raw form stays in case an alias is wrong.
    real = nexus_url_domain(primary) if primary else None
    add(real)
    add(primary)
    for d in NEXUS_DOMAIN_FALLBACKS.get(primary, []) + \
            NEXUS_DOMAIN_FALLBACKS.get(real or "", []):
        add(d)
    # Also seed fallbacks from the forced/primary even if primary was unknown.
    if forced:
        for d in NEXUS_DOMAIN_FALLBACKS.get(forced.lower(), []):
            add(d)
    return ordered


# ----------------------------------------------------------------------
# Existing-file index (so we never re-download what we already have)
# ----------------------------------------------------------------------
_EXISTING_CACHE = {}


def invalidate_existing_cache():
    """Call after anything writes into the pools."""
    _EXISTING_CACHE.clear()


# Folder names under a game root that belong to a modlist we follow. None
# means "unknown, do not restrict". Set once at start-up from the manifest
# folder - by main() for a standalone run, by wj_autoupdate.resolve_dirs() for
# an imported one - so the two paths agree without every call site having to
# remember to pass it.
KNOWN_LIST_DIRS = None


def set_known_list_dirs(names):
    """Declare which subfolders are legitimate modlist folders.

    An EMPTY set is treated as "unknown" rather than "none are legitimate":
    excluding every list folder at once would make an entire collection read
    as absent and trigger a re-download of all of it, which is a far worse
    failure than indexing one folder too many."""
    global KNOWN_LIST_DIRS
    KNOWN_LIST_DIRS = frozenset(n.lower() for n in names) if names else None
    invalidate_existing_cache()
    return KNOWN_LIST_DIRS


def known_list_dirs_from(manifest_dir):
    """The folder-name set implied by the manifests in `manifest_dir`."""
    try:
        return {os.path.splitext(f)[0].lower()
                for f in list_manifest_files(manifest_dir)}
    except OSError:
        return set()


# ----------------------------------------------------------------------
# The light manifest cache, shared by every tool that plans against demand
# ----------------------------------------------------------------------
MANIFEST_CACHE_FILE = "wj_manifest_cache.json"


def load_light_records(manifest_dir, cache_path, progress=None, files=None):
    """Lightweight per-list records, cached on disk.

    Planning needs each archive's name, size and source kind - and nothing
    else. A full parse also carries every archive's Meta block, which is the
    bulk of a manifest; only a download needs that, and the download re-parses
    the one file it works on anyway. Caching the light form turns a crawl over
    ~19 GB of zips on the share into a sub-second read, and this cache is what
    the wizard, the updater AND the sorter plan from, so the manifests are
    read once per change rather than once per tool per run.

    An entry is keyed by its manifest's mtime and size, so editing, replacing
    or re-downloading a manifest invalidates just that entry. There is no way
    for the cache to describe a file that is no longer there: entries for
    vanished manifests are dropped whenever the cache is rewritten.

    Returns {filename: {"stamp", "game", "readable", "archives": [
        {"name", "size", "kind"[, "raw"]} ]}}."""
    try:
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, json.JSONDecodeError):
        cache = {}

    names = files if files is not None else list_manifest_files(manifest_dir)
    out, dirty, misses = {}, False, []
    for i, fn in enumerate(names, 1):
        full = os.path.join(manifest_dir, fn)
        try:
            st = os.stat(full)
        except OSError:
            continue
        stamp = [int(st.st_mtime), st.st_size]
        hit = cache.get(fn)
        if isinstance(hit, dict) and hit.get("stamp") == stamp:
            out[fn] = hit
            continue
        misses.append((i, fn, full, stamp))

    for n, (i, fn, full, stamp) in enumerate(misses, 1):
        if progress:
            progress(n, len(misses), fn)
        rec = parse_manifest(full)
        entry = {
            "stamp": stamp,
            "game": rec["game"],
            "readable": rec["readable"],
            # The raw $type is kept only for the entries we could not
            # classify, so the cache stays small while an unrecognised
            # downloader can still be named in a report.
            "archives": [
                dict({"name": a["name"], "size": a["size"], "kind": k},
                     **({"raw": state_type_raw(a["state"])}
                        if k == "unknown" else {}))
                for a, k in ((a, state_type(a["state"]))
                             for a in rec["archives"])],
        }
        cache[fn] = entry
        out[fn] = entry
        dirty = True

    if dirty:
        cache = {k: v for k, v in cache.items() if k in out}
        tmp = cache_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(tmp, cache_path)
        except OSError as e:
            LOG.debug("Could not write the manifest cache: %s", e)
    return out


def index_existing(game_base, known_lists=None):
    """Index every archive already sitting in this game's pools.

    Returns { name_lower: [(abs_path, size_bytes), ...] } - EVERY copy, not
    just the first one found.

    The size is what makes the "already present" test meaningful: a name match
    alone would treat a truncated or wrong-version file as done forever, and
    you would only find out when Wabbajack hash-verifies it at install time.

    Keeping every copy matters because a filename does not identify content
    here. Several modlists pin different builds of a file with the same generic
    name - "Synthesis.zip" appears in four of these lists at four different
    sizes - so each list legitimately has its own copy in its own folder.
    Collapsing them to whichever turned up first meant judging one list's
    requirement against another list's file.

    Folders listed in EXCLUDE_POOL_DIRS - another installer's (Nolvus) and the
    unmanaged store - are skipped entirely, so archives that are not ours are
    neither claimed nor disturbed.

    `known_lists`, when given, is the set of lowercased folder names that are
    legitimate modlist folders. Passing it restricts the scan to Master, Shared
    and those, and any other subfolder is reported rather than indexed. That
    matters because the SORTER only ever scans manifest-named folders: a file in
    some other folder counted as present here would be skipped by the download
    and then never staged, leaving Wabbajack short of a file this tooling had
    just called downloaded. Same failure the game root used to cause.

    The scanned set is exactly what the sorter scans: Master, Shared, and the
    per-list folders. The game ROOT itself is deliberately excluded, even though
    a file sitting loose there is technically on disk. The sorter does not scan
    the root either, so it would never stage such a file into the list folder -
    meaning counting it as present made the archive get skipped here and then be
    missing when Wabbajack looked for it. Better to fetch a redundant copy into
    the right place than to be quietly short one file.

    Keys are CANONICAL names: a copy parked as 'Name~1.ext' is indexed under
    'name.ext', because that is the name a manifest asks for, and the sorter
    restores it when it stages the file. Without this a wanted build that had
    been parked under a suffix was invisible - re-downloaded here, never staged
    there, and deleted by a cleanup that saw a name nobody wanted."""
    known = (frozenset(n.lower() for n in known_lists)
             if known_lists is not None else KNOWN_LIST_DIRS)
    cache_key = (os.path.normcase(os.path.abspath(game_base)), known)
    if cache_key in _EXISTING_CACHE:
        return _EXISTING_CACHE[cache_key]

    index = {}
    pool_low = {p.lower() for p in POOL_FOLDERS}
    locations = [os.path.join(game_base, p) for p in POOL_FOLDERS]
    skipped, stray = [], []
    try:
        for entry in os.listdir(game_base):
            full = os.path.join(game_base, entry)
            if not os.path.isdir(full):
                continue
            low = entry.lower()
            if low in pool_low:
                continue  # already listed, and listing it twice is harmless
            if low in EXCLUDE_POOL_DIRS:
                skipped.append(entry)
                continue
            if known is not None and low not in known:
                stray.append(entry)
                continue
            locations.append(full)
    except OSError:
        pass
    # Three different reasons, worth telling apart: another installer's
    # territory, content you told us to keep out of, and what stage 5 has
    # reclaimed and is holding until its purge date.
    unmanaged_low = CFG.UNMANAGED_FOLDER.lower()
    trash_low = CFG.TRASH_FOLDER.lower()
    for entry in sorted(skipped):
        if entry.lower() == unmanaged_low:
            LOG.info("  [SKIP] Ignoring the unmanaged store '%s' - archives "
                     "there are yours to manage, not ours.", entry)
        elif entry.lower() == trash_low:
            LOG.debug("  [SKIP] Ignoring the trash folder '%s'.", entry)
        else:
            LOG.info("  [SKIP] Ignoring foreign installer folder: %s", entry)
    if stray:
        LOG.warning("  [WARN] %d folder(s) under %s are neither a pool nor a "
                    "modlist this tooling follows, so they are not indexed: %s",
                    len(stray), os.path.basename(game_base),
                    ", ".join(sorted(stray)))
        LOG.warning("         Move them into '%s' to say so deliberately, or "
                    "put their modlist back in the manifest folder.",
                    CFG.UNMANAGED_FOLDER)

    seen = set()
    for loc in locations:
        key = os.path.normcase(os.path.abspath(loc))
        if key in seen or not os.path.isdir(loc):
            continue
        seen.add(key)
        # scandir, not listdir + getsize: on Windows the directory listing
        # already carries each entry's type and size, so this is one pass
        # over the folder rather than one round trip per file. On an SMB
        # share holding 15,000 archives that is the difference between a
        # scan that takes seconds and one that takes half a minute, and the
        # scan runs in every tool, on every run.
        try:
            with os.scandir(loc) as it:
                for e in it:
                    name = e.name
                    low = name.lower()
                    if low.endswith(".meta") or low.endswith(".part"):
                        continue
                    if is_junk_name(name):
                        continue
                    try:
                        if not e.is_file():
                            continue
                        size = e.stat().st_size
                    except OSError:
                        size = None
                    index.setdefault(canonical_name(low), []).append((e.path, size))
        except OSError as e:
            LOG.warning("  [WARN] Cannot read %s: %s", loc, e)
    _EXISTING_CACHE[cache_key] = index
    return index


def present_match(existing, archive):
    """Decide whether `archive` is already on disk.

    Returns (path, why) when it counts as present, or (None, why_not). Any copy
    of the right size satisfies the requirement, wherever it sits in the pools;
    it takes every copy being the wrong size to call the archive missing."""
    copies = existing.get(archive["name"].lower()) or []
    if not copies:
        return None, "absent"
    want = archive.get("size")
    if want is None:
        return copies[0][0], "name match (manifest states no size)"
    for path, size in copies:
        if size == want:
            return path, "size match"
    known = [s for _p, s in copies if s is not None]
    if not known:
        return copies[0][0], "name match (size unreadable)"
    if len(known) == 1:
        return None, "on disk at %s but the manifest expects %s" % (known[0], want)
    return None, ("%d cop%s on disk (%s), none of them the expected %s"
                  % (len(known), "y" if len(known) == 1 else "ies",
                     ", ".join(str(s) for s in sorted(known)), want))


# ----------------------------------------------------------------------
# Hashing (optional, matches Wabbajack's base64 little-endian xxHash64)
# ----------------------------------------------------------------------
try:
    import xxhash  # type: ignore
    _HAVE_XXHASH = True
except Exception:
    _HAVE_XXHASH = False


def wabbajack_hash(path):
    if not _HAVE_XXHASH:
        return None
    try:
        h = xxhash.xxh64()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(DOWNLOAD_CHUNK), b""):
                h.update(chunk)
        return base64.b64encode(struct.pack("<Q", h.intdigest())).decode("ascii")
    except OSError:
        return None


def verify_file(path, expected_size, expected_hash):
    try:
        actual = os.path.getsize(path)
    except OSError as e:
        return False, f"cannot stat ({e})"
    if expected_size is not None and actual != expected_size:
        return False, f"size {actual} != expected {expected_size}"
    if expected_hash and _HAVE_XXHASH:
        got = wabbajack_hash(path)
        if got and got != expected_hash:
            return False, "hash mismatch"
    return True, "ok"


# ----------------------------------------------------------------------
# Nexus API
# ----------------------------------------------------------------------
class NexusClient:
    def __init__(self, api_key):
        self.api_key = api_key
        self.daily_remaining = None
        self.hourly_remaining = None
        self.is_premium = None
        self.user_name = None
        # files.json answers by (domain, mod_id): asked once per mod, read
        # several times - to place the mod, to see whether the pinned file is
        # still offered, to find its replacement.
        self._files_cache = {}

    def _headers(self):
        return {
            "apikey": self.api_key,
            "Accept": "application/json",
            "Application-Name": APP_NAME,
            "Application-Version": APP_VERSION,
            "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        }

    def _note_limits(self, headers):
        def geti(name):
            try:
                return int(headers.get(name))
            except (TypeError, ValueError):
                return None
        self.daily_remaining = geti("X-RL-Daily-Remaining")
        self.hourly_remaining = geti("X-RL-Hourly-Remaining")

    def _get_json(self, url):
        req = urllib.request.Request(url, headers=self._headers())
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    self._note_limits(resp.headers)
                    return json.load(resp), None
            except urllib.error.HTTPError as e:
                self._note_limits(e.headers or {})
                if e.code == 429:
                    wait = int((e.headers or {}).get("Retry-After", 0) or 0) or (5 * (attempt + 1))
                    LOG.warning("  [RATE] 429 from Nexus; waiting %ds...", wait)
                    time.sleep(wait)
                    continue
                return None, e   # let caller interpret 401/403/404 etc.
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                LOG.warning("  [RETRY] Nexus request failed (%s, attempt %d)", e, attempt + 1)
                time.sleep(3 * (attempt + 1))
        return None, RuntimeError("gave up after repeated Nexus API failures")

    def validate(self):
        """Confirm the key and detect Premium. Returns True on success."""
        data, err = self._get_json(f"{NEXUS_API_BASE}/users/validate.json")
        if err is not None:
            code = getattr(err, "code", None)
            if code == 401:
                LOG.error("[ERROR] Nexus rejected the API key (401). Check the key.")
            else:
                LOG.error("[ERROR] Could not validate Nexus account: %s", err)
            return False
        self.is_premium = bool(data.get("is_premium") or data.get("is_premium?"))
        self.user_name = data.get("name")
        return True

    def _link_payload(self, url):
        """Low-level link request. Returns (uri, status) where status is one of
        'ok' / 'notfound' / 'forbidden' / 'error:<msg>'. Never raises for the
        HTTP cases the caller is expected to branch on."""
        payload, err = self._get_json(url)
        if err is not None:
            code = getattr(err, "code", None)
            if code == 403:
                return None, "forbidden"
            if code in (404, 410):
                return None, "notfound"
            return None, f"error:{err}"
        if isinstance(payload, list) and payload and payload[0].get("URI"):
            return payload[0]["URI"], "ok"
        return None, "error:API returned no download URI"

    def _download_link_url(self, domain, mod_id, file_id, key=None, expires=None):
        url = (f"{NEXUS_API_BASE}/games/{domain}/mods/{mod_id}"
               f"/files/{file_id}/download_link.json")
        if key and expires:
            url += "?" + urllib.parse.urlencode({"key": key, "expires": expires})
        return url

    def mod_files(self, domain, mod_id):
        """List a mod's files. Returns (files_list, status) where status is
        'ok' / 'notfound' / 'error:<msg>'. Used to recover a file id that the
        manifest pinned but Nexus has since purged (author updated the mod).
        A definite answer is remembered for the run; an error is asked again."""
        key = (domain, mod_id)
        if key in self._files_cache:
            return self._files_cache[key]
        url = f"{NEXUS_API_BASE}/games/{domain}/mods/{mod_id}/files.json"
        payload, err = self._get_json(url)
        if err is not None:
            code = getattr(err, "code", None)
            if code in (404, 410):
                self._files_cache[key] = (None, "notfound")
                return self._files_cache[key]
            return None, f"error:{err}"
        files = payload.get("files") if isinstance(payload, dict) else None
        self._files_cache[key] = ((files if isinstance(files, list) else []), "ok")
        return self._files_cache[key]

    def mod_info(self, domain, mod_id):
        """The mod's own record. Returns (record, status), status 'ok' /
        'notfound' / 'error:<msg>'. The record's `status` field says whether
        the page has anything on it - 'published', or 'hidden', 'removed',
        'not_published', 'under_moderation' - and is served for a hidden or
        removed mod when its files (403) no longer are."""
        url = f"{NEXUS_API_BASE}/games/{domain}/mods/{mod_id}.json"
        payload, err = self._get_json(url)
        if err is not None:
            code = getattr(err, "code", None)
            if code in (404, 410):
                return None, "notfound"
            return None, f"error:{err}"
        return (payload if isinstance(payload, dict) else {}), "ok"

    @staticmethod
    def _file_ids(files):
        """The ids in a files.json listing."""
        ids = set()
        for f in files or []:
            if isinstance(f, dict) and isinstance(f.get("file_id"), int):
                ids.add(f["file_id"])
        return ids

    @staticmethod
    def _file_size_bytes(entry):
        """Nexus file entries report size in a few historical fields/units."""
        for k in ("size_in_bytes", "sizeInBytes"):
            v = entry.get(k)
            if isinstance(v, int):
                return v
        for k in ("size_kb", "size"):  # 'size' has historically been KB
            v = entry.get(k)
            if isinstance(v, (int, float)):
                return int(v) * 1024
        return None

    def recover_file_id(self, domain, mod_id, archive):
        """When the pinned file id is gone, find a current file on the same mod
        that matches the manifest by name (and size, when known). Returns a new
        file_id or None. We never substitute on name alone if the size clearly
        disagrees, since Wabbajack verifies by hash and a wrong file just wastes
        a download."""
        files, status = self.mod_files(domain, mod_id)
        if status != "ok" or not files:
            return None
        want_name = (archive.get("name") or "").lower()
        want_size = archive.get("size")

        def name_keys(entry):
            keys = set()
            for k in ("file_name", "name"):
                v = entry.get(k)
                if isinstance(v, str):
                    keys.add(v.lower())
            return keys

        # 1) exact filename match (best: often the same bytes re-uploaded).
        for entry in files:
            if want_name and want_name in name_keys(entry):
                fid = entry.get("file_id") or entry.get("id")
                if fid:
                    return int(fid)
        # 2) size match among non-archived files (last resort, flagged loudly).
        if isinstance(want_size, int) and want_size > 0:
            for entry in files:
                if entry.get("category_name") in (None, "OLD_VERSION", "ARCHIVED"):
                    continue
                sz = self._file_size_bytes(entry)
                if sz is not None and sz == want_size:
                    fid = entry.get("file_id") or entry.get("id")
                    if fid:
                        return int(fid)
        return None

    def resolve_link(self, archive, forced_domain=None, key=None, expires=None,
                     allow_fileid_fallback=True):
        """Resolve a download URL for a Nexus archive, trying each candidate
        domain and (for premium/no-key requests) recovering a purged file id.

        Returns (url, note) on success, or raises RuntimeError describing the
        most informative failure encountered. `note` is None or a human warning
        (e.g. that a different domain or file id was used)."""
        mod_id, file_id = nexus_ids(archive)
        if not (mod_id and file_id):
            raise RuntimeError("incomplete Nexus identifiers (no mod/file id)")

        domains = candidate_domains(archive, forced=forced_domain)
        if not domains:
            raise RuntimeError("no Nexus domain could be determined for this file")

        saw_forbidden = False
        last_err = None
        for i, domain in enumerate(domains):
            uri, status = self._link_payload(
                self._download_link_url(domain, mod_id, file_id, key, expires))
            time.sleep(REQUEST_PAUSE_S)
            if status == "ok":
                note = None if i == 0 else f"resolved on fallback domain '{domain}'"
                return uri, note
            if status == "forbidden":
                # 403 == free account; falling through domains won't change that.
                saw_forbidden = True
                last_err = ("403 - the API only mints links for free accounts via "
                            "an nxm:// click. Use --nxm-assist or --nxm-file (or Premium).")
                break
            if status == "notfound":
                # Is it the wrong domain, or a purged file id on the right mod?
                files, fstatus = self.mod_files(domain, mod_id)
                time.sleep(REQUEST_PAUSE_S)
                if fstatus == "notfound":
                    last_err = "404 - mod/file not present under tried domain(s)."
                    continue  # try the next candidate domain
                if fstatus == "ok" and allow_fileid_fallback and key is None:
                    new_fid = self.recover_file_id(domain, mod_id, archive)
                    if new_fid and new_fid != file_id:
                        uri2, status2 = self._link_payload(
                            self._download_link_url(domain, mod_id, new_fid))
                        time.sleep(REQUEST_PAUSE_S)
                        if status2 == "ok":
                            return uri2, (f"original file id {file_id} was gone; "
                                          f"used current file id {new_fid} on "
                                          f"'{domain}' (hash may differ - Wabbajack "
                                          f"will re-verify)")
                last_err = ("404 - this exact file revision is no longer on Nexus "
                            "(author replaced/archived it) and no matching current "
                            "file was found.")
                continue
            last_err = status.split("error:", 1)[-1]
        if saw_forbidden:
            raise RuntimeError(last_err)
        raise RuntimeError(last_err or "could not resolve a Nexus download link")


# ----------------------------------------------------------------------
# nxm:// parsing + handler registration + capture listener (free accounts)
# ----------------------------------------------------------------------
def parse_nxm(url):
    """Parse nxm://<domain>/mods/<mod>/files/<file>?key=..&expires=..

    Returns dict {domain, mod_id, file_id, key, expires} or None."""
    try:
        u = urllib.parse.urlparse(url.strip())
        if u.scheme.lower() != "nxm":
            return None
        domain = u.netloc.lower()
        parts = [p for p in u.path.split("/") if p]
        # expect: mods, <mod>, files, <file>
        mod_id = int(parts[parts.index("mods") + 1])
        file_id = int(parts[parts.index("files") + 1])
        qs = urllib.parse.parse_qs(u.query)
        return {
            "domain": domain,
            "mod_id": mod_id,
            "file_id": file_id,
            "key": (qs.get("key") or [None])[0],
            "expires": (qs.get("expires") or [None])[0],
        }
    except (ValueError, IndexError, AttributeError):
        return None


def _python_launcher():
    """Best command to re-invoke this script as the nxm handler."""
    exe = sys.executable or "python"
    return exe, os.path.abspath(__file__)


def nxm_handler_command():
    """The command line Windows runs when you click a mod's download link.

    Frozen there is no script to hand an interpreter, so the console binary is
    told to be the downloader instead. The --nxm flag lands in the same place
    either way, and so does the URL Windows substitutes for %1.

    From source the same binary is used whenever a build sits beside the
    scripts, and the interpreter only when none does. Firefox will not launch
    a protocol handler whose program is python.exe: the click ends on
    Windows' 'no app associated' dialog, although Windows itself runs the
    very same command from a shell. The exe is launched without complaint,
    and all the handler does is forward the link to the run that is waiting
    for it, which every build has done the same way."""
    if wj_paths.frozen():
        return (f'"{wj_paths.console_exe()}" {wj_paths.STAGE_FLAG}wj_downloader'
                f' --nxm "%1"')
    built = os.path.join(wj_paths.app_dir(__file__), wj_paths.CONSOLE_EXE)
    if os.path.isfile(built):
        return f'"{built}" {wj_paths.STAGE_FLAG}wj_downloader --nxm "%1"'
    exe, script = _python_launcher()
    return f'"{exe}" "{script}" --nxm "%1"'


def register_nxm(script_dir):
    """Register this script as the nxm:// handler under HKCU, saving whatever
    was there before (e.g. Wabbajack) so --unregister-nxm can restore it."""
    if not _HAVE_WINREG:
        LOG.error("[ERROR] nxm registration is Windows-only.")
        return False
    base = r"Software\Classes\nxm"
    cmd_key = base + r"\shell\open\command"
    # Save the previous command (if any) so we can restore it later.
    prev = None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cmd_key) as k:
            prev, _ = winreg.QueryValueEx(k, None)
    except OSError:
        prev = None
    # Only record a previous handler that is not already us. Registering twice
    # without unregistering in between would otherwise save OUR command as the
    # "previous" one, and --unregister-nxm would then restore the hijack
    # permanently instead of giving Wabbajack its handler back.
    ours = nxm_handler_command()
    if prev and os.path.normcase(prev.strip()) == os.path.normcase(ours.strip()):
        LOG.debug("nxm handler is already ours; keeping the saved previous handler.")
    else:
        try:
            with open(os.path.join(script_dir, NXM_PREV_HANDLER), "w", encoding="utf-8") as f:
                f.write(prev or "")
        except OSError:
            pass
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, "URL:NXM Protocol")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, cmd_key) as k:
            winreg.SetValueEx(k, None, 0, winreg.REG_SZ, nxm_handler_command())
    except OSError as e:
        LOG.error("[ERROR] Could not register nxm handler: %s", e)
        return False
    _associations_changed()
    LOG.info("[OK] Registered this script as the nxm:// handler: %s", ours)
    if prev and os.path.normcase(prev.strip()) != os.path.normcase(ours.strip()):
        LOG.info("     Previous handler saved; --unregister-nxm restores it.")
    return True


def _associations_changed():
    """Tell the shell the protocol associations changed.

    Explorer, and every process that asked the shell about a protocol, keeps
    what it learnt in a cache that only this notification flushes. Without
    it a browser that had already asked about nxm:// keeps its old answer -
    'no handler' - and a click lands on Windows' 'no app associated' dialog
    although the registry is right. Harmless anywhere it cannot run."""
    try:
        import ctypes
        # SHCNE_ASSOCCHANGED, SHCNF_IDLIST | SHCNF_FLUSH
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x1000, None, None)
    except (OSError, AttributeError):
        pass


def _delete_key_tree(root, path):
    """Remove a registry key and everything under it. winreg deletes one
    key at a time, and only when it has no subkeys."""
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
            subs = []
            i = 0
            while True:
                try:
                    subs.append(winreg.EnumKey(k, i))
                    i += 1
                except OSError:
                    break
        for s in subs:
            _delete_key_tree(root, path + "\\" + s)
        winreg.DeleteKey(root, path)
    except OSError:
        pass


def unregister_nxm(script_dir):
    """Restore the previously-saved nxm:// handler (or remove ours)."""
    if not _HAVE_WINREG:
        LOG.error("[ERROR] nxm registration is Windows-only.")
        return False
    cmd_key = r"Software\Classes\nxm\shell\open\command"
    prev = None
    try:
        with open(os.path.join(script_dir, NXM_PREV_HANDLER), encoding="utf-8") as f:
            prev = f.read().strip()
    except OSError:
        prev = None
    try:
        if prev:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, cmd_key) as k:
                winreg.SetValueEx(k, None, 0, winreg.REG_SZ, prev)
            LOG.info("[OK] Restored the previous nxm:// handler.")
        else:
            # No prior handler recorded -> remove the whole class, not only
            # the command under it. A class that says 'URL Protocol' but has
            # no command is a registered protocol with nothing to run, and
            # every nxm:// click then meets Windows' 'no app associated'
            # dialog until something registers again.
            _delete_key_tree(winreg.HKEY_CURRENT_USER, r"Software\Classes\nxm")
            LOG.info("[OK] Removed this script's nxm:// handler "
                     "(no previous handler was recorded; re-run your mod manager "
                     "to re-register if needed).")
    except OSError as e:
        LOG.error("[ERROR] Could not restore nxm handler: %s", e)
        return False
    _associations_changed()
    return True


def forward_nxm(url, script_dir):
    """Handler-invocation entry point: hand the nxm URL to a running listener,
    or append it to the fallback file if no listener is up."""
    port = None
    try:
        with open(os.path.join(script_dir, NXM_PORT_FILE), encoding="utf-8") as f:
            port = int(f.read().strip())
    except (OSError, ValueError):
        port = None
    if port:
        try:
            with socket.create_connection((NXM_LISTEN_HOST, port), timeout=5) as s:
                s.sendall((url.strip() + "\n").encode("utf-8"))
            return
        except OSError:
            pass
    # No listener: persist it so a later --nxm-file run can use it.
    try:
        with open(os.path.join(script_dir, NXM_FALLBACK_FILE), "a", encoding="utf-8") as f:
            f.write(url.strip() + "\n")
    except OSError:
        pass


class NxmListener:
    """Tiny localhost listener that the handler forwards captured links to."""
    def __init__(self, script_dir):
        self.script_dir = script_dir
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((NXM_LISTEN_HOST, 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        with open(os.path.join(script_dir, NXM_PORT_FILE), "w", encoding="utf-8") as f:
            f.write(str(self.port))

    def wait(self, timeout):
        """Block up to `timeout` s for one nxm URL; return it or None."""
        self.sock.settimeout(timeout)
        try:
            conn, _ = self.sock.accept()
        except socket.timeout:
            return None
        with conn:
            conn.settimeout(5)
            data = b""
            try:
                while b"\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except socket.timeout:
                pass
        return data.decode("utf-8", "replace").strip() or None

    def close(self):
        try:
            self.sock.close()
        finally:
            try:
                os.remove(os.path.join(self.script_dir, NXM_PORT_FILE))
            except OSError:
                pass


# Domains that turn up in manifests but are not the slug the WEBSITE uses.
# nexusmods.com/skyrimse/... serves a "mod could not be found" page, so the
# click that assist mode waits 180s for can never happen. The API path already
# routed around this through NEXUS_DOMAIN_FALLBACKS; the browser URL did not.
NEXUS_URL_ALIASES = {
    "skyrimse":         "skyrimspecialedition",
    "falloutnv":        "newvegas",
    "falloutnewvegas":  "newvegas",
    "enderalse":        "enderalspecialedition",
}


def nexus_url_domain(domain):
    d = (domain or "").lower()
    return NEXUS_URL_ALIASES.get(d, d)


def resolve_browser_domain(nexus, archive, learned):
    """The domain whose website actually hosts this mod's page, or None when
    Nexus says there is no page to click.

    Asks the API instead of trusting the manifest. Meta gameName and
    State.GameName disagree often enough to matter - this list records
    "skyrimse" in Meta and "SkyrimSpecialEdition" in State, and only the second
    is a real site slug - and nothing in the manifest says which is right.

    A game other than the manifest's own is believed only when it lists the
    wanted file. Mod ids are per game and dense, so a Special Edition mod's id
    is usually some unrelated old-Skyrim mod's as well, and "that game serves
    a mod by this id" proved nothing: a mod removed from Special Edition sent
    the run to old Skyrim's page for a stranger, and had it remember old
    Skyrim for every Special Edition mod after it. Instead the manifest's own
    game is asked why it will not serve the files: a mod it lists as hidden,
    removed or unpublished has no page to click, and the caller skips it
    rather than waiting out the click timeout.

    The domain that worked last time is tried first, so a list normally costs
    one API call per mod. Returns (domain, note); (None, reason) when the mod
    is gone."""
    primary = (nexus_domain(archive) or "").lower()
    home = nexus_url_domain(primary)           # the manifest's game, as the site names it
    mod_id, file_id = nexus_ids(archive)
    if not mod_id:
        return home, None
    order = []
    if learned.get(primary):
        order.append(learned[primary])
    for d in candidate_domains(archive):
        cand = nexus_url_domain(d)
        if cand not in order:
            order.append(cand)

    def accept(cand):
        note = None
        if cand != primary and learned.get(primary) != cand:
            note = "manifest records domain '%s'; the site uses '%s'" % (primary, cand)
        learned[primary] = cand
        return cand, note

    serves_mod = []                 # other games serving a mod by this id, not this file
    for cand in order:
        files, status = nexus.mod_files(cand, mod_id)
        time.sleep(REQUEST_PAUSE_S)
        if status != "ok":
            continue
        if cand == home or file_id in NexusClient._file_ids(files):
            return accept(cand)
        serves_mod.append(cand)
    record, status = nexus.mod_info(home, mod_id)
    time.sleep(REQUEST_PAUSE_S)
    state = (record or {}).get("status") if status == "ok" else None
    if isinstance(state, str) and state and state != "published":
        return None, ("Nexus lists mod %s as %s on %s; there is no page to click"
                      % (mod_id, state.replace("_", " "), home))
    if serves_mod:
        # Not learned: a stranger under the same id is the likelier reading,
        # and the next mod is measured on its own.
        return serves_mod[0], ("manifest records domain '%s'; '%s' serves mod %s but does "
                               "not list file %s - the page may not offer it"
                               % (primary, serves_mod[0], mod_id, file_id))
    return home, "no Nexus domain served mod %s; this page link may 404" % mod_id


def nexus_file_page(domain, mod_id, file_id):
    """The mod-manager download page; clicking its button fires the nxm link."""
    return (f"https://www.nexusmods.com/{domain}/mods/{mod_id}"
            f"?tab=files&file_id={file_id}&nmm=1")


# ----------------------------------------------------------------------
# MediaFire (best effort)
# ----------------------------------------------------------------------
_MF_PATTERNS = [
    re.compile(r'href="(https://download[^"]+?mediafire\.com/[^"]+)"'),
    re.compile(r'id="downloadButton"[^>]*href="([^"]+)"'),
    re.compile(r'(https://download\d+\.mediafire\.com/[^"\']+)'),
]


def resolve_mediafire(page_url):
    try:
        req = urllib.request.Request(sanitize_url(page_url),
                                     headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception as e:
        LOG.warning("  [WARN] Could not load MediaFire page: %s", e)
        return None
    for pat in _MF_PATTERNS:
        m = pat.search(html)
        if m:
            return m.group(1)
    return None


# ----------------------------------------------------------------------
# Generic streaming download with resume + progress
# ----------------------------------------------------------------------
def stream_download(url, dest, expected_size=None, retries=DOWNLOAD_RETRIES, quiet=False):
    """Download with resume-aware retries. Each attempt resumes from the .part
    file the previous attempt left behind, so a dropped connection on a large
    file doesn't restart from zero. A clean HTTP error (e.g. 403/404) is not
    retried; only transient connection/timeout/write failures are.

    `quiet` suppresses the per-file progress line, for a download that runs
    beside others: several streams writing carriage returns to one console
    would garble each other, so the pool prints one line for all of them."""
    attempt = 0
    while True:
        attempt += 1
        status = _stream_download_once(url, dest, expected_size, quiet=quiet)
        if status is True:
            return True
        if status == "fatal" or attempt > retries:
            return False
        wait = min(30, 3 * attempt)
        LOG.warning("  [RETRY] %s: transient failure, retrying in %ds (%d/%d)...",
                    os.path.basename(dest), wait, attempt, retries)
        time.sleep(wait)


def _stream_download_once(url, dest, expected_size=None, quiet=False):
    """One download attempt. Returns True on success, 'fatal' on an
    unrecoverable HTTP error, or False on a transient failure worth retrying.

    STREAM_TIMEOUT bounds every wait on the socket, the connect and each
    read alike, so a connection that goes silent becomes a retry rather than
    a hang. That, with the resume above, is what makes a stall cost at most
    the timeout - whether the file runs alone or beside others."""
    url = sanitize_url(url)
    part = dest + ".part"
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    existing = os.path.getsize(part) if os.path.exists(part) else 0
    headers = {"User-Agent": f"{APP_NAME}/{APP_VERSION}"}
    mode = "wb"
    if existing:
        headers["Range"] = f"bytes={existing}-"

    try:
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=STREAM_TIMEOUT)
    except urllib.error.HTTPError as e:
        if e.code == 416:
            try:
                os.remove(part)
            except OSError:
                pass
            return _stream_download_once(url, dest, expected_size, quiet=quiet)
        LOG.error("  [ERROR] HTTP %s downloading %s", e.code, os.path.basename(dest))
        return "fatal"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        LOG.error("  [ERROR] Connection failed for %s: %s", os.path.basename(dest), e)
        return False

    with resp:
        if existing and resp.status == 206:
            mode = "ab"
        else:
            existing = 0
        try:
            total = int(resp.headers.get("Content-Length", 0)) + existing
        except (TypeError, ValueError):
            total = expected_size or 0

        downloaded = existing
        last_print = 0.0
        try:
            with open(part, mode) as f:
                while True:
                    chunk = resp.read(DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    now = time.time()
                    if not quiet and now - last_print > 0.5:
                        last_print = now
                        if total:
                            pct = downloaded * 100 / total
                            say_progress(
                                f"\r    {pct:5.1f}%  {human_bytes(downloaded)} / {human_bytes(total)}   ")
                        else:
                            say_progress(f"\r    {human_bytes(downloaded)}   ")
        except (OSError, urllib.error.URLError, TimeoutError,
                http.client.IncompleteRead, http.client.HTTPException) as e:
            if not quiet:
                say_progress("\n")
            LOG.warning("  [WARN] Transfer interrupted for %s: %s (will resume)",
                        os.path.basename(dest), e)
            return False

    if not quiet:
        say_progress("\r" + " " * 60 + "\r")

    # A connection that closes cleanly part-way through looks exactly like a
    # finished download: read() returns b"" and nothing raises. Without this
    # check the short .part was renamed into place and reported as SUCCESS, so
    # the retry-and-resume machinery below never engaged at all, and the caller
    # only discovered the problem later by hashing - which is how a pool fills
    # up with files that are an exact multiple of the chunk size.
    #
    # Returning False instead keeps the .part and lets the retry resume from
    # where it stopped, which is what the resume support was for.
    want = total or expected_size
    if want and downloaded < want:
        LOG.warning("  [WARN] %s ended early at %s of %s; resuming.",
                    os.path.basename(dest), human_bytes(downloaded),
                    human_bytes(want))
        return False

    try:
        if os.path.exists(dest):
            os.remove(dest)
        os.replace(part, dest)
    except OSError as e:
        LOG.error("  [ERROR] Could not finalise %s: %s", os.path.basename(dest), e)
        return False
    return True


def write_meta(dest, meta_text):
    if not meta_text:
        return
    try:
        with open(dest + ".meta", "w", encoding="utf-8") as f:
            f.write(meta_text if meta_text.endswith("\n") else meta_text + "\n")
    except OSError as e:
        LOG.warning("  [WARN] Could not write .meta for %s: %s", os.path.basename(dest), e)


_PROGRESS_BROKEN = False


def say_progress(text):
    """Write a progress line to stdout, and never let that fail a download.

    Under the window, stdout is a pipe the window reads. If the window is
    closed while a download runs, the child lives on but every write to that
    pipe raises EINVAL - and the write sat inside the transfer loop, so each
    chunk's progress line was read as a broken transfer, retried four times,
    and given up on: every file after the window closed 'failed' although
    the network was fine. Progress is decoration; the first failure switches
    it off for the rest of the run and the bytes keep flowing."""
    global _PROGRESS_BROKEN
    if _PROGRESS_BROKEN:
        return
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except (OSError, ValueError, AttributeError):
        _PROGRESS_BROKEN = True


# The results dict is shared by every download of a run. With several files
# in flight it is written from several threads, so every change goes through
# here; a lost increment would make the summary lie about what landed.
_RESULTS_LOCK = threading.Lock()


def _tally(results, ok, name=None, reason=None):
    with _RESULTS_LOCK:
        if ok:
            results["done"] += 1
        else:
            results["failed"] += 1
            results["failures"].append((name, reason))


def download_one(url, archive, dest_dir, results, quiet=False):
    """Download a resolved URL into dest_dir, verify, write .meta. Update results.

    `quiet` is for a download running beside others: no progress line of its
    own, and a CDN file is fetched with fewer part-workers so a pool of them
    does not open a dozen connections at once."""
    name = archive["name"]
    dest = os.path.join(dest_dir, name)

    # WabbajackCDN URLs are directory prefixes, not files.
    if _HAVE_CDN and (archive.get("_cdn") or _CDN.is_cdn_url(url)):
        os.makedirs(dest_dir, exist_ok=True)
        # strict_hash: the CDN checks the assembled file against ITS definition,
        # which describes whatever is being served today. The manifest pins the
        # build the list was made against. When the two disagree the file is
        # one Wabbajack will reject, so it is refused before the download
        # rather than fetched and then thrown away. The HTTP path below has
        # always discarded a wrong-hash file; this path used to keep it.
        ok, got = _CDN.download(url, dest, expected_size=archive["size"],
                                expected_hash=archive["hash"], detail=True,
                                strict_hash=True, progress=not quiet,
                                workers=(2 if quiet else _CDN.DEFAULT_WORKERS))
        if not ok:
            _tally(results, False, name, "CDN multi-part download failed (see log: "
                                         "definition, parts, or a hash that differs "
                                         "from the manifest)")
            return False
        # The CDN module already hashed the assembled file; do not read a
        # multi-GB archive off the share a second time just to repeat that.
        if archive["hash"] and got and got != archive["hash"]:
            good, reason = False, "hash %s != manifest %s" % (got, archive["hash"])
        else:
            good, reason = verify_file(dest, archive["size"],
                                       None if got else archive["hash"])
        if not good:
            LOG.error("  [ERROR] %s failed verification: %s - discarding.", name, reason)
            try:
                os.remove(dest)
            except OSError:
                pass
            _tally(results, False, name, f"verify: {reason}")
            return False
        write_meta(dest, archive["meta_text"])
        _tally(results, True)
        return True

    if _HAVE_CDN is False and isinstance(url, str) and (
            "wabbajack.org" in url or "b-cdn.net" in url):
        LOG.error("  [SKIP] %s needs wj_cdn.py, which is not next to "
                  "this script.", name)
        _tally(results, False, name, "wj_cdn.py missing")
        return False

    if not stream_download(url, dest, expected_size=archive["size"], quiet=quiet):
        _tally(results, False, name, "download failed")
        return False
    good, reason = verify_file(dest, archive["size"], archive["hash"])
    if not good:
        # Previously this still counted as done and wrote a .meta, which left a
        # junk file in the pool for the sorter to shuffle around. Discard it.
        LOG.error("  [ERROR] %s failed verification: %s - discarding.", name, reason)
        try:
            os.remove(dest)
        except OSError:
            pass
        _tally(results, False, name, f"verify: {reason}")
        return False
    write_meta(dest, archive["meta_text"])
    _tally(results, True)
    return True


# ----------------------------------------------------------------------
# Interactive selection (shared style with the sorter)
# ----------------------------------------------------------------------
def choose_game(games_present, preset=None):
    if preset is not None:
        for g in games_present:
            if preset.lower() in (g.lower(), game_label(g).lower(), game_folder(g).lower()):
                return g
        LOG.error("[ERROR] --game '%s' is not among the games present: %s",
                  preset, ", ".join(games_present))
        sys.exit(2)
    if len(games_present) == 1:
        LOG.info("\nOnly one game present: %s - selecting it automatically.",
                 game_label(games_present[0]))
        return games_present[0]
    LOG.info("\nWhich game do you want to download for?")
    game_map = {str(i): g for i, g in enumerate(games_present, 1)}
    for num, g in game_map.items():
        LOG.info("  [%s] %s", num, game_label(g))
    for _ in range(3):
        try:
            choice = input("\nSelect a game number: ").strip()
        except (EOFError, KeyboardInterrupt):
            LOG.info("\n[ABORT] No selection made.")
            sys.exit(1)
        if choice in game_map:
            return game_map[choice]
        LOG.warning("[WARN] '%s' is not a listed option. Try again.", choice)
    LOG.error("[ERROR] No valid game selected after 3 attempts.")
    sys.exit(2)


def choose_modlist(modlists_map, preset=None):
    """Resolve --list. Accepts a menu number OR a modlist name.

    Names are what callers should use. A menu number is positional: it depends
    on how many manifests parsed successfully this run, so a transient read
    error on an unrelated file silently shifts every number and the wrong list
    gets operated on. A name cannot drift."""
    if preset is not None:
        if preset in modlists_map:
            return modlists_map[preset]
        by_name = {nm.lower(): nm for nm in modlists_map.values()}
        if preset.lower() in by_name:
            return by_name[preset.lower()]
        LOG.error("[ERROR] Preset list selector '%s' is not valid. Known lists: %s",
                  preset, ", ".join(sorted(modlists_map.values())))
        sys.exit(2)
    LOG.info("\nWhich modlist do you want to download archives for?")
    for num, nm in modlists_map.items():
        LOG.info("  [%s] %s", num, nm)
    for _ in range(3):
        try:
            choice = input("\nSelect a modlist number: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            LOG.info("\n[ABORT] No selection made.")
            sys.exit(1)
        if choice in modlists_map:
            return modlists_map[choice]
        LOG.warning("[WARN] '%s' is not a listed option. Try again.", choice)
    LOG.error("[ERROR] No valid selection after 3 attempts.")
    sys.exit(2)


def confirm(prompt, assume_yes=False):
    if assume_yes:
        LOG.info("%s [auto-yes]", prompt)
        return True
    try:
        return input(prompt + " [y/N]: ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


# ----------------------------------------------------------------------
# API key resolution (never logged)
# ----------------------------------------------------------------------
def resolve_api_key(cli_key, script_dir, prompt=True):
    if cli_key:
        return cli_key.strip()
    env = os.environ.get("NEXUS_API_KEY")
    if env:
        return env.strip()
    keyfile = os.path.join(script_dir, "nexus_api_key.txt")
    if os.path.isfile(keyfile):
        try:
            with open(keyfile, encoding="utf-8") as f:
                k = f.readline().strip()
                if k:
                    return k
        except OSError:
            pass
    if not prompt:
        return ""
    try:
        return input("Enter your Nexus Personal API key: ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


# ----------------------------------------------------------------------
# Download routines per situation
# ----------------------------------------------------------------------
def _direct_url(kind, a, results):
    """The URL a direct-source archive is fetched from, or None after
    recording why there is none."""
    try:
        if kind in ("http", "wabbajackcdn"):
            url = (a["state"] or {}).get("Url") or a["meta"].get("directurl")
        elif kind == "mediafire":
            page = (a["state"] or {}).get("Url") or a["meta"].get("directurl")
            url = resolve_mediafire(page) if page else None
            if not url:
                raise RuntimeError("could not resolve MediaFire direct link")
        else:
            url = None
    except RuntimeError as e:
        LOG.error("  [SKIP] %s: %s", a["name"], e)
        _tally(results, False, a["name"], str(e))
        return None
    if not url:
        LOG.error("  [SKIP] %s: no usable download URL.", a["name"])
        _tally(results, False, a["name"], "no URL")
        return None
    # The manifest's own State type is the authoritative signal that this
    # is a multi-part CDN file, more reliable than matching the hostname.
    a["_cdn"] = (kind == "wabbajackcdn")
    return url


def is_small_download(archive, small_bytes):
    """May this file share the line with others? Only a file of known size at
    or under the threshold. Unknown counts as large: a file that turns out
    to be 4 GB must not have started beside two others."""
    size = archive.get("size")
    return isinstance(size, int) and size <= small_bytes


def download_direct(items, dest_dir, results, limit=0):
    """Non-Nexus sources (http / wabbajackcdn / mediafire). No account needed.

    One file at a time, unless PARALLEL_SMALL says otherwise: then up to
    that many files under SMALL_BYTES run together, and anything larger
    still runs alone - it starts as soon as a slot is free, and nothing new
    starts beside it. The order the items arrive in is the order they are
    admitted in, so 'smallest first' turns a run into a parallel phase of
    small files followed by a sequential phase of large ones."""
    if PARALLEL_SMALL <= 1:
        for kind, a in items:
            if limit and results["done"] >= limit:
                LOG.info("  [STOP] Reached --limit; stopping.")
                return
            LOG.info("[%s] %s  (%s)", kind, a["name"],
                     human_bytes(a["size"]) if a["size"] else "size unknown")
            url = _direct_url(kind, a, results)
            if url:
                download_one(url, a, dest_dir, results)
        return
    _DirectPool(PARALLEL_SMALL, SMALL_BYTES, dest_dir, results, limit).run(items)


class _DirectPool:
    """Several small direct downloads at once, large ones alone.

    Every stream keeps its own timeout, resume and retries, so a stall in one
    slot costs that slot at most STREAM_TIMEOUT and holds up nothing else:
    the other slots finish and are refilled around it. Small files run
    quiet, with one aggregate progress line for the pool; a large file has
    the console to itself and prints its own line as it always did."""

    def __init__(self, workers, small_bytes, dest_dir, results, limit):
        self.workers = max(1, workers)
        self.small_bytes = small_bytes
        self.dest_dir, self.results, self.limit = dest_dir, results, limit
        self.running = {}                 # future -> (archive, is_large)
        self.large_running = False
        self.small_total = self.small_done = 0
        self.small_bytes_done = 0
        self.started = time.time()
        self.last_print = 0.0
        self.printed = False

    def run(self, items):
        self.small_total = sum(1 for _k, a in items if is_small_download(a, self.small_bytes))
        LOG.info("Up to %d small file(s) (under %s) at once; larger ones one at a time.",
                 self.workers, human_bytes(self.small_bytes))
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=self.workers,
                                                     thread_name_prefix="download")
        try:
            for kind, a in items:
                if self.limit and self.results["done"] >= self.limit:
                    LOG.info("  [STOP] Reached --limit; stopping.")
                    break
                is_large = not is_small_download(a, self.small_bytes)
                # A slot must be free, and nothing may start while a large
                # file runs: that is what keeps the line whole for it.
                while self.large_running or len(self.running) >= self.workers:
                    self._reap(0.5)
                url = _direct_url(kind, a, self.results)
                if not url:
                    continue
                LOG.info("[%s] %s  (%s)%s", kind, a["name"],
                         human_bytes(a["size"]) if a["size"] else "size unknown",
                         "" if is_large else "  [pooled]")
                fut = pool.submit(download_one, url, a, self.dest_dir, self.results,
                                  quiet=not is_large)
                self.running[fut] = (a, is_large)
                if is_large:
                    self.large_running = True
            while self.running:
                self._reap(0.5)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            if self.printed:
                say_progress("\r" + " " * 70 + "\r")

    def _reap(self, timeout):
        """Collect whatever finished, then refresh the pool's progress line."""
        done, _ = concurrent.futures.wait(list(self.running), timeout=timeout,
                                          return_when=concurrent.futures.FIRST_COMPLETED)
        for fut in done:
            a, is_large = self.running.pop(fut)
            if is_large:
                self.large_running = False
            try:
                ok = fut.result()
            except Exception as e:                  # a bug, not a bad download
                LOG.error("  [ERROR] %s: %s", a["name"], e)
                _tally(self.results, False, a["name"], str(e))
                ok = False
            if not is_large:
                self.small_done += 1
                if ok:
                    self.small_bytes_done += a.get("size") or 0
                LOG.info("  [%s] %s", "OK" if ok else "FAILED", a["name"])
        self._progress()

    def _progress(self):
        now = time.time()
        in_flight = sum(1 for _f, (_a, large) in self.running.items() if not large)
        if not in_flight and not (self.small_total and self.small_done < self.small_total):
            return
        if now - self.last_print < 0.5:
            return
        self.last_print = now
        pct = (100.0 * self.small_done / self.small_total) if self.small_total else 0.0
        rate = self.small_bytes_done / max(0.1, now - self.started) / (1 << 20)
        say_progress(f"\r    {pct:5.1f}%  {self.small_done}/{self.small_total} small "
                     f"file(s), {in_flight} in flight, {rate:.1f} MB/s   ")
        self.printed = True


def download_nexus_premium(items, nexus, dest_dir, results, limit=0,
                           forced_domain=None, allow_fileid_fallback=True):
    for a in items:
        if limit and results["done"] >= limit:
            LOG.info("  [STOP] Reached --limit; stopping.")
            return
        if nexus.daily_remaining is not None and nexus.daily_remaining <= NEXUS_DAILY_FLOOR:
            LOG.error("  [STOP] Nexus daily request allowance nearly exhausted (%s left). "
                      "Stopping; re-run later.", nexus.daily_remaining)
            return
        LOG.info("[nexus] %s  (%s)", a["name"],
                 human_bytes(a["size"]) if a["size"] else "size unknown")
        try:
            url, note = nexus.resolve_link(
                a, forced_domain=forced_domain,
                allow_fileid_fallback=allow_fileid_fallback)
        except RuntimeError as e:
            LOG.error("  [SKIP] %s: %s", a["name"], e)
            _tally(results, False, a["name"], str(e))
            continue
        if note:
            LOG.info("  [NOTE] %s: %s", a["name"], note)
        download_one(url, a, dest_dir, results)


def _resolve_nxm(link, nexus, by_ids, results, fetched):
    """Turn a captured nxm link into (download_url, archive, ids) through the
    API, or None after saying why not. The resolving is done at once, while
    the click is fresh: the key in the link expires, the URL it mints does
    not need to be fetched this second."""
    ids = (link["mod_id"], link["file_id"])
    a = by_ids.get(ids)
    if a is None:
        LOG.info("  [SKIP] nxm for mod %s file %s is not a pending file for this list.",
                 link["mod_id"], link["file_id"])
        return None
    if ids in fetched:
        return None
    if not (link["key"] and link["expires"]):
        LOG.error("  [SKIP] %s: nxm link has no key/expires.", a["name"])
        _tally(results, False, a["name"], "nxm missing key/expires")
        return None
    LOG.info("[nexus] %s  (%s)", a["name"],
             human_bytes(a["size"]) if a["size"] else "size unknown")
    try:
        # The click pins the file id: the key in the link is good for that id
        # alone, so no substitution here - and a page opened for a replacement
        # file (_assist_loop, when the pinned one is gone) yields a click for
        # the replacement's id, which is then the one asked for. The domain
        # may still fall back (the nxm netloc is sometimes the target game
        # rather than the file's host game).
        a_with_domain = dict(a)
        a_with_domain["meta"] = dict(a.get("meta") or {})
        a_with_domain["meta"]["gamename"] = link["domain"]
        a_with_domain["meta"]["modid"] = str(link["mod_id"])
        a_with_domain["meta"]["fileid"] = str(link["file_id"])
        url, note = nexus.resolve_link(
            a_with_domain, key=link["key"], expires=link["expires"],
            allow_fileid_fallback=False)
    except RuntimeError as e:
        LOG.error("  [SKIP] %s: %s", a["name"], e)
        _tally(results, False, a["name"], str(e))
        return None
    if note:
        LOG.info("  [NOTE] %s: %s", a["name"], note)
    return url, a, ids


def _resolve_and_download_nxm(link, nexus, by_ids, dest_dir, results, fetched):
    """Given a parsed nxm dict, resolve via the API and download the matching archive."""
    resolved = _resolve_nxm(link, nexus, by_ids, results, fetched)
    if resolved is None:
        return
    url, a, ids = resolved
    if download_one(url, a, dest_dir, results):
        fetched.add(ids)


class _AssistDownloader:
    """Assist mode's downloads on a thread of their own, one at a time, so
    the next Nexus page can open while the current file is still arriving.

    At most one file waits behind the one downloading. That bound is what
    keeps a minted link from sitting long enough to expire, and it is where
    the person and the transfer meet: when a large file is downloading, the
    next page opens as soon as the waiting file starts, not before."""

    def __init__(self, dest_dir, results, fetched):
        self.dest_dir, self.results, self.fetched = dest_dir, results, fetched
        self.q = queue.Queue()
        self.current = None
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="assist-download")
        self.thread.start()

    def _run(self):
        while True:
            item = self.q.get()
            if item is None:
                self.q.task_done()
                return
            url, a, ids = item
            self.current = a["name"]
            try:
                if download_one(url, a, self.dest_dir, self.results):
                    self.fetched.add(ids)
                    LOG.info("  [OK] %s", a["name"])
            except Exception as e:                  # a bug, not a bad download
                LOG.error("  [ERROR] %s: %s", a["name"], e)
                _tally(self.results, False, a["name"], str(e))
            finally:
                self.current = None
                self.q.task_done()

    def wait_for_room(self):
        """Block while a file is already waiting behind the one downloading.

        Said aloud only when the wait is real: right after a submit the queue
        holds the file for the instant before the thread takes it, and a note
        on every page for that would be noise."""
        said = False
        while self.q.qsize() >= 1:
            time.sleep(0.25)
            if not said and self.q.qsize() >= 1:
                LOG.info("  (%s is still downloading; the next page opens when the "
                         "queued file starts)", self.current or "a file")
                said = True

    def submit(self, url, a, ids):
        self.q.put((url, a, ids))

    def finish(self):
        """Let the queue drain, then stop the thread."""
        self.q.put(None)
        self.thread.join()

    def abort(self):
        """Drop whatever is waiting. The download in progress is left to
        run out with the process: its .part resumes next time."""
        while True:
            try:
                self.q.get_nowait()
                self.q.task_done()
            except queue.Empty:
                break
        self.q.put(None)


def download_nexus_from_file(items, nexus, dest_dir, results, nxm_path, limit=0):
    """Replay a file of nxm:// links (free-account batch path)."""
    by_ids = {nexus_ids(a): a for a in items}
    fetched = set()
    try:
        with open(nxm_path, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except OSError as e:
        LOG.error("[ERROR] Could not read nxm link file %s: %s", nxm_path, e)
        return
    LOG.info("Loaded %d nxm link(s) from %s.", len(lines), nxm_path)
    for ln in lines:
        if limit and results["done"] >= limit:
            LOG.info("  [STOP] Reached --limit; stopping.")
            return
        link = parse_nxm(ln)
        if not link:
            LOG.warning("  [WARN] Not a valid nxm:// link, skipping: %s", ln[:60])
            continue
        _resolve_and_download_nxm(link, nexus, by_ids, dest_dir, results, fetched)
    missing = [a["name"] for ids, a in by_ids.items() if ids not in fetched]
    if missing:
        LOG.warning("[WARN] %d Nexus file(s) had no matching nxm link in the file.",
                    len(missing))


class _BrowserOpener:
    """The pages in the user's normal browser, one tab each: the arrangement
    before the window existed, and the fallback when it cannot open."""
    closed = threading.Event()          # a browser is never 'closed' on us

    def show(self, url, banner=""):
        try:
            webbrowser.open(url)
            return True
        except Exception:
            LOG.info("  (Could not auto-open a browser; open the URL above manually.)")
            return False


def _wait_click(listener, timeout, stop=None, skip=None):
    """One captured nxm URL within `timeout` seconds; SKIPPED when the
    window's Skip button was pressed (`skip` is cleared for the next page);
    None on the timeout or on `stop`.

    Wakes every two seconds to notice `stop` and `skip`: the window being
    closed must end the wait at once, not after the three minutes a click is
    given."""
    deadline = time.time() + timeout
    while True:
        if skip is not None and skip.is_set():
            skip.clear()
            return SKIPPED
        left = deadline - time.time()
        if left <= 0 or (stop is not None and stop.is_set()):
            return None
        url = listener.wait(min(2.0, left))
        if url:
            if url.strip().lower().startswith(NXM_SKIP_PREFIX):
                return SKIPPED          # the button, by way of the protocol handler
            return url


def download_nexus_assist(items, nexus, dest_dir, results, script_dir, args, opener=None):
    """Interactive free-account capture: show each page, catch the nxm click.

    Where the pages are shown: `opener` if given; else a window of our own
    when ASSIST_WINDOW is on and the engine is there (see wj_assistwin); else
    the browser. The window owns the main thread while it is open, so the
    capture loop then runs on a thread of the window's making."""
    if not _HAVE_WINREG:
        LOG.error("[ERROR] winreg unavailable; cannot register the nxm handler.")
        return
    if opener is None:
        if ASSIST_WINDOW and _ASSISTWIN is not None and _ASSISTWIN.available():
            LOG.info("The Nexus pages open in a window of their own. Sign in to Nexus there "
                     "if it asks - once; the window keeps its login. Closing it ends the run.")
            win = _ASSISTWIN.AssistWindow()
            win.run(lambda w: _assist_loop(items, nexus, dest_dir, results, script_dir, args, w))
            return
        if ASSIST_WINDOW:
            LOG.info("The Nexus pages open in your browser (%s).",
                     _ASSISTWIN.why_not() if _ASSISTWIN is not None else "wj_assistwin is missing")
        opener = _BrowserOpener()
    _assist_loop(items, nexus, dest_dir, results, script_dir, args, opener)


def _assist_loop(items, nexus, dest_dir, results, script_dir, args, opener):
    by_ids = {nexus_ids(a): a for a in items}
    fetched = set()
    learned_domains = {}

    # Make sure we are the nxm handler (so clicks reach us).
    if not register_nxm(script_dir):
        LOG.error("[ERROR] Could not register the nxm handler; aborting assist mode.")
        return

    listener = NxmListener(script_dir)
    if hasattr(opener, "on_link"):
        # The window catches the nxm:// link itself and hands it here; the
        # registered handler above is only the fallback should it not.
        opener.on_link = lambda uri: forward_nxm(uri, script_dir)
    LOG.info("Capture listener on %s:%d. For each file, the Nexus download page opens - "
             "click the mod-manager download button.", NXM_LISTEN_HOST, listener.port)
    LOG.info("(The Nexus site shows a short timer for free downloads; wait for it, then click.)")
    worker = None
    if CLICK_AHEAD:
        worker = _AssistDownloader(dest_dir, results, fetched)
        LOG.info("Click-ahead is on: the next page opens while the current file downloads.")
    skip = getattr(opener, "skip", None)      # the window's Skip button; the browser has none
    try:
        pending = list(items)
        for idx, a in enumerate(pending, 1):
            if opener.closed.is_set():
                LOG.info("\n[ABORT] The download window was closed; stopping here. Everything "
                         "downloaded so far is kept.")
                if worker is not None:
                    worker.abort()
                    worker = None
                break
            if args.limit and results["done"] >= args.limit:
                LOG.info("  [STOP] Reached --limit; stopping.")
                break
            ids = nexus_ids(a)
            if ids in fetched:
                continue
            if not (nexus_domain(a) and ids[0] and ids[1]):
                LOG.error("  [SKIP] %s: incomplete Nexus identifiers.", a["name"])
                _tally(results, False, a["name"], "incomplete Nexus identifiers")
                continue
            if (nexus.daily_remaining is not None
                    and nexus.daily_remaining <= NEXUS_DAILY_FLOOR):
                LOG.error("  [STOP] Nexus daily request allowance nearly "
                          "exhausted (%s left). Stopping; re-run later. "
                          "Everything downloaded so far is kept.",
                          nexus.daily_remaining)
                break
            if worker is not None:
                worker.wait_for_room()
            LOG.info("\n[%d/%d] %s", idx, len(pending), a["name"])
            # Confirm the domain against the API rather than trusting the
            # manifest's Meta gameName: this list records "skyrimse", which the
            # website does not serve, and every page then opens on "mod could
            # not be found". Cached, so it costs about one call per mod. A mod
            # Nexus has taken down has no page worth opening: recorded and
            # passed over, not waited out.
            domain, dom_note = resolve_browser_domain(nexus, a, learned_domains)
            if domain is None:
                LOG.warning("  [SKIP] %s", dom_note)
                _tally(results, False, a["name"], dom_note)
                continue
            page_ids = ids
            files, fstatus = nexus.mod_files(domain, ids[0])    # remembered from the resolve
            if fstatus == "ok" and ids[1] not in NexusClient._file_ids(files):
                # The author has updated the mod and taken this file down
                # since the list was built: its page would show the mod with
                # no button for this file, the other kind of dead page. A
                # current file of the same name (or size) is the likelier
                # same bytes, so its page is opened instead, and the hash
                # check after the download is the judge of that.
                new_fid = nexus.recover_file_id(domain, ids[0], a)
                if not new_fid:
                    reason = ("Nexus no longer offers file %s of mod %s - the mod was updated "
                              "since the list was built - and no current file matches it; "
                              "there is no page to click" % (ids[1], ids[0]))
                    LOG.warning("  [SKIP] %s", reason)
                    _tally(results, False, a["name"], reason)
                    continue
                LOG.info("  [NOTE] file %s is no longer offered; opening its replacement %s, which "
                         "matches by name or size (the hash is checked after the download)",
                         ids[1], new_fid)
                page_ids = (ids[0], new_fid)
                by_ids[page_ids] = a                # the click will carry the replacement's id
            page = nexus_file_page(domain, page_ids[0], page_ids[1])
            if dom_note:
                LOG.info("  [NOTE] %s", dom_note)
            LOG.info("  Opening: %s", page)
            if skip is not None:
                skip.clear()                    # a press meant for the last page is spent
            opener.show(page, f"Wabbajack Wizard  ·  file {idx} of {len(pending)}  ·  "
                              f"{a['name']}  ·  click Slow download")
            LOG.info("  Waiting up to %ds for your click... (Ctrl+C to stop%s)", NXM_CLICK_TIMEOUT,
                     "; Skip in the window's banner moves on" if skip is not None else "")
            url = _wait_click(listener, NXM_CLICK_TIMEOUT, opener.closed, skip)
            if url is SKIPPED:
                LOG.warning("  [SKIP] You skipped %s; moving on.", a["name"])
                _tally(results, False, a["name"], "skipped in the download window")
                continue
            if not url:
                if opener.closed.is_set():
                    continue                    # the loop's head says why, and stops
                LOG.warning("  [TIMEOUT] No click captured for %s; moving on.", a["name"])
                with _RESULTS_LOCK:
                    results["failures"].append((a["name"], "no nxm click captured"))
                continue
            link = parse_nxm(url)
            if not link:
                LOG.warning("  [WARN] Captured a non-nxm URL; skipping.")
                continue
            if worker is None:
                _resolve_and_download_nxm(link, nexus, by_ids, dest_dir, results, fetched)
                continue
            resolved = _resolve_nxm(link, nexus, by_ids, results, fetched)
            if resolved is not None:
                worker.submit(*resolved)
        if worker is not None:
            if worker.q.qsize() or worker.current:
                LOG.info("  Every page has been clicked; finishing the download(s) in flight.")
            worker.finish()
            worker = None
    except KeyboardInterrupt:
        LOG.info("\n[ABORT] Capture interrupted by user.")
        if worker is not None:
            worker.abort()
    finally:
        listener.close()
        # Hand the nxm:// handler back to whatever owned it (normally
        # Wabbajack). Without this, every "download with manager" click on
        # Nexus keeps landing on this script until you notice and fix it by
        # hand - including the clicks Wabbajack itself is waiting for.
        unregister_nxm(script_dir)


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Download the archives a Wabbajack modlist needs (Premium or free).")
    parser.add_argument("--game", help="Pre-select a game (GameType or friendly name).")
    parser.add_argument("--list", dest="preset", help="Pre-select a modlist number.")
    parser.add_argument("--apikey", help="Nexus Personal API key (else env/file/prompt).")
    parser.add_argument("--dest", help="Override the destination folder for downloads.")
    parser.add_argument("--dry-run", action="store_true",
                        help="List what would be downloaded but fetch nothing.")
    parser.add_argument("--force", action="store_true",
                        help="Re-download even if a correct-size copy already exists.")
    parser.add_argument("--order", choices=("smallest", "largest", "manifest"),
                        help="Order archives are fetched in (default: %s). "
                             "'smallest' finishes the most files soonest and "
                             "loses the least to an interruption." % DOWNLOAD_ORDER)
    parser.add_argument("--limit", type=int, default=0,
                        help="Stop after N downloads (handy for testing).")
    parser.add_argument("--skip-nexus", action="store_true",
                        help="Download only the non-Nexus sources (no key needed).")
    parser.add_argument("--nexus-domain",
                        help="Force a Nexus game domain for all Nexus files "
                             "(e.g. 'skyrimspecialedition'). Use this if a list "
                             "records the wrong host game and every file 404s.")
    parser.add_argument("--no-fileid-fallback", action="store_true",
                        help="Do not substitute a current file when the pinned "
                             "Nexus file revision is gone (stricter; avoids "
                             "downloading a file Wabbajack may reject by hash).")
    parser.add_argument("--nxm-assist", action="store_true",
                        help="Free accounts: open each Nexus page and capture your click.")
    parser.add_argument("--nxm-file", dest="nxm_file",
                        help="Free accounts: replay a file of captured nxm:// links.")
    parser.add_argument("--register-nxm", action="store_true",
                        help="Register this script as the nxm:// handler and exit (Windows).")
    parser.add_argument("--unregister-nxm", action="store_true",
                        help="Restore the previous nxm:// handler and exit (Windows).")
    parser.add_argument("--nxm", help=argparse.SUPPRESS)  # internal: handler invocation
    parser.add_argument("--check-window", action="store_true",
                        help="Report whether assist mode can open its own download window "
                             "here (the WebView2 engine and its Python wrapper), then exit.")
    parser.add_argument("--allow-size-only", action="store_true",
                        help="Proceed even though 'xxhash' is missing and files "
                             "can only be checked by size. Off by default: a "
                             "silent downgrade to size-only verification is how "
                             "a corrupt archive survives to install time.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose console output.")
    args = parser.parse_args()

    script_dir = wj_paths.app_dir(__file__)

    # --- Handler-invocation entry point: forward the URL and exit immediately.
    if args.nxm:
        forward_nxm(args.nxm, script_dir)
        return

    if args.check_window:
        # No window is opened: the engine's presence and the .NET bridge are
        # what fail on a machine that cannot do this, and both can be asked
        # about without one.
        if _ASSISTWIN is None:
            print("no: wj_assistwin.py could not be imported (pywebview missing?)")
            return
        if not _ASSISTWIN.available():
            print(f"no: {_ASSISTWIN.why_not()}")
            return
        try:
            import clr  # noqa: F401  (pythonnet: loads the .NET runtime)
        except Exception as e:
            print(f"no: the .NET bridge failed to load ({e})")
            return
        print(f"yes: WebView2 runtime present, pywebview and .NET loaded; profile at "
              f"{_ASSISTWIN.storage_dir()}")
        return

    log_path = setup_logging(script_dir, verbose=args.verbose)

    # --- Standalone handler (un)registration.
    if args.register_nxm:
        register_nxm(script_dir)
        return
    if args.unregister_nxm:
        unregister_nxm(script_dir)
        return

    # The .wabbajack manifests need not live beside this script (wj_autoupdate.py
    # sets WJ_MANIFEST_DIR so the tooling can sit in its own folder, and this
    # script otherwise looks in ./modlists). Logs, the API key file and the CSV
    # reports stay in script_dir either way.
    manifest_dir = resolve_manifest_dir(script_dir)
    if manifest_dir:
        # Tell the index which subfolders are modlist folders, so one that is
        # neither a pool nor a modlist is reported rather than counted as
        # archives we own and then never staged by the sorter.
        set_known_list_dirs(known_list_dirs_from(manifest_dir))

    banner("ENVIRONMENT CHECK")
    LOG.info("Archive root:   %s", ARCHIVE_ROOT)
    if log_path:
        LOG.info("Log file:       %s", log_path)
    if args.dry_run:
        LOG.info("Mode:           DRY-RUN (nothing will be downloaded)")
    LOG.info("Hash verify:    %s", "xxHash64 (xxhash installed)" if _HAVE_XXHASH
             else "size only (install 'xxhash' for hash checks)")
    if not _HAVE_XXHASH and REQUIRE_HASH and not args.allow_size_only:
        LOG.error("[TERMINATED] 'xxhash' is not importable by this interpreter "
                  "(%s), so downloads could only be checked by size.", sys.executable)
        LOG.error("             Run setup_env.bat and launch via run.bat, or pass "
                  "--allow-size-only to accept size-only checks.")
        sys.exit(2)

    wj_files = list_manifest_files(manifest_dir)
    if not wj_files:
        LOG.error("[TERMINATED] No .wabbajack files found in %s", manifest_dir)
        sys.exit(1)
    if manifest_dir != script_dir:
        LOG.info("Manifest dir:   %s", manifest_dir)

    banner("STEP 1: SCANNING MANIFESTS")
    records = [parse_manifest(os.path.join(manifest_dir, f)) for f in wj_files]

    # A manifest we could not read is NOT the same as one with no GameType. It
    # drops out of the menu, which used to shift every list number below it, and
    # would make this list's archives look unwanted to the sorter. Say so.
    unreadable = [r["name"] for r in records if not r["readable"]]
    if unreadable:
        LOG.error("[ERROR] %d manifest(s) could not be read and are excluded from "
                  "this run: %s", len(unreadable), ", ".join(unreadable))
        LOG.error("        Re-download or remove them; do not rely on list "
                  "numbers while this is true.")

    games_present = sorted({r["game"] for r in records if r["game"]})
    if not games_present:
        LOG.error("[TERMINATED] None of the .wabbajack files declared a GameType.")
        sys.exit(1)

    banner("STEP 2: SELECT GAME")
    chosen_game = choose_game(games_present, preset=args.game)
    in_scope = [r for r in records if r["game"] == chosen_game]
    modlists_map = {str(i + 1): r["name"] for i, r in enumerate(in_scope)}

    banner("STEP 3: SELECT MODLIST")
    target = choose_modlist(modlists_map, preset=args.preset)
    record = next(r for r in in_scope if r["name"] == target)

    # The list's folder is named after it, so a list called "Master" would
    # download straight into the Master pool. The sorter refuses these names;
    # so does this.
    if target.lower() in reserved_list_names():
        LOG.error("[TERMINATED] Modlist '%s' clashes with a reserved pool folder "
                  "name. Rename the .wabbajack file.", target)
        sys.exit(2)

    game_base, _, _ = resolve_game_dirs(chosen_game)
    dest_dir = args.dest or os.path.join(game_base, target)
    LOG.info("\nGame:           %s", game_label(chosen_game))
    LOG.info("Modlist:        %s", target)
    LOG.info("Download into:  %s", dest_dir)

    # ---- Decide what needs fetching ---------------------------------
    banner("STEP 4: PLANNING DOWNLOADS")
    existing = {} if args.force else index_existing(game_base)
    LOG.info("[SUMMARY] %d file(s) already present in this game's pools.", len(existing))

    direct_items, nexus_items, skipped_present, skipped_gamefile, unsupported = [], [], 0, 0, []
    stale = []
    for a in record["archives"]:
        hit, why = present_match(existing, a)
        if hit:
            skipped_present += 1
            continue
        if why.startswith("on disk"):
            # Right name, wrong bytes. Previously this counted as present and
            # was never fetched again; Wabbajack would then fail on it at
            # install time with no clue why.
            stale.append((a["name"], why))
        kind = state_type(a["state"])
        if kind == "gamefile":
            skipped_gamefile += 1
        elif kind == "nexus":
            nexus_items.append(a)
        elif kind in ("http", "wabbajackcdn", "mediafire"):
            direct_items.append((kind, a))
        else:
            label = kind if kind != "unknown" else (
                "unknown:" + state_type_raw(a["state"]))
            unsupported.append((label, a["name"]))

    if args.skip_nexus and nexus_items:
        LOG.info("[INFO] --skip-nexus: ignoring %d Nexus file(s).", len(nexus_items))
        nexus_items = []

    order = args.order or DOWNLOAD_ORDER
    label = order_downloads(direct_items, nexus_items, order)
    if label:
        LOG.info("Order:          %s", label)

    by_kind = defaultdict(int)
    for kind, _ in direct_items:
        by_kind[kind] += 1
    if nexus_items:
        by_kind["nexus"] = len(nexus_items)

    LOG.info("To download:    %d  (%s)", len(direct_items) + len(nexus_items),
             ", ".join(f"{k}:{v}" for k, v in sorted(by_kind.items())) or "none")
    LOG.info("Already present:%d", skipped_present)
    if stale:
        LOG.warning("[WARN] %d file(s) are on disk under the right name but the "
                    "wrong size, and will be re-downloaded:", len(stale))
        for nm, why in stale[:20]:
            LOG.warning("  ? %s (%s)", nm, why)
        if len(stale) > 20:
            LOG.warning("  ... and %d more", len(stale) - 20)
    LOG.info("From game files:%d (Wabbajack copies these from your install)", skipped_gamefile)
    if unsupported:
        LOG.warning("[WARN] %d archive(s) use a source this script can't fetch "
                    "(download these manually in Wabbajack):", len(unsupported))
        for kind, nm in unsupported:
            LOG.warning("  ? [%s] %s", kind, nm)

    if not direct_items and not nexus_items:
        LOG.info("\n[DONE] Nothing to download.")
        return

    if args.dry_run:
        banner("DRY-RUN: FILES THAT WOULD BE DOWNLOADED")
        for kind, a in direct_items:
            LOG.info("  [%s] %s  (%s)", kind, a["name"],
                     human_bytes(a["size"]) if a["size"] else "size unknown")
        for a in nexus_items:
            LOG.info("  [nexus] %s  (%s)", a["name"],
                     human_bytes(a["size"]) if a["size"] else "size unknown")
        LOG.info("\n[DRY-RUN COMPLETE] Re-run without --dry-run to download.")
        return

    # ---- Nexus API key + account detection (only if Nexus work remains)
    nexus = None
    if nexus_items:
        key = resolve_api_key(args.apikey, script_dir)
        if not key:
            LOG.error("[TERMINATED] A Nexus API key is required for %d Nexus file(s). "
                      "(Use --skip-nexus to fetch only the non-Nexus archives.)",
                      len(nexus_items))
            sys.exit(1)
        nexus = NexusClient(key)
        if not nexus.validate():
            sys.exit(1)
        LOG.info("Nexus account:  %s (%s)", nexus.user_name or "?",
                 "PREMIUM" if nexus.is_premium else "FREE")

    total = len(direct_items) + len(nexus_items)
    if not confirm(f"\nProceed to download {total} archive(s) for '{target}' into {dest_dir}?",
                   assume_yes=args.yes):
        LOG.info("[ABORT] Cancelled by user.")
        return

    results = {"done": 0, "failed": 0, "failures": []}

    # ---- Non-Nexus first (works for everyone) ------------------------
    if direct_items:
        banner("STEP 5: DOWNLOADING (DIRECT SOURCES)")
        download_direct(direct_items, dest_dir, results, limit=args.limit)

    # ---- Nexus: pick the path based on account type ------------------
    if nexus_items:
        if nexus.is_premium and not args.nxm_assist and not args.nxm_file:
            banner("STEP 6: DOWNLOADING (NEXUS, PREMIUM)")
            download_nexus_premium(nexus_items, nexus, dest_dir, results, limit=args.limit,
                                   forced_domain=args.nexus_domain,
                                   allow_fileid_fallback=not args.no_fileid_fallback)
        elif args.nxm_file:
            banner("STEP 6: DOWNLOADING (NEXUS, nxm LINK FILE)")
            download_nexus_from_file(nexus_items, nexus, dest_dir, results,
                                     args.nxm_file, limit=args.limit)
        elif args.nxm_assist:
            banner("STEP 6: DOWNLOADING (NEXUS, CLICK-CAPTURE)")
            download_nexus_assist(nexus_items, nexus, dest_dir, results, script_dir, args)
        else:
            # Free account, no capture method chosen: explain and write a helper file.
            banner("STEP 6: NEXUS (FREE ACCOUNT) - ACTION NEEDED")
            LOG.info("Your Nexus account is FREE, so the API will not hand out direct links "
                     "for these %d file(s) automatically.", len(nexus_items))
            LOG.info("Choose one of:")
            LOG.info("  * Re-run with --nxm-assist to open each page and capture your clicks.")
            LOG.info("  * Re-run with --nxm-file <links.txt> to replay nxm:// links you saved.")
            LOG.info("  * Or just let Wabbajack download the Nexus files (it handles the free "
                     "click flow itself).")
            pages = os.path.join(script_dir, f"nexus_pages_{target}.csv")
            try:
                with open(pages, "w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(["archive", "download_page"])
                    for a in nexus_items:
                        d = nexus_url_domain(nexus_domain(a))
                        mid, fid = nexus_ids(a)
                        if d and mid and fid:
                            w.writerow([a["name"], nexus_file_page(d, mid, fid)])
                LOG.info("Wrote the download-page list to: %s", pages)
            except OSError:
                pass

    # ---- Summary -----------------------------------------------------
    banner("DOWNLOAD COMPLETE")
    LOG.info("-> Downloaded:        %d", results["done"])
    LOG.info("-> Failed/skipped:    %d", results["failed"])
    LOG.info("-> Already present:   %d", skipped_present)
    LOG.info("-> Game-file sources: %d", skipped_gamefile)
    LOG.info("-> Unsupported source:%d", len(unsupported))
    if nexus and nexus.daily_remaining is not None:
        LOG.info("-> Nexus daily reqs left: %s", nexus.daily_remaining)
    if results["failures"]:
        report = os.path.join(script_dir, f"download_failures_{target}.csv")
        try:
            with open(report, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["archive", "reason"])
                w.writerows(results["failures"])
            LOG.info("-> Wrote failure report: %s", report)
        except OSError:
            pass
    LOG.info("========================================================")
    sys.exit(1 if results["failed"] else 0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        LOG.info("\n[ABORT] Interrupted by user.")
        sys.exit(130)
