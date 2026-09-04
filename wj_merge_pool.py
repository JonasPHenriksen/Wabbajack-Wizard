#!/usr/bin/env python3
"""
Merging one game's pool folder into another
===========================================
Used once, on 2026-09-02, to fold EnderalSpecialEdition into Skyrim after
GAME_FOLDERS was changed to say the two share a folder. Kept because the
question comes back whenever a game turns out to be another game's mods in a
different box, and because a move-and-delete over 86 GB deserves a dry run,
a lock, a hash check and a written record rather than a shell one-liner.

    wj_merge_pool.py EnderalSpecialEdition Skyrim --dry-run
    wj_merge_pool.py EnderalSpecialEdition Skyrim

Both arguments are FOLDER names under ARCHIVE_ROOT, not GameTypes. What it
does, in order:

  1. Refuses unless everything under the source folder is a pool (Master,
     Shared) or a folder named after a manifest you hold. Anything else - the
     unmanaged store, another installer's folder, a stray, a loose file - has
     to be dealt with first, because this tool will not guess what it is. It
     also refuses if a list folder of the same name already exists on the
     destination side.
  2. Drops every archive under the source that the destination pool already
     holds at the same name (read through any '~N' suffix), the same size AND
     the same xxHash64. Both files are read in full for that. Same name and
     size with different content keeps both, and says so.
  3. Moves each list folder across - a rename on the same volume.
  4. Moves the Master and Shared contents across file by file. A name already
     taken on the destination side gets a '~N' suffix; the sorter and the
     wizard read through those.
  5. Removes the source folders once they are empty.

Every drop and every suffixed move is written to
merged_<src>_into_<dst>_<stamp>.csv as it happens, with the copy that was
kept. Run the sorter's cleanup pass for the destination game afterwards, so
names that several lists now share settle into Shared.

Exit codes: 0 done, 1 done with errors, 2 refused, 3 another job holds the
lock.
"""

import argparse
import csv
import os
import shutil
import sys
from datetime import datetime

try:
    import wj_config as CFG
except ImportError:
    sys.stderr.write("wj_config.py is missing from the folder holding these "
                     "scripts. It defines ARCHIVE_ROOT and is not optional.\n")
    raise SystemExit(2)

import wj_downloader as WJ
import wj_autoupdate as A
from wj_sorter import unique_path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = CFG.resolved_archive_root()
POOLS = {p.lower() for p in CFG.POOL_FOLDERS}
RESUME_SUFFIXES = (".part", ".parts.json")


def say(msg=""):
    print(msg, flush=True)


def human(n):
    return WJ.human_bytes(n or 0)


def norm(path):
    return os.path.normcase(os.path.abspath(path))


def is_archive(name):
    """A file that a manifest could ask for: not a sidecar, not a partial
    download, not OS litter."""
    low = name.lower()
    return not (low.endswith(".meta") or low.endswith(RESUME_SUFFIXES)
                or WJ.is_junk_name(name))


_HASHES = {}


def hashed(path):
    """xxHash64 of a file, computed once per path."""
    key = norm(path)
    if key not in _HASHES:
        _HASHES[key] = WJ.wabbajack_hash(path)
    return _HASHES[key]


class Record:
    """One CSV row per drop or suffixed move, flushed as it happens, so the
    file on disk is exact at every moment however the run ends."""

    def __init__(self, src_name, dst_name, dry):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = os.path.join(
            HERE, f"merged_{src_name}_into_{dst_name}_{stamp}.csv")
        self.rows = 0
        self._fh = self._w = None
        if dry:
            return
        self._fh = open(self.path, "w", newline="", encoding="utf-8")
        self._w = csv.writer(self._fh)
        self._w.writerow(["action", "archive", "size_bytes", "from",
                          "kept_or_to", "hash"])
        self._fh.flush()

    def note(self, action, archive, size, frm, to, h=""):
        self.rows += 1
        if self._w is not None:
            self._w.writerow([action, archive, size, frm, to, h or ""])
            self._fh.flush()

    def close(self):
        if self._fh is None:
            return None
        self._fh.close()
        if self.rows == 0:
            try:
                os.remove(self.path)
            except OSError:
                pass
            return None
        return self.path


# ----------------------------------------------------------------------
def inventory(src, known):
    """Sort the source folder's children into list folders and pools, and
    name everything that is neither."""
    lists, pools, problems = [], [], []
    for name in sorted(os.listdir(src)):
        p = os.path.join(src, name)
        low = name.lower()
        if os.path.isfile(p):
            if not WJ.is_junk_name(name):
                problems.append(f"a loose file at the game root: {name}")
            continue
        if low in POOLS:
            pools.append(name)
        elif low in CFG.excluded_dirs():
            problems.append(f"{name}: another installer's folder, the unmanaged "
                            f"store, or the trash; decide about it by hand first "
                            f"(the wizard's stage 5 can empty a trash folder)")
        elif low in known:
            lists.append(name)
        else:
            problems.append(f"{name}: neither a pool nor named after a "
                            f"manifest you hold; park it or remove it first")
    return lists, pools, problems


def archives_under(folder):
    """(path, name, size) for every archive directly in `folder`."""
    out = []
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for name in names:
        p = os.path.join(folder, name)
        if os.path.isfile(p) and is_archive(name):
            try:
                out.append((p, name, os.path.getsize(p)))
            except OSError:
                pass
    return out


def find_duplicates(src, folders, dst):
    """Archives under the source that the destination pool already holds at
    the same canonical name and size. Returns [(path, name, size, other)]."""
    WJ.invalidate_existing_cache()
    index = WJ.index_existing(dst)
    found = []
    for folder in folders:
        for p, name, size in archives_under(os.path.join(src, folder)):
            key = WJ.canonical_name(name).lower()
            others = [op for op, os_ in index.get(key, []) if os_ == size]
            if others:
                found.append((p, name, size, others))
    return found


def drop_duplicates(dups, rec, dry, dst_name):
    """Delete each duplicate whose hash matches the destination's copy."""
    dropped = dropped_bytes = differs = 0
    read = 0
    total_bytes = sum(size for _p, _n, size, _o in dups) * 2
    for i, (p, name, size, others) in enumerate(dups, 1):
        # Re-checked on disk: the index is a snapshot.
        other = next((o for o in others
                      if os.path.isfile(o) and os.path.getsize(o) == size), None)
        if other is None:
            rec.note("kept-other-copy-vanished", name, size, p, "", "")
            continue
        if dry:
            rec.note("would-drop-duplicate", name, size, p, other, "")
            dropped += 1
            dropped_bytes += size
            continue
        h_src, h_dst = hashed(p), hashed(other)
        read += 2 * size
        if h_src and h_src == h_dst:
            try:
                os.remove(p)
                meta = p + ".meta"
                if os.path.isfile(meta):
                    os.remove(meta)
            except OSError as e:
                say(f"    could not delete {name}: {e}")
                continue
            rec.note("dropped-duplicate", name, size, p, other, h_src)
            dropped += 1
            dropped_bytes += size
        else:
            rec.note("kept-content-differs", name, size, p, other,
                     f"{h_src} vs {h_dst}")
            differs += 1
        if i % 10 == 0 or i == len(dups):
            say(f"    [{i}/{len(dups)}] {human(read)} of {human(total_bytes)} "
                f"hashed, {dropped} dropped")
    return dropped, dropped_bytes, differs


def move_list_folder(src, dst, folder, dry):
    s, d = os.path.join(src, folder), os.path.join(dst, folder)
    if dry:
        return True
    try:
        shutil.move(s, d)
        return True
    except (OSError, shutil.Error) as e:
        say(f"    could not move {folder}: {e}")
        return False


def merge_pool(src_dir, dst_dir, rec, dry):
    """Move every file in one pool folder into the matching one across.
    Returns (moved, suffixed, errors)."""
    if not os.path.isdir(src_dir):
        return 0, 0, 0
    names = sorted(os.listdir(src_dir))
    files = [n for n in names if os.path.isfile(os.path.join(src_dir, n))]
    metas = {n.lower(): n for n in files if n.lower().endswith(".meta")}
    others = [n for n in files if not n.lower().endswith(".meta")
              and not WJ.is_junk_name(n)]
    moved = suffixed = errors = 0
    if not dry:
        os.makedirs(dst_dir, exist_ok=True)
    for name in others:
        s = os.path.join(src_dir, name)
        d = os.path.join(dst_dir, name)
        meta = metas.pop((name + ".meta").lower(), None)
        try:
            size = os.path.getsize(s)
            if os.path.exists(d):
                d = unique_path(d)
                rec.note("moved-suffixed" if not dry else "would-move-suffixed",
                         name, size, s, d, "")
                suffixed += 1
            if not dry:
                shutil.move(s, d)
                if meta:
                    target = d + ".meta"
                    if os.path.isfile(target):
                        os.remove(target)
                    shutil.move(os.path.join(src_dir, meta), target)
            moved += 1
        except (OSError, shutil.Error) as e:
            say(f"    could not move {name}: {e}")
            errors += 1
    for m in list(metas.values()):
        try:
            if not dry:
                shutil.move(os.path.join(src_dir, m),
                            unique_path(os.path.join(dst_dir, m)))
            moved += 1
        except (OSError, shutil.Error) as e:
            say(f"    could not move {m}: {e}")
            errors += 1
    return moved, suffixed, errors


def remove_if_empty(folder):
    """Remove a folder holding nothing but OS litter. True if it is gone."""
    try:
        for name in os.listdir(folder):
            p = os.path.join(folder, name)
            if os.path.isfile(p) and WJ.is_junk_name(name):
                os.remove(p)
        os.rmdir(folder)
        return True
    except OSError:
        return os.path.isdir(folder) is False


# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Fold one game's pool folder into another's.")
    ap.add_argument("src", help="folder under the archive root to empty")
    ap.add_argument("dst", help="folder under the archive root to fill")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would happen; move and delete nothing.")
    ap.add_argument("--yes", action="store_true",
                    help="Skip the confirmation prompt.")
    ap.add_argument("--allow-size-only", action="store_true",
                    help="Drop duplicates on name and size alone, without "
                         "xxhash. Off by default: a deletion deserves the "
                         "hash check.")
    args = ap.parse_args()

    src = os.path.join(ROOT, args.src)
    dst = os.path.join(ROOT, args.dst)
    if not os.path.isdir(src):
        say(f"[REFUSED] {src} is not a folder.")
        return 2
    if not os.path.isdir(dst):
        say(f"[REFUSED] {dst} is not a folder. Create the destination first, so "
            f"a typo cannot conjure up a new game.")
        return 2
    if norm(src) == norm(dst):
        say("[REFUSED] Source and destination are the same folder.")
        return 2
    if not getattr(WJ, "_HAVE_XXHASH", False) and not args.allow_size_only:
        say("[REFUSED] xxhash is not importable, so duplicates could only be "
            "matched by size. Run through run.bat's environment, or pass "
            "--allow-size-only.")
        return 2

    manifest_dir = WJ.resolve_manifest_dir(HERE)
    known = WJ.known_list_dirs_from(manifest_dir)
    WJ.set_known_list_dirs(known)

    lists, pools, problems = inventory(src, known)
    for folder in lists:
        if os.path.exists(os.path.join(dst, folder)):
            problems.append(f"{folder}: a folder of that name already exists "
                            f"under {args.dst}; merge those two by hand first")
    if problems:
        say(f"[REFUSED] {args.src} holds things this tool will not guess about:")
        for p in problems:
            say(f"   - {p}")
        say("Nothing was moved.")
        return 2
    if not lists and not pools:
        say(f"[REFUSED] {args.src} holds nothing to merge.")
        return 2

    lock_fd = None
    if not os.environ.get("WJ_LOCK_HELD"):
        lock_fd = A.acquire_lock(os.path.join(HERE, A.LOCK_FILE))
        if lock_fd is A.HELD:
            say("[SKIPPED] Another archive job holds the lock. Nothing was done.")
            return 3
        if lock_fd is A.UNAVAILABLE:
            say("[WARN] Could not lock on this filesystem; continuing. Do not "
                "run the wizard or the downloader at the same time.")
            lock_fd = None

    dry = args.dry_run
    rec = Record(args.src, args.dst, dry)
    rc = 0
    try:
        say(f"{'DRY RUN: ' if dry else ''}merging {src}")
        say(f"                 into {dst}")
        say(f"  list folders: {', '.join(lists) or 'none'}")
        say(f"  pools:        {', '.join(pools) or 'none'}")

        say()
        say(f"  Looking for archives the {args.dst} pool already holds ...")
        dups = find_duplicates(src, lists + pools, dst)
        dup_bytes = sum(size for _p, _n, size, _o in dups)
        total = sum(len(archives_under(os.path.join(src, f))) for f in lists + pools)
        say(f"  {len(dups)} of {total} archive(s) ({human(dup_bytes)}) have a "
            f"same-size copy there"
            + ("; each will be hashed against it before it goes."
               if not dry else ". A real run hashes each pair first."))

        if not dry and not args.yes:
            say()
            try:
                answer = input(f"  Proceed: drop up to {len(dups)} duplicate(s), "
                               f"move {len(lists)} list folder(s) and the pools? "
                               f"[y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer not in ("y", "yes"):
                say("[ABORT] Cancelled. Nothing moved.")
                return 2

        say()
        say("  Step 1: duplicates")
        dropped, dropped_bytes, differs = drop_duplicates(dups, rec, dry, args.dst)
        say(f"  {'would drop' if dry else 'dropped'} {dropped} duplicate(s), "
            f"{human(dropped_bytes)}; {differs} kept because the content differs")

        say()
        say("  Step 2: list folders")
        for folder in lists:
            ok = move_list_folder(src, dst, folder, dry)
            say(f"    {folder} -> {os.path.join(args.dst, folder)}"
                f"{'' if ok else '  FAILED'}")
            if not ok:
                rc = 1

        say()
        say("  Step 3: pools")
        for pool in pools:
            moved, suffixed, errors = merge_pool(os.path.join(src, pool),
                                                 os.path.join(dst, pool), rec, dry)
            say(f"    {pool}: {moved} file(s) {'would move' if dry else 'moved'}, "
                f"{suffixed} under a ~N suffix, {errors} error(s)")
            if errors:
                rc = 1

        if not dry and rc == 0:
            say()
            say("  Step 4: the empty source")
            for pool in pools:
                remove_if_empty(os.path.join(src, pool))
            if remove_if_empty(src):
                say(f"    {args.src} removed")
            else:
                say(f"    {args.src} still holds something and was left in place")
    finally:
        path = rec.close()
        A.release_lock(lock_fd)

    say()
    if dry:
        say("[DRY RUN COMPLETE] Nothing was moved or deleted.")
    else:
        say(f"[DONE] Record: {os.path.basename(path) if path else 'nothing to record'}")
        say(f"Now run the sorter's cleanup pass for a {args.dst} game so shared "
            f"names settle into Shared.")
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\n[ABORT] Interrupted.")
        sys.exit(130)
