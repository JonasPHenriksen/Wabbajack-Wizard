#!/usr/bin/env python3
"""
Interactive front end for the Wabbajack tooling
===============================================
One launch, five stages, each of which can be skipped independently:

  1. BROWSE      every modlist published for a game you keep archives for,
                 with how much of each one you already hold. Read from
                 Wabbajack's own validation reports, so the answer arrives
                 before the .wabbajack is downloaded rather than after.
  2. UPDATES     which tracked modlists have a newer published version, and
                 which are already current. Fetch all, some, or none.
  3. INCOMPLETE  which modlists are missing archives. Fetch all, some, or
                 none - one list at a time, each finished before the next is
                 offered, because a long Nexus click session is fragile
                 enough without two of them interleaved.
  4. PREPARE     gather everything one modlist needs into its own folder, so
                 Wabbajack can install it.
  5. SPACE       what the manifests and the per-game Master pools are costing,
                 with the option to delete modlists and then clear Master.

Nothing here re-implements the work: every stage drives the same functions the
flag interface uses (wj_autoupdate.py), so the two cannot drift apart. This
module only decides what to show you and what to ask.

Order matters in stage 5 and is not cosmetic. Deleting a modlist moves its
archives into Master, which is exactly where step 5c then wants to delete
from - so the pools are rebuilt in between, and every candidate is re-checked
against the remaining lists' demands before it is removed. A file that any
surviving modlist still needs is never deleted, whatever you select.

The rules stage 5 deletes under, all of them checked at the moment of
deletion rather than trusted from the table:

  * a file is protected by NAME AND SIZE, and the name is read through any
    '~N' suffix a parked duplicate carries (wj_config.canonical_name);
  * demand is counted per POOL FOLDER, so every GameType sharing a folder
    protects it (Skyrim and Skyrim Special Edition share one here);
  * a manifest named after a pool folder is never deleted - its "folder" IS
    the pool;
  * if the rebuild after a deletion fails, that game's Master is not offered;
  * if any manifest is unreadable, or lists no archives at all, its demand is
    unknown and Master is not offered - for that game, or for any game when
    the manifest's game itself cannot be read;
  * the "any known modlist" rule refuses on any gap in the published picture
    except the permanent one (lists their authors have pulled), which it names;
  * a Master copy of a build that also exists in Shared, in a modlist's
    folder, or under a better name inside Master is a duplicate, reclaimable
    under either rule; the other copy is re-read and hashed first, a pair
    that differs is kept, and the copy that made it redundant is named in
    the record;
  * nothing is deleted outright: reclaimed files go to <game>/Trash, stamped
    with their arrival, and are purged TRASH_KEEP_DAYS days later at the
    start of a run - or at once, from 5d. Until then they can be moved back;
  * nothing that moves archives starts while a Wabbajack process is running,
    because it may be reading them.
"""

import os
import csv
import io
import json
import sys
import time
import shutil
from datetime import datetime, timezone

import wj_autoupdate as A
import wj_wabbajack as WB
import wj_gallery as G

try:
    import wj_config as CFG
except ImportError:
    CFG = None


class Quit(Exception):
    """The user asked to stop. Raised from any prompt, caught in run()."""


# ----------------------------------------------------------------------
# Terminal helpers
# ----------------------------------------------------------------------
WIDTH = 78


def out(msg=""):
    print(msg, flush=True)


def rule(char="-"):
    out(char * WIDTH)


STAGES = 5


def stage_header(number, title):
    out()
    rule("=")
    out(f"  STAGE {number} of {STAGES}   {title}")
    rule("=")


def human(n):
    return A.WJ.human_bytes(n or 0)


def ask(question, allowed):
    """Prompt until the answer is one of `allowed` (single letters).

    EOF or Ctrl+C means stop, not 'yes'. A wizard that treats a closed stdin
    as consent would happily delete several hundred GB on a stray keystroke."""
    opts = "/".join(allowed)
    while True:
        try:
            raw = input(f"{question} [{opts}]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            out()
            raise Quit()
        if raw in allowed:
            return raw
        if raw in ("q", "quit", "exit"):
            raise Quit()
        out(f"  '{raw}' is not one of {opts}.")


def ask_path(question, default=None):
    """Prompt for a filesystem path. Empty means skip; Ctrl+C means stop."""
    suffix = ("\n   [Enter] to accept %s, or 's' to skip" % default) if default \
        else "\n   [Enter] or 's' to skip"
    out()
    out("   " + question + suffix)
    try:
        raw = input("> ").strip().strip('"')
    except (EOFError, KeyboardInterrupt):
        out()
        raise Quit()
    if raw.lower() in ("q", "quit", "exit"):
        raise Quit()
    if raw.lower() in ("s", "skip"):
        return None
    if not raw:
        return default
    return os.path.expandvars(os.path.expanduser(raw))


def parse_selection(text, count):
    """Turn '1,3,5-7' into [0,2,4,5,6]. Returns None if it does not parse.

    Out-of-range numbers are rejected rather than clamped: silently ignoring
    a '9' when there are only 8 items would act on a different set than the
    one that was typed."""
    picked = []
    for chunk in text.replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk:
            a, _, b = chunk.partition("-")
            if not (a.isdigit() and b.isdigit()):
                return None
            lo, hi = int(a), int(b)
            if lo > hi:
                lo, hi = hi, lo
            rng = range(lo, hi + 1)
        elif chunk.isdigit():
            rng = [int(chunk)]
        else:
            return None
        for n in rng:
            if not 1 <= n <= count:
                return None
            if n - 1 not in picked:
                picked.append(n - 1)
    return picked


def choose(count, what="items", allow_all=True, single=False):
    """Ask which of `count` numbered rows to act on.

    Returns a list of indices, or None for 'skip this stage'. Raises Quit on
    q. Every stage offers the same three shapes of answer - all, some, none -
    so the interaction is the same wherever you are."""
    if count == 0:
        return None
    bits = []
    if single:
        bits.append("[1] pick one")
    else:
        if allow_all:
            bits.append("[a] all")
        bits.append("[1,3-5] pick some")
    bits.append("[s] skip")
    bits.append("[q] quit")
    out()
    out("   " + "   ".join(bits))
    while True:
        try:
            raw = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            out()
            raise Quit()
        if raw in ("q", "quit", "exit"):
            raise Quit()
        if raw in ("s", "skip", ""):
            return None
        if raw in ("a", "all") and allow_all and not single:
            return list(range(count))
        got = parse_selection(raw, count)
        if got is None or not got:
            out(f"  Enter numbers between 1 and {count}, or a/s/q.")
            continue
        if single and len(got) > 1:
            out("  Only one may be selected here.")
            continue
        return got


def confirm_destructive(title, lines, total_bytes=None,
                        total_label="Total to be freed", undo=None):
    """Show exactly what is about to be destroyed and ask.

    Returns 'yes', 'no', or 'change'. The 'change' answer exists so a wrong
    selection can be corrected here rather than by aborting the whole stage
    and walking through it again. `undo` replaces the default "cannot be
    undone" line for a step that can, in fact, be undone for a while."""
    out()
    rule("!")
    out(f"  {title}")
    rule("!")
    for line in lines:
        out("   " + line)
    if total_bytes is not None:
        out()
        out(f"   {total_label}: {human(total_bytes)}")
    out()
    out("   " + (undo or "This cannot be undone."))
    a = ask("   Proceed?  [y] yes  [n] no, skip  [c] change selection", ("y", "n", "c"))
    return {"y": "yes", "n": "no", "c": "change"}[a]


def dir_size(path):
    """(bytes, file_count) for one directory, non-recursive.

    scandir carries the size along from the directory enumeration on Windows,
    so this costs one pass rather than a stat() per file - which matters when
    the directory holds several thousand archives on an SMB share."""
    total = count = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    # .meta sidecars are a few hundred bytes and travel with
                    # their archive. Counting them would report twice as many
                    # "files" as there are actual mods, which is confusing when
                    # you are deciding what to delete.
                    if e.is_file() and not e.name.lower().endswith(".meta"):
                        total += e.stat().st_size
                        count += 1
                except OSError:
                    continue
    except OSError:
        return 0, 0
    return total, count


def tree_size(path):
    """(bytes, files) for a whole directory tree.

    dir_size() is deliberately non-recursive: a pool folder is flat, and not
    descending is both correct and faster there. A stray folder is not a pool
    though - it is whatever someone put on the share. "Workbase Improved" here
    reported 81.7 KB from its eight top-level files while actually holding
    42.9 GB in PatchedBa2 and Repack7z, and TTW reported 1.7 GB against a real
    18.8 GB. Those numbers are what a decision gets made on, so they have to
    count the whole tree."""
    total = files = 0
    # A hand-rolled walk over scandir rather than os.walk plus getsize: the
    # directory listing already carries each entry's size, so a 40 GB tree of
    # ten thousand files costs one round trip per folder, not one per file.
    stack = [path]
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                            continue
                        if e.is_file() and not e.name.lower().endswith(".meta"):
                            total += e.stat().st_size
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total, files


def wanted_builds(records, game):
    """{ name_lower: set(sizes) or None } for everything wanted from this
    game's POOL FOLDER.

    A value of None means "some list wants this name but states no size", in
    which case every copy has to be protected. Keyed by name AND size because
    a name does not identify content: with only names, a junk build of
    Synthesis.zip that no list wants reads as still needed purely because some
    OTHER build of that name is wanted, and stage 5c then refuses to free it.

    Every list drawing from the same folder counts, not just those with the
    same GameType string: Skyrim and Skyrim Special Edition share one Master
    here, and an LE list's archives in it must not read as unwanted just
    because the SE lists were the only ones asked."""
    want = {}
    for e in records.values():
        if not A.WJ.same_pool(e.get("game"), game):
            continue
        for a in e["archives"]:
            low = a["name"].lower()
            size = a.get("size")
            if size is None:
                want[low] = None
            elif low in want:
                if want[low] is not None:
                    want[low].add(size)
            else:
                want[low] = {size}
    return want


def is_wanted(want, name, size):
    """Is this exact file one that a remaining modlist asks for?

    The name is read through any '~N' suffix: 'Synthesis~1.zip' at a size some
    list wants IS that list's Synthesis.zip, parked under a second name because
    another build held the first. Judging it by the suffixed name made every
    such copy reclaimable, wanted or not."""
    low = A.WJ.canonical_name(name).lower()
    if low not in want:
        return False
    sizes = want[low]
    return sizes is None or size in sizes


def known_list_dirs():
    """Lowercased folder names that legitimately belong to a modlist we follow."""
    return A.WJ.known_list_dirs_from(A.MANIFESTS)


def refresh_known_dirs():
    """Re-read which folders belong to a modlist we hold.

    The downloader module snapshots this at start-up. Stage 1 can adopt a
    list and stage 5 can delete one, and after either the snapshot is stale:
    a list adopted and fetched in one run read as entirely missing afterwards,
    because its brand-new folder was not in the snapshot, so it was treated as
    a stray and left out of the index."""
    A.WJ.set_known_list_dirs(known_list_dirs())


def demand_problems(records):
    """Manifests whose demand cannot be trusted when deciding what to delete.

    Returns (unreadable, empty): the filenames that could not be read at all,
    and { folder key: [filenames] } for manifests that read fine but list no
    archives. The sorter refuses to run on either. Stage 5c refuses likewise,
    because a list that protects nothing is not a list, it is a gap - and an
    unreadable one does not even say which game's Master it would have
    protected."""
    unreadable = sorted(fn for fn, e in records.items()
                        if not e.get("readable", True))
    empty = {}
    for fn in sorted(records):
        e = records[fn]
        if e.get("readable", True) and e.get("game") and not e.get("archives"):
            empty.setdefault(A.WJ.folder_key(e["game"]), []).append(fn)
    return unreadable, empty


def strays_in(game_dir):
    """(folder, files, bytes) for subfolders of one game root that are nobody's.

    Not Master, not Shared, not excluded, and not named after a manifest we
    hold. Usually a hand-built install or a tool; occasionally a modlist whose
    manifest was deleted while its archives stayed."""
    known = known_list_dirs()
    pools = {p.lower() for p in A.WJ.POOL_FOLDERS}
    rows = []
    try:
        names = sorted(os.listdir(game_dir))
    except OSError:
        return rows
    for n in names:
        p = os.path.join(game_dir, n)
        if not os.path.isdir(p):
            continue
        low = n.lower()
        if low in pools or low in A.WJ.EXCLUDE_POOL_DIRS or low in known:
            continue
        b, f = tree_size(p)
        rows.append((n, f, b))
    return rows


def stray_folders(game):
    """strays_in() for one GameType."""
    base, _m, _s = A.WJ.resolve_game_dirs(game)
    return strays_in(base)


def all_stray_folders():
    """(game_folder, folder, files, bytes) across every game on the share.

    Walks the archive root rather than the games named in the manifests. A game
    whose ONLY content is orphaned - Dark Souls 3 here, holding a tool drop and
    no modlist at all - would otherwise never be looked at, and that is exactly
    the case worth reporting."""
    root = A.WJ.ARCHIVE_ROOT
    rows = []
    try:
        games = sorted(os.listdir(root))
    except OSError:
        return rows
    for g in games:
        gp = os.path.join(root, g)
        if not os.path.isdir(gp):
            continue
        for name, files, byts in strays_in(gp):
            rows.append((g, name, files, byts))
    return rows


def stray_origin(name):
    """What is known locally about a stray folder that looks like a modlist.

    A folder is a stray when no manifest of the same name is held. Some strays
    are hand-built and always were; others are a list whose paperwork went
    missing, and those are not junk at all - their archives are still wanted,
    and re-adopting the list makes the folder legitimate again. Parking such a
    folder in Unmanaged buries that, so the stray tables say which it is.

    Wabbajack writes a .metadata sidecar beside every modlist it downloads, so
    a sidecar with no manifest is the last trace of exactly that. Whether the
    list is still published is only checked against feeds already loaded this
    run: stage 5 must not depend on the network, and a cold cache simply
    leaves that unknown rather than fetching 142 feeds mid-deletion.

    Returns None for a folder with nothing to say about it, else a dict with
    `sidecar`, `title`, `key` and `published` (True, False or None-unknown)."""
    side = os.path.join(A.MANIFESTS, name + ".wabbajack.metadata")
    title = None
    if os.path.isfile(side):
        try:
            with io.open(side, encoding="utf-8") as f:
                title = (json.load(f) or {}).get("title") or None
        except (OSError, ValueError):
            title = None
    elif "_@@_" not in name:
        return None

    parts = name.rsplit("_@@_", 1)
    key = "/".join(parts) if len(parts) == 2 else None

    published = None
    cached = A._FEED_CACHE.get("all")
    if cached is not None and key:
        low = {k.lower() for k in cached}
        published = key.lower() in low
        if published and not title:
            for k, ent in cached.items():
                if k.lower() == key.lower():
                    title = ent.get("title")
                    break
    if not os.path.isfile(side) and not published:
        return None
    return {"sidecar": os.path.isfile(side), "title": title, "key": key,
            "published": published}


def stray_note(origin):
    """One short phrase for a stray table's last column."""
    if not origin:
        return ""
    if origin["published"]:
        return "manifest gone, still published - adopt it"
    if origin["published"] is False:
        return "manifest gone, no longer published"
    return "manifest gone, sidecar remains"


def strays_with_notes(strays):
    """The stray rows, each with what is known about where it came from."""
    return [(g, n, f, b, stray_origin(n)) for g, n, f, b in strays]


def park_unmanaged_dir(game_dir, folder):
    """Move one stray folder into that game root's Unmanaged store.

    Moved rather than deleted, and moved wholesale rather than file by file:
    the point is to record that this content is not ours to judge, and the
    cheapest way to be sure nothing is lost is not to take it apart."""
    src = os.path.join(game_dir, folder)
    name = CFG.UNMANAGED_FOLDER if CFG else "Unmanaged"
    store = os.path.join(game_dir, name)
    dst = unique_path(os.path.join(store, folder))
    try:
        os.makedirs(store, exist_ok=True)
        shutil.move(src, dst)
    except OSError as e:
        out(f"    could not move {folder}: {e}")
        return None
    return dst


def park_unmanaged(game, folder):
    """park_unmanaged_dir() for one GameType."""
    base, _m, _s = A.WJ.resolve_game_dirs(game)
    return park_unmanaged_dir(base, folder)



def gallery_protection(pools, progress=True):
    """What every published list drawing from these pool folders wants, or
    None if that cannot be known.

    `pools` is a set of folder keys (wj_config.folder_key). Returns
    (want, note, pulled). A None want means the picture is incomplete and the
    caller must not delete on the strength of it.

    `pulled` names the published lists whose authors have taken them down.
    The validator does not walk those, so their report carries an empty
    Archives[] for as long as they stay down: a permanent gap, which would
    disable this feature for good if it blocked, so it is named in the
    confirmation instead. Every OTHER kind of gap - a feed that did not
    answer, a list with no summary row yet, a report that is missing,
    unreadable, or empty for some other reason - closes with a refresh or the
    validator's next run, and so refuses. Before this, all of those were
    lumped in with the pulled lists and described as pulled."""
    try:
        summary = G.load_summary()
    except G.GalleryError as e:
        return None, f"the gallery index could not be read ({e})", []
    feeds, complete = A.load_feeds()
    if not feeds:
        return None, "no author feed could be read", []
    if not complete:
        return None, ("some author feeds did not respond, so the list of "
                      "published modlists is short"), []

    # A hidden list is not a published list as far as this rule is
    # concerned: the user has said it is never going to be installed from
    # here, so it protects nothing and a missing report for it blocks
    # nothing. Named in the note, because it changes what is deleted.
    hidden = sorted(k for k, e in feeds.items()
                    if G.pool_key(e.get("game")) in pools and G.is_hidden(k))
    if hidden:
        drop = set(hidden)
        feeds = {k: e for k, e in feeds.items() if k not in drop}

    keys = [k for k, e in feeds.items() if G.pool_key(e.get("game")) in pools]
    missing = [k for k in keys if k in summary and not G.have_report(k)]
    if missing:
        return None, (f"{len(missing)} of {len(keys)} published list(s) have no "
                      f"cached validation report; run stage 1 and fetch them, "
                      f"or use --gallery --sync-reports"), []

    if progress:
        out(f"  Reading what {len(keys)} published list(s) want ...")
    want, checked, unchecked = G.published_builds(
        set(pools), feeds, summary, A.WJ)
    pulled = sorted(k for k, why in unchecked.items() if why == G.PULLED)
    gaps = sorted((k, why) for k, why in unchecked.items() if why != G.PULLED)
    if gaps:
        shown = "; ".join(f"{k}: {why}" for k, why in gaps[:3])
        return None, (f"{len(gaps)} published list(s) could not be checked and "
                      f"the gap is not permanent ({shown}"
                      f"{' ...' if len(gaps) > 3 else ''}); refresh the "
                      f"reports in stage 1, or wait for the validator's next "
                      f"run"), []
    note = f"{checked} published list(s) consulted"
    if pulled:
        note += (f"; {len(pulled)} pulled by their authors and publishing no "
                 f"archive list ({', '.join(pulled[:3])}"
                 f"{' ...' if len(pulled) > 3 else ''})")
    if hidden:
        note += (f"; {len(hidden)} hidden list(s) ignored ({', '.join(hidden[:3])}"
                 f"{' ...' if len(hidden) > 3 else ''})")
    return want, note, pulled


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


def _still_there(path, size):
    try:
        return os.path.isfile(path) and os.path.getsize(path) == size
    except OSError:
        return False


# xxHash64 by path, for one clearing pass. Cleared at the start of each pass:
# after a rebuild the same path can hold a different file.
_HASH_CACHE = {}


def file_hash(path):
    key = _norm(path)
    if key not in _HASH_CACHE:
        _HASH_CACHE[key] = A.WJ.wabbajack_hash(path)
    return _HASH_CACHE[key]


def _same_content(a, b):
    """Both files read in full and hashed. False without xxhash, so nothing
    is ever called a duplicate on name and size alone when it is about to go."""
    ha = file_hash(a)
    return bool(ha) and ha == file_hash(b)


def duplicate_of(index, master, path, name, size, verify=False):
    """Another copy of this exact build that makes the Master copy redundant,
    or None.

    `index` is the downloader's index of the game's pools - Master, Shared and
    every modlist folder, keyed by canonical name - so "the same build" means
    what it means everywhere else here: the same name, read through any '~N'
    suffix, at the same size. A copy in a stray, excluded or trash folder is
    not in the index and so does not count; that errs towards keeping.

    A Master copy is redundant when such a copy exists (a) anywhere outside
    Master, or (b) inside Master under a better name - the unsuffixed one,
    then the first in name order. The winner is fixed by that rule rather
    than by which file is looked at first, so the outcome does not depend on
    the order the folder is walked in.

    The other copy is re-checked on disk before it counts. With verify=True
    both files are also read in full and their xxHash64 compared, and a pair
    that differs does not count: same name and size with different bytes is
    exactly the case the size model cannot see, and the one place it must
    not be trusted is the moment before a file goes. The estimate that draws
    the table skips the hashing; the clearing pass does not."""
    if size is None:
        return None
    key = A.WJ.canonical_name(name).lower()
    here = _norm(path)
    mnorm = _norm(master)
    outside, inside = [], [(path, here)]
    for p, s in index.get(key) or []:
        if s != size:
            continue
        pn = _norm(p)
        if pn == here:
            continue
        (inside if os.path.dirname(pn) == mnorm else outside).append((p, pn))
    for p, _pn in outside:
        if _still_there(p, size) and (not verify or _same_content(path, p)):
            return p
    inside.sort(key=lambda t: (A.WJ.is_suffixed(os.path.basename(t[0])),
                               os.path.basename(t[0]).lower()))
    best, best_norm = inside[0]
    if (best_norm != here and _still_there(best, size)
            and (not verify or _same_content(path, best))):
        return best
    return None


def keep_days():
    return CFG.TRASH_KEEP_DAYS if CFG else 30


def trash_folder_for(game):
    base, _m, _s = A.WJ.resolve_game_dirs(game)
    return os.path.join(base, CFG.TRASH_FOLDER if CFG else "Trash")


def send_to_trash(path, trash):
    """Move one file into `trash`, stamped with the moment it arrived.

    A rename keeps the file's own mtime, which says when the archive was
    built, not when it was reclaimed. The purge judges by arrival, so the
    stamp is set here. Returns the destination, suffixed if the name was
    taken."""
    os.makedirs(trash, exist_ok=True)
    dst = unique_path(os.path.join(trash, os.path.basename(path)))
    shutil.move(path, dst)
    now = time.time()
    try:
        os.utime(dst, (now, now))
    except OSError:
        pass
    return dst


def master_report(game, records, extra_want=None, label=None):
    """Master's contents for one game, split into kept and reclaimable.

    Master is *meant* to hold only what no present modlist needs, but that is
    only true just after a cleanup pass. Reporting the folder's raw size would
    promise space that stage 5c will then refuse to free, because it re-checks
    every file against the surviving lists' demands. So measure what is
    actually reclaimable and show that.

    `extra_want` adds a second body of demand - in practice everything the
    published gallery asks for - and is measured SEPARATELY rather than merged,
    so the two protection modes can be shown side by side and the difference
    between them is a number rather than a guess.

    A duplicate (see duplicate_of) is reclaimable under both rules, and is
    counted apart in dup_files/dup_bytes so the confirmation can say how much
    of the total is duplication rather than dead weight."""
    keep = wanted_builds(records, game)
    wide = merge_want(keep, extra_want) if extra_want else None
    base, master, _s = A.WJ.resolve_game_dirs(game)
    index = A.WJ.index_existing(base)
    r = {"game": game, "path": master, "bytes": 0, "files": 0,
         "free_bytes": 0, "free_files": 0, "kept_files": 0,
         "dup_bytes": 0, "dup_files": 0,
         "wide_free_bytes": 0, "wide_free_files": 0, "wide_kept_files": 0,
         "wide": wide is not None,
         "label": label or A.WJ.game_label(game),
         "folder": A.WJ.folder_key(game), "games": [game]}
    try:
        with os.scandir(master) as it:
            for x in it:
                try:
                    if not x.is_file() or x.name.lower().endswith(".meta"):
                        continue
                    size = x.stat().st_size
                except OSError:
                    continue
                r["bytes"] += size
                r["files"] += 1
                if duplicate_of(index, master, x.path, x.name, size):
                    r["dup_files"] += 1
                    r["dup_bytes"] += size
                    r["free_files"] += 1
                    r["free_bytes"] += size
                    if wide is not None:
                        r["wide_free_files"] += 1
                        r["wide_free_bytes"] += size
                    continue
                if is_wanted(keep, x.name, size):
                    r["kept_files"] += 1
                else:
                    r["free_bytes"] += size
                    r["free_files"] += 1
                if wide is not None:
                    if is_wanted(wide, x.name, size):
                        r["wide_kept_files"] += 1
                    else:
                        r["wide_free_bytes"] += size
                        r["wide_free_files"] += 1
    except OSError:
        pass
    return r


def merge_want(*maps):
    """Union of { name: sizes | None } maps; None (unstated size) wins."""
    return G.merge_builds(*maps)


def measure_masters(records, extra_want=None):
    """One master_report() per POOL FOLDER the held manifests draw from.

    Grouped by folder rather than by GameType, because two GameTypes can share
    a folder. Measured per GameType, the shared Skyrim Master appeared twice
    with two different answers, and clearing it "for Skyrim Special Edition"
    would have removed what the Skyrim LE lists needed."""
    groups = {}
    for e in records.values():
        g = e.get("game")
        if g:
            groups.setdefault(A.WJ.folder_key(g), set()).add(g)
    out_rows = []
    for fkey in sorted(groups):
        games = sorted(groups[fkey])
        label = " / ".join(A.WJ.game_label(g) for g in games)
        m = master_report(games[0], records, extra_want, label=label)
        m["games"] = games
        out_rows.append(m)
    return out_rows



def unique_path(path):
    """`path` if free, else insert ~1, ~2 ... before the extension."""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{root}~{i}{ext}"):
        i += 1
    return f"{root}~{i}{ext}"


# ----------------------------------------------------------------------
# Shared data gathering
# ----------------------------------------------------------------------
def load_records(force=False):
    """Lightweight parsed manifests, cached. Shows progress on a cold cache."""
    shown = [False]

    def progress(n, total, name):
        if not shown[0]:
            out(f"  Reading {total} manifest(s) not in the cache. This is the "
                f"slow part, and only happens when a manifest is new or has "
                f"changed.")
            shown[0] = True
        out(f"    [{n}/{total}] {name}")

    return A.load_plan_records(progress=progress)


def list_status(entry):
    """What is missing for one list. Returns None if it declares no game."""
    game = entry.get("game")
    if not game:
        return None
    base, _, _ = A.WJ.resolve_game_dirs(game)
    existing = A.WJ.index_existing(base)
    # `unsafe`: archives the manifest names with something other than a plain
    # file name, which the downloader refuses the whole list over.
    st = {"game": game, "present": 0, "gamefile": 0, "other": 0, "stale": 0,
          "nexus": 0, "nexus_bytes": 0, "direct": 0, "direct_bytes": 0,
          "unsafe": int(entry.get("unsafe") or 0)}
    for a in entry["archives"]:
        kind = a.get("kind")
        if kind == "gamefile":
            st["gamefile"] += 1
            continue
        hit, why = A.WJ.present_match(existing, a)
        if hit:
            st["present"] += 1
            continue
        # "absent" and "there but wrong" are both missing, but they are not the
        # same problem: a wrong-size copy is usually a download that was
        # finalised short, and it is the reason a list can look complete for
        # months and then fail in Wabbajack.
        if not why.startswith("absent"):
            st["stale"] += 1
        if kind == "nexus":
            st["nexus"] += 1
            st["nexus_bytes"] += a["size"] or 0
        elif kind in ("http", "wabbajackcdn", "mediafire"):
            st["direct"] += 1
            st["direct_bytes"] += a["size"] or 0
        else:
            st["other"] += 1
    st["missing"] = st["nexus"] + st["direct"]
    st["bytes"] = st["nexus_bytes"] + st["direct_bytes"]
    return st


def stem_of(filename):
    return os.path.splitext(filename)[0]


def fit(values, minimum=4):
    """Width that shows every one of `values` in full.

    Fixed widths meant "Skyrim Special Edition" and "Enderal Special Edition"
    both arrived as "Skyrim Speci", which is not much use when the point of the
    column is telling the games apart. Sizing from the rows costs nothing and
    cannot truncate."""
    return max([len(str(v)) for v in values] + [minimum])


# ----------------------------------------------------------------------
# Stage 1 - browse the published gallery
# ----------------------------------------------------------------------
PAGE = 20


def ask_row(count, extra, search=False):
    """Prompt for a row number, or one of `extra` single-letter actions.

    Returns an int index (0-based) or the letter. Numbers outside the table
    are refused rather than clamped, for the same reason parse_selection()
    refuses them: acting on a different row than the one typed is worse than
    asking again. With `search`, a line starting with '/' is returned as it
    was typed, '/' included: the words after it are a search, and '/' alone
    clears one."""
    out()
    out("   " + "   ".join(list(extra) + (["[/words] search"] if search else [])
                           + ["[q] quit"]))
    while True:
        try:
            raw = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            out()
            raise Quit()
        if raw in ("q", "quit", "exit"):
            raise Quit()
        if search and raw.startswith("/"):
            return raw
        for letter in extra:
            if raw == letter[1:2]:
                return letter[1:2]
        # Every other stage uses 's' to step back out, so accept it as a
        # synonym rather than making this one screen the exception.
        if raw in ("s", "skip") and any(l[1:2] == "b" for l in extra):
            return "b"
        if raw.isdigit() and 1 <= int(raw) <= count:
            return int(raw) - 1
        out(f"  Enter a number between 1 and {count}, or one of "
            f"{'/'.join(l[1:2] for l in extra)}/q.")


def gallery_indexes():
    """Per-game pool index, built once per run.

    index_existing() walks every pool folder for a game, which is 28 seconds
    for the 15,000-archive Skyrim pool. Browsing 115 Skyrim lists must not pay
    that 115 times."""
    return {}


def evaluate_rows(rows, pool_dir, indexes, label):
    """Fill in each row's gap against the pools. Shows progress; it is slow.

    `pool_dir` is None for a game with no pool on the share. Nothing is owned
    yet, so the gap is measured against an empty index: every row then reads
    as 0% held, which is the honest answer and is what makes the full download
    size of an unfamiliar game visible before committing to it."""
    if pool_dir is None:
        existing = {}
    else:
        key = os.path.normcase(pool_dir)
        if key not in indexes:
            out(f"  Indexing {pool_dir} ...")
            indexes[key] = A.WJ.index_existing(pool_dir)
            out(f"    {len(indexes[key])} distinct archive name(s) on disk.")
        existing = indexes[key]

    todo = [r for r in rows if "gap" not in r]
    if todo:
        out(f"  Reading {len(todo)} validation report(s) for {label} ...")
    for i, r in enumerate(todo, 1):
        rep = G.load_report(r["key"])
        r["report_status"] = G.report_status(rep) if rep is not None else None
        if rep is None or G.report_is_empty(rep):
            r["gap"] = None
        else:
            r["gap"] = G.evaluate(G.report_archives(rep, A.WJ), existing, A.WJ)
        if todo and (i % 25 == 0 or i == len(todo)):
            out(f"    [{i}/{len(todo)}]")
    return rows


def gap_sort_key(row):
    """Most-complete first; lists with no published report last.

    A missing report is not 0% - it is unknown - so it sorts to the bottom
    rather than pretending to be the worst case."""
    g = row.get("gap")
    if not g or not g["total"]:
        return (1, 0.0, row["title"].lower())
    return (0, -g["percent"], row["title"].lower())


def set_hide_nsfw(hide):
    """HIDE_NSFW, saved through the call the Settings tab uses, so the
    window's box and the console's [n] are one setting."""
    if CFG is None:
        return False
    values = CFG.current_settings()
    values["HIDE_NSFW"] = bool(hide)
    CFG.save_settings(values)
    A.reload_settings()
    return bool(CFG.HIDE_NSFW)


def show_gallery_table(rows, label, shown, hidden=None, search="", nsfw=0):
    """The browse table for one game, most-complete first.

    `hidden` is (blocked, unknown): how many lists the 'only lists that can
    be completed' rule kept out of this table, and why, so the table never
    reads as the whole gallery when it is not. `search` and `nsfw` say the
    same of a search and of HIDE_NSFW."""
    out()
    rule("-")
    out(f"  {label.upper()}   {len(rows)} list(s), most complete first"
        + (f", matching '{search}'" if search else ""))
    rule("-")
    nw = fit([r["title"] for r in rows[:shown]], 7) + 2
    vw = fit([r["version"] for r in rows[:shown]], 7) + 2
    out(f"     {'#':>3} own {'modlist':<{nw}}{'version':<{vw}}"
        f"{'arch':>6}{'yours':>8}{'to fetch':>11}{'total':>11}  {'updated':<10}")
    for i, r in enumerate(rows[:shown], 1):
        g = r.get("gap")
        if G.completable(r) is False:
            mark = "x"
        elif r.get("nsfw"):
            mark = "n"
        else:
            mark = "!" if G.unavailable(r) else (">" if r["retired"] else " ")
        own = "[x]" if r["have"] else "[ ]"
        if g and g["total"]:
            tail = (f"{g['total']:>6}{g['percent']:>7.1f}%"
                    f"{human(g['missing_bytes']):>11}"
                    f"{human(g['total_bytes']):>11}")
        else:
            n = r["archives"] or 0
            tail = f"{n:>6}{'-':>8}{'-':>11}{human(r['arch_size'] or 0):>11}"
        out(f"   {mark} {i:>3} {own} {r['title'][:nw - 2]:<{nw}}"
            f"{r['version'][:vw - 2]:<{vw}}{tail}  {r.get('updated') or '':<10}")
    if len(rows) > shown:
        out(f"         ... {len(rows) - shown} more not shown")
    out()
    bits = []
    if any(r.get("nsfw") and G.completable(r) is not False for r in rows[:shown]):
        bits.append("n  marked NSFW by its author")
    if nsfw:
        bits.append(f"{nsfw} list(s) marked NSFW are hidden. [n] shows them.")
    if any(G.completable(r) is False for r in rows[:shown]):
        bits.append("x  cannot be completed: a dead archive is not in your pools")
    if any(G.unavailable(r) and G.completable(r) is not False for r in rows[:shown]):
        bits.append("!  flagged Unavailable by Wabbajack")
    if any(r["retired"] for r in rows[:shown]):
        bits.append(">  retired from the gallery")
    if any(not r.get("gap") for r in rows[:shown]):
        bits.append("-  not checked upstream, so the overlap is unknown")
    if hidden and any(hidden):
        blocked, unknown = hidden
        kept = []
        if blocked:
            kept.append(f"{blocked} that cannot be completed (a dead archive not in "
                        f"your pools)")
        if unknown:
            kept.append(f"{unknown} unmeasurable (retired, pulled, or no report)")
        bits.append("not shown: " + "; ".join(kept) + ". [c] shows every list.")
    if bits:
        for b in bits:
            out("   " + b)


def show_gallery_detail(state, row):
    """One list in full, and the offer to fetch its manifest."""
    g = row.get("gap")
    out()
    rule("=")
    out(f"  {row['title']}   v{row['version']}")
    out(f"  {row['key']}      {A.WJ.game_label(row['game'])}")
    rule("=")

    if G.unavailable(row):
        out("  UNAVAILABLE in the Wabbajack gallery")
        for why in G.why_unavailable(row):
            out(f"    - {why}")
        out()
        out("  That state only greys out the Install button on Wabbajack's own")
        out("  gallery tile. The .wabbajack stays on the CDN, and the CLI")
        out("  installer this tooling drives never reads the flag - so it is a")
        out("  warning here, not a wall.")
        out()
    if row["retired"]:
        out("  RETIRED from the gallery. Its download link still works, but")
        out("  there is no feed behind it, so its version can never be")
        out("  tracked or updated by this tooling.")
        out()

    if g is None:
        st = row.get("report_status")
        if st is None:
            out("  No validation report is published for this list.")
        elif st == "ForcedDown":
            out("  Its report exists but carries no archives: the validator")
            out("  does not walk a list whose author has pulled it. The zeros")
            out("  upstream mean NOT CHECKED, not 'none of it works'.")
        else:
            out(f"  Its report is present but lists no archives (status "
                f"{st}).")
        out("  So how much of it you already hold cannot be worked out without")
        out("  downloading the .wabbajack itself, which is the offer below.")
        if row["archives"]:
            out(f"  Its feed claims {row['archives']} archive(s), "
                f"{human(row['arch_size'] or 0)}.")
    else:
        out(f"  archives           {g['total']:>7}")
        out(f"  already yours      {g['present']:>7}   ({g['percent']:.1f}%)")
        out(f"  still to fetch     {g['missing']:>7}   {human(g['missing_bytes'])}")
        if g["gamefile"]:
            out(f"  from the game      {g['gamefile']:>7}   "
                f"supplied by your install, not downloaded")
        out(f"  list total                 {human(g['total_bytes'])}")

        dead = g["dead_have"] + g["dead_need"]
        if dead:
            out()
            out(f"  {len(dead)} archive(s) the validator can no longer download:")
            if g["dead_have"]:
                out("    already in your pools, so they cost you nothing:")
                for a in g["dead_have"][:10]:
                    out(f"       {a['name'][:52]:<52}{human(a['size'] or 0):>11}")
                if len(g["dead_have"]) > 10:
                    out(f"       ... and {len(g['dead_have']) - 10} more")
            if g["dead_need"]:
                out("    NOT on disk - these are what would stop the install:")
                for a in g["dead_need"][:10]:
                    out(f"       {a['name'][:52]:<52}{human(a['size'] or 0):>11}")
                if len(g["dead_need"]) > 10:
                    out(f"       ... and {len(g['dead_need']) - 10} more")
            out()
            if not g["dead_need"]:
                out("    You already hold every one of them. Nothing here blocks")
                out("    this install.")
            else:
                out(f"    {len(g['dead_need'])} of {len(dead)} is unobtainable and "
                    f"not on disk.")

    ent = row.get("entry")
    out()
    if row["have"]:
        out("  You already track this list. Stage 2 handles its updates, and a")
        out("  tracked list cannot be hidden.")
        ask("   [b] back", ("b",))
        return
    if not ent or not ent.get("download"):
        out("  Its feed carries no download link, so there is nothing to fetch.")
        a = ask("   [h] hide this list from the gallery   [b] back", ("h", "b"))
    else:
        out(f"  The .wabbajack itself is {human(ent.get('size') or 0)}. That is the")
        out("  manifest alone; the archives listed above are a separate job that")
        out("  stage 3 does.")
        a = ask("   [d] download the .wabbajack and start tracking it   "
                "[h] hide this list   [b] back", ("d", "h", "b"))
    if a == "b":
        return
    if a == "h":
        set_list_hidden(row["key"], True)
        row["hidden"] = True
        out(f"  {row['title']} is hidden: out of every stage, its report never")
        out("  fetched, and its archives unprotected when Master is cleared.")
        out("  [h] in the games menu lists hidden lists and brings one back.")
        return
    adopt_gallery_list(state, row)


def set_list_hidden(key, hidden):
    """Flag one published list as hidden, or clear the flag, and save it.

    Goes through the same save the Settings tab uses, so HIDDEN in
    wj_settings.json is the one record and the console and the window agree
    from their next read. Returns the new HIDDEN list."""
    if CFG is None:
        return []
    low = str(key).strip().lower()
    kept = [k for k in CFG.HIDDEN if str(k).strip().lower() != low]
    if hidden:
        kept.append(str(key))
    values = CFG.current_settings()
    values["HIDDEN"] = kept
    CFG.save_settings(values)
    A.reload_settings()
    return kept


def show_hidden_lists(hidden):
    """The lists hidden from the gallery, with the offer to unhide one.

    Shown with the feeds' own figures only: a hidden list is never measured
    against the pools, that being most of what hiding it saves."""
    hidden.sort(key=lambda r: (G.game_label(r["game"]).lower(), r["title"].lower()))
    while hidden:
        out()
        rule("-")
        out(f"  HIDDEN   {len(hidden)} list(s) kept out of every stage")
        rule("-")
        nw = fit([r["title"] for r in hidden], 7) + 2
        gw = fit([G.game_label(r["game"]) for r in hidden], 4) + 2
        out(f"     {'#':>3} {'modlist':<{nw}}{'game':<{gw}}{'version':<10}"
            f"{'arch':>6}{'total':>11}")
        for i, r in enumerate(hidden, 1):
            out(f"     {i:>3} {r['title'][:nw - 2]:<{nw}}"
                f"{G.game_label(r['game'])[:gw - 2]:<{gw}}{r['version'][:9]:<10}"
                f"{r['archives'] or 0:>6}{human(r['arch_size'] or 0):>11}")
        out()
        out("  A number unhides that list.")
        pick = ask_row(len(hidden), ["[b] back"])
        if pick == "b":
            return
        r = hidden.pop(pick)
        set_list_hidden(r["key"], False)
        r["hidden"] = False
        out(f"  {r['title']} is back in the gallery.")
    out("  Nothing is hidden now.")


def existing_pool_folder(folder):
    """The folder on the share matching this name case-insensitively, or None.

    The share is the authority on its own spelling. Creating 'Fallout4' beside
    an existing 'fallout4' would split one game's archives across two pools,
    and each pool would then read the other's contents as not present."""
    if CFG is None:
        return None
    root = CFG.resolved_archive_root()
    want = os.path.normcase(folder)
    try:
        for n in os.listdir(root):
            if os.path.normcase(n) == want and os.path.isdir(os.path.join(root, n)):
                return os.path.join(root, n)
    except OSError:
        pass
    return None


def pool_folder_problem(game, folder):
    """Why this game cannot get this pool folder, or None if it can."""
    if CFG is None:
        return "wj_config is not importable, so the pool layout is unknown."
    if not CFG.valid_game_type(game):
        return f"{game!r} is not a usable GameType."
    why = CFG.plain_name_problem(folder) if folder else "it is empty"
    if why:
        return f"{folder!r} cannot be a folder name: {why}."
    if folder.lower() in CFG.reserved_list_names():
        return (f"{folder!r} is reserved. A game folder by that name would "
                "collide with a pool folder inside every game.")
    why = CFG.folder_setting_problem(folder)
    if why:
        return f"{folder!r} cannot be a folder name: {why}."
    return None


def create_game_pool(game, folder=None):
    """Lay out the standard pool for a game that has none yet.

    Creates <archive root>/<folder>/ with the same Master, Shared, Unmanaged
    and Trash the other games have, so the sorter, the downloader and stage 5
    all recognise it without any further setup.

    `folder` defaults to the folder the config already maps this game onto,
    which for an unmapped game is the GameType itself. Passing a different
    folder folds the game into an existing pool the way Skyrim VR folds into
    Skyrim, and the mapping is written to the settings file so that every
    later run, and every other tool in the suite, agrees about it.

    Returns (path, created, mapped): whether the tree was made here, and
    whether a GAME_FOLDERS entry had to be written."""
    if CFG is None:
        raise ValueError("wj_config is not importable, so the pool layout is unknown.")
    folder = folder or CFG.game_folder(game)
    problem = pool_folder_problem(game, folder)
    if problem:
        raise ValueError(problem)

    root = CFG.resolved_archive_root()
    if not os.path.isdir(root):
        raise ValueError(f"The archive root is not there: {root}")

    path = existing_pool_folder(folder) or os.path.join(root, folder)
    created = not os.path.isdir(path)

    made = []
    for sub in tuple(CFG.POOL_FOLDERS) + (CFG.UNMANAGED_FOLDER, CFG.TRASH_FOLDER):
        p = os.path.join(path, sub)
        if not os.path.isdir(p):
            made.append(sub)
        os.makedirs(p, exist_ok=True)

    # Only write a mapping when the folder is not simply the game's own name.
    # Recording the identity mapping would bloat the settings file with a line
    # that changes nothing.
    mapped = False
    if os.path.normcase(folder) != os.path.normcase(str(game)):
        values = CFG.current_settings()
        mapping = dict(values.get("GAME_FOLDERS") or {})
        if mapping.get(game) != folder:
            mapping[game] = folder
            values["GAME_FOLDERS"] = mapping
            CFG.save_settings(values)
            A.reload_settings()
            mapped = True

    out(f"  Pool ready: {path}")
    if made:
        out(f"    created {', '.join(made)}")
    else:
        out("    every folder was already there; nothing changed.")
    if mapped:
        out(f"    {CFG.SETTINGS_FILE} now maps {game} onto {folder}, so its lists")
        out(f"    draw from and protect that one pool.")
    return path, created, mapped


def offer_game_pool(entry):
    """Console prompt: lay out a pool for a game that has none yet.

    `entry` is the (label, pool_dir, GameType) triple the browse stage keeps
    per game."""
    label, pool, game = entry
    out()
    if pool:
        out(f"  {label} already draws from {pool}.")
        return
    default = CFG.game_folder(game) if CFG else game
    unmanaged = CFG.UNMANAGED_FOLDER if CFG else "Unmanaged"
    trash = CFG.TRASH_FOLDER if CFG else "Trash"
    out(f"  {label} has no pool. One folder under the archive root, laid out")
    out(f"  with the same Master, Shared, {unmanaged} and {trash} every other")
    out("  game here has.")
    out()
    out(f"   [enter] call it {default}, and give this game its own pool")
    out("   or type the name of a pool you already keep, to fold this game into")
    out("   it the way Skyrim VR draws from Skyrim: one Master between them, and")
    out("   each list protecting the other's archives.")
    out("   [b] back")
    try:
        raw = input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        out()
        raise Quit()
    if raw.lower() == "b":
        return
    folder = raw or default
    problem = pool_folder_problem(game, folder)
    if problem:
        out(f"  {problem}")
        return
    try:
        create_game_pool(game, folder)
    except (OSError, ValueError) as e:
        out(f"  [ERROR] The pool could not be made: {e}")


def adopt_gallery_list(state, row):
    """Fetch a new list's manifest and record it as tracked.

    Writes the same state shape the update stage does, so from the next run
    on this list is indistinguishable from one that was always followed."""
    ent = row["entry"]
    filename = A.manifest_filename_for(row["key"], ent)
    out()
    out(f"  Downloading {filename} ...")
    if not A.fetch_manifest(ent, filename):
        out("  The manifest download failed. Nothing was recorded, so this can")
        out("  simply be tried again.")
        return False
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state.setdefault("lists", {})[row["key"]] = {
        "version": ent.get("version") or "",
        "hash": ent.get("hash"),
        "file": filename,
        "title": ent.get("title") or row["title"],
        "updated": ent.get("updated") or "",
        "applied": now,
        # Its archives have certainly not been fetched yet, and this is the
        # flag stage 3 keys off.
        "needs_archives": True,
    }
    if row["entry"].get("repo"):
        state.setdefault("map", {})[filename.lower()] = row["key"]
    A.save_state(state)
    # Its folder is legitimate from now on. Without this, stage 3's download
    # into that folder read back as "still missing", because the index
    # treated a folder it had never heard of as a stray.
    refresh_known_dirs()
    row["have"] = True
    out(f"  Tracked. {filename} is in {A.MANIFESTS}.")
    out("  Stage 3 will offer to fetch its archives, and stage 4 to stage it")
    out("  for Wabbajack.")
    return True


def stage_browse(state, tracked):
    stage_header(1, "BROWSE THE PUBLISHED GALLERY")
    out("  Every modlist published for a game you keep archives for, and how")
    out("  much of each one is already in your pools. This reads Wabbajack's")
    out("  own validation reports, so the answer arrives before committing to")
    out("  a multi-gigabyte .wabbajack rather than after. A game's table shows")
    out("  only the lists you could finish - every archive held or still")
    out("  obtainable, or a list you track - until [c] says otherwise.")
    out()

    try:
        summary = G.load_summary()
        featured = G.load_featured()
        disabled = G.load_disabled()
    except G.GalleryError as e:
        out(f"  The gallery index could not be read ({e}).")
        out("  Skipping this stage; nothing else depends on it.")
        return
    feeds, complete = A.load_feeds()
    if not feeds:
        out("  No feed could be read, so there is no gallery to show.")
        return
    if not complete:
        out("  [WARN] Some author feeds did not respond, so this gallery is")
        out("  incomplete. Nothing is concluded from an absence here.")

    pooled = G.pooled_games({e.get("game") for e in feeds.values()})
    show_all = False
    if not pooled:
        out("  None of the published games has a pool folder on the share, so")
        out("  every game below is one you would be starting from scratch.")
        show_all = True

    def build_gallery(every):
        """Rows for the games in view, one entry per game among them, and
        the hidden rows kept apart.

        A game with no pool folder carries None for its directory. That is
        what tells the rest of this stage to measure it against nothing, and
        to offer to lay a pool out for it. The hidden rows are gathered from
        every game whatever the filter: they are shown only in their own
        view, and that view is where a flag is taken off again."""
        every_row = G.build_rows(feeds, summary, featured, disabled,
                                 set(tracked))
        hid = [r for r in every_row if r["hidden"]]
        rs = [r for r in every_row
              if not r["hidden"] and (every or r["game_key"] in pooled)]
        inf = {}
        for r in rs:
            gk = r["game_key"]
            if gk not in inf:
                inf[gk] = (G.game_label(r["game"]),
                           (pooled.get(gk) or (None, None))[1], r["game"])
        return rs, inf, hid

    rows, info, hidden = build_gallery(show_all)

    def say_counts(rs, inf, every, hid):
        retired = sum(1 for r in rs if r["retired"])
        out(f"  {len(feeds)} published list(s)"
            + (f", plus {retired} retired one(s) still listed" if retired else "")
            + ".")
        out(f"  Showing {len(rs)} across {len(inf)} game(s)"
            + (", every game the gallery covers" if every
               else ", the one(s) you keep pools for")
            + f". You already track {sum(1 for r in rs if r['have'])}."
            + (f" {len(hid)} hidden." if hid else ""))

    say_counts(rows, info, show_all, hidden)

    size, files = G.cache_size()
    have_reports = sum(1 for r in rows if G.have_report(r["key"]))
    out()
    out(f"  Report cache: {files} file(s), {human(size)}; "
        f"{have_reports} of {len(rows)} rows covered.")
    if have_reports < len(rows):
        out("  A full sweep is 224 reports, about 45 MB and under a minute.")
        a = ask("   [f] fetch the missing reports   [c] carry on with what is "
                "cached", ("f", "c"))
        if a == "f":
            sync_gallery_reports([r["key"] for r in rows if not r["retired"]])
    else:
        a = ask("   [r] refresh every report   [c] carry on with the cache",
                ("r", "c"))
        if a == "r":
            sync_gallery_reports([r["key"] for r in rows if not r["retired"]],
                                 force=True)

    indexes = gallery_indexes()
    only_completable = True
    while True:
        # Pools you keep first, then games you do not, each by list count.
        games = sorted(info, key=lambda k: (
            info[k][1] is None, -sum(1 for r in rows if r["game_key"] == k), k))
        out()
        gw = fit([info[k][0] for k in games], 4) + 2
        out(f"    {'#':>3}  {'game':<{gw}}{'lists':>7}{'yours':>7}  pool")
        for i, k in enumerate(games, 1):
            grp = [r for r in rows if r["game_key"] == k]
            out(f"    {i:>3}  {info[k][0]:<{gw}}{len(grp):>7}"
                f"{sum(1 for r in grp if r['have']):>7}"
                f"  {os.path.basename(info[k][1]) if info[k][1] else '-'}")
        out()
        out("   [1-%d] browse a game   [a] %s   [p N] set up a pool for game N"
            % (len(games), "only my games" if show_all else "show every game"))
        if hidden:
            out(f"   [h] the {len(hidden)} hidden list(s), and unhide")
        out("   [s] skip this stage   [q] quit")
        try:
            raw = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            out()
            raise Quit()
        if raw in ("q", "quit", "exit"):
            raise Quit()
        if raw in ("s", "skip", ""):
            out("  Skipped.")
            return
        if raw == "a":
            show_all = not show_all
            rows, info, hidden = build_gallery(show_all)
            out()
            say_counts(rows, info, show_all, hidden)
            continue
        if raw == "h":
            if not hidden:
                out("  Nothing is hidden.")
                continue
            show_hidden_lists(hidden)
            rows, info, hidden = build_gallery(show_all)
            continue
        if raw.startswith("p"):
            arg = raw[1:].strip()
            if not (arg.isdigit() and 1 <= int(arg) <= len(games)):
                out(f"  Use 'p N', where N is between 1 and {len(games)}.")
                continue
            offer_game_pool(info[games[int(arg) - 1]])
            pooled = G.pooled_games({e.get("game") for e in feeds.values()})
            rows, info, hidden = build_gallery(show_all)
            continue
        if not (raw.isdigit() and 1 <= int(raw) <= len(games)):
            out(f"  Enter a number between 1 and {len(games)}, a/p N, or s/q.")
            continue

        gk = games[int(raw) - 1]
        label, pool, _game = info[gk]
        measured = [r for r in rows if r["game_key"] == gk]
        evaluate_rows(measured, pool, indexes, label)
        measured.sort(key=gap_sort_key)

        shown = min(PAGE, len(measured))
        search = ""
        while True:
            # Only lists that could be finished, unless [c] has said
            # otherwise: a tracked list, or one whose every archive is held
            # or still obtainable. The rest are counted under the table.
            if only_completable:
                grp, blocked, unknown = G.split_completable(measured)
                kept_out = (len(blocked), len(unknown))
            else:
                grp, kept_out = list(measured), None
            hide_nsfw = bool(CFG.HIDE_NSFW) if CFG else False
            grp, nsfw = G.browse_filter(grp, search, hide_nsfw)
            show_gallery_table(grp, label, shown, hidden=kept_out, search=search, nsfw=nsfw)
            extra = ["[m] more"] if len(grp) > shown else []
            extra.append("[c] " + ("show every list" if only_completable
                                   else "only lists that can be completed"))
            extra.append("[n] " + ("show NSFW lists" if hide_nsfw else "hide NSFW lists"))
            extra.append("[b] back to the games")
            pick = ask_row(len(grp), extra, search=True)
            if isinstance(pick, str) and pick.startswith("/"):
                # Words after the slash narrow the table; a slash alone
                # clears the search.
                search = pick[1:].strip()
                shown = min(PAGE, len(measured))
                continue
            if pick == "n":
                set_hide_nsfw(not hide_nsfw)
                continue
            if pick == "b":
                break
            if pick == "m":
                shown = min(len(grp), shown + PAGE)
                continue
            if pick == "c":
                only_completable = not only_completable
                shown = min(PAGE, len(measured))
                continue
            show_gallery_detail(state, grp[pick])
            if grp[pick].get("hidden"):
                # Hidden from the detail screen: out of this table, out of
                # the games menu's counts, into the hidden view.
                gone = grp[pick]
                measured.remove(gone)
                hidden.append(gone)
                rows = [r for r in rows if not r.get("hidden")]
                if not measured:
                    break
                shown = min(shown, len(measured))


def sync_gallery_reports(keys, force=False):
    """Pull the per-list reports into the cache, with progress."""
    out()
    out(f"  Fetching validation reports ({len(keys)} list(s)) ...")
    last = [0.0]

    def progress(done, total, cached, key):
        now = datetime.now(timezone.utc).timestamp()
        if total and (done == total or now - last[0] > 1.5):
            last[0] = now
            out(f"    [{done}/{total}] {(key or '')[:56]}")

    fetched, cached, failed = G.sync_reports(keys, progress=progress,
                                             force=force)
    size, files = G.cache_size()
    out(f"  {fetched} fetched, {cached} already cached, {len(failed)} without "
        f"a published report.")
    out(f"  Cache is now {human(size)} across {files} file(s).")
    if failed:
        out("  No report published for:")
        for k in failed[:8]:
            out(f"    {k}")
        if len(failed) > 8:
            out(f"    ... and {len(failed) - 8} more")


# ----------------------------------------------------------------------
# Stage 2 - updates
# ----------------------------------------------------------------------
def stage_updates(state, tracked):
    stage_header(2, "MODLIST UPDATES")
    seen, pending, resume = A.pending_updates(state, tracked)
    if seen is None:
        out("  Could not read the published feeds. Skipping this stage.")
        return False

    updates = [(k, e, p, f) for k, e, p, f, changed, _d in seen if changed]
    current = [(k, e, p, f) for k, e, p, f, changed, _d in seen if not changed]

    out()
    out(f"  UPDATE AVAILABLE ({len(updates)})")
    if not updates:
        out("    none - every tracked list is at its published version.")
    else:
        out(f"    {'#':>3}  {'modlist':<44}{'have':>10} -> {'published':<12}{'manifest':>10}")
        for i, (_k, e, p, f) in enumerate(updates, 1):
            have = p.get("version") or "absent"
            out(f"    {i:>3}  {stem_of(f)[:43]:<44}{have[:10]:>10} -> "
                f"{e['version'][:12]:<12}{human(e['size']):>10}")

    out()
    out(f"  UP TO DATE ({len(current)})")
    for _k, e, _p, f in current:
        out(f"         {stem_of(f)[:52]:<53} {e['version']}")

    if not updates:
        out()
        out("  Nothing to do in this stage.")
        return False

    picks = choose(len(updates), "modlists")
    if picks is None:
        out("  Skipped. No manifest fetched.")
        return False

    want = [updates[i] for i in picks]
    need = sum(e["size"] or 0 for _k, e, _p, _f in want)
    out()
    out(f"  Fetching {len(want)} manifest(s), {human(need)}.")
    if not A.free_space_ok(A.MANIFESTS, need):
        out("  Not enough free space. Skipping this stage.")
        return False

    done = fetch_updates(state, want)
    out()
    out(f"  {done} of {len(want)} manifest(s) updated.")
    return done > 0


# ----------------------------------------------------------------------
# The work each stage does, apart from the asking. Both front ends - this
# console wizard and the desktop window - call these, so they cannot drift.
# ----------------------------------------------------------------------
def fetch_updates(state, want):
    """Fetch each (key, entry, prev, filename) in `want`. Returns how many
    landed. State is saved after each, so a crash loses nothing done."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    done = 0
    for key, ent, prev, filename in want:
        out()
        out(f"  {stem_of(filename)}  {prev.get('version') or 'absent'} -> {ent['version']}")
        if A.fetch_manifest(ent, filename):
            state.setdefault("lists", {})[key] = {
                "version": ent["version"], "hash": ent["hash"],
                "file": filename, "title": ent["title"],
                "updated": ent["updated"], "applied": now,
                # A new manifest almost always wants new archives. Stage 3
                # measures it properly; this only ensures it is looked at.
                "needs_archives": True,
            }
            A.save_state(state)
            done += 1
        else:
            out("    Failed. Your existing copy is untouched.")
    return done


def download_list(records, fn, st, assist):
    """Run the downloader for one list. Returns (ran, rc, status_after)."""
    base, _, _ = A.WJ.resolve_game_dirs(st["game"])
    if not A.free_space_ok(base, st["bytes"]):
        out("  Not enough free space for this list. Skipping it.")
        return False, None, st
    flag = ["--nxm-assist"] if assist else ["--skip-nexus"]
    if A.DOWNLOAD_ORDER:
        flag += ["--order", A.DOWNLOAD_ORDER]
    rc = A.run_script(A.DOWNLOADER_SCRIPT,
                      ["--game", st["game"], "--list", stem_of(fn), "--yes"] + flag,
                      "downloader", interactive=assist)
    A.WJ.invalidate_existing_cache()
    after = list_status(records[fn]) or st
    if after["missing"]:
        out(f"  {after['missing']} archive(s) still missing "
            f"({human(after['bytes'])}). Re-run to continue.")
    else:
        out("  Complete.")
    if rc not in (0, 1):
        out(f"  The downloader exited {rc}; see the log.")
    return True, rc, after


def stage_list(state, fn, st):
    """Run the sorter to gather one list, and remember that it is staged.
    Returns the sorter's exit code. The caller checks for Wabbajack first."""
    rc = A.run_script(A.SORTER_SCRIPT,
                      ["--game", st["game"], "--list", stem_of(fn), "--yes"],
                      "sorter")
    out(f"  Sorter exited {rc}.")
    if rc == 0 and state is not None:
        # Remember which list this folder's pools are arranged for, so stage
        # 5 can say so before a rebuild undoes it.
        state.setdefault("staged", {})[A.WJ.folder_key(st["game"])] = stem_of(fn)
        A.save_state(state)
        A.WJ.invalidate_existing_cache()
    return rc


def delete_modlists(state, records, chosen):
    """Delete the chosen modlists (rows from gather_space), then rebuild each
    affected game's pools. Returns (failed_rebuilds, archives_moved,
    manifest_bytes_freed). The caller confirms, and checks for Wabbajack."""
    folders = {}
    for x in chosen:
        if x["game"]:
            folders.setdefault(A.WJ.folder_key(x["game"]), x["game"])
    # What the SURVIVING lists want, so that a build one of them needs
    # keeps its name when it lands in Master beside a different build.
    survivors = {fn: e for fn, e in records.items()
                 if fn not in {x["file"] for x in chosen}}
    total_moved = total_freed = 0
    for x in chosen:
        out(f"  Deleting {stem_of(x['file'])}...")
        keep = wanted_builds(survivors, x["game"]) if x["game"] else None
        moved, redundant, freed = delete_modlist(state, x, keep=keep)
        total_moved += moved
        total_freed += freed
        out(f"    {moved} archive(s) moved to Master, "
            f"{redundant} redundant copy(ies) removed, "
            f"manifest freed {human(freed)}")
    A.save_state(state)
    refresh_known_dirs()
    out(f"  Manifests freed {human(total_freed)}; "
        f"{total_moved} archive(s) now sit in Master.")

    # Rebuild the pools so Master holds only what nothing needs. Without
    # this, 5c would be choosing from a Master that still contains other
    # lists' archives. A rebuild that fails leaves exactly that Master, so
    # it is remembered and 5c refuses to offer it.
    failed_rebuilds = {}
    for fkey, game in sorted(folders.items()):
        out(f"  Rebuilding {A.WJ.game_label(game)} pools so nothing still "
            f"needed is left in Master...")
        rc = A.run_script(A.SORTER_SCRIPT,
                          ["--game", game, "--cleanup-only", "--yes"],
                          "sorter")
        (state.get("staged") or {}).pop(fkey, None)
        if rc != 0:
            failed_rebuilds[fkey] = rc
            out(f"  The rebuild for {A.WJ.game_label(game)} FAILED (sorter "
                f"exited {rc}). Its Master may still hold archives other")
            out("  lists need, so it will not be offered for clearing in "
                "5c. See the sorter log.")
    A.save_state(state)
    A.WJ.invalidate_existing_cache()
    return failed_rebuilds, total_moved, total_freed


def master_refusals(records, failed_rebuilds):
    """Why stage 5c must not offer some, or any, Master.

    Returns (unreadable, blocked): the unreadable manifests, which disarm
    every Master because they do not even say which game they belong to; and
    {folder key: reason} for folders whose demand cannot be trusted or whose
    rebuild failed."""
    unreadable, empty = demand_problems(records)
    blocked = {}
    for fkey, rc in failed_rebuilds.items():
        blocked[fkey] = (f"its rebuild failed (sorter exited {rc}), so Master "
                         f"may still hold archives other lists need")
    for fkey, fns in empty.items():
        blocked.setdefault(fkey, (f"{', '.join(fns)} lists no archives at "
                                  f"all, which no real modlist does, so its "
                                  f"demand is unknown"))
    return unreadable, blocked


def clear_masters(records, chosen, mode, wide_want):
    """Clear Master for each chosen master row, under `mode` ("y" or "a").
    Returns the bytes moved to the trash. The caller confirms, and checks
    for Wabbajack."""
    rule_name = "any known modlist" if mode == "a" else "your modlists only"
    grand = 0
    for m in chosen:
        game = m["game"]
        # Recomputed here rather than reused from the table: a cleanup pass
        # may have moved files since, and the whole point of this map is
        # that it is current.
        keep = wanted_builds(records, game)
        if mode == "a":
            keep = merge_want(keep, wide_want)
        out(f"  Clearing Master for {m['label']} "
            f"({len(keep)} archive name(s) wanted and protected)...")
        record = DeletionRecord(game, rule_name, m["path"])
        deleted, kept, freed = clear_master(game, keep, on_delete=record.note)
        rec = record.close()
        grand += freed
        out(f"    {len(deleted)} file(s) moved to Trash ({human(freed)}); "
            f"kept {kept} still wanted")
        if rec:
            out(f"    record: {os.path.basename(rec)}")
    return grand


# ----------------------------------------------------------------------
# Stage 3 - incomplete modlists
# ----------------------------------------------------------------------
def stage_incomplete(records, assist):
    stage_header(3, "INCOMPLETE MODLISTS")
    out("  Checking what is on disk...")
    A.WJ.invalidate_existing_cache()

    rows, complete = [], []
    for fn in sorted(records):
        st = list_status(records[fn])
        if st is None:
            continue
        (rows if st["missing"] else complete).append((fn, st))
    rows.sort(key=lambda r: -r[1]["bytes"])
    stale = prune_stale_csvs(records, dict(rows + complete))
    if stale:
        out(f"  Removed {len(stale)} stale to-do CSV(s) for lists that are "
            f"complete or gone.")

    out()
    out(f"  INCOMPLETE ({len(rows)})")
    if not rows:
        out("    none - every modlist has all the archives this tool can fetch.")
    else:
        nw = fit([stem_of(fn) for fn, _s in rows], 7) + 2
        gw = fit([A.WJ.game_label(s["game"]) for _f, s in rows], 4) + 2
        out(f"    {'#':>3}  {'modlist':<{nw}}{'game':<{gw}}{'absent':>7}"
            f"{'wrong sz':>9}{'size':>10}{'nexus':>7}")
        for i, (fn, st) in enumerate(rows, 1):
            out(f"    {i:>3}  {stem_of(fn):<{nw}}"
                f"{A.WJ.game_label(st['game']):<{gw}}"
                f"{st['missing'] - st['stale']:>7}{st['stale'] or '-':>9}"
                f"{human(st['bytes']):>10}{st['nexus'] or '-':>7}")
        refused = [stem_of(fn) for fn, st in rows if st.get("unsafe")]
        if refused:
            out()
            out(f"    REFUSED: {', '.join(refused)} name(s) archives with something other")
            out("    than a plain file name, which only an edited manifest does. The")
            out("    downloader fetches nothing for such a list; fetch the manifest again.")
        total_stale = sum(st["stale"] for _f, st in rows)
        if total_stale:
            out()
            out(f"    absent    = no file of that name anywhere in the game's pools.")
            out(f"    wrong sz  = a file of that name IS there, but not the number of")
            out(f"                bytes the manifest says it should be ({total_stale} of them).")
            out(f"                Wabbajack verifies every archive before installing, so")
            out(f"                those would be rejected at install time - they count as")
            out(f"                missing. Mostly downloads cut short by an older version")
            out(f"                of these scripts. Fetching the list overwrites them; you")
            out(f"                do not have to do anything about them yourself.")

    out()
    out(f"  COMPLETE ({len(complete)})")
    for fn, st in complete:
        out(f"         {stem_of(fn)[:45]:<46} {st['present']} archive(s) on disk")

    if not rows:
        out()
        out("  Nothing to do in this stage.")
        return False

    if assist:
        out()
        out("  Nexus is in assist mode: each Nexus file opens its page and waits")
        out("  for you to click 'download with manager'. Ctrl+C is safe - what is")
        out("  already downloaded is kept and the run resumes next time.")

    picks = choose(len(rows), "modlists")
    if picks is None:
        out("  Skipped. Nothing downloaded.")
        return False

    want = [rows[i] for i in picks]
    any_done = False
    for n, (fn, st) in enumerate(want, 1):
        out()
        rule()
        out(f"  [{n}/{len(want)}]  {stem_of(fn)}   "
            f"{st['missing']} archive(s), {human(st['bytes'])}")
        rule()
        ran, _rc, _after = download_list(records, fn, st, assist)
        any_done = any_done or ran
        # One list at a time, on purpose: a long click session is unstable
        # enough without the next one starting behind it.
        if n < len(want):
            if ask("  Continue with the next modlist?  [y] yes  [n] stop here",
                   ("y", "n")) == "n":
                out("  Stopping after this one.")
                break
    return any_done


# ----------------------------------------------------------------------
# Stage 4 - prepare one modlist for Wabbajack
# ----------------------------------------------------------------------
def stage_prepare(records, state=None):
    stage_header(4, "PREPARE A MODLIST FOR WABBAJACK")
    out("  Gathers everything one modlist needs into its own folder, so")
    out("  Wabbajack can install it. Only one at a time: staging a second")
    out("  list would pull the shared archives back out of the first.")

    rows = []
    for fn in sorted(records):
        st = list_status(records[fn])
        if st is None:
            continue
        rows.append((fn, st))
    if not rows:
        out("  No modlist declares a game. Nothing to prepare.")
        return False

    out()
    nw = fit([stem_of(fn) for fn, _s in rows], 7) + 2
    gw = fit([A.WJ.game_label(s["game"]) for _f, s in rows], 4) + 2
    out(f"    {'#':>3}  {'modlist':<{nw}}{'game':<{gw}}{'on disk':>9}{'missing':>9}")
    for i, (fn, st) in enumerate(rows, 1):
        out(f"    {i:>3}  {stem_of(fn):<{nw}}"
            f"{A.WJ.game_label(st['game']):<{gw}}"
            f"{st['present']:>9}{st['missing']:>9}")

    picks = choose(len(rows), "modlist", allow_all=False, single=True)
    if picks is None:
        out("  Skipped. Nothing staged.")
        return False

    fn, st = rows[picks[0]]
    out()
    out(f"  Staging {stem_of(fn)} ({A.WJ.game_label(st['game'])}).")
    if st["missing"]:
        out(f"  Note: {st['missing']} archive(s) are still missing, so this")
        out("  prepares what you have. Wabbajack will ask for the rest.")
    wait_for_wabbajack("Staging")
    rc = stage_list(state, fn, st)
    if rc == 0 and state is not None:
        fresh = list_status(records[fn]) or st
        offer_install(state, fn, fresh)
    return rc == 0


def offer_install(state, filename, st):
    """Hand a freshly staged list to Wabbajack.

    Everything Wabbajack needs is now in one folder, so the only outstanding
    decisions are where to install and whether to go. The check that matters
    most happens before any of that: Wabbajack cannot install a list for a game
    it cannot find, because a modlist copies files out of the base game rather
    than downloading them, and finding that out after committing to an install
    is a waste of an evening."""
    mode = getattr(CFG, "INSTALL_MODE", "cli") if CFG else "cli"
    if mode == "off":
        return False

    stem = stem_of(filename)
    wj = WB.find_wabbajack()
    out()
    rule()
    out("  Hand it to Wabbajack?")
    if not wj:
        out("  Wabbajack was not found. Set WABBAJACK_DIR in wj_settings.json if")
        out("  it is installed somewhere other than in or beside this folder.")
        return False
    for name in WB.prune_old_versions(wj):
        out(f"    Removed Wabbajack {name}, superseded by {wj['version']}.")

    downloads = os.path.join(A.WJ.resolve_game_dirs(st["game"])[0], stem)
    manifest = os.path.join(A.MANIFESTS, filename)
    update = WB.update_status(wj)
    out(f"    Wabbajack   {wj['dir']}" + (f"  ({wj['version']})" if wj["version"] else ""))
    out(f"                {update['text']}")
    if update["outdated"]:
        out(f"                {update['url']}")
        out("                Run Wabbajack.exe once to let it update itself; the old")
        out("                version's folder is removed from here the next time it is checked.")
    out(f"    Modlist     {manifest}")
    out(f"    Downloads   {downloads}")

    ready, detail = WB.game_ready(wj.get("cli"), st["game"])
    mark = {True: "ok", False: "BLOCKED", None: "unknown"}[ready]
    out(f"    Game        {A.WJ.game_label(st['game'])}  [{mark}]")
    out(f"                {detail}")
    if st["missing"]:
        out(f"    Missing     {st['missing']} archive(s) ({human(st['bytes'])}) not "
            f"downloaded yet")
        out("                Wabbajack will try to fetch these itself, which on a "
            "free")
        out("                account means its own click-per-file flow.")

    if ready is False:
        out()
        out("  Not offering to install: the check above has to pass first.")
        return False
    if update["outdated"]:
        out()
        out("  Wabbajack is out of date. A modlist built for a newer Wabbajack can")
        out("  fail to install on an older one.")
        if ask("   [c] carry on with this version   [s] skip", ("c", "s")) == "s":
            out("  Skipped. Nothing installed.")
            return False

    if mode == "gui":
        if not wj["exe"]:
            out("  Wabbajack.exe was not found, so the window cannot be opened.")
            return False
        out()
        out("  Opening Wabbajack. Paste these two paths into the install page:")
        out(f"    Modlist   {manifest}")
        out(f"    Downloads {downloads}")
        if WB.launch_gui(wj["exe"]):
            out("  Wabbajack is starting.")
            return True
        out("  Could not start Wabbajack.exe.")
        return False

    if not wj.get("cli"):
        out("  wabbajack-cli.exe was not found, so the install cannot be driven")
        out("  from here. Set INSTALL_MODE = \"gui\" in wj_config.py to open the")
        out("  window instead.")
        return False

    remembered = (state.get("install") or {}).get(stem)
    target = ask_path("Where should Wabbajack install this list?", remembered)
    if not target:
        out("  Skipped. Nothing installed.")
        return False
    target = os.path.abspath(target)

    problems = WB.check_paths(manifest, downloads, target)
    if problems:
        out()
        for p in problems:
            out("  [NO] " + p)
        out("  Nothing started. Run the stage again with a different path.")
        return False

    lines = [f"modlist   {stem}",
             f"downloads {downloads}",
             f"install   {target}"]
    if os.path.isdir(target) and os.listdir(target):
        lines.append("")
        lines.append("That folder already has something in it. Wabbajack will")
        lines.append("overwrite anything belonging to a modlist install.")
    try:
        free = shutil.disk_usage(os.path.splitdrive(target)[0] or target).free
        lines.append("")
        lines.append(f"free on that drive: {human(free)}")
    except (OSError, ValueError):
        pass
    out()
    rule("!")
    out("  About to start a Wabbajack install")
    rule("!")
    for line in lines:
        out("   " + line)
    out()
    if ask("   Start it?  [y] yes  [n] no", ("y", "n")) == "n":
        out("  Skipped. Nothing installed.")
        return False

    state.setdefault("install", {})[stem] = target
    A.save_state(state)
    out()
    out("  Running Wabbajack. This takes a while; its output follows.")
    rule()
    rc = WB.run_install(wj["cli"], manifest, downloads, target,
                        on_line=lambda l: out("  | " + l))
    rule()
    if rc == 0:
        out(f"  Wabbajack finished. {stem} is installed at {target}.")
    else:
        out(f"  Wabbajack exited {rc}. Its output above says why; the staged "
            f"folder is untouched, so re-running is safe.")
    return rc == 0


# ----------------------------------------------------------------------
# Stage 5 - disk space
# ----------------------------------------------------------------------
def orphan_sidecars():
    """.metadata files whose .wabbajack is no longer there.

    Wabbajack writes one beside every modlist it downloads. They are tiny, so
    these are reported rather than deleted: an orphan is usually the last trace
    of a list that went missing, which is worth seeing rather than tidying
    away."""
    try:
        names = os.listdir(A.MANIFESTS)
    except OSError:
        return []
    out_list = []
    for n in names:
        if not n.lower().endswith(".metadata"):
            continue
        if not os.path.isfile(os.path.join(A.MANIFESTS, n[:-len(".metadata")])):
            out_list.append(n)
    return sorted(out_list)


def unmanaged_report():
    """(game_folder, bytes, files) for each game's Unmanaged store.

    Shown because it is often not small - 79.7 GB across four folders on this
    share - and a disk-space stage that silently omits it would leave the
    biggest single thing on the volume unaccounted for."""
    name = (CFG.UNMANAGED_FOLDER if CFG else "Unmanaged")
    root = A.WJ.ARCHIVE_ROOT
    rows = []
    try:
        games = sorted(os.listdir(root))
    except OSError:
        return rows
    for g in games:
        store = os.path.join(root, g, name)
        if not os.path.isdir(store):
            continue
        b, f = tree_size(store)
        if f:
            rows.append((g, b, f))
    return rows


def gather_space(records, extra_want=None):
    """Manifest sizes, per-list pool folder sizes, and Master per game."""
    out("  Measuring... (walking the pools on the share)")
    lists = []
    for fn in sorted(records):
        e = records[fn]
        path = os.path.join(A.MANIFESTS, fn)
        try:
            msize = os.path.getsize(path)
        except OSError:
            continue
        folder_bytes = folder_files = 0
        game = e.get("game")
        if game:
            base, _, _ = A.WJ.resolve_game_dirs(game)
            folder_bytes, folder_files = dir_size(os.path.join(base, stem_of(fn)))
        lists.append({"file": fn, "game": game, "manifest_bytes": msize,
                      "folder_bytes": folder_bytes, "folder_files": folder_files})

    return lists, measure_masters(records, extra_want)


def show_space(lists, masters):
    out()
    out(f"  MODLIST MANIFESTS ({human(sum(x['manifest_bytes'] for x in lists))} "
        f"across {len(lists)} file(s))")
    nw = fit([stem_of(x["file"]) for x in lists], 7) + 2
    gw = fit([A.WJ.game_label(x["game"]) if x["game"] else "?"
              for x in lists], 4) + 2
    out(f"    {'#':>3}  {'modlist':<{nw}}{'game':<{gw}}{'manifest':>10}"
        f"{'its archives':>14}")
    reserved = A.WJ.reserved_list_names()
    flagged = False
    for i, x in enumerate(lists, 1):
        mark = "!" if stem_of(x["file"]).lower() in reserved else " "
        flagged = flagged or mark == "!"
        out(f"  {mark} {i:>3}  {stem_of(x['file']):<{nw}}"
            f"{(A.WJ.game_label(x['game']) if x['game'] else '?'):<{gw}}"
            f"{human(x['manifest_bytes']):>10}"
            f"{human(x['folder_bytes']) + ' ' if x['folder_files'] else '- ':>14}")
    if flagged:
        out("    !  named after a pool folder, so its 'folder' IS that pool. It")
        out("       cannot be deleted here; rename the .wabbajack file.")
    orphans = orphan_sidecars()
    if orphans:
        out()
        out(f"  {len(orphans)} .metadata sidecar(s) whose modlist is gone. Tiny, and")
        out(f"  left alone: an orphan is usually the last trace of a list that")
        out(f"  went missing, which is worth seeing.")
        for n in orphans[:5]:
            out(f"    {n}")
        if len(orphans) > 5:
            out(f"    ... and {len(orphans) - 5} more")

    out()
    out(f"  MASTER POOLS ({human(sum(m['bytes'] for m in masters))}, of which "
        f"{human(sum(m['free_bytes'] for m in masters))} is reclaimable)")
    gw = fit([m["label"] for m in masters], 4) + 2
    out(f"    {'#':>3}  {'game':<{gw}}{'files':>8}{'size':>11}"
        f"{'reclaimable':>13}{'still needed':>14}")
    for i, m in enumerate(masters, 1):
        out(f"    {i:>3}  {m['label']:<{gw}}"
            f"{m['files']:>8}{human(m['bytes']):>11}"
            f"{human(m['free_bytes']):>13}{m['kept_files']:>10} file(s)")


def _empty_into_master(folder, master, keep):
    """Move every file in a deleted list's folder into Master.

    Returns (archives moved, archives dropped as redundant). Sidecar .meta
    files travel with their archive, under whatever name the archive ends up
    with, so 5c can still judge each one by its owner.

    The hard case is a name already taken in Master by a DIFFERENT build. Both
    files are kept, but only one can keep the name, and both the sorter and
    the cleanup find files by name. `keep` - what the surviving lists want -
    decides: a build a survivor wants keeps the name, and the other is parked
    under a '~N' suffix. Before this the incoming copy was always the one
    suffixed, which is how a build another list still needed came to be the
    one deleted: the rebuild handed that list the junk under the real name,
    and 5c removed the suffixed file as something nobody asked for."""
    try:
        names = sorted(os.listdir(folder))
    except OSError as e:
        out(f"    cannot read {folder}: {e}")
        return 0, 0
    files = [n for n in names if os.path.isfile(os.path.join(folder, n))]
    metas = {n.lower(): n for n in files if n.lower().endswith(".meta")}
    archives = [n for n in files if not n.lower().endswith(".meta")]
    moved = redundant = 0

    def take_meta(owner):
        return metas.pop((owner + ".meta").lower(), None)

    def place_meta(meta_name, owner_dst):
        target = owner_dst + ".meta"
        try:
            if os.path.isfile(target):
                os.remove(target)          # an orphan left from something else
            shutil.move(os.path.join(folder, meta_name), target)
        except (OSError, shutil.Error) as e:
            out(f"    could not move {meta_name}: {e}")

    for name in archives:
        src = os.path.join(folder, name)
        dst = os.path.join(master, name)
        try:
            size = os.path.getsize(src)
            if os.path.isfile(dst):
                there = os.path.getsize(dst)
                if there == size:
                    # An identical copy already in Master makes this one
                    # redundant; keeping both would just be litter for 5c.
                    os.remove(src)
                    m = take_meta(name)
                    if m:
                        os.remove(os.path.join(folder, m))
                    redundant += 1
                    continue
                inc_wanted = keep is not None and is_wanted(keep, name, size)
                old_wanted = keep is not None and is_wanted(keep, name, there)
                if inc_wanted and not old_wanted:
                    # The survivors want THIS build. The one already there
                    # gives up the name and is parked, sidecar and all.
                    aside = unique_path(dst)
                    shutil.move(dst, aside)
                    if os.path.isfile(dst + ".meta"):
                        shutil.move(dst + ".meta", aside + ".meta")
                else:
                    dst = unique_path(dst)
            shutil.move(src, dst)
            m = take_meta(name)
            if m:
                place_meta(m, dst)
            moved += 1
        except (OSError, shutil.Error) as e:
            out(f"    could not move {name}: {e}")

    # Sidecars whose archive was not in the folder go along anyway; 5c judges
    # a .meta by its archive and removes an orphan.
    for m in list(metas.values()):
        try:
            shutil.move(os.path.join(folder, m),
                        unique_path(os.path.join(master, m)))
        except (OSError, shutil.Error) as e:
            out(f"    could not move {m}: {e}")
    return moved, redundant


def delete_modlist(state, entry, keep=None):
    """Remove one modlist: archives to Master, manifest gone, state cleaned.

    The archives are moved rather than deleted because some of them are very
    likely needed by modlists you are keeping. Master is where 'needed by
    nothing' lives, and the cleanup pass that runs next pulls back whatever
    is still wanted. Only what survives that is a candidate for deletion.

    `keep` is what the SURVIVING lists want, as wanted_builds() shapes it; see
    _empty_into_master() for what it settles. Without it the old behaviour
    applies: an incoming build that collides with a different one in Master is
    the one that gets the suffix."""
    fn = entry["file"]
    stem = stem_of(fn)
    if stem.lower() in A.WJ.reserved_list_names():
        # Its "folder" is a pool. Emptying it into Master would remove every
        # file in Master as a redundant copy of itself.
        out(f"    refusing to delete '{stem}': it is named after a pool folder, "
            f"so its folder IS that pool.")
        out("    Rename the .wabbajack file instead.")
        return 0, 0, 0
    moved = redundant = 0
    if entry["game"]:
        base, master, _s = A.WJ.resolve_game_dirs(entry["game"])
        folder = os.path.join(base, stem)
        if os.path.isdir(folder):
            try:
                os.makedirs(master, exist_ok=True)
            except OSError:
                pass
            moved, redundant = _empty_into_master(folder, master, keep)
            try:
                os.rmdir(folder)
            except OSError:
                pass  # not empty (subfolders, or a move failed) - leave it

    # The manifest, its .metadata sidecar, and any superseded copy parked in
    # manifest_backups. Leaving the sidecar behind is how the folder ends up
    # with records of lists that no longer exist; leaving the .bak behind keeps
    # a rollback for a version update that can no longer happen, and those run
    # to hundreds of megabytes each.
    freed = 0
    leftovers = [os.path.join(A.MANIFESTS, fn),
                 os.path.join(A.MANIFESTS, fn + ".metadata")]
    if A.BACKUPS != A.MANIFESTS:
        leftovers += [os.path.join(A.BACKUPS, fn + ".bak"),
                      os.path.join(A.BACKUPS, fn + ".rejected")]
    for path in leftovers:
        if not os.path.isfile(path):
            continue
        try:
            size = os.path.getsize(path)
            os.remove(path)
            freed += size
        except OSError as e:
            out(f"    could not delete {os.path.basename(path)}: {e}")

    for k, v in list(state.get("lists", {}).items()):
        if v.get("file") == fn:
            del state["lists"][k]
    state.get("map", {}).pop(fn.lower(), None)
    return moved, redundant, freed


def clear_master(game, keep_builds, on_delete=None):
    """Move Master files that no remaining modlist demands into the trash.

    keep_builds maps name -> the sizes a surviving list asks for, from
    wanted_builds(). It is recomputed rather than trusted from earlier in the
    run: between selecting and confirming, a cleanup pass has moved files
    about, and losing something a list still needs would mean re-downloading
    it one Nexus click at a time.

    Matching on name and size together means a junk build of a shared name is
    still reclaimable, while the build a list actually wants is not - and the
    name is read through a '~N' suffix, so a wanted build parked under one is
    protected too. A wanted build of which another copy exists elsewhere in
    the pools is a duplicate (duplicate_of, with the hash check) and goes as
    well; its sidecar follows it under the same final name.

    Nothing is deleted here. Files go to <game>/Trash stamped with their
    arrival, and purge_trash() removes them TRASH_KEEP_DAYS days later.

    `on_delete(name, size, reason, destination)` is called after each move,
    so the caller can write the record as it goes rather than after the fact:
    a run interrupted half-way used to leave no record of what it had done.

    Returns (moved, kept, bytes): `moved` is [(name, size)] of what went to
    the trash, `kept` how many files were left because a list wants them.
    Nothing moves when A.trash_name_problem() objects to the trash folder."""
    base, master, _s = A.WJ.resolve_game_dirs(game)
    trash = trash_folder_for(game)
    deleted, kept, freed = [], 0, 0
    why = A.trash_name_problem()
    if why:
        out(f"    not clearing Master: the trash folder "
            f"{CFG.TRASH_FOLDER if CFG else 'Trash'!r} cannot be used ({why}).")
        return deleted, kept, freed
    try:
        entries = sorted(os.scandir(master), key=lambda e: e.name.lower())
    except OSError as e:
        out(f"    cannot read {master}: {e}")
        return deleted, kept, freed
    # One listing carries every size; nothing below stats a file to learn it.
    sizes = {}
    for e in entries:
        try:
            if e.is_file():
                sizes[e.name] = e.stat().st_size
        except OSError:
            continue
    # A fresh index: the rebuild and the parking step have both moved files
    # since the table was drawn, and a duplicate is only a duplicate if the
    # other copy is where the index says it is. The hash cache is reset for
    # the same reason.
    A.WJ.invalidate_existing_cache()
    _HASH_CACHE.clear()
    index = A.WJ.index_existing(base)
    gone = set()

    def move(name, reason):
        src = os.path.join(master, name)
        try:
            dst = send_to_trash(src, trash)
        except (OSError, shutil.Error) as ex:
            out(f"    could not move {name} to the trash: {ex}")
            return None
        gone.add(name)
        deleted.append((name, sizes[name]))
        if on_delete:
            on_delete(name, sizes[name], reason, dst)
        return dst

    for e in entries:
        name = e.name
        if name not in sizes or name in gone:
            continue
        low = name.lower()
        is_meta = low.endswith(".meta")
        owner = name[:-5] if is_meta else name
        # A .meta rides along with its archive, so judge it by the archive.
        judge_size = sizes[name]
        if is_meta:
            judge_size = None if (owner in gone or owner not in sizes) else sizes[owner]
        if judge_size is None:
            reason = "sidecar with no archive"
        elif is_wanted(keep_builds, owner, judge_size):
            other = duplicate_of(index, master, os.path.join(master, owner),
                                 owner, judge_size, verify=True)
            if other is None:
                kept += 1
                continue
            reason = "duplicate of %s (xxh64 %s)" % (other, file_hash(other) or "?")
        else:
            reason = "wanted by no list"
        dst = move(name, reason)
        if dst is None:
            continue
        freed += sizes[name]
        # Its sidecar goes with it, under whatever name it ended up with.
        meta = name + ".meta"
        if not is_meta and meta in sizes and meta not in gone:
            try:
                mdst = dst + ".meta"
                if os.path.isfile(mdst):
                    os.remove(mdst)
                shutil.move(os.path.join(master, meta), mdst)
                now = time.time()
                os.utime(mdst, (now, now))
            except (OSError, shutil.Error) as ex:
                out(f"    could not move {meta} to the trash: {ex}")
                continue
            gone.add(meta)
            deleted.append((meta, sizes[meta]))
            freed += sizes[meta]
            if on_delete:
                on_delete(meta, sizes[meta], "sidecar of %s" % name, mdst)
    return deleted, kept, freed


class DeletionRecord:
    """The CSV of what stage 5c reclaimed, written one row per file.

    Opened before the first move and flushed after every row, so the file on
    disk is exact at every moment: what it lists is what has gone to the
    trash, however the run ended. A record written after the loop was no
    record at all if the loop did not finish. The trashed_to column is what a
    restore by hand needs."""

    def __init__(self, game, rule, master):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = A.runtime_path(f"deleted_{A.WJ.game_folder(game)}_{stamp}.csv")
        self.rule, self.master, self.rows = rule, master, 0
        self._fh = self._w = None
        try:
            self._fh = open(self.path, "w", newline="", encoding="utf-8")
            self._w = csv.writer(self._fh)
            self._w.writerow(["archive", "size_bytes", "reason", "trashed_to",
                              "master", "rule"])
            self._fh.flush()
        except OSError as e:
            out(f"    could not open the deletion record: {e}")
            self._fh = None

    def note(self, name, size, reason="", to=""):
        self.rows += 1
        if self._w is None:
            return
        try:
            self._w.writerow([name, size, reason, to, self.master, self.rule])
            self._fh.flush()
        except OSError as e:
            out(f"    could not write to the deletion record: {e}")

    def close(self):
        """Close, dropping the file if nothing was deleted. Returns the path
        of a record that has rows, else None."""
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
        if self.rows == 0:
            try:
                os.remove(self.path)
            except OSError:
                pass
            return None
        return self.path if self._fh is not None else None


def wait_for_wabbajack(what):
    """Block while a Wabbajack process is running; it may be reading the pools.

    Asks rather than proceeds: moving archives under a running install breaks
    that install, and nothing here can tell which folder it is reading. 'q'
    at the prompt stops the wizard, as everywhere else."""
    while True:
        procs = WB.running_processes()
        if not procs:
            return True
        out()
        out(f"  Wabbajack is running "
            f"({', '.join(f'{n} pid {p}' for p, n in procs)}).")
        out(f"  {what} moves archives it may be reading. Close Wabbajack first.")
        ask("   [r] I have closed it, check again   [q] quit", ("r",))


def trash_report():
    """(game_folder, files, bytes, oldest_arrival) per non-empty trash."""
    name = CFG.TRASH_FOLDER if CFG else "Trash"
    rows = []
    try:
        games = sorted(os.listdir(A.WJ.ARCHIVE_ROOT))
    except OSError:
        return rows
    for g in games:
        t = os.path.join(A.WJ.ARCHIVE_ROOT, g, name)
        if not os.path.isdir(t):
            continue
        total = files = 0
        oldest = None
        try:
            with os.scandir(t) as it:
                for e in it:
                    try:
                        if not e.is_file():
                            continue
                        st = e.stat()
                    except OSError:
                        continue
                    total += st.st_size
                    files += 1
                    oldest = st.st_mtime if oldest is None else min(oldest, st.st_mtime)
        except OSError:
            continue
        if files:
            rows.append((g, files, total, oldest))
    return rows


# Files an operating system or an editor drops beside real content. None of
# them is ever read by anything here, and they accumulate for good, because
# every other step in this suite only ever judges archives.
JUNK_NAMES = (".ds_store", "thumbs.db", "desktop.ini")

# Never swept, whatever they contain: managed on their own schedule, or by git,
# or by the interpreter. __pycache__ is deliberately here rather than in the
# sweep - Python rebuilds it on the very next import, so offering to delete it
# would put a prompt in front of you on every single run and reclaim nothing
# that stays reclaimed.
# Folders under the tools folder the sweep does not enter: version control,
# the caches and records other steps own, and the three that are programs
# kept inside the project rather than installed on the machine - Python,
# git, and Wabbajack itself, each thousands of files nothing here should
# judge.
LEFTOVER_SKIP_DIRS = (".git", ".venv-win", ".venv-build", ".venv", "dist", "build",
                      "node_modules", ".gallery-cache", "manifest_backups", "__pycache__",
                      ".python", ".mingit", "_internal", "wabbajack")


def _is_junk_file(name):
    low = name.lower()
    # AppleDouble sidecars: a Mac writes '._x' beside 'x' on a non-HFS share.
    return low in JUNK_NAMES or (name.startswith("._") and len(name) > 2)


def leftover_report(roots=None):
    """Files nothing else in the suite ever removes.

    Stage 5 judges archives by demand, the trash is purged on its own timer,
    and backups expire on theirs. What none of that reaches is the debris
    beside them: operating-system metadata, AppleDouble sidecars from a Mac
    that touched the share, and Wabbajack's .metadata sidecar for a modlist
    that is gone.

    Only debris that stays gone is listed. __pycache__ and the captured
    output of the last child run are both rewritten by the very next run, so
    sweeping them would put a prompt in front of you every time and reclaim
    nothing lasting.

    Returns [(category, path, bytes)], never touching live state: the lock, a
    resumable .part, the nxm listener files a running assist session owns, the
    API key, settings, manifests, or the deletion records, which are the only
    account of what stage 5 took."""
    seen, rows = set(), []

    def add(cat, path):
        p = os.path.abspath(path)
        if p in seen:
            return
        seen.add(p)
        try:
            # tree_size returns (bytes, files); a plain getsize returns bytes.
            size = tree_size(p)[0] if os.path.isdir(p) else os.path.getsize(p)
        except OSError:
            size = 0
        rows.append((cat, p, size))

    if roots is None:
        roots = [A.HERE, A.WJ.ARCHIVE_ROOT]
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in LEFTOVER_SKIP_DIRS
                           and d.lower() not in A.WJ.EXCLUDE_POOL_DIRS]
            for n in filenames:
                if _is_junk_file(n):
                    add("OS metadata", os.path.join(dirpath, n))

    for n in orphan_sidecars():
        add("orphaned sidecar", os.path.join(A.MANIFESTS, n))

    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


def remove_leftovers(rows, on_delete=None):
    """Delete what leftover_report() found. Returns (count, bytes, failures)."""
    count = total = 0
    failures = []
    for cat, path, size in rows:
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except OSError as e:
            failures.append((path, str(e)))
            continue
        count += 1
        total += size
        if on_delete:
            on_delete(cat, path, size)
    return count, total, failures


def leftover_step():
    """5e: the debris no other step reaches."""
    rows = leftover_report()
    if not rows:
        return False
    total = sum(b for _c, _p, b in rows)
    out()
    rule()
    out("  5e. Leftovers")
    out("      Debris beside the archives that nothing else here ever removes:")
    out("      operating-system metadata, and Wabbajack's .metadata sidecar for")
    out("      a modlist that is gone. No archive is ever in this list.")
    out()
    by_cat = {}
    for cat, _p, b in rows:
        n, t = by_cat.get(cat, (0, 0))
        by_cat[cat] = (n + 1, t + b)
    cw = fit(list(by_cat), 4) + 2
    out(f"    {'what':<{cw}}{'items':>7}{'size':>11}")
    for cat in sorted(by_cat):
        n, t = by_cat[cat]
        out(f"    {cat:<{cw}}{n:>7}{human(t):>11}")
    out()
    for _c, p, _b in rows[:8]:
        out(f"      {p}")
    if len(rows) > 8:
        out(f"      ... and {len(rows) - 8} more")
    a = ask(f"   [d] delete these {len(rows)} item(s) ({human(total)})   [s] leave them",
            ("d", "s"))
    if a == "s":
        return False
    verdict = confirm_destructive(
        "About to delete leftovers",
        [f"{cat}: {n} item(s), {human(t)}" for cat, (n, t) in sorted(by_cat.items())],
        total=total, total_label="Total", undo="This cannot be undone.")
    if not verdict:
        return False
    count, freed, failures = remove_leftovers(rows, lambda c, p, b: out(f"    {p}"))
    out(f"  {count} item(s) deleted, {human(freed)} freed.")
    for p, err in failures:
        out(f"  [WARN] {p}: {err}")
    return count > 0


def trash_step():
    """5d: what the trash holds, and the offer to empty it now."""
    rows = trash_report()
    if not rows:
        return False
    days = keep_days()
    out()
    rule()
    out("  5d. Trash")
    out(f"      What stage 5c reclaims waits here {days} days, then goes for good at")
    out("      the start of a run. Until then a file can be moved back into Master")
    out("      by hand, and the next rebuild sorts it home.")
    out()
    gw = fit([g for g, _f, _b, _o in rows], 4) + 2
    out(f"    {'game folder':<{gw}}{'files':>7}{'size':>11}   purged from")
    for g, files, byts, oldest in rows:
        due = datetime.fromtimestamp(oldest + days * 86400).strftime("%Y-%m-%d")
        out(f"    {g:<{gw}}{files:>7}{human(byts):>11}   {due}")
    total = sum(b for _g, _f, b, _o in rows)
    a = ask(f"   [e] empty the trash now ({human(total)})   [s] leave it", ("e", "s"))
    if a == "s":
        return False
    verdict = confirm_destructive(
        "About to empty the trash",
        [f"{g}: {f} file(s), {human(b)}" for g, f, b, _o in rows],
        total_bytes=total, undo="This is the deletion itself. There is no "
                                "further undo.")
    if verdict != "yes":
        out("  Left alone.")
        return False
    wait_for_wabbajack("Emptying the trash")
    purged, rec = A.purge_trash(days=0)
    out(f"  Purged {len(purged)} file(s), {human(sum(r[2] for r in purged))} freed."
        + (f" Record: {os.path.basename(rec)}" if rec else ""))
    return True


def prune_stale_csvs(records, statuses):
    """Remove per-list to-do CSVs for lists that are gone or complete.

    nexus_todo_, nexus_pages_ and download_failures_ files are written per
    run for lists with work outstanding; nothing removed them once the work
    was done, and ten had piled up. A list still missing archives keeps its
    files. Returns the names removed."""
    stems = {stem_of(fn) for fn in records}
    busy = {stem_of(fn) for fn, st in statuses.items() if st and st.get("missing")}
    removed = []
    folder = A.runtime_path()
    try:
        names = os.listdir(folder)
    except OSError:
        return removed
    for n in names:
        low = n.lower()
        if not low.endswith(".csv"):
            continue
        for prefix in ("nexus_todo_", "nexus_pages_", "download_failures_"):
            if low.startswith(prefix):
                stem = n[len(prefix):-4]
                if stem not in stems or stem not in busy:
                    try:
                        os.remove(os.path.join(folder, n))
                        removed.append(n)
                    except OSError:
                        pass
                break
    return removed


def stage_space(state, records):
    stage_header(5, "DISK SPACE")
    lists, masters = gather_space(records)
    show_space(lists, masters)

    parked = unmanaged_report()
    if parked:
        store = CFG.UNMANAGED_FOLDER if CFG else "Unmanaged"
        out()
        out(f"  {store.upper()} ({human(sum(b for _g, b, _f in parked))}) - "
            f"yours, never touched by this tooling")
        gw = fit([g for g, _b, _f in parked], 4) + 2
        for g, b, f in parked:
            out(f"    {g:<{gw}}{f:>9}{human(b):>12}")

    try:
        free = shutil.disk_usage(A.WJ.ARCHIVE_ROOT).free
        out()
        out(f"  Free on this volume: {human(free)}")
    except OSError:
        pass

    # ---- 5a: delete modlists -----------------------------------------
    out()
    rule()
    out("  5a. Delete modlists?")
    out("      The manifest goes; its archives move into that game's Master,")
    out("      so anything another list still needs can be pulled back before")
    out("      any of it is deleted in 5c.")
    deleted_any = False
    failed_rebuilds = {}          # folder key -> the sorter's exit code
    reserved = A.WJ.reserved_list_names()
    while True:
        picks = choose(len(lists), "modlists")
        if picks is None:
            out("  Skipped. No modlist deleted.")
            break
        chosen = [lists[i] for i in picks]
        named = [stem_of(x["file"]) for x in chosen
                 if stem_of(x["file"]).lower() in reserved]
        if named:
            out(f"  Cannot delete {', '.join(named)}: a manifest named after a "
                f"pool folder claims")
            out("  that pool as its own folder, and deleting it would empty the "
                "pool. Rename")
            out("  the .wabbajack file instead. Choose again.")
            continue
        lines = [f"{stem_of(x['file'])}  -  manifest {human(x['manifest_bytes'])}, "
                 f"{x['folder_files']} archive(s) ({human(x['folder_bytes'])}) "
                 f"moved to Master" for x in chosen]

        # One rebuild per POOL FOLDER afterwards. It re-sorts everything in
        # that folder, which un-stages whatever stage 4 arranged there.
        folders = {}
        for x in chosen:
            if x["game"]:
                folders.setdefault(A.WJ.folder_key(x["game"]), x["game"])
        staged = state.get("staged") or {}
        going = {stem_of(x["file"]) for x in chosen}
        for fkey in sorted(folders):
            s = staged.get(fkey)
            if s and s not in going:
                lines.append("")
                lines.append(f"Rebuilding the {A.WJ.game_label(folders[fkey])} "
                             f"pools afterwards UN-STAGES {s}:")
                lines.append("its shared archives go back to Shared. Run stage "
                             "4 again before installing it.")

        verdict = confirm_destructive(
            f"About to delete {len(chosen)} modlist(s)", lines,
            total_bytes=sum(x["manifest_bytes"] for x in chosen))
        if verdict == "change":
            continue
        if verdict == "no":
            out("  Skipped. No modlist deleted.")
            break

        wait_for_wabbajack("Deleting a modlist")
        failed, _moved, _freed = delete_modlists(state, records, chosen)
        failed_rebuilds.update(failed)
        deleted_any = True
        break

    if deleted_any:
        A.WJ.invalidate_existing_cache()
        records = load_records()
        _lists, masters = gather_space(records)
        out()
        out("  MASTER POOLS after the rebuild")
        gw = fit([m["label"] for m in masters], 4) + 2
        for m in masters:
            out(f"    {m['label']:<{gw}}{m['files']:>9}{human(m['bytes']):>12}")

    # ---- 5b: folders that are nobody's -------------------------------
    # Across every game on the share, not just those with a manifest: a game
    # holding nothing but orphaned content has no manifest by definition.
    strays = all_stray_folders()
    if strays:
        out()
        rule()
        out("  5b. Folders that belong to nothing this tooling follows")
        out("      Not a pool, not excluded, and not named after a manifest you")
        out("      hold. Every archive in them reads as unwanted to a cleanup")
        out(f"      that judges by demand, so move them into '{CFG.UNMANAGED_FOLDER if CFG else 'Unmanaged'}'")
        out("      and both tools will leave them alone for good.")
        out()
        noted = strays_with_notes(strays)
        gw = fit([g for g, _n, _f, _b, _o in noted], 4) + 2
        out(f"    {'#':>3}  {'game folder':<{gw}}{'folder':<38}{'files':>7}{'size':>11}"
            f"  where it came from")
        for i, (game, name, files, byts, origin) in enumerate(noted, 1):
            out(f"    {i:>3}  {game:<{gw}}{name[:38]:<38}"
                f"{files:>7}{human(byts):>11}  {stray_note(origin)}")
        out()

        # A stray that is a published list with its manifest missing is the one
        # case where parking is the wrong answer: adopting puts the folder back
        # in service, and its archives back under protection, at no cost.
        adoptable = [(n, o) for _g, n, _f, _b, o in noted if o and o["published"]]
        if adoptable:
            out("      Some of these are lists the gallery still publishes, whose")
            out("      manifest is simply missing here. Parking one hides archives")
            out("      you already own from the list that wants them. Adopt it in")
            out("      stage 1 instead, and the folder is legitimate again:")
            for n, o in adoptable:
                out(f"        {o['title'] or n}   ({n})")
            out()
        elif any(o and o["sidecar"] for _g, _n, _f, _b, o in noted):
            out("      A 'manifest gone' folder was downloaded by Wabbajack once.")
            out("      Run stage 1 first if you want to know whether its list is")
            out("      still published, and could just be adopted back.")
            out()
        out("      Nothing is deleted here - the folder is moved intact.")
        picks = choose(len(strays), "folders")
        if picks is None:
            out("  Skipped. Left where they are.")
        else:
            for i in picks:
                game, name, files, byts = strays[i]
                dst = park_unmanaged_dir(os.path.join(A.WJ.ARCHIVE_ROOT, game),
                                         name)
                if dst:
                    out(f"    {name} -> {dst}")
            A.WJ.invalidate_existing_cache()

    cleared = clear_master_step(records, masters, failed_rebuilds)
    trash_step()
    leftover_step()
    return deleted_any or cleared


def clear_master_step(records, masters, failed_rebuilds):
    """5c: move what nothing wants out of Master, into the trash."""
    deleted_any = False
    out()
    rule()
    out("  5c. Clear Master for which games?")
    out("      Master holds archives no modlist you HAVE needs. Some of them")
    out("      are still wanted by modlists published in the gallery, so what")
    out("      counts as protected is worth choosing rather than assuming.")
    out("      A Master copy of a build that is also held in Shared or in a")
    out("      modlist's folder is a duplicate, and goes under either rule.")
    out(f"      Nothing is deleted here: files move to that game's Trash and are")
    out(f"      purged {keep_days()} days later, or at once from 5d.")

    # Refusals first. What gets deleted is decided by what the surviving
    # manifests want, so a manifest whose demand cannot be trusted disarms
    # this step - for its own game, or for every game when it is not even
    # readable enough to say which game it belongs to.
    unreadable, blocked = master_refusals(records, failed_rebuilds)
    if unreadable:
        out()
        out(f"  Not clearing any Master: {len(unreadable)} manifest(s) could "
            f"not be read")
        out(f"  ({', '.join(unreadable[:4])}{' ...' if len(unreadable) > 4 else ''}).")
        out("  A list whose manifest cannot be read protects nothing, and does")
        out("  not even say which game's Master it would have protected.")
        out("  Re-download or remove it, then run this stage again.")
        return deleted_any
    if blocked:
        out()
        for m in masters:
            if m["folder"] in blocked:
                out(f"  {m['label']}: not offered - {blocked[m['folder']]}.")
    masters = [m for m in masters if m["folder"] not in blocked]

    # Ask nothing when there is nothing to ask about. The narrow rule protects
    # strictly less than the wide one, so if IT finds nothing reclaimable then
    # neither will the wide one, and prompting for a preference between two
    # identical outcomes is just noise.
    if not any(m["free_files"] for m in masters):
        out()
        out("  Nothing in any Master pool is reclaimable - every file there is")
        out("  still needed by a modlist you have. Nothing to do.")
        return deleted_any

    out()
    out("      [y] only the modlists you have")
    out("          Frees the most. An archive a published list wants is deleted")
    out("          and re-downloaded if you ever install that list.")
    out("      [a] any known modlist, yours or published (hidden lists excluded)")
    out("          Deletes only what nothing anywhere wants - dead weight from")
    out("          superseded builds and lists that no longer exist. A list you")
    out("          have hidden counts for nothing here: its archives go unless")
    out("          another list wants them.")
    mode = ask("      Which?", ("y", "a"))

    wide_want, pulled = None, []
    if mode == "a":
        pools = {m["folder"] for m in masters}
        wide_want, note, pulled = gallery_protection(pools)
        if wide_want is None:
            out()
            out(f"  Cannot do that safely: {note}.")
            out("  Deleting on the strength of a list of published modlists")
            out("  known to be incomplete would remove archives that are in")
            out("  fact still wanted, so this is refused rather than guessed.")
            out("  Stage 5c is skipped; the [y] mode is unaffected next run.")
            return deleted_any
        out(f"  {note}.")
        out(f"  {len(wide_want)} archive name(s) are wanted somewhere in the "
            f"gallery.")
        out("  Re-measuring Master against that...")
        # Only the Master pools need re-measuring, so call measure_masters
        # directly rather than gather_space: the latter also walks all 22
        # per-list folders to size them, which is the slow half of stage 5 and
        # cannot have changed since it ran a moment ago.
        masters = [m for m in measure_masters(records, wide_want)
                   if m["folder"] not in blocked]

    def free_files(m):
        return m["wide_free_files"] if mode == "a" else m["free_files"]

    def free_bytes(m):
        return m["wide_free_bytes"] if mode == "a" else m["free_bytes"]

    def kept_files(m):
        return m["wide_kept_files"] if mode == "a" else m["kept_files"]

    # Summed across every game BEFORE the table drops the ones with nothing
    # reclaimable: a game whose Master is protected entirely by the gallery has
    # no row to show, and is exactly the case this number exists to report.
    held = (sum(m["free_bytes"] - m["wide_free_bytes"] for m in masters)
            if mode == "a" else 0)

    masters = [m for m in masters if free_files(m)]
    if not masters:
        out("  Nothing in any Master pool is reclaimable under that rule -")
        out("  every file there is still wanted. Nothing to do.")
        return deleted_any

    out()
    gw = fit([m["label"] for m in masters], 4) + 2
    out(f"    {'#':>3}  {'game':<{gw}}{'reclaimable':>13}{'files':>8}"
        f"{'protected':>12}")
    for i, m in enumerate(masters, 1):
        out(f"    {i:>3}  {m['label']:<{gw}}"
            f"{human(free_bytes(m)):>13}{free_files(m):>8}"
            f"{kept_files(m):>7} file(s)")
    if mode == "a" and held > 0:
        out()
        out(f"    {human(held)} is being kept that the [y] rule would have")
        out(f"    deleted, because a published modlist still wants it.")

    rule_name = "any known modlist" if mode == "a" else "your modlists only"
    while True:
        picks = choose(len(masters), "games")
        if picks is None:
            out("  Skipped. Master untouched.")
            return deleted_any
        chosen = [masters[i] for i in picks]
        lines = [f"{m['label']}  -  {free_files(m)} file(s), "
                 f"{human(free_bytes(m))}   "
                 f"({kept_files(m)} still wanted, kept)" for m in chosen]
        dups = sum(m["dup_files"] for m in chosen)
        if dups:
            lines.append("")
            lines.append(f"Up to {dups} of those are duplicates: the same build "
                         f"is also held in Shared or a modlist's")
            lines.append(f"folder ({human(sum(m['dup_bytes'] for m in chosen))}). "
                         f"Each is hashed against its other copy first; a pair")
            lines.append("that differs is kept.")
        lines.append("")
        if mode == "a":
            lines.append("Protected by: your modlists AND every published list, "
                         "hidden lists excluded.")
            if pulled:
                lines.append("")
                lines.append(f"{len(pulled)} published list(s) have been pulled "
                             f"by their authors, so their reports list")
                lines.append("no archives and nothing is protected on their "
                             "behalf:")
                for k in pulled[:6]:
                    lines.append(f"   {k}")
                if len(pulled) > 6:
                    lines.append(f"   ... and {len(pulled) - 6} more")
                lines.append("If you hold archives for one of those and no "
                             "manifest for it, they are not protected.")
        else:
            lines.append("Protected by: your modlists only. Archives a "
                         "published list wants WILL be deleted.")
        lines.append("Every file goes to that game's Trash, and into a CSV "
                     "record, as it happens.")
        verdict = confirm_destructive(
            f"About to clear Master for {len(chosen)} game(s)", lines,
            total_bytes=sum(free_bytes(m) for m in chosen),
            total_label="Total moving to Trash",
            undo=(f"Trash is purged {keep_days()} days after a file arrives, at "
                  f"the start of a run, or at once from 5d. Until then a file "
                  f"can be moved back into Master by hand."))
        if verdict == "change":
            continue
        if verdict == "no":
            out("  Skipped. Master untouched.")
            return deleted_any

        wait_for_wabbajack("Clearing Master")
        grand = clear_masters(records, chosen, mode, wide_want)
        out()
        out(f"  {human(grand)} moved to Trash in total. It is purged after "
            f"{keep_days()} days, or now in 5d.")
        return True


# ----------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------
def run(verbose=False, nexus_mode=None, allow_size_only=False, no_lock=False):
    rc = A.bootstrap(verbose=verbose, nexus_mode=nexus_mode,
                     allow_size_only=allow_size_only)
    if rc:
        return rc

    lock_fd = None
    if not no_lock:
        lock_fd = A.acquire_lock(A.runtime_path(A.LOCK_FILE))
        if lock_fd is A.HELD:
            out("Another run holds the lock. Nothing was done.")
            return 3
        if lock_fd is A.UNAVAILABLE:
            lock_fd = None

    assist = (A.NEXUS_MODE == "assist")
    try:
        state = A.load_state()
        tracked = A.resolve_tracking(state)

        out()
        rule("=")
        out("  WABBAJACK MODLIST MANAGER")
        out(f"  Manifests: {A.MANIFESTS}")
        out(f"  Archives:  {A.WJ.ARCHIVE_ROOT}")
        out(f"  Nexus:     {A.NEXUS_MODE}"
            + ("  (one browser click per file)" if assist else "  (left to Wabbajack)"))
        rule("=")

        # What stage 5c reclaimed on earlier runs and has now waited its
        # TRASH_KEEP_DAYS: gone for good, recorded.
        purged, rec = A.purge_trash()
        if purged:
            out(f"  Trash: {len(purged)} file(s) older than {keep_days()} days "
                f"purged, {human(sum(r[2] for r in purged))} freed"
                + (f"  ({os.path.basename(rec)})" if rec else ""))

        stage_browse(state, tracked)
        # Re-read the tracking map: browsing may have adopted a new list, and
        # stage 2 has to see it as tracked or it would offer to fetch the
        # manifest that was just fetched.
        tracked = A.resolve_tracking(state)

        stage_updates(state, tracked)
        # A pinned list with no local file yet gets one here; its folder has
        # to count as a modlist's from now on.
        refresh_known_dirs()
        # The cache is read after stage 1 so a manifest fetched just now is
        # parsed with its new contents rather than served from a stale entry.
        records = load_records()
        stage_incomplete(records, assist)
        stage_prepare(records, state)
        stage_space(state, records)

        out()
        rule("=")
        out("  All stages finished.")
        rule("=")
        return 0
    except Quit:
        out()
        out("Stopped. Nothing further was changed.")
        return 0
    finally:
        A.release_lock(lock_fd)
