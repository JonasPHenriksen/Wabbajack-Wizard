#!/usr/bin/env python3
"""
Wabbajack Archive Sorter
========================
Stages the downloaded archives for a chosen Wabbajack modlist by relocating
files into:

  * a per-list folder            -> files needed by exactly one present list
  * the Shared pool              -> files needed by two or more present lists
  * the Master pool              -> files needed by none of the present lists

In every run the pools are first rebuilt from scratch based on demand (the
automatic cleanup/reset pass), and then every archive the chosen list needs is
gathered into that list's folder (the staging pass).

This version adds:
  * an automatic cleanup/reset pass that runs before you pick a list, so staging
    always starts from a freshly-sorted state (reset is no longer a menu choice).
  * log retention: only the previous and current run's log files are kept.
  * a GAME prompt: you pick a game first, and only modlists for that game are
    considered, so the disk scan and demand calculation stay relevant.
  * game-aware pools: Master/Shared/list folders live under a per-game root, so
    files are never mis-classified just because a *different* game's list does
    not need them.
  * size-checked conflict handling: when a move would collide with an existing
    file, both copies are checked against the manifest's expected Size. The copy
    that matches the manifest keeps the destination; any copy that does NOT match
    is moved into Master instead of being silently overwritten. (Size is a cheap,
    dependency-free proxy: it reliably catches truncated/partial/wrong-version
    files; Wabbajack still hash-verifies everything on its end afterwards.)
  * structured logging to console + a timestamped log file
  * a --dry-run preview that performs no moves
  * duplicate-file detection across all search locations
  * a "required but missing on disk" report for the target list
  * confirmation prompts before any destructive operation
  * Windows-safe path comparison and Unicode-safe console output
  * an explicit, non-destructive failure on invalid menu input

Changes from the original v2 (two edits, both in this file):
  1. build_disk_index() now skips "*.part" as well as "*.meta". A failed
     download leaves a .part file behind on purpose so the next attempt can
     resume; indexing it would park it in Master and lose that progress.
  2. main() adds the companion scripts and their log/state files to the
     protected set, so they can never be relocated even if the script folder
     and a pool folder turn out to be the same directory.
  3. The WJ_MANIFEST_DIR environment variable, when set to a real directory,
     overrides where .wabbajack files are looked for, so this script can live
     apart from the manifests. Log files still go beside this script.
  4. A file parked under a '~N' suffix (two builds, one name) is indexed under
     the name the manifest uses and gets that name back when it is staged.
     Before this, a parked build was invisible to demand: never staged, and
     reclaimable by a cleanup that saw a name nobody asked for.
  5. Scope is the POOL FOLDER, not the GameType string. Skyrim and Skyrim
     Special Edition share one folder here, so both games' lists decide what
     that folder's Master may hold.
  6. An in-scope manifest that lists no archives at all stops the run, like an
     unreadable one does: its files would all look unwanted.
"""

import os
import sys
import json
import shutil
import zipfile
import filecmp
import logging
import argparse
from datetime import datetime
from collections import defaultdict

# ============================ CONFIGURATION ============================
# ---------------------------------------------------------------------
# Layout settings come from wj_config.py, which both this script and
# wabbajack_archive_sorter_v2.py import. They used to be duplicated here and
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
# Includes CFG.UNMANAGED_FOLDER: a modlist must not be able to claim that
# name, and its contents must never be pulled into the pools.
EXCLUDE_POOL_DIRS = CFG.excluded_dirs()
MANIFEST_SUBDIR = CFG.MANIFEST_SUBDIR

# Shared with wj_autoupdate.py: one archive-moving job at a time. Skipped when
# WJ_LOCK_HELD is set, which the parent sets for the children it launches.
LOCK_FILE = ".wj_autoupdate.lock"
# =======================================================================

# These three are resolved to the per-game paths once the game is chosen.
MASTER_ARCHIVE_DIR = None
OUTPUT_BASE_DIR    = None
SHARED_DIR         = None

LOG = logging.getLogger("wj_sorter")

# Folder names that must not be used as modlist names (they are reserved pools).
RESERVED_NAMES = set()


# ----------------------------------------------------------------------
# Logging / console setup
# ----------------------------------------------------------------------
def prune_old_logs(log_dir, keep=1):
    """Delete old wj_sorter_*.log files, keeping only the newest `keep` of them.

    Called just before the new run's log file is created, so with keep=1 the
    folder ends up holding exactly the previous run's log plus the current one.
    Timestamped filenames sort chronologically, so a plain name sort works."""
    try:
        logs = sorted(
            f for f in os.listdir(log_dir)
            if f.lower().startswith("wj_sorter_") and f.lower().endswith(".log")
        )
    except OSError:
        return
    for old in logs[:-keep] if keep else logs:
        try:
            os.remove(os.path.join(log_dir, old))
        except OSError:
            pass  # not fatal; a stale log we couldn't delete just lingers


def setup_logging(log_dir, verbose=False):
    """Configure console + file logging. Returns the log file path (or None)."""
    # Make the console tolerant of emoji / non-cp1252 chars on Windows.
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
        # Retain only the previous run's log; the new file below becomes the
        # current one, so at most two wj_sorter_*.log files ever remain.
        prune_old_logs(log_dir, keep=1)
        log_path = os.path.join(log_dir, f"wj_sorter_{datetime.now():%Y%m%d_%H%M%S}.log")
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%H:%M:%S"))
        LOG.addHandler(fh)
    except OSError as e:
        LOG.warning("[WARN] Could not open a log file in %s: %s", log_dir, e)
    return log_path


def banner(title):
    LOG.info("\n=================== %s ===================", title)


def acquire_pool_lock(script_dir):
    """Best-effort exclusive lock over the archive pools.

    This script relocates files while wabbajack_archive_downloader.py may be
    writing them. Serialising the two avoids a move landing on a file that is
    still being written. A filesystem that cannot lock (SMB sometimes cannot)
    is reported and the run continues - the lock is a guard rail, not a
    guarantee."""
    if os.environ.get("WJ_LOCK_HELD"):
        return None
    path = os.path.join(script_dir, LOCK_FILE)
    try:
        fd = open(path, "a+")
    except OSError:
        return None
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
        LOG.error("[TERMINATED] Another archive job (downloader or updater) is "
                  "running. Wait for it to finish, or the two will fight over "
                  "the same files.")
        sys.exit(3)
    except Exception:
        fd.close()
        LOG.warning("[WARN] Could not take the pool lock on this filesystem; "
                    "continuing. Do not run the downloader at the same time.")
        return None
    import atexit

    def _release():
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
    atexit.register(_release)
    return fd


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
    try:
        names = os.listdir(directory)
    except OSError as e:
        LOG.error("[ERROR] Cannot read manifest folder %s: %s", directory, e)
        return []
    return sorted(f for f in names
                  if f.lower().endswith(".wabbajack") and not is_junk_name(f))


def resolve_manifest_dir(script_dir):
    """WJ_MANIFEST_DIR wins when it is a real directory; a bad value is an
    error rather than a silent fallback, because falling back means sorting
    against a different set of modlists than the caller intended."""
    env = os.environ.get("WJ_MANIFEST_DIR")
    if env:
        if os.path.isdir(env):
            return env
        LOG.error("[TERMINATED] WJ_MANIFEST_DIR points at %s, which is not a "
                  "directory. Refusing to guess.", env)
        sys.exit(2)
    sub_dir = os.path.join(script_dir, MANIFEST_SUBDIR)
    if os.path.isdir(sub_dir) and list_manifest_files(sub_dir):
        return sub_dir
    return script_dir


def short(path):
    """A short, human-friendly label for a path."""
    return os.path.basename(path.rstrip("\\/")) or path


# ----------------------------------------------------------------------
# Path helpers (Windows-safe)
# ----------------------------------------------------------------------
def norm(path):
    return os.path.normcase(os.path.abspath(path))


def same_path(a, b):
    return norm(a) == norm(b)


def on_same_volume(a, b):
    da = os.path.splitdrive(os.path.abspath(a))[0].lower()
    db = os.path.splitdrive(os.path.abspath(b))[0].lower()
    return da == db


def unique_path(path):
    """Return `path` if free, else insert ~1, ~2, ... before the extension."""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 1
    while True:
        candidate = f"{root}~{i}{ext}"
        if not os.path.exists(candidate):
            return candidate
        i += 1


# ----------------------------------------------------------------------
# Game helpers
# ----------------------------------------------------------------------
def game_folder(game):
    """Filesystem folder name for a GameType (GameType values are already safe)."""
    return GAME_FOLDERS.get(game, game)


def game_label(game):
    return GAME_DISPLAY.get(game, game)


def resolve_game_dirs(game):
    """Return (output_base, master, shared) for a given GameType."""
    base = os.path.join(ARCHIVE_ROOT, game_folder(game))
    return base, os.path.join(base, "Master"), os.path.join(base, "Shared")


# ----------------------------------------------------------------------
# Size check (Wabbajack records each archive's expected byte Size)
# ----------------------------------------------------------------------
def file_size(path):
    """Return the file's size in bytes, or None if it can't be read."""
    try:
        return os.path.getsize(path)
    except OSError as e:
        LOG.warning("  [WARN] Could not stat %s: %s", short(path), e)
        return None


# ----------------------------------------------------------------------
# Manifest parsing
# ----------------------------------------------------------------------
def parse_manifest(wj_path):
    """Open a .wabbajack archive and return a record describing it:

        {
          "game":   GameType string or None,
          "names":  set of expected download filenames (lower-cased),
          "sizes":  { name_lower: set(expected_byte_size, ...) },
        }

    Returns the record with empty fields on any failure (logged)."""
    record = {"game": None, "names": set(), "sizes": defaultdict(set),
              "readable": True, "gamefile": set()}
    base = os.path.basename(wj_path)
    LOG.info("  [SCAN] %s", base)

    try:
        with zipfile.ZipFile(wj_path, "r") as z:
            members = z.namelist()
            # The manifest is a member literally named "modlist" (no extension),
            # usually at the archive root. Match on basename to be safe.
            target = next((m for m in members if os.path.basename(m).lower() == "modlist"), None)
            if target is None:
                LOG.warning("  [WARN] No 'modlist' manifest found inside %s - skipping.", base)
                record["readable"] = False
                return record

            with z.open(target) as f:
                data = json.load(f)

        if isinstance(data, dict):
            gt = data.get("GameType")
            if gt and not CFG.valid_game_type(gt):
                LOG.error("  [ERROR] %s declares GameType %r, which is not a "
                          "folder name. Treating it as no game.", base, gt)
            elif gt:
                record["game"] = str(gt)

        archives = data.get("Archives") if isinstance(data, dict) else None
        if not isinstance(archives, list):
            LOG.warning("  [WARN] 'Archives' list missing/malformed in %s.", base)
            return record

        for archive in archives:
            if not isinstance(archive, dict):
                continue
            name = archive.get("Name") or archive.get("NameLog")
            if not name:
                continue
            low = str(name).lower()
            record["names"].add(low)
            # GameFile entries are copied out of your game install by
            # Wabbajack; they are never downloaded and are never expected on
            # disk. Counting them as "missing" makes a healthy list look as
            # though hundreds of archives are absent.
            st = archive.get("State")
            if isinstance(st, dict) and str(st.get("$type", "")).startswith(
                    "GameFileSourceDownloader"):
                record["gamefile"].add(low)
            size = archive.get("Size")
            if isinstance(size, int) and size >= 0:
                record["sizes"][low].add(size)

    except zipfile.BadZipFile:
        LOG.error("  [ERROR] %s is not a readable zip/wabbajack file.", base)
        record["readable"] = False
    except json.JSONDecodeError as e:
        LOG.error("  [ERROR] Manifest JSON in %s is corrupt: %s", base, e)
        record["readable"] = False
    except OSError as e:
        LOG.error("  [ERROR] Could not open %s: %s", base, e)
        record["readable"] = False
    except Exception as e:  # last-resort guard so one bad file never aborts the run
        LOG.error("  [ERROR] Unexpected failure reading %s: %s", base, e)
        record["readable"] = False

    if record["names"]:
        LOG.info("  [OK]   %s -> game=%s, %d archive(s) required.",
                 base, record["game"] or "unknown", len(record["names"]))
    else:
        LOG.warning("  [WARN] %s produced 0 archive entries (treated as empty).", base)
    return record


# ----------------------------------------------------------------------
# Disk indexing
# ----------------------------------------------------------------------
def build_disk_index(search_locations, protected_names):
    """Index every real file across the search locations.

    Returns:
        index:      { name_lower: [(abs_src_path, actual_case_name), ...] }
        duplicates: { name_lower: [paths...] }                   (>=2 copies)

    EVERY copy is kept, not just the first one found. A filename does not
    identify content here: several modlists legitimately pin different builds
    of a file with the same generic name - Synthesis.zip exists five times in
    the live pools at five different sizes, one per list. A first-wins index
    made staging hand a list whichever build happened to be scanned first,
    which is then rejected by Wabbajack while the correct build sits untouched
    in another list's folder.

    Keys are canonical names (wj_config.canonical_name): 'Name~1.ext', the
    form park_in_master() gives a second build, is indexed under 'name.ext',
    which is what the manifests ask for. The tuple keeps the real filename.
    """
    index = {}
    duplicates = defaultdict(list)
    seen_dirs = set()

    for loc in search_locations:
        key = norm(loc)
        if key in seen_dirs:
            continue  # same folder listed twice (e.g. overlapping config)
        seen_dirs.add(key)

        if not os.path.isdir(loc):
            LOG.debug("  [skip] Not a directory: %s", loc)
            continue
        try:
            entries = os.listdir(loc)
        except OSError as e:
            LOG.warning("  [WARN] Cannot read %s: %s", loc, e)
            continue

        counted = 0
        for name in entries:
            full = os.path.join(loc, name)
            low = name.lower()

            if low.endswith(".meta"):
                continue  # meta sidecars travel with their parent file
            if low.endswith(".part"):
                # An in-flight (or failed-and-resumable) download from
                # wabbajack_archive_downloader.py. No modlist demands a file by
                # this name, so indexing it would park it in Master and destroy
                # the resume progress. Leave it exactly where it is.
                continue
            if is_junk_name(name):
                continue  # '._x' / '.DS_Store' left by a Mac on the share
            if low in protected_names:
                continue  # never relocate the manifests or this script
            if not os.path.isfile(full):
                continue  # ignore subfolders / symlinked dirs

            counted += 1
            key = CFG.canonical_name(low)
            if key in index:
                if not duplicates[key]:
                    duplicates[key].append(index[key][0][0])
                duplicates[key].append(full)
                index[key].append((full, name))
            else:
                index[key] = [(full, name)]

        LOG.info("  -> Indexed %d file(s) in '%s'", counted, short(loc))

    return index, duplicates


# ----------------------------------------------------------------------
# Plan building
# ----------------------------------------------------------------------
def pick_copy(copies, wanted_sizes):
    """Choose which copy of a name to move, given what the destination wants.

    `copies` is [(path, name)] and `wanted_sizes` the set of byte sizes the
    receiving modlist's manifest states for that name. The copy that matches is
    the one that list can actually install; without this the first copy scanned
    wins, which is arbitrary and frequently wrong when two lists pin different
    builds under one name. Falls back to the first copy when nothing matches or
    nothing is known, leaving the existing size-checked collision handling to
    sort out what it can."""
    if len(copies) == 1 or not wanted_sizes:
        return copies[0]
    for path, name in copies:
        try:
            if os.path.getsize(path) in wanted_sizes:
                return path, name
        except OSError:
            continue
    return copies[0]


def build_plan(target_modlist, file_demands, disk_index, list_sizes=None):
    """Compute the list of intended moves without touching disk.

    Returns a list of dicts: {src, dst, name, reason}.
    """
    plan = []
    list_sizes = list_sizes or {}

    if target_modlist:
        candidates = [low for low in disk_index
                      if target_modlist in file_demands.get(low, [])]
    else:
        candidates = list(disk_index.keys())

    for low in candidates:
        copies = disk_index[low]
        lists = file_demands.get(low, [])

        if target_modlist:
            dest_dir = os.path.join(OUTPUT_BASE_DIR, target_modlist)
            reason = "needed by target"
            wanted = list_sizes.get(target_modlist, {}).get(low)
        else:
            n = len(lists)
            if n >= 2:
                dest_dir, reason = SHARED_DIR, f"shared by {n} lists"
                # Two lists wanting one name at different sizes cannot both be
                # served from Shared, and the duplicate report says so. But a
                # copy that SOME wanting list can use must win over one that
                # none can: taking the first copy found used to carry a junk
                # build into Shared while the right one sat in Master.
                wanted = set()
                for l in lists:
                    wanted |= list_sizes.get(l, {}).get(low, set())
                wanted = wanted or None
            elif n == 1:
                dest_dir, reason = os.path.join(OUTPUT_BASE_DIR, lists[0]), f"only '{lists[0]}'"
                wanted = list_sizes.get(lists[0], {}).get(low)
            else:
                dest_dir, reason = MASTER_ARCHIVE_DIR, "unused -> master"
                wanted = None

        src, name = pick_copy(copies, wanted)
        # A '~N' suffix is storage only. Staging gives the file back the name
        # the manifest uses, so Wabbajack finds it. A copy bound for Master
        # keeps whatever name it has: renaming there would make every rebuild
        # churn the parked duplicates.
        out_name = name if same_path(dest_dir, MASTER_ARCHIVE_DIR) \
            else CFG.canonical_name(name)
        dst = os.path.join(dest_dir, out_name)
        plan.append({"src": src, "dst": dst, "name": out_name, "reason": reason})

    return plan


def build_list_sizes(records_in_scope):
    """{ list_name: { name_lower: set(expected sizes) } }.

    Per-list rather than pooled, so a move can pick the build the receiving
    list actually wants instead of any build that some list somewhere wants."""
    return {rec["name"]: {low: set(sizes)
                          for low, sizes in rec["sizes"].items()}
            for rec in records_in_scope}


def build_expected_sizes(records_in_scope, target_modlist):
    """Map { name_lower: set(expected_byte_size) } of what the manifest(s) expect.

    In TARGET mode only the target list's sizes are authoritative (so we don't
    accept a *different* list's version of a same-named file). In RESET mode we
    take the union across every in-scope list."""
    expected = defaultdict(set)
    for rec in records_in_scope:
        if target_modlist and rec["name"] != target_modlist:
            continue
        for low, sizes in rec["sizes"].items():
            expected[low].update(sizes)
    return expected


def estimate_cross_volume_bytes(moves):
    """Total bytes that would actually be copied (cross-volume moves only)."""
    total = 0
    for m in moves:
        try:
            if not on_same_volume(m["src"], m["dst"]):
                total += os.path.getsize(m["src"])
        except OSError:
            pass
    return total


# ----------------------------------------------------------------------
# Move execution
# ----------------------------------------------------------------------
def _raw_move(src, dst):
    """Move src -> dst, clearing a colliding file first. Raises on failure."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.islink(dst) or os.path.exists(dst):
        if os.path.isdir(dst) and not os.path.islink(dst):
            raise IsADirectoryError(f"Destination is a directory, refusing: {dst}")
        os.remove(dst)
    shutil.move(src, dst)


def move_with_meta(src, dst, counters, dry_run):
    """Move src -> dst and carry its sidecar .meta along. Updates counters."""
    if dry_run:
        return
    _raw_move(src, dst)
    counters["moved"] += 1
    meta_src = src + ".meta"
    if os.path.exists(meta_src):
        try:
            _raw_move(meta_src, dst + ".meta")
            counters["meta"] += 1
        except OSError as e:
            LOG.warning("  [WARN] Moved '%s' but its .meta failed: %s", os.path.basename(src), e)


def park_in_master(path, counters, dry_run, why):
    """Move a file (and its .meta) into Master under a non-colliding name."""
    name = os.path.basename(path)
    LOG.info("  [PARK] '%s' -> Master  (%s)", name, why)
    if dry_run:
        counters["parked"] += 1
        return
    dest = unique_path(os.path.join(MASTER_ARCHIVE_DIR, name))
    try:
        os.makedirs(MASTER_ARCHIVE_DIR, exist_ok=True)
        shutil.move(path, dest)
        counters["parked"] += 1
        meta_src = path + ".meta"
        if os.path.exists(meta_src):
            try:
                shutil.move(meta_src, unique_path(dest + ".meta"))
                counters["meta"] += 1
            except OSError as e:
                LOG.warning("  [WARN] Parked '%s' but its .meta failed: %s", name, e)
    except (OSError, shutil.Error) as e:
        LOG.error("  [ERROR] Could not park '%s' in Master: %s", name, e)
        counters["errors"] += 1


def resolve_collision(src, dst, name, expected, counters, dry_run):
    """Decide what to do when `dst` already holds a file with the same name.

    Strategy (per the manifest's expected Size for this file):
      * the copy whose size matches the manifest keeps the destination;
      * any copy that does NOT match is moved into Master rather than deleted.

    Falls back to a byte-for-byte comparison when no expected size is known
    (the name is not in any in-scope manifest)."""
    want = expected.get(name.lower()) if expected else None
    s_src = file_size(src) if want else None
    s_dst = file_size(dst) if want else None

    if want and s_src is not None and s_dst is not None:
        src_ok = s_src in want
        dst_ok = s_dst in want

        if src_ok and dst_ok:
            # Both copies are the expected size. Keep dst; the incoming copy is
            # redundant -> Master (nothing is destroyed).
            LOG.info("  [CONFLICT] '%s': both copies match the manifest size; keeping "
                     "the one already in place.", name)
            if not same_path(os.path.dirname(src), MASTER_ARCHIVE_DIR):
                park_in_master(src, counters, dry_run, "redundant copy (size matches)")
            return

        if dst_ok and not src_ok:
            # The file already at the destination is the right size.
            LOG.info("  [CONFLICT] '%s': existing copy matches the manifest size, "
                     "incoming does not (%s vs expected %s).",
                     name, s_src, sorted(want))
            park_in_master(src, counters, dry_run, "incoming copy: wrong size")
            return

        if src_ok and not dst_ok:
            # Incoming copy is the right size; move the wrong one aside, then place it.
            LOG.info("  [CONFLICT] '%s': incoming copy matches the manifest size, "
                     "existing does not (%s vs expected %s).",
                     name, s_dst, sorted(want))
            park_in_master(dst, counters, dry_run, "existing copy: wrong size")
            move_with_meta(src, dst, counters, dry_run)
            return

        # Neither matches the expected size -> preserve both, leave the slot empty
        # so Wabbajack will re-download the correct file.
        LOG.warning("  [CONFLICT] '%s': NEITHER copy matches the manifest size "
                    "(%s / %s vs expected %s); parking both in Master "
                    "(re-download required).", name, s_src, s_dst, sorted(want))
        park_in_master(dst, counters, dry_run, "wrong size")
        park_in_master(src, counters, dry_run, "wrong size")
        return

    # ---- Fallback: no expected size for this file ---------------------------
    try:
        identical = filecmp.cmp(src, dst, shallow=False)
    except OSError:
        identical = False

    if identical:
        LOG.info("  [CONFLICT] '%s': no manifest size to check, but the two copies "
                 "are byte-identical; keeping the one already in place.", name)
        if not same_path(os.path.dirname(src), MASTER_ARCHIVE_DIR):
            park_in_master(src, counters, dry_run, "redundant identical copy")
        return

    # Different content and we cannot tell which is right: keep both. Preserve the
    # existing copy in Master, then place the incoming one.
    LOG.warning("  [CONFLICT] '%s': no manifest size to check and copies differ; "
                "preserving the existing copy in Master, placing the incoming one.", name)
    park_in_master(dst, counters, dry_run, "unverified collision (existing copy preserved)")
    move_with_meta(src, dst, counters, dry_run)


def execute_plan(moves, expected, dry_run):
    """Carry out the planned moves. Returns a counters dict."""
    counters = {"moved": 0, "meta": 0, "parked": 0, "errors": 0}

    for m in moves:
        src, dst, name = m["src"], m["dst"], m["name"]
        LOG.info("  [MOVE] '%s' -> %s  (%s)", name, short(os.path.dirname(dst)), m["reason"])

        try:
            if not os.path.exists(src):
                LOG.warning("  [WARN] Source vanished before move: %s", src)
                counters["errors"] += 1
                continue

            # Collision: a *different* file already occupies the destination.
            if (os.path.islink(dst) or os.path.exists(dst)) and not same_path(src, dst):
                resolve_collision(src, dst, name, expected, counters, dry_run)
                continue

            move_with_meta(src, dst, counters, dry_run)

        except (OSError, shutil.Error) as e:
            LOG.error("  [ERROR] Could not move '%s': %s", name, e)
            counters["errors"] += 1

    return counters


# ----------------------------------------------------------------------
# Interactive helpers
# ----------------------------------------------------------------------
def choose_game(games_present, preset=None):
    """Ask which game to sort for. `games_present` is a sorted list of GameType
    strings. Returns the chosen GameType. Auto-selects when only one is present."""
    if preset is not None:
        # Accept a GameType, friendly label, or pool-folder name, case-insensitively.
        for g in games_present:
            if preset.lower() in (g.lower(), game_label(g).lower(), game_folder(g).lower()):
                return g
        LOG.error("[ERROR] --game '%s' is not among the games present: %s",
                  preset, ", ".join(games_present))
        sys.exit(2)

    if len(games_present) == 1:
        only = games_present[0]
        LOG.info("\nOnly one game present: %s - selecting it automatically.", game_label(only))
        return only

    LOG.info("\nWhich game do you want to sort mods for?")
    game_map = {}
    for i, g in enumerate(games_present, 1):
        game_map[str(i)] = g
        LOG.info("  [%d] %s", i, game_label(g))

    for _ in range(3):
        try:
            choice = input("\nSelect a game number: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            LOG.info("\n[ABORT] No selection made.")
            sys.exit(1)
        if choice in game_map:
            return game_map[choice]
        LOG.warning("[WARN] '%s' is not a listed option. Try again.", choice)

    LOG.error("[ERROR] No valid game selected after 3 attempts. Aborting.")
    sys.exit(2)


def resolve_preset(modlists_map, preset):
    """A --list value as a modlist name, or None if it matches nothing.

    Accepts a menu number or a name, case-insensitively."""
    if preset in modlists_map:
        return modlists_map[preset]
    by_name = {nm.lower(): nm for nm in modlists_map.values()}
    return by_name.get(str(preset).lower())


def choose_modlist(modlists_map, preset=None):
    """Resolve the target modlist to stage. Returns a list name.

    A valid modlist selection is now required: the cleanup/reset pass runs
    automatically beforehand, so it is no longer offered here as a choice."""
    if preset is not None:
        name = resolve_preset(modlists_map, preset)
        if name:
            return name
        LOG.error("[ERROR] Preset list selector '%s' is not valid. Known lists: %s",
                  preset, ", ".join(sorted(modlists_map.values())))
        sys.exit(2)

    LOG.info("\nWhich modlist do you want to prepare the install for?")
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

    LOG.error("[ERROR] No valid selection after 3 attempts. Aborting (nothing moved).")
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
# Environment validation
# ----------------------------------------------------------------------
def validate_environment():
    """Make sure the pool directories exist / can be created and are writable."""
    ok = True
    for label, path in (("Master", MASTER_ARCHIVE_DIR),
                        ("Output base", OUTPUT_BASE_DIR),
                        ("Shared", SHARED_DIR)):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            LOG.error("[ERROR] %s path is unreachable (%s): %s", label, path, e)
            ok = False
            continue
        if not os.access(path, os.W_OK):
            LOG.error("[ERROR] %s path is not writable: %s", label, path)
            ok = False
    return ok


def run_stage(stage_label, target_modlist, file_demands, expected_sizes,
              modlists_map, protected, chosen_game, args, gamefile_names=frozenset(),
              list_sizes=None):
    """Scan disk, build, and (unless --dry-run) execute the plan for ONE pass.

    target_modlist is None for the automatic cleanup/reset pass, or a list name
    for the staging pass. Returns a stats dict so main() can print a combined
    summary."""
    stats = {
        "counters": {"moved": 0, "meta": 0, "parked": 0, "errors": 0},
        "already_in_place": 0,
        "missing": [],
        "duplicates": 0,
    }

    # ---- Disk scan ----------------------------------------------------
    banner(f"{stage_label}: DISK SCAN (INDEXING)")
    # EXCLUDE_POOL_DIRS is enforced here as well as by never deriving a list
    # folder from a foreign name: if a modlist ever happened to be called
    # "Nolvus", it still must not drag that installer's archives into our pools.
    list_dirs = [os.path.join(OUTPUT_BASE_DIR, nm) for nm in modlists_map.values()
                 if nm.lower() not in EXCLUDE_POOL_DIRS]
    search_locations = [SHARED_DIR, MASTER_ARCHIVE_DIR] + list_dirs

    disk_index, duplicates = build_disk_index(search_locations, protected)
    stats["duplicates"] = len(duplicates)
    LOG.info("[SUMMARY] %d unique file(s) indexed on disk.", len(disk_index))

    if duplicates:
        LOG.warning("[WARN] %d filename(s) exist in more than one location. The first copy "
                    "found is treated as authoritative; collisions are resolved by size "
                    "during the move:", len(duplicates))
        for low, paths in sorted(duplicates.items()):
            LOG.warning("  * %s", disk_index[low][0][1])
            for p in paths:
                LOG.warning("      - %s", p)

    # ---- Build plan ---------------------------------------------------
    banner(f"{stage_label}: BUILDING PLAN")
    if target_modlist:
        LOG.info("TARGET MODE: staging '%s'.", target_modlist)
    else:
        LOG.info("RESET MODE: rebuilding pools from current demand.")

    plan = build_plan(target_modlist, file_demands, disk_index, list_sizes)
    moves = [m for m in plan if not same_path(m["src"], m["dst"])]
    stats["already_in_place"] = len(plan) - len(moves)

    # Required-but-missing report (only meaningful when staging a target).
    if target_modlist:
        absent = sorted(f for f in file_demands
                        if target_modlist in file_demands[f] and f not in disk_index)
        from_game = [f for f in absent if f in gamefile_names]
        missing = [f for f in absent if f not in gamefile_names]
        stats["missing"] = missing
        stats["from_game"] = len(from_game)
        if from_game:
            LOG.info("%d archive(s) come from your game install (Wabbajack copies "
                     "these itself); they are not expected on disk.", len(from_game))
        if missing:
            LOG.warning("[WARN] '%s' needs %d archive(s) that are NOT on disk anywhere. "
                        "Wabbajack (or the downloader script) will have to fetch these:",
                        target_modlist, len(missing))
            for f in missing:
                LOG.warning("  ? %s", f)

    LOG.info("\nPlan: %d move(s), %d already in place, %d missing.",
             len(moves), stats["already_in_place"], len(stats["missing"]))

    if not moves:
        LOG.info("[DONE] Nothing to relocate in this pass.")
        return stats

    xfer = estimate_cross_volume_bytes(moves)
    if xfer:
        LOG.info("Note: ~%.2f GB will be physically copied across volumes.",
                 xfer / (1024 ** 3))

    # ---- Confirm + execute -------------------------------------------
    if args.dry_run:
        banner(f"{stage_label}: DRY-RUN PREVIEW (NO CHANGES)")
        execute_plan(moves, expected_sizes, dry_run=True)
        return stats

    action = f"stage '{target_modlist}'" if target_modlist else "clean up / rebuild all pools"
    if not confirm(f"\nProceed to {action} ({len(moves)} move(s)) for {game_label(chosen_game)}?",
                   assume_yes=args.yes):
        LOG.info("[ABORT] Cancelled by user. Nothing moved in this pass.")
        return stats

    banner(f"{stage_label}: RELOCATING ARCHIVES")
    stats["counters"] = execute_plan(moves, expected_sizes, dry_run=False)
    return stats


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main():
    global MASTER_ARCHIVE_DIR, OUTPUT_BASE_DIR, SHARED_DIR, RESERVED_NAMES

    parser = argparse.ArgumentParser(description="Stage Wabbajack archives into pools.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show the full plan but move nothing.")
    parser.add_argument("--yes", action="store_true",
                        help="Skip the confirmation prompt (use with care).")
    parser.add_argument("--game", dest="game",
                        help="Pre-select a game (GameType or friendly name) non-interactively.")
    parser.add_argument("--list", dest="preset",
                        help="Pre-select a modlist by name (or menu number).")
    parser.add_argument("--cleanup-only", action="store_true",
                        help="Run only the cleanup/reset pass that rebuilds the "
                             "pools from current demand, then stop without "
                             "staging any list. Used after deleting a modlist, "
                             "so anything the remaining lists still need is "
                             "pulled back out of Master before Master is cleared.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Verbose (debug-level) console output.")
    args = parser.parse_args()

    current_dir = os.path.dirname(os.path.abspath(__file__))
    log_path = setup_logging(current_dir, verbose=args.verbose)

    # Nothing below may run concurrently with a download into the same pools.
    acquire_pool_lock(current_dir)

    # Manifests may live elsewhere (see change 3 in the docstring). Logs stay
    # beside this script regardless.
    manifest_dir = resolve_manifest_dir(current_dir)

    banner("ENVIRONMENT CHECK")
    LOG.info("Archive root:         %s", ARCHIVE_ROOT)
    if log_path:
        LOG.info("Log file:             %s", log_path)
    if args.dry_run:
        LOG.info("Mode:                 DRY-RUN (no files will be moved)")

    wj_files = list_manifest_files(manifest_dir)
    if not wj_files:
        LOG.error("[TERMINATED] No .wabbajack files found in %s", manifest_dir)
        sys.exit(1)
    if manifest_dir != current_dir:
        LOG.info("Manifest dir:         %s", manifest_dir)

    # ---- Step 1: parse every manifest once ---------------------------
    banner("STEP 1: SCANNING MANIFESTS")
    records = []
    empty_lists = []
    for wj_file in wj_files:
        rec = parse_manifest(os.path.join(manifest_dir, wj_file))
        rec["file"] = wj_file
        rec["name"] = os.path.splitext(wj_file)[0]
        records.append(rec)
        if not rec["names"]:
            empty_lists.append(rec["name"])

    # A manifest that failed to READ yields an empty demand set, which is
    # indistinguishable from "this list needs nothing". The cleanup pass would
    # then decide every archive that list needs is unused and park the lot in
    # Master - potentially hundreds of GB of pointless movement, and a broken
    # install. A read failure is never a safe assumption, so stop.
    unreadable = [r["name"] for r in records if not r["readable"]]
    if unreadable:
        LOG.error("[TERMINATED] %d manifest(s) could not be read: %s",
                  len(unreadable), ", ".join(unreadable))
        LOG.error("             Their archives would look unwanted and be moved "
                  "into Master. Re-download or remove these files, then re-run.")
        sys.exit(2)

    if empty_lists:
        LOG.warning("[WARN] %d modlist(s) yielded no archives: %s",
                    len(empty_lists), ", ".join(empty_lists))

    # ---- Step 2: choose the game -------------------------------------
    games_present = sorted({r["game"] for r in records if r["game"]})
    no_game = [r["name"] for r in records if not r["game"]]
    if no_game:
        LOG.warning("[WARN] %d modlist(s) have no GameType and will be ignored: %s",
                    len(no_game), ", ".join(no_game))
    if not games_present:
        LOG.error("[TERMINATED] None of the .wabbajack files declared a GameType.")
        sys.exit(1)

    banner("STEP 2: SELECT GAME")
    chosen_game = choose_game(games_present, preset=args.game)

    # Resolve the per-game pool layout now that we know the game.
    OUTPUT_BASE_DIR, MASTER_ARCHIVE_DIR, SHARED_DIR = resolve_game_dirs(chosen_game)
    # A modlist called "Master", "Shared" or "Unmanaged" would have its folder
    # collide with a pool, so those names are refused outright.
    RESERVED_NAMES = CFG.reserved_list_names()

    LOG.info("\nGame selected:        %s", game_label(chosen_game))
    LOG.info("Master archive path:  %s", MASTER_ARCHIVE_DIR)
    LOG.info("Output base path:     %s", OUTPUT_BASE_DIR)
    LOG.info("Shared folder path:   %s", SHARED_DIR)

    if not validate_environment():
        LOG.error("[TERMINATED] Fix the path problems above and re-run.")
        sys.exit(1)

    # Keep every list that draws from the chosen game's POOL FOLDER. Two
    # GameTypes can share one folder (Skyrim and SkyrimSpecialEdition do), and
    # then both games' lists decide what is wanted there. Scoping by the
    # GameType string alone made the other game's archives look unused.
    in_scope = [r for r in records if CFG.same_pool(r["game"], chosen_game)]
    LOG.info("[SUMMARY] %d of %d modlist(s) draw from the %s pools.",
             len(in_scope), len(records), game_label(chosen_game))
    sharing = sorted({r["game"] for r in in_scope} - {chosen_game})
    if sharing:
        LOG.info("          (%s share this folder, so their lists count too.)",
                 ", ".join(game_label(g) for g in sharing))

    # A manifest that reads fine but lists no archives is as unsafe a basis as
    # one that could not be read: every file that list holds would look
    # unwanted and be moved into Master. No real modlist wants nothing.
    empty_in_scope = [r["name"] for r in in_scope if not r["names"]]
    if empty_in_scope:
        LOG.error("[TERMINATED] %d in-scope manifest(s) list no archives at all: %s",
                  len(empty_in_scope), ", ".join(empty_in_scope))
        LOG.error("             Their archives would look unwanted and be moved "
                  "into Master. Re-download or remove these files, then re-run.")
        sys.exit(2)

    # Reserved-name collision check (only for in-scope lists).
    modlists_map = {}
    for i, rec in enumerate(in_scope):
        if rec["name"].lower() in RESERVED_NAMES:
            LOG.error("[TERMINATED] Modlist '%s' clashes with a reserved pool folder name. "
                      "Rename the .wabbajack file.", rec["name"])
            sys.exit(1)
        modlists_map[str(i + 1)] = rec["name"]

    # Check --list now, before the cleanup pass. Cleanup runs first by design
    # and it MOVES FILES: a mistyped list name used to trigger a full pool
    # reshuffle - pulling the shared archives back out of a list you had just
    # staged - and only report the bad argument afterwards, by which time the
    # damage was done and the run exited non-zero having quietly undone your
    # preparation.
    if args.preset is not None and resolve_preset(modlists_map, args.preset) is None:
        LOG.error("[TERMINATED] --list '%s' is not one of: %s",
                  args.preset, ", ".join(sorted(modlists_map.values())))
        LOG.error("             Nothing has been moved.")
        sys.exit(2)

    # ---- Build demand (independent of which list we end up staging) --
    file_demands = defaultdict(list)
    for rec in in_scope:
        for req in rec["names"]:
            file_demands[req].append(rec["name"])
    LOG.info("[SUMMARY] %d unique archive(s) requested across %d in-scope modlist(s).",
             len(file_demands), len(in_scope))

    protected = {f.lower() for f in wj_files}
    protected.add(os.path.basename(__file__).lower())
    # Also shield the companion scripts and their bookkeeping files, in case the
    # script folder and a pool folder are ever the same directory. Cheap
    # insurance: none of these are ever legitimate archive names.
    _KEEP_EXT = (".py", ".bat", ".sh", ".log", ".csv", ".json",
                 ".bak", ".out", ".md", ".txt", ".lock",
                 ".metadata", ".part", ".rejected", ".wabbajack")
    for _d in {current_dir, manifest_dir}:
        try:
            for _f in os.listdir(_d):
                if os.path.splitext(_f)[1].lower() in _KEEP_EXT:
                    protected.add(_f.lower())
        except OSError:
            pass

    # ---- Step 3: AUTOMATIC cleanup / reset ---------------------------
    # The reset pass redistributes every file into Master / Shared / per-list
    # pools based on current demand. It now runs automatically before you pick a
    # list, so the staging pass below starts from a clean, correctly-sorted state.
    banner("STEP 3: AUTOMATIC CLEANUP / RESET")
    reset_expected = build_expected_sizes(in_scope, None)
    list_sizes = build_list_sizes(in_scope)
    reset_stats = run_stage("CLEANUP", None, file_demands, reset_expected,
                            modlists_map, protected, chosen_game, args,
                            list_sizes=list_sizes)

    if args.cleanup_only:
        c = reset_stats["counters"]
        banner("CLEANUP COMPLETE (no list staged)")
        LOG.info("  -> Files relocated:          %d", c["moved"])
        LOG.info("  -> Conflicts parked->Master: %d", c["parked"])
        LOG.info("  -> Errors:                   %d", c["errors"])
        sys.exit(1 if c["errors"] else 0)

    # ---- Step 4: choose the list to stage ----------------------------
    banner("STEP 4: SELECT MODLIST")
    target_modlist = choose_modlist(modlists_map, preset=args.preset)

    # ---- Step 5: stage the chosen list -------------------------------
    banner(f"STEP 5: STAGE '{target_modlist}'")
    target_expected = build_expected_sizes(in_scope, target_modlist)
    target_rec = next((r for r in in_scope if r["name"] == target_modlist), None)
    stage_stats = run_stage(f"STAGE '{target_modlist}'", target_modlist, file_demands,
                            target_expected, modlists_map, protected, chosen_game, args,
                            gamefile_names=(target_rec or {}).get("gamefile", frozenset()),
                            list_sizes=list_sizes)

    if args.dry_run:
        LOG.info("\n[DRY-RUN COMPLETE] Re-run without --dry-run to apply.")
        return

    # ---- Combined summary --------------------------------------------
    rc = reset_stats["counters"]
    sc = stage_stats["counters"]
    banner("SORTING COMPLETE")
    LOG.info("Cleanup pass:")
    LOG.info("  -> Files relocated:          %d", rc["moved"])
    LOG.info("  -> Conflicts parked->Master: %d", rc["parked"])
    LOG.info("Staging pass ('%s'):", target_modlist)
    LOG.info("  -> Files relocated:          %d", sc["moved"])
    LOG.info("  -> Conflicts parked->Master: %d", sc["parked"])
    LOG.info("  -> Sidecar .meta moved:      %d", rc["meta"] + sc["meta"])
    LOG.info("  -> Already in place:         %d", stage_stats["already_in_place"])
    LOG.info("  -> Missing (must download):  %d", len(stage_stats["missing"]))
    LOG.info("  -> From game install:        %d", stage_stats.get("from_game", 0))
    LOG.info("  -> Duplicate names flagged:  %d", stage_stats["duplicates"])
    total_errors = rc["errors"] + sc["errors"]
    LOG.info("  -> Errors:                   %d", total_errors)
    LOG.info("========================================================")

    sys.exit(1 if total_errors else 0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        LOG.info("\n[ABORT] Interrupted by user.")
        sys.exit(130)
