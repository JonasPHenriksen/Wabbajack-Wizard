#!/usr/bin/env python3
"""
Wabbajack Wizard - the sorter
=============================
Arranges the archives of one game's pool folder by demand, then gathers what
one chosen modlist needs into that list's own folder for Wabbajack:

  * a per-list folder            -> files needed by exactly one present list
  * the Shared pool              -> files needed by two or more present lists
  * the Master pool              -> files needed by none of the present lists

Every run rebuilds the pools from current demand first (the cleanup pass),
then stages the chosen list. --cleanup-only stops after the first pass, which
is what the wizard runs after deleting a modlist.

The rules it moves under:

  * Scope is the POOL FOLDER, not the GameType. Skyrim, Skyrim SE, Skyrim VR
    and Enderal SE share one folder, so all their lists decide what that
    folder's Master may hold.
  * A file is identified by name AND size, never by name alone: several lists
    pin different builds of one filename, and the copy a list can install is
    the one whose size its manifest states. When a move collides with a
    different build, the copy matching the manifest keeps the destination and
    the other is parked in Master under a '~N' suffix rather than overwritten.
  * A '~N' suffix is storage only. A parked build is indexed under the name
    the manifest uses and gets that name back when staged.
  * Identical extra copies of a build are parked in Master during the cleanup
    pass, so the pools settle in one run and the wizard's stage 5c can
    reclaim them. Copies of other sizes stay where they are.
  * A manifest that cannot be read, or lists no archives, stops the run: its
    archives would all look unwanted and be moved into Master.
  * "*.part" files are never indexed or moved; a failed download leaves one
    behind on purpose so the next attempt can resume.
  * Manifests are read through the light cache shared with the other tools
    (wj_manifest_cache.json, keyed by each file's mtime and size), so a run
    opens no manifest it already knows.

Logs: a timestamped wj_sorter_*.log in the runtime folder beside this script;
only the previous and current run's are kept. --dry-run previews every move
and performs none.
"""

import os
import sys
import time
import shutil
import _thread
import filecmp
import logging
import argparse
import threading
from datetime import datetime
from collections import defaultdict

import wj_paths

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

# The downloader doubles as the shared library: manifest parsing and the
# light cache live there, so every tool reads a manifest the same way and
# reads it once.
import wj_downloader as WJ

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

# A stop asked for through WJ_STOP_FILE - the window's, there being no console
# to send Ctrl+C to - is taken between two moves, each of them whole: an
# archive with its sidecar, a collision's park and its place. While no move is
# under way nothing has been changed, and the main thread is interrupted as
# Ctrl+C interrupts it. Interrupting it mid-move, as the stop once did, could
# land between an archive and its sidecar.
_STOP = threading.Event()
_MOVING = threading.Event()
_STOP_LOCK = threading.Lock()


def watch_stop_file(path, interval=0.5):
    """Stop this sort when `path` appears: between two moves while it is
    moving files, at once while it is not."""
    def watch():
        while not os.path.exists(path):
            time.sleep(interval)
        try:
            os.remove(path)
        except OSError:
            pass
        LOG.info("\n[STOP] Asked to stop; stopping between two moves. Every move made so "
                 "far is whole, and the next sort carries on from there.")
        with _STOP_LOCK:
            _STOP.set()
            if not _MOVING.is_set():
                _thread.interrupt_main()
    threading.Thread(target=watch, daemon=True, name="stop-watch").start()


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
    # The shared library's own logger has no handler here. Its warnings and
    # errors - an unreadable manifest, above all - must land in this log.
    WJ.LOG.handlers = list(LOG.handlers)
    WJ.LOG.setLevel(logging.WARNING)
    WJ.LOG.propagate = False
    return log_path


def banner(title):
    LOG.info("\n=================== %s ===================", title)


def acquire_pool_lock(script_dir):
    """Best-effort exclusive lock over the archive pools.

    This script relocates files while wj_downloader.py may be
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
        import msvcrt
        fd.seek(0)
        msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
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
            import msvcrt
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        try:
            fd.close()
        except OSError:
            pass
    atexit.register(_release)
    return fd


# One implementation each, shared with the downloader: what counts as OS
# litter, and which files in a folder are manifests. The downloader decides
# what is present and the sorter what to move, so the two must never
# disagree about either.
is_junk_name = WJ.is_junk_name
list_manifest_files = WJ.list_manifest_files


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


# `path`, or the first free 'Name~N.ext' beside it: wj_paths' one
# implementation, which every tool uses (wj_merge_pool imports it from here).
unique_path = wj_paths.unique_path


# ----------------------------------------------------------------------
# Game helpers - the layout is wj_config's; the archive root is this run's
# ----------------------------------------------------------------------
def game_folder(game):
    """Filesystem folder name for a GameType."""
    return CFG.game_folder(game)


def game_label(game):
    return CFG.game_label(game)


def resolve_game_dirs(game):
    """Return (output_base, master, shared) for a given GameType."""
    return CFG.resolve_game_dirs(game, ARCHIVE_ROOT)


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
# Manifest records, from the shared light cache
# ----------------------------------------------------------------------
def light_to_record(entry):
    """The sorter's view of one cached manifest:

        {
          "game":     GameType string or None,
          "names":    set of expected download filenames (lower-cased),
          "sizes":    { name_lower: set(expected_byte_size, ...) },
          "gamefile": names Wabbajack copies out of the game install,
          "readable": False if the manifest could not be read at all,
        }

    GameFile entries are copied out of your game install by Wabbajack; they
    are never downloaded and are never expected on disk. Counting them as
    "missing" makes a healthy list look as though hundreds of archives are
    absent, so they are kept apart."""
    record = {"game": entry.get("game"), "names": set(),
              "sizes": defaultdict(set),
              "readable": bool(entry.get("readable", True)), "gamefile": set()}
    for a in entry.get("archives") or []:
        low = str(a.get("name") or "").lower()
        if not low:
            continue
        record["names"].add(low)
        if a.get("kind") == "gamefile":
            record["gamefile"].add(low)
        size = a.get("size")
        if isinstance(size, int) and size >= 0:
            record["sizes"][low].add(size)
    return record


def load_records(manifest_dir, cache_dir, wj_files):
    """One record per manifest, from the cache where it is current.

    Only manifests whose mtime or size changed since the cache saw them are
    opened; the rest cost a JSON read. A manifest that cannot be stat'ed is
    reported as unreadable, which stops the run further down."""
    parsed = []

    def progress(n, total, fn):
        parsed.append(fn)
        LOG.info("  [SCAN] %s  (%d of %d not in the cache)", fn, n, total)

    light = WJ.load_light_records(
        manifest_dir, os.path.join(cache_dir, WJ.MANIFEST_CACHE_FILE),
        progress=progress, files=wj_files)
    records = []
    for wj_file in wj_files:
        entry = light.get(wj_file)
        if entry is None:
            LOG.error("  [ERROR] %s could not be read.", wj_file)
            rec = {"game": None, "names": set(), "sizes": defaultdict(set),
                   "readable": False, "gamefile": set()}
        else:
            rec = light_to_record(entry)
        rec["file"] = wj_file
        rec["name"] = os.path.splitext(wj_file)[0]
        records.append(rec)
    LOG.info("  %d manifest(s); %d read from the cache.", len(records),
             len(records) - len(parsed))
    return records


# ----------------------------------------------------------------------
# Disk indexing
# ----------------------------------------------------------------------
def build_disk_index(search_locations, protected_names):
    """Index every real file across the search locations.

    Returns:
        index:      { name_lower: [(abs_src_path, actual_case_name, size), ...] }
        duplicates: { name_lower: [paths...] }                   (>=2 copies)

    Sizes come from the directory listing itself (scandir), not from a stat
    per file: over SMB that is one round trip per folder instead of one per
    archive, and the plan below then never has to stat a file to choose
    between copies.

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
            entries = list(os.scandir(loc))
        except OSError as e:
            LOG.warning("  [WARN] Cannot read %s: %s", loc, e)
            continue

        counted = 0
        for e in entries:
            name = e.name
            full = e.path
            low = name.lower()

            if low.endswith(".meta"):
                continue  # meta sidecars travel with their parent file
            if WJ.is_resume_file(low):
                # An in-flight (or failed-and-resumable) download from
                # wj_downloader.py, or the CDN downloader's record of which
                # parts it holds. No modlist demands a file by this name, so
                # indexing it would park it in Master and destroy the resume
                # progress. Leave it exactly where it is.
                continue
            if is_junk_name(name):
                continue  # '._x' / '.DS_Store' left by a Mac on the share
            if low in protected_names:
                continue  # never relocate the manifests or this script
            try:
                if not e.is_file():
                    continue  # ignore subfolders / symlinked dirs
                size = e.stat().st_size
            except OSError:
                continue

            counted += 1
            key = CFG.canonical_name(low)
            if key in index:
                if not duplicates[key]:
                    duplicates[key].append(index[key][0][0])
                duplicates[key].append(full)
                index[key].append((full, name, size))
            else:
                index[key] = [(full, name, size)]

        LOG.info("  -> Indexed %d file(s) in '%s'", counted, short(loc))

    return index, duplicates


# ----------------------------------------------------------------------
# Plan building
# ----------------------------------------------------------------------
def pick_copy(copies, wanted_sizes):
    """Choose which copy of a name to move, given what the destination wants.

    `copies` is [(path, name, size)] and `wanted_sizes` the set of byte sizes
    the receiving modlist's manifest states for that name. The copy that
    matches is the one that list can actually install; without this the first
    copy scanned wins, which is arbitrary and frequently wrong when two lists
    pin different builds under one name. Returns (path, name, size); the
    first copy when nothing is known about the size; and None when the sizes
    are known and no copy has one of them.

    None, not the first copy: there is then nothing on disk the list can
    use. Staging used to move whichever copy there was into the list's
    folder under the manifest's name - another list's build, out of Shared -
    and report nothing missing, so Wabbajack was handed the wrong bytes, and
    the next download for the list deleted the build in its way."""
    if not wanted_sizes:
        return copies[0]
    for path, name, size in copies:
        if size in wanted_sizes:
            return path, name, size
    return None


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
                # build into Shared while the right one sat in Master. A list
                # that states no size could use any of them, and then which
                # copy goes is anybody's guess, as it always was.
                wanted = set()
                for l in lists:
                    sizes = list_sizes.get(l, {}).get(low)
                    if not sizes:
                        wanted = None
                        break
                    wanted |= sizes
            elif n == 1:
                dest_dir, reason = os.path.join(OUTPUT_BASE_DIR, lists[0]), f"only '{lists[0]}'"
                wanted = list_sizes.get(lists[0], {}).get(low)
            else:
                dest_dir, reason = MASTER_ARCHIVE_DIR, "unused -> master"
                wanted = None

        picked = pick_copy(copies, wanted)
        if picked is None:
            if target_modlist:
                # Not one copy is the build this list wants: nothing moves,
                # and run_stage() reports the archive as missing.
                continue
            # The lists that want this name want other builds of it, and no
            # copy here is one of those: every copy is a build nobody in this
            # pool asks for, which is what Master is for. Parked, so nothing
            # lands on a file of the same name there.
            for p, n_, _s in copies:
                if not same_path(os.path.dirname(p), MASTER_ARCHIVE_DIR):
                    plan.append({"src": p, "dst": os.path.join(MASTER_ARCHIVE_DIR, n_),
                                 "name": n_, "park": True,
                                 "reason": "a build no list here wants"})
            continue
        src, name, chosen_size = picked
        # Of the copies at that size, one already sitting in the destination
        # folder wins: moving another one onto it would only collide with it
        # and park the mover, while this one was never going anywhere.
        for p, n, s_ in copies:
            if s_ == chosen_size and same_path(os.path.dirname(p), dest_dir):
                src, name = p, n
                break
        # A '~N' suffix is storage only. Staging gives the file back the name
        # the manifest uses, so Wabbajack finds it. A copy bound for Master
        # keeps whatever name it has: renaming there would make every rebuild
        # churn the parked duplicates.
        out_name = name if same_path(dest_dir, MASTER_ARCHIVE_DIR) \
            else CFG.canonical_name(name)
        dst = os.path.join(dest_dir, out_name)
        plan.append({"src": src, "dst": dst, "name": out_name, "reason": reason})

        # Every OTHER copy of the same build - same name, same size - is
        # redundant once one copy has a home, and goes to Master for stage 5c
        # to reclaim. Doing this here, in the cleanup pass, is what lets the
        # pools settle in one run instead of one copy per run. A copy of
        # another size that some list here wants is left alone: a name
        # several lists pin at different sizes has one home per build, and
        # Shared can hold only one of them. One that no list wants - a build
        # a download set aside under a '~N' name, say - goes to Master too:
        # that is where "needed by nothing" lives, and in a list's folder it
        # was never reclaimed at all. Only the cleanup pass does this; staging
        # finds the pools already settled.
        if target_modlist is None:
            for p, n_, s in copies:
                if p == src or same_path(os.path.dirname(p), MASTER_ARCHIVE_DIR):
                    continue
                if s == chosen_size:
                    why = "redundant copy of the same build"
                elif s is not None and (not lists or (wanted is not None and s not in wanted)):
                    why = "a build no list here wants"
                else:
                    continue
                plan.append({"src": p, "dst": os.path.join(MASTER_ARCHIVE_DIR, n_),
                             "name": n_, "reason": why, "park": True})

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
    """Move src -> dst, never over another file: FileExistsError when dst is
    taken (wj_paths.move_file). It used to delete whatever was in the way,
    so a collision whose other file could not be parked lost that file."""
    if os.path.isdir(dst) and not os.path.islink(dst):
        raise IsADirectoryError(f"Destination is a directory, refusing: {dst}")
    wj_paths.move_file(src, dst)


def move_with_meta(src, dst, counters, dry_run):
    """Move src -> dst and carry its sidecar .meta along. Updates counters."""
    if dry_run:
        return
    _raw_move(src, dst)
    counters["moved"] += 1
    meta_src = src + ".meta"
    if os.path.exists(meta_src):
        meta_dst = dst + ".meta"
        try:
            if os.path.lexists(meta_dst):
                # A sidecar with no archive of its own, left by something
                # else: kept under another name rather than written over.
                wj_paths.move_file(meta_dst, wj_paths.unique_path(meta_dst))
            wj_paths.move_file(meta_src, meta_dst)
            counters["meta"] += 1
        except OSError as e:
            LOG.warning("  [WARN] Moved '%s' but its .meta failed: %s", os.path.basename(src), e)


def park_in_master(path, counters, dry_run, why):
    """Move a file (and its .meta) into Master under a non-colliding name.
    True when it moved (or would have, in a dry run)."""
    name = os.path.basename(path)
    LOG.info("  [PARK] '%s' -> Master  (%s)", name, why)
    if dry_run:
        counters["parked"] += 1
        return True
    # Free for the archive AND its sidecar, so the sidecar keeps the exact
    # name that ties it to its archive.
    dest = unique_path(os.path.join(MASTER_ARCHIVE_DIR, name))
    try:
        wj_paths.move_file(path, dest)
    except OSError as e:
        LOG.error("  [ERROR] Could not park '%s' in Master: %s", name, e)
        counters["errors"] += 1
        return False
    counters["parked"] += 1
    meta_src = path + ".meta"
    if os.path.exists(meta_src):
        try:
            wj_paths.move_file(meta_src, dest + ".meta")
            counters["meta"] += 1
        except OSError as e:
            LOG.warning("  [WARN] Parked '%s' but its .meta failed: %s", name, e)
    return True


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
            if park_in_master(dst, counters, dry_run, "existing copy: wrong size"):
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
    if park_in_master(dst, counters, dry_run, "unverified collision (existing copy preserved)"):
        move_with_meta(src, dst, counters, dry_run)


def execute_plan(moves, expected, dry_run):
    """Carry out the planned moves. Returns a counters dict, with "stopped"
    set when a stop (watch_stop_file) ended it between two moves."""
    counters = {"moved": 0, "meta": 0, "parked": 0, "errors": 0, "stopped": False}
    with _STOP_LOCK:
        if _STOP.is_set():
            counters["stopped"] = True
            return counters
        _MOVING.set()
    try:
        return _execute_moves(moves, expected, dry_run, counters)
    finally:
        _MOVING.clear()


def _execute_moves(moves, expected, dry_run, counters):
    for i, m in enumerate(moves):
        if _STOP.is_set():
            LOG.info("  [STOP] Stopped between two moves; %d of %d left for the next sort.",
                     len(moves) - i, len(moves))
            counters["stopped"] = True
            break
        src, dst, name = m["src"], m["dst"], m["name"]
        LOG.info("  [MOVE] '%s' -> %s  (%s)", name, short(os.path.dirname(dst)), m["reason"])

        try:
            if not os.path.exists(src):
                LOG.warning("  [WARN] Source vanished before move: %s", src)
                counters["errors"] += 1
                continue

            # A redundant copy is parked, not placed: Master may already hold
            # a file of that name, and park_in_master() finds a free one.
            if m.get("park"):
                park_in_master(src, counters, dry_run, m["reason"])
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
        LOG.warning("[WARN] %d filename(s) exist in more than one location. The copy "
                    "whose size a list wants is placed; identical extras are parked "
                    "in Master for stage 5c; other builds stay put:", len(duplicates))
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
        # On disk under the right name, but not one copy at a size this list
        # states: another list's build. It is as missing as an absent one,
        # and build_plan() left it where it was. Counting names alone said
        # "0 missing" while the list had nothing it could install.
        sizes = (list_sizes or {}).get(target_modlist, {})
        other_build = sorted(
            f for f in file_demands
            if target_modlist in file_demands[f] and f in disk_index
            and f not in gamefile_names and sizes.get(f)
            and not any(s in sizes[f] for _p, _n, s in disk_index[f]))
        stats["missing"] = missing + other_build
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
        if other_build:
            LOG.warning("[WARN] '%s' needs %d archive(s) of which only a DIFFERENT build is "
                        "on disk - another list's. Left where they are; these have to be "
                        "fetched too:", target_modlist, len(other_build))
            for f in other_build:
                LOG.warning("  ? %s (wants %s; on disk at %s)", f,
                            ", ".join(str(s) for s in sorted(sizes[f])),
                            ", ".join(str(s) for _p, _n, s in disk_index[f]))

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

    current_dir = wj_paths.app_dir(__file__)
    # The log, the lock and the manifest cache: see wj_paths.RUNTIME_DIR.
    wj_paths.adopt_runtime(current_dir)
    run_dir = wj_paths.runtime_dir(__file__)
    log_path = setup_logging(run_dir, verbose=args.verbose)

    # Nothing below may run concurrently with a download into the same pools.
    acquire_pool_lock(run_dir)
    # The window that started this sort asks it to stop through this file:
    # between two moves, every one of which is whole (watch_stop_file).
    if os.environ.get("WJ_STOP_FILE"):
        watch_stop_file(os.environ["WJ_STOP_FILE"])

    # Manifests may live elsewhere (see change 3 in the docstring). The log
    # stays in the runtime folder beside this script regardless.
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

    # ---- Step 1: read every manifest, from the cache where it is current --
    banner("STEP 1: SCANNING MANIFESTS")
    records = load_records(manifest_dir, run_dir, wj_files)
    empty_lists = [r["name"] for r in records if not r["names"]]

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

    # Reserved-name collision check (only for in-scope lists). Judged as
    # Windows reads the name: 'Master.' is Master.
    modlists_map = {}
    for i, rec in enumerate(in_scope):
        why = CFG.list_name_problem(rec["name"])
        if why:
            LOG.error("[TERMINATED] Modlist '%s' cannot have a folder of its own: %s. "
                      "Rename the .wabbajack file.", rec["name"], why)
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
    # Frozen, __file__ names a file inside the bundle that nobody can see;
    # the two exes beside the pools are what actually needs shielding.
    protected |= wj_paths.own_names()
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
    if _STOP.is_set():
        LOG.info("\n[STOP] Stopped as asked; nothing is staged this time.")
        sys.exit(130)

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
    if _STOP.is_set():
        LOG.info("\n[STOP] Stopped as asked; '%s' is staged only in part. Stage it again "
                 "before installing it.", target_modlist)
        sys.exit(130)

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
