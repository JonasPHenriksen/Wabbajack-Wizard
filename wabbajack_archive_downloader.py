#!/usr/bin/env python3
"""
Wabbajack Archive Downloader
============================
A companion to wabbajack_archive_sorter.py. Instead of *sorting* archives you
already have, this script *fetches* the archives a chosen modlist needs and
drops them straight into that list's folder, ready for Wabbajack to verify and
install.

It reads the same .wabbajack manifests as the sorter and supports the download
sources that appear in them:

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
  * xxHash64 verification (matching Wabbajack) runs if the optional `xxhash`
    package is installed; otherwise a size check is used.
  * Downloads stream to a .part file and resume when the server allows it.

Changes from the original (three edits):
  1. WabbajackCDN archives now go through wabbajack_cdn.py, which speaks the
     definition.json.gz + parts/N protocol. Previously they were treated as
     plain HTTP URLs, which yields a ~1 KB CDN error page instead of the file.
  2. An archive that fails size/hash verification is deleted and counted as a
     failure, instead of being kept with a .meta sidecar written beside it.
  3. wabbajack_cdn.py must sit next to this script; a clear error is logged per
     affected archive if it is missing.
  4. The WJ_MANIFEST_DIR environment variable, when set to a real directory,
     overrides where .wabbajack files are looked for. Logs, nexus_api_key.txt
     and the CSV reports still live beside this script.
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
import subprocess
import http.client
import webbrowser
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from collections import defaultdict

# Wabbajack CDN multi-part support. Files on authored-files.wabbajack.org are
# served as a definition plus numbered parts; a plain GET on the URL returns a
# ~1 KB error page, so these must not go through stream_download().
try:
    import wabbajack_cdn as _CDN
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
# Kept deliberately identical to wabbajack_archive_sorter.py so the two tools
# agree on where archives live.
ARCHIVE_ROOT = "../Mod Archive"

# A relative ARCHIVE_ROOT is resolved against THIS SCRIPT's folder, never the
# current working directory, so the tree survives a different drive letter or a
# UNC path no matter where the script is launched from. e.g. with the scripts in
# Z:\Wabbajack_Tools, r"..\Mod Archive" resolves to Z:\Mod Archive.
def _looks_absolute(_p):
    return (os.path.isabs(_p) or _p.startswith("\\\\") or _p.startswith("//")
            or (len(_p) > 1 and _p[1] == ":"))

if not _looks_absolute(ARCHIVE_ROOT):
    ARCHIVE_ROOT = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ARCHIVE_ROOT))

GAME_FOLDERS = {
    "SkyrimSpecialEdition": "Skyrim",
    "Skyrim":               "Skyrim",
    # Maps a GameType onto an existing pool folder whose name is not the
    # GameType itself. Without this a Dark Souls 3 list would create a fresh
    # "DarkSouls3" folder and orphan the "DARK SOULS 3" one already on the
    # share. Inert if the GameType turns out to be spelled differently.
    "DarkSouls3":           "DARK SOULS 3",
}

GAME_DISPLAY = {
    "SkyrimSpecialEdition":  "Skyrim Special Edition",
    "Skyrim":                "Skyrim (Legendary Edition)",
    "Fallout4":              "Fallout 4",
    "FalloutNewVegas":       "Fallout: New Vegas",
    "Fallout3":              "Fallout 3",
    "Oblivion":              "Oblivion",
    "Morrowind":             "Morrowind",
    "StardewValley":         "Stardew Valley",
    "Enderal":               "Enderal",
    "EnderalSpecialEdition": "Enderal Special Edition",
    "BaldursGate3":          "Baldur's Gate 3",
    "Cyberpunk2077":         "Cyberpunk 2077",
    "Witcher3":              "The Witcher 3",
    "DarkSouls3":            "Dark Souls III",
}

# Pool sub-folders that exist alongside the list folders (mirrors the sorter).
POOL_FOLDERS = ("Master", "Shared")

# Folders directly under the game root that belong to a DIFFERENT installer and
# must be left completely alone: not indexed, not counted as "already present",
# never staged or relocated. Nolvus ships its own installer and its own archive
# layout, so its files are none of our business. Matched case-insensitively.
EXCLUDE_POOL_DIRS = {"nolvus"}

# Where the .wabbajack manifests live, relative to this script, when
# WJ_MANIFEST_DIR is not set. Falls back to the script folder if absent, so
# running this script by hand finds the same manifests wj_autoupdate.py uses.
MANIFEST_SUBDIR = "modlists"

# Hash verification is the whole point of downloading a pinned revision. When
# the optional xxhash package is absent this script can only compare sizes,
# which passes a file that is the right length but the wrong content. Rather
# than quietly downgrading (the same command gives you different guarantees
# depending on which Python picked it up), stop and say so. --allow-size-only
# is the deliberate opt-out.
REQUIRE_HASH = True

# Nexus API tuning -----------------------------------------------------
NEXUS_API_BASE   = "https://api.nexusmods.com/v1"
APP_NAME         = "WJArchiveDownloader"
APP_VERSION      = "1.1"
REQUEST_PAUSE_S  = 0.30          # polite gap between Nexus API calls
DOWNLOAD_CHUNK   = 1 << 20       # 1 MiB streaming chunks
NEXUS_DAILY_FLOOR = 5            # stop minting links when daily allowance hits this
DOWNLOAD_RETRIES = 4             # attempts per file when the stream drops mid-transfer

# Many modlists pin a file to the *target* game's Nexus domain even though the
# file actually lives on a parent game's page. The classic case: an Enderal
# Special Edition list whose files are uploaded to skyrimspecialedition. When a
# download_link request 404s under the manifest's stated domain, we retry the
# same mod/file under these fallbacks (in order) before giving up. Keys and
# values are lowercase Nexus domain slugs.
NEXUS_DOMAIN_FALLBACKS = {
    "enderalspecialedition": ["skyrimspecialedition", "skyrim"],
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
NXM_CLICK_TIMEOUT = 180          # seconds to wait per file for a click

# Assist mode opens one download page per file. Over a 300-file list that
# stacks 300 tabs in whatever browser you were already using, so the tab is
# closed again once its click has been captured. Set False to leave them open.
# Assist mode drives its OWN Firefox window over Marionette, Firefox's built-in
# automation protocol, using a profile of its own. One window, one tab,
# re-navigated for each file, so nothing piles up in the browser you are
# actually using and no focus is taken from it. "default" uses your normal
# browser instead, which means one tab per file. "auto" prefers the dedicated
# window and falls back if Firefox cannot be found or Marionette does not answer.
ASSIST_BROWSER = "auto"          # "auto" | "firefox" | "default"
ASSIST_PROFILE_DIR = ".assist-profile"   # persists between runs: holds the login
ASSIST_MARIONETTE_PORT = 2830

# Only relevant to the "default" fallback, where a tab really is opened per
# file. The dedicated window re-uses one tab and does not need this.
CLOSE_TAB_AFTER_CLICK = False
BROWSER_PROCESSES = ("firefox.exe", "chrome.exe", "msedge.exe", "brave.exe",
                     "opera.exe", "vivaldi.exe", "chromium.exe", "iexplore.exe")
TAB_SETTLE_S = 0.7               # let the new tab take focus before noting it
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
            record["game"] = str(data["GameType"])

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


def state_type(state):
    t = (state or {}).get("$type", "")
    if t.startswith("NexusDownloader"):           return "nexus"
    if t.startswith("HttpDownloader"):            return "http"
    if t.startswith("WabbajackCDNDownloader"):    return "wabbajackcdn"
    if t.startswith("MediaFireDownloader"):       return "mediafire"
    if t.startswith("GameFileSourceDownloader"):  return "gamefile"
    if t.startswith("GoogleDriveDownloader"):     return "googledrive"
    if t.startswith("ManualDownloader"):          return "manual"
    return "unknown"


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
    add(primary)
    for d in NEXUS_DOMAIN_FALLBACKS.get(primary, []):
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


def index_existing(game_base):
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

    Folders listed in EXCLUDE_POOL_DIRS (e.g. Nolvus) are skipped entirely, so
    another installer's archives are neither claimed nor disturbed."""
    cache_key = os.path.normcase(os.path.abspath(game_base))
    if cache_key in _EXISTING_CACHE:
        return _EXISTING_CACHE[cache_key]

    index = {}
    locations = [game_base] + [os.path.join(game_base, p) for p in POOL_FOLDERS]
    skipped = []
    try:
        for entry in os.listdir(game_base):
            full = os.path.join(game_base, entry)
            if not os.path.isdir(full):
                continue
            if entry.lower() in EXCLUDE_POOL_DIRS:
                skipped.append(entry)
                continue
            locations.append(full)
    except OSError:
        pass
    if skipped:
        LOG.info("  [SKIP] Ignoring foreign installer folder(s): %s",
                 ", ".join(sorted(skipped)))

    seen = set()
    for loc in locations:
        key = os.path.normcase(os.path.abspath(loc))
        if key in seen or not os.path.isdir(loc):
            continue
        seen.add(key)
        try:
            for name in os.listdir(loc):
                full = os.path.join(loc, name)
                low = name.lower()
                if low.endswith(".meta") or low.endswith(".part"):
                    continue
                if is_junk_name(name) or not os.path.isfile(full):
                    continue
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = None
                index.setdefault(low, []).append((full, size))
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
        manifest pinned but Nexus has since purged (author updated the mod)."""
        url = f"{NEXUS_API_BASE}/games/{domain}/mods/{mod_id}/files.json"
        payload, err = self._get_json(url)
        if err is not None:
            code = getattr(err, "code", None)
            if code in (404, 410):
                return None, "notfound"
            return None, f"error:{err}"
        files = payload.get("files") if isinstance(payload, dict) else None
        return (files if isinstance(files, list) else []), "ok"

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
    LOG.info("[OK] Registered this script as the nxm:// handler.")
    if prev:
        LOG.info("     Previous handler saved; --unregister-nxm restores it.")
    return True


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
            # No prior handler recorded -> remove our command key.
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, cmd_key)
            except OSError:
                pass
            LOG.info("[OK] Removed this script's nxm:// handler "
                     "(no previous handler was recorded; re-run your mod manager "
                     "to re-register if needed).")
    except OSError as e:
        LOG.error("[ERROR] Could not restore nxm handler: %s", e)
        return False
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
    """The domain whose website actually hosts this mod's page.

    Asks the API instead of trusting the manifest. Meta gameName and
    State.GameName disagree often enough to matter - this list records
    "skyrimse" in Meta and "SkyrimSpecialEdition" in State, and only the second
    is a real site slug - and nothing in the manifest says which is right.

    The domain that worked last time is tried first, so a list normally costs
    one API call per mod. Returns (domain, note)."""
    primary = (nexus_domain(archive) or "").lower()
    mod_id, _fid = nexus_ids(archive)
    order = []
    if learned.get(primary):
        order.append(learned[primary])
    for d in candidate_domains(archive):
        cand = nexus_url_domain(d)
        if cand not in order:
            order.append(cand)
    for cand in order:
        if not mod_id:
            break
        _files, status = nexus.mod_files(cand, mod_id)
        time.sleep(REQUEST_PAUSE_S)
        if status == "ok":
            note = None
            if cand != primary and learned.get(primary) != cand:
                note = ("manifest records domain '%s'; the site uses '%s'"
                        % (primary, cand))
            learned[primary] = cand
            return cand, note
    return nexus_url_domain(primary), ("no Nexus domain served mod %s; this "
                                       "page link may 404" % mod_id)


def nexus_file_page(domain, mod_id, file_id):
    """The mod-manager download page; clicking its button fires the nxm link."""
    return (f"https://www.nexusmods.com/{domain}/mods/{mod_id}"
            f"?tab=files&file_id={file_id}&nmm=1")


# ----------------------------------------------------------------------
# A Firefox window of our own, driven over Marionette
# ----------------------------------------------------------------------
def firefox_candidates():
    """Likely firefox.exe locations, built from the environment rather than
    hard-coded drive paths."""
    out = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(var)
        if root:
            out.append(os.path.join(root, "Mozilla Firefox", "firefox.exe"))
    return [p for p in out if os.path.isfile(p)]


# Prefs seeded into our own profile. The protocol-handler three matter most:
# they let Firefox hand an nxm:// link straight to the registered OS handler
# instead of raising a "choose an application" dialog for every single file.
# That dialog is also what defeated closing tabs with a keystroke - at the
# moment the click landed, the focused window was the dialog, not the page.
ASSIST_PREFS = (
    ("network.protocol-handler.external.nxm", "true"),
    ("network.protocol-handler.warn-external.nxm", "false"),
    ("network.protocol-handler.expose.nxm", "false"),
    ("browser.shell.checkDefaultBrowser", "false"),
    ("browser.aboutwelcome.enabled", "false"),
    ("datareporting.policy.dataSubmissionEnabled", "false"),
    ("browser.tabs.warnOnClose", "false"),
    ("browser.sessionstore.resume_from_crash", "false"),
)


class AssistBrowser:
    """A Firefox instance this script owns and steers.

    Marionette is Firefox's own automation protocol - length-prefixed JSON over
    a TCP socket - so this needs nothing outside the standard library and no
    driver binary. One tab is re-navigated for each file in turn, which is what
    stops 300 download pages becoming 300 tabs.

    The profile persists between runs deliberately: it is where the Nexus login
    lives, and signing in every session would be worse than the tab spam."""

    def __init__(self, script_dir):
        self.script_dir = script_dir
        self.profile = os.path.join(script_dir, ASSIST_PROFILE_DIR)
        self.proc = None
        self.sock = None
        self.counter = 0
        self.port = ASSIST_MARIONETTE_PORT
        self.fresh = not os.path.isdir(self.profile)

    # -- wire protocol -------------------------------------------------
    def _recv(self):
        buf = b""
        while b":" not in buf:
            ch = self.sock.recv(1)
            if not ch:
                raise IOError("marionette closed the connection")
            buf += ch
        want = int(buf.split(b":")[0])
        data = b""
        while len(data) < want:
            chunk = self.sock.recv(want - len(data))
            if not chunk:
                raise IOError("marionette closed the connection")
            data += chunk
        return json.loads(data.decode("utf-8"))

    def _cmd(self, name, params=None):
        self.counter += 1
        body = json.dumps([0, self.counter, name, params or {}]).encode("utf-8")
        self.sock.sendall(str(len(body)).encode("ascii") + b":" + body)
        return self._recv()

    # -- lifecycle -----------------------------------------------------
    def start(self):
        exes = firefox_candidates()
        if not exes:
            LOG.info("  [INFO] Firefox not found; using your default browser.")
            return False
        try:
            os.makedirs(self.profile, exist_ok=True)
            lines = ['user_pref("%s", %s);' % kv for kv in ASSIST_PREFS]
            lines.append('user_pref("marionette.port", %d);' % self.port)
            with open(os.path.join(self.profile, "user.js"), "w",
                      encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError as e:
            LOG.warning("  [WARN] Cannot prepare the assist profile (%s); "
                        "using your default browser.", e)
            return False

        env = dict(os.environ, MOZ_MARIONETTE_PORT=str(self.port))
        try:
            self.proc = subprocess.Popen(
                [exes[0], "-marionette", "-no-remote", "-profile", self.profile,
                 "-new-window", "about:blank"],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            LOG.warning("  [WARN] Could not start Firefox (%s); using your "
                        "default browser.", e)
            return False

        for _ in range(45):
            if self.proc.poll() is not None:
                LOG.warning("  [WARN] The assist Firefox exited at once; using "
                            "your default browser.")
                return False
            try:
                self.sock = socket.create_connection(("127.0.0.1", self.port),
                                                     timeout=3)
                break
            except OSError:
                time.sleep(1)
        if not self.sock:
            LOG.warning("  [WARN] Marionette did not answer on port %d; using "
                        "your default browser.", self.port)
            self.stop()
            return False
        try:
            self.sock.settimeout(120)
            self._recv()                       # handshake
            self._cmd("WebDriver:NewSession", {})
        except (IOError, OSError, ValueError) as e:
            LOG.warning("  [WARN] Marionette handshake failed (%s); using your "
                        "default browser.", e)
            self.stop()
            return False
        return True

    def navigate(self, url):
        """Point the one tab at `url`. False if the browser has gone away."""
        if not self.sock:
            return False
        try:
            self._cmd("WebDriver:Navigate", {"url": url})
            return True
        except (IOError, OSError, ValueError) as e:
            LOG.warning("  [WARN] Lost the assist browser (%s); falling back "
                        "to your default browser.", e)
            self.sock = None
            return False

    def stop(self):
        if self.sock:
            try:
                self._cmd("Marionette:Quit", {})
            except Exception:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None


def open_assist_browser(script_dir):
    """An AssistBrowser if one can be had, else None (use the default browser)."""
    if ASSIST_BROWSER == "default":
        return None
    browser = AssistBrowser(script_dir)
    if browser.start():
        return browser
    return None


# ----------------------------------------------------------------------
# Closing the tab we opened (Windows)
# ----------------------------------------------------------------------
_WINKEY = None


def _winkey():
    """ctypes plumbing for SendInput, built once. None where unavailable."""
    global _WINKEY
    if _WINKEY is not None:
        return _WINKEY or None
    if os.name != "nt":
        _WINKEY = False
        return None
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u32.GetForegroundWindow.restype = wintypes.HWND
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)

        # MOUSEINPUT is the largest member of the real INPUT union. Declaring
        # only KEYBDINPUT would make sizeof(INPUT) too small and SendInput
        # would reject every call.
        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        _WINKEY = (ctypes, wintypes, u32, k32, KEYBDINPUT, INPUT)
        return _WINKEY
    except Exception as e:
        LOG.debug("Keyboard plumbing unavailable (%s); tabs will stay open.", e)
        _WINKEY = False
        return None


def foreground_browser_window():
    """(hwnd, exe_name, window_title) when a browser has focus, else Nones."""
    w = _winkey()
    if not w:
        return None, None, ""
    ctypes, wintypes, u32, k32, _KI, _IN = w
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return None, None, ""
    pid = wintypes.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = k32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFO
    if not handle:
        return None, None, ""
    buf = ctypes.create_unicode_buffer(1024)
    size = wintypes.DWORD(1024)
    ok = k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
    k32.CloseHandle(handle)
    if not ok:
        return None, None, ""
    name = os.path.basename(buf.value).lower()
    if name not in BROWSER_PROCESSES:
        return None, None, ""
    title = ctypes.create_unicode_buffer(512)
    u32.GetWindowTextW(hwnd, title, 512)
    return hwnd, name, title.value or ""


def close_browser_tab(expect_hwnd=None):
    """Send Ctrl+W to close the download page we opened.

    Guarded twice, because closing a tab cannot be undone and this runs
    hundreds of times: the focused window must belong to a browser, and it
    must still be the same window the page was opened in. If you have switched
    to another window - or another browser window - this does nothing and
    leaves the tab alone. The one case it cannot detect is you switching to a
    different tab of the same window before the click is captured."""
    if not CLOSE_TAB_AFTER_CLICK:
        return False
    w = _winkey()
    if not w:
        return False
    ctypes, _wt, u32, _k32, KEYBDINPUT, INPUT = w
    hwnd, _name, title = foreground_browser_window()
    if not hwnd:
        LOG.debug("  Leaving the tab open: the focused window is not a browser.")
        return False
    if expect_hwnd and hwnd != expect_hwnd:
        LOG.debug("  Leaving the tab open: focus moved to another window.")
        return False
    # The tab showing must still be a Nexus page. Matching the window alone is
    # not enough: switching to one of your own tabs in the same window keeps
    # the same hwnd, and Ctrl+W would then close your tab instead of ours.
    if "nexus" not in title.lower():
        LOG.debug("  Leaving the tab open: the active tab is not a Nexus page "
                  "(%s).", title[:60])
        return False
    VK_CONTROL, VK_W, KEYEVENTF_KEYUP = 0x11, 0x57, 0x0002
    seq = []
    for vk, flags in ((VK_CONTROL, 0), (VK_W, 0),
                      (VK_W, KEYEVENTF_KEYUP), (VK_CONTROL, KEYEVENTF_KEYUP)):
        item = INPUT(type=1)
        item.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0,
                             dwExtraInfo=None)
        seq.append(item)
    arr = (INPUT * len(seq))(*seq)
    return u32.SendInput(len(seq), arr, ctypes.sizeof(INPUT)) == len(seq)


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
def stream_download(url, dest, expected_size=None, retries=DOWNLOAD_RETRIES):
    """Download with resume-aware retries. Each attempt resumes from the .part
    file the previous attempt left behind, so a dropped connection on a large
    file doesn't restart from zero. A clean HTTP error (e.g. 403/404) is not
    retried; only transient connection/timeout/write failures are."""
    attempt = 0
    while True:
        attempt += 1
        status = _stream_download_once(url, dest, expected_size)
        if status is True:
            return True
        if status == "fatal" or attempt > retries:
            return False
        wait = min(30, 3 * attempt)
        LOG.warning("  [RETRY] %s: transient failure, retrying in %ds (%d/%d)...",
                    os.path.basename(dest), wait, attempt, retries)
        time.sleep(wait)


def _stream_download_once(url, dest, expected_size=None):
    """One download attempt. Returns True on success, 'fatal' on an
    unrecoverable HTTP error, or False on a transient failure worth retrying."""
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
        resp = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:
        if e.code == 416:
            try:
                os.remove(part)
            except OSError:
                pass
            return _stream_download_once(url, dest, expected_size)
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
                    if now - last_print > 0.5:
                        last_print = now
                        if total:
                            pct = downloaded * 100 / total
                            sys.stdout.write(
                                f"\r    {pct:5.1f}%  {human_bytes(downloaded)} / {human_bytes(total)}   ")
                        else:
                            sys.stdout.write(f"\r    {human_bytes(downloaded)}   ")
                        sys.stdout.flush()
        except (OSError, urllib.error.URLError, TimeoutError,
                http.client.IncompleteRead, http.client.HTTPException) as e:
            sys.stdout.write("\n")
            LOG.warning("  [WARN] Transfer interrupted for %s: %s (will resume)",
                        os.path.basename(dest), e)
            return False

    sys.stdout.write("\r" + " " * 60 + "\r")
    sys.stdout.flush()

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


def download_one(url, archive, dest_dir, results):
    """Download a resolved URL into dest_dir, verify, write .meta. Update results."""
    name = archive["name"]
    dest = os.path.join(dest_dir, name)

    # WabbajackCDN URLs are directory prefixes, not files.
    if _HAVE_CDN and (archive.get("_cdn") or _CDN.is_cdn_url(url)):
        os.makedirs(dest_dir, exist_ok=True)
        if not _CDN.download(url, dest, expected_size=archive["size"],
                             expected_hash=archive["hash"]):
            results["failed"] += 1
            results["failures"].append((name, "CDN multi-part download failed"))
            return False
        write_meta(dest, archive["meta_text"])
        results["done"] += 1
        return True

    if _HAVE_CDN is False and isinstance(url, str) and (
            "wabbajack.org" in url or "b-cdn.net" in url):
        LOG.error("  [SKIP] %s needs wabbajack_cdn.py, which is not next to "
                  "this script.", name)
        results["failed"] += 1
        results["failures"].append((name, "wabbajack_cdn.py missing"))
        return False

    if not stream_download(url, dest, expected_size=archive["size"]):
        results["failed"] += 1
        results["failures"].append((name, "download failed"))
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
        results["failed"] += 1
        results["failures"].append((name, f"verify: {reason}"))
        return False
    write_meta(dest, archive["meta_text"])
    results["done"] += 1
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
def download_direct(items, dest_dir, results, limit=0):
    """Non-Nexus sources (http / wabbajackcdn / mediafire). No account needed."""
    for kind, a in items:
        if limit and results["done"] >= limit:
            LOG.info("  [STOP] Reached --limit; stopping.")
            return
        LOG.info("[%s] %s  (%s)", kind, a["name"],
                 human_bytes(a["size"]) if a["size"] else "size unknown")
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
            results["failed"] += 1
            results["failures"].append((a["name"], str(e)))
            continue
        if not url:
            LOG.error("  [SKIP] %s: no usable download URL.", a["name"])
            results["failed"] += 1
            results["failures"].append((a["name"], "no URL"))
            continue
        # The manifest's own State type is the authoritative signal that this
        # is a multi-part CDN file, more reliable than matching the hostname.
        a["_cdn"] = (kind == "wabbajackcdn")
        download_one(url, a, dest_dir, results)


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
            results["failed"] += 1
            results["failures"].append((a["name"], str(e)))
            continue
        if note:
            LOG.info("  [NOTE] %s: %s", a["name"], note)
        download_one(url, a, dest_dir, results)


def _resolve_and_download_nxm(link, nexus, by_ids, dest_dir, results, fetched):
    """Given a parsed nxm dict, resolve via the API and download the matching archive."""
    a = by_ids.get((link["mod_id"], link["file_id"]))
    if a is None:
        LOG.info("  [SKIP] nxm for mod %s file %s is not a pending file for this list.",
                 link["mod_id"], link["file_id"])
        return
    if (link["mod_id"], link["file_id"]) in fetched:
        return
    if not (link["key"] and link["expires"]):
        LOG.error("  [SKIP] %s: nxm link has no key/expires.", a["name"])
        results["failed"] += 1
        results["failures"].append((a["name"], "nxm missing key/expires"))
        return
    LOG.info("[nexus] %s  (%s)", a["name"],
             human_bytes(a["size"]) if a["size"] else "size unknown")
    try:
        # The click pins this exact file id, so we don't substitute file ids
        # here; we still let the domain fall back (the nxm netloc is sometimes
        # the target game rather than the file's host game).
        a_with_domain = dict(a)
        a_with_domain["meta"] = dict(a.get("meta") or {})
        a_with_domain["meta"]["gamename"] = link["domain"]
        url, note = nexus.resolve_link(
            a_with_domain, key=link["key"], expires=link["expires"],
            allow_fileid_fallback=False)
    except RuntimeError as e:
        LOG.error("  [SKIP] %s: %s", a["name"], e)
        results["failed"] += 1
        results["failures"].append((a["name"], str(e)))
        return
    if note:
        LOG.info("  [NOTE] %s: %s", a["name"], note)
    if download_one(url, a, dest_dir, results):
        fetched.add((link["mod_id"], link["file_id"]))


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


def download_nexus_assist(items, nexus, dest_dir, results, script_dir, args):
    """Interactive free-account capture: open each page, catch the nxm click."""
    if not _HAVE_WINREG and platform.system() == "Windows":
        LOG.error("[ERROR] winreg unavailable; cannot register the nxm handler.")
        return
    by_ids = {nexus_ids(a): a for a in items}
    fetched = set()
    learned_domains = {}

    # Make sure we are the nxm handler (so clicks reach us).
    if platform.system() == "Windows":
        if not register_nxm(script_dir):
            LOG.error("[ERROR] Could not register the nxm handler; aborting assist mode.")
            return
    else:
        LOG.warning("[WARN] Not on Windows: nxm registration skipped. Clicks will only be "
                    "captured if something else forwards them to this listener.")

    browser = open_assist_browser(script_dir)
    listener = NxmListener(script_dir)
    LOG.info("Capture listener on %s:%d. For each file, your browser opens the Nexus "
             "download page - click the mod-manager download button.", NXM_LISTEN_HOST, listener.port)
    LOG.info("(The Nexus site shows a short timer for free downloads; wait for it, then click.)")
    if browser:
        LOG.info("A separate Firefox window has opened, with its own profile. It "
                 "re-uses one tab for every file, so nothing accumulates in the "
                 "browser you were using and it will not steal focus from it.")
        if browser.fresh:
            LOG.info("")
            LOG.info("That profile is new, so it is not signed in to Nexus yet - "
                     "and free downloads need a signed-in session. Signing in "
                     "once is enough; the profile is kept between runs.")
            browser.navigate("https://users.nexusmods.com/auth/sign_in")
            try:
                input("  Sign in in that window, then press Enter to begin: ")
            except (EOFError, KeyboardInterrupt):
                LOG.info("  [ABORT] Nothing downloaded.")
                browser.stop()
                listener.close()
                if platform.system() == "Windows":
                    unregister_nxm(script_dir)
                return
    if CLOSE_TAB_AFTER_CLICK:
        LOG.info("Each page is closed again once its click is captured, so the "
                 "tabs do not stack up. Stay in the browser window while "
                 "clicking; if you switch away, the tab is left open rather "
                 "than risking closing something of yours.")
    try:
        pending = list(items)
        for idx, a in enumerate(pending, 1):
            if args.limit and results["done"] >= args.limit:
                LOG.info("  [STOP] Reached --limit; stopping.")
                break
            ids = nexus_ids(a)
            if ids in fetched:
                continue
            if not (nexus_domain(a) and ids[0] and ids[1]):
                LOG.error("  [SKIP] %s: incomplete Nexus identifiers.", a["name"])
                results["failed"] += 1
                continue
            if (nexus.daily_remaining is not None
                    and nexus.daily_remaining <= NEXUS_DAILY_FLOOR):
                LOG.error("  [STOP] Nexus daily request allowance nearly "
                          "exhausted (%s left). Stopping; re-run later. "
                          "Everything downloaded so far is kept.",
                          nexus.daily_remaining)
                break
            domain, dom_note = resolve_browser_domain(nexus, a, learned_domains)
            page = nexus_file_page(domain, ids[0], ids[1])
            LOG.info("\n[%d/%d] %s", idx, len(pending), a["name"])
            if dom_note:
                LOG.info("  [NOTE] %s", dom_note)
            LOG.info("  Opening: %s", page)
            tab_hwnd = None
            shown = False
            if browser:
                # Re-navigate the one tab we own. No new tab, no focus grab.
                shown = browser.navigate(page)
                if not shown:
                    browser = None      # it died; fall back for the rest
            if not shown:
                try:
                    webbrowser.open(page)
                    if CLOSE_TAB_AFTER_CLICK:
                        # Note which window the tab landed in, so the Ctrl+W
                        # later can refuse to fire if focus moved elsewhere.
                        time.sleep(TAB_SETTLE_S)
                        tab_hwnd, _n, _t = foreground_browser_window()
                except Exception:
                    LOG.info("  (Could not open a browser; open the URL above "
                             "by hand.)")
            LOG.info("  Waiting up to %ds for your click... (Ctrl+C to stop)", NXM_CLICK_TIMEOUT)
            url = listener.wait(NXM_CLICK_TIMEOUT)
            # Close the page as soon as its click is in, not after the download:
            # the download can take minutes, by which time you have very likely
            # switched windows and the guard would refuse to close anything.
            closed = close_browser_tab(tab_hwnd)
            if not url:
                LOG.warning("  [TIMEOUT] No click captured for %s; moving on.", a["name"])
                results["failures"].append((a["name"], "no nxm click captured"))
                continue
            link = parse_nxm(url)
            if not link:
                LOG.warning("  [WARN] Captured a non-nxm URL; skipping.")
                continue
            if not closed and CLOSE_TAB_AFTER_CLICK:
                LOG.debug("  Tab left open (focus had moved).")
            _resolve_and_download_nxm(link, nexus, by_ids, dest_dir, results, fetched)
    except KeyboardInterrupt:
        LOG.info("\n[ABORT] Capture interrupted by user.")
    finally:
        listener.close()
        if browser:
            browser.stop()
        # Hand the nxm:// handler back to whatever owned it (normally
        # Wabbajack). Without this, every "download with manager" click on
        # Nexus keeps landing on this script until you notice and fix it by
        # hand - including the clicks Wabbajack itself is waiting for.
        if platform.system() == "Windows":
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
    parser.add_argument("--allow-size-only", action="store_true",
                        help="Proceed even though 'xxhash' is missing and files "
                             "can only be checked by size. Off by default: a "
                             "silent downgrade to size-only verification is how "
                             "a corrupt archive survives to install time.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose console output.")
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))

    # --- Handler-invocation entry point: forward the URL and exit immediately.
    if args.nxm:
        forward_nxm(args.nxm, script_dir)
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
            unsupported.append((kind, a["name"]))

    if args.skip_nexus and nexus_items:
        LOG.info("[INFO] --skip-nexus: ignoring %d Nexus file(s).", len(nexus_items))
        nexus_items = []

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
