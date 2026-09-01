#!/usr/bin/env python3
"""
Interactive front end for the Wabbajack tooling
===============================================
One launch, four stages, each of which can be skipped independently:

  1. UPDATES     which tracked modlists have a newer published version, and
                 which are already current. Fetch all, some, or none.
  2. INCOMPLETE  which modlists are missing archives. Fetch all, some, or
                 none - one list at a time, each finished before the next is
                 offered, because a long Nexus click session is fragile
                 enough without two of them interleaved.
  3. PREPARE     gather everything one modlist needs into its own folder, so
                 Wabbajack can install it.
  4. SPACE       what the manifests and the per-game Master pools are costing,
                 with the option to delete modlists and then clear Master.

Nothing here re-implements the work: every stage drives the same functions the
flag interface uses (wj_autoupdate.py), so the two cannot drift apart. This
module only decides what to show you and what to ask.

Order matters in stage 4 and is not cosmetic. Deleting a modlist moves its
archives into Master, which is exactly where step 4b then wants to delete
from - so the pools are rebuilt in between, and every candidate is re-checked
against the remaining lists' demands before it is removed. A file that any
surviving modlist still needs is never deleted, whatever you select.
"""

import os
import csv
import sys
import shutil
from datetime import datetime, timezone

import wj_autoupdate as A


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


def stage_header(number, title):
    out()
    rule("=")
    out(f"  STAGE {number} of 4   {title}")
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


def confirm_destructive(title, lines, total_bytes=None):
    """Show exactly what is about to be destroyed and ask.

    Returns 'yes', 'no', or 'change'. The 'change' answer exists so a wrong
    selection can be corrected here rather than by aborting the whole stage
    and walking through it again."""
    out()
    rule("!")
    out(f"  {title}")
    rule("!")
    for line in lines:
        out("   " + line)
    if total_bytes is not None:
        out()
        out(f"   Total to be freed: {human(total_bytes)}")
    out()
    out("   This cannot be undone.")
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


def wanted_builds(records, game):
    """{ name_lower: set(sizes) or None } for everything this game's lists want.

    A value of None means "some list wants this name but states no size", in
    which case every copy has to be protected. Keyed by name AND size because
    a name does not identify content: with only names, a junk build of
    Synthesis.zip that no list wants reads as still needed purely because some
    OTHER build of that name is wanted, and stage 4b then refuses to free it."""
    want = {}
    for e in records.values():
        if e.get("game") != game:
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
    """Is this exact file one that a remaining modlist asks for?"""
    if name.lower() not in want:
        return False
    sizes = want[name.lower()]
    return sizes is None or size in sizes


def master_report(game, records):
    """Master's contents for one game, split into kept and reclaimable.

    Master is *meant* to hold only what no present modlist needs, but that is
    only true just after a cleanup pass. Reporting the folder's raw size would
    promise space that stage 4b will then refuse to free, because it re-checks
    every file against the surviving lists' demands. So measure what is
    actually reclaimable and show that."""
    keep = wanted_builds(records, game)
    _b, master, _s = A.WJ.resolve_game_dirs(game)
    r = {"game": game, "path": master, "bytes": 0, "files": 0,
         "free_bytes": 0, "free_files": 0, "kept_files": 0}
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
                if is_wanted(keep, x.name, size):
                    r["kept_files"] += 1
                else:
                    r["free_bytes"] += size
                    r["free_files"] += 1
    except OSError:
        pass
    return r


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
    st = {"game": game, "present": 0, "gamefile": 0, "other": 0, "stale": 0,
          "nexus": 0, "nexus_bytes": 0, "direct": 0, "direct_bytes": 0}
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
# Stage 1 - updates
# ----------------------------------------------------------------------
def stage_updates(state, tracked):
    stage_header(1, "MODLIST UPDATES")
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
                # A new manifest almost always wants new archives. Stage 2
                # measures it properly; this only ensures it is looked at.
                "needs_archives": True,
            }
            A.save_state(state)
            done += 1
        else:
            out("    Failed. Your existing copy is untouched.")
    out()
    out(f"  {done} of {len(want)} manifest(s) updated.")
    return done > 0


# ----------------------------------------------------------------------
# Stage 2 - incomplete modlists
# ----------------------------------------------------------------------
def stage_incomplete(records, assist):
    stage_header(2, "INCOMPLETE MODLISTS")
    out("  Checking what is on disk...")
    A.WJ.invalidate_existing_cache()

    rows, complete = [], []
    for fn in sorted(records):
        st = list_status(records[fn])
        if st is None:
            continue
        (rows if st["missing"] else complete).append((fn, st))
    rows.sort(key=lambda r: -r[1]["bytes"])

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
        base, _, _ = A.WJ.resolve_game_dirs(st["game"])
        if not A.free_space_ok(base, st["bytes"]):
            out("  Not enough free space for this list. Skipping it.")
            continue
        flag = ["--nxm-assist"] if assist else ["--skip-nexus"]
        rc = A.run_script(A.DOWNLOADER_SCRIPT,
                          ["--game", st["game"], "--list", stem_of(fn), "--yes"] + flag,
                          "downloader", interactive=assist)
        any_done = True
        A.WJ.invalidate_existing_cache()
        after = list_status(records[fn])
        if after and after["missing"]:
            out(f"  {after['missing']} archive(s) still missing "
                f"({human(after['bytes'])}). Re-run to continue.")
        else:
            out("  Complete.")
        if rc not in (0, 1):
            out(f"  The downloader exited {rc}; see the log.")
        # One list at a time, on purpose: a long click session is unstable
        # enough without the next one starting behind it.
        if n < len(want):
            if ask("  Continue with the next modlist?  [y] yes  [n] stop here",
                   ("y", "n")) == "n":
                out("  Stopping after this one.")
                break
    return any_done


# ----------------------------------------------------------------------
# Stage 3 - prepare one modlist for Wabbajack
# ----------------------------------------------------------------------
def stage_prepare(records):
    stage_header(3, "PREPARE A MODLIST FOR WABBAJACK")
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
    rc = A.run_script(A.SORTER_SCRIPT,
                      ["--game", st["game"], "--list", stem_of(fn), "--yes"],
                      "sorter")
    out(f"  Sorter exited {rc}.")
    return rc == 0


# ----------------------------------------------------------------------
# Stage 4 - disk space
# ----------------------------------------------------------------------
def gather_space(records):
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

    games = {}
    for e in records.values():
        if e.get("game"):
            games.setdefault(e["game"], None)
    masters = [master_report(game, records) for game in sorted(games)]
    return lists, masters


def show_space(lists, masters):
    out()
    out(f"  MODLIST MANIFESTS ({human(sum(x['manifest_bytes'] for x in lists))} "
        f"across {len(lists)} file(s))")
    nw = fit([stem_of(x["file"]) for x in lists], 7) + 2
    gw = fit([A.WJ.game_label(x["game"]) if x["game"] else "?"
              for x in lists], 4) + 2
    out(f"    {'#':>3}  {'modlist':<{nw}}{'game':<{gw}}{'manifest':>10}"
        f"{'its archives':>14}")
    for i, x in enumerate(lists, 1):
        out(f"    {i:>3}  {stem_of(x['file']):<{nw}}"
            f"{(A.WJ.game_label(x['game']) if x['game'] else '?'):<{gw}}"
            f"{human(x['manifest_bytes']):>10}"
            f"{human(x['folder_bytes']) + ' ' if x['folder_files'] else '- ':>14}")
    out()
    out(f"  MASTER POOLS ({human(sum(m['bytes'] for m in masters))}, of which "
        f"{human(sum(m['free_bytes'] for m in masters))} is reclaimable)")
    gw = fit([A.WJ.game_label(m["game"]) for m in masters], 4) + 2
    out(f"    {'#':>3}  {'game':<{gw}}{'files':>8}{'size':>11}"
        f"{'reclaimable':>13}{'still needed':>14}")
    for i, m in enumerate(masters, 1):
        out(f"    {i:>3}  {A.WJ.game_label(m['game']):<{gw}}"
            f"{m['files']:>8}{human(m['bytes']):>11}"
            f"{human(m['free_bytes']):>13}{m['kept_files']:>10} file(s)")


def delete_modlist(state, entry):
    """Remove one modlist: archives to Master, manifest gone, state cleaned.

    The archives are moved rather than deleted because some of them are very
    likely needed by modlists you are keeping. Master is where 'needed by
    nothing' lives, and the cleanup pass that runs next pulls back whatever
    is still wanted. Only what survives that is a candidate for deletion."""
    fn = entry["file"]
    stem = stem_of(fn)
    moved = redundant = 0
    if entry["game"]:
        base, master, _s = A.WJ.resolve_game_dirs(entry["game"])
        folder = os.path.join(base, stem)
        if os.path.isdir(folder):
            try:
                os.makedirs(master, exist_ok=True)
            except OSError:
                pass
            for name in sorted(os.listdir(folder)):
                src = os.path.join(folder, name)
                if not os.path.isfile(src):
                    continue
                dst = os.path.join(master, name)
                try:
                    # An identical copy already in Master makes this one
                    # redundant; keeping both would just be litter for 4b.
                    if (os.path.isfile(dst)
                            and os.path.getsize(dst) == os.path.getsize(src)):
                        os.remove(src)
                        redundant += 1
                        continue
                    shutil.move(src, unique_path(dst))
                    moved += 1
                except (OSError, shutil.Error) as e:
                    out(f"    could not move {name}: {e}")
            try:
                os.rmdir(folder)
            except OSError:
                pass  # not empty (subfolders, or a move failed) - leave it

    freed = 0
    path = os.path.join(A.MANIFESTS, fn)
    try:
        freed = os.path.getsize(path)
        os.remove(path)
    except OSError as e:
        out(f"    could not delete {fn}: {e}")
        freed = 0

    for k, v in list(state.get("lists", {}).items()):
        if v.get("file") == fn:
            del state["lists"][k]
    state.get("map", {}).pop(fn.lower(), None)
    return moved, redundant, freed


def clear_master(game, keep_builds):
    """Delete Master files that no remaining modlist demands.

    keep_builds maps name -> the sizes a surviving list asks for, from
    wanted_builds(). It is recomputed rather than trusted from earlier in the
    run: between selecting and confirming, a cleanup pass has moved files
    about, and deleting something a list still needs would mean re-downloading
    it one Nexus click at a time.

    Matching on name and size together means a junk build of a shared name is
    still reclaimable, while the build a list actually wants is not."""
    _b, master, _s = A.WJ.resolve_game_dirs(game)
    deleted, kept, freed = [], 0, 0
    try:
        names = sorted(os.listdir(master))
    except OSError as e:
        out(f"    cannot read {master}: {e}")
        return deleted, kept, freed
    for name in names:
        path = os.path.join(master, name)
        if not os.path.isfile(path):
            continue
        low = name.lower()
        owner = name[:-5] if low.endswith(".meta") else name
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        # A .meta rides along with its archive, so judge it by the archive.
        judge_size = size
        if low.endswith(".meta"):
            try:
                judge_size = os.path.getsize(os.path.join(master, owner))
            except OSError:
                judge_size = None
        if judge_size is not None and is_wanted(keep_builds, owner, judge_size):
            kept += 1
            continue
        try:
            os.remove(path)
            deleted.append((name, size))
            freed += size
        except OSError as e:
            out(f"    could not delete {name}: {e}")
    return deleted, kept, freed


def write_deletion_record(game, deleted):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(A.HERE, f"deleted_{A.WJ.game_folder(game)}_{stamp}.csv")
    try:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["archive", "size_bytes"])
            w.writerows(deleted)
        return path
    except OSError as e:
        out(f"    could not write the deletion record: {e}")
        return None


def stage_space(state, records):
    stage_header(4, "DISK SPACE")
    lists, masters = gather_space(records)
    show_space(lists, masters)

    try:
        free = shutil.disk_usage(A.WJ.ARCHIVE_ROOT).free
        out()
        out(f"  Free on this volume: {human(free)}")
    except OSError:
        pass

    # ---- 4a: delete modlists -----------------------------------------
    out()
    rule()
    out("  4a. Delete modlists?")
    out("      The manifest goes; its archives move into that game's Master,")
    out("      so anything another list still needs can be pulled back before")
    out("      any of it is deleted in 4b.")
    deleted_any = False
    while True:
        picks = choose(len(lists), "modlists")
        if picks is None:
            out("  Skipped. No modlist deleted.")
            break
        chosen = [lists[i] for i in picks]
        lines = [f"{stem_of(x['file'])}  -  manifest {human(x['manifest_bytes'])}, "
                 f"{x['folder_files']} archive(s) ({human(x['folder_bytes'])}) "
                 f"moved to Master" for x in chosen]
        verdict = confirm_destructive(
            f"About to delete {len(chosen)} modlist(s)", lines,
            total_bytes=sum(x["manifest_bytes"] for x in chosen))
        if verdict == "change":
            continue
        if verdict == "no":
            out("  Skipped. No modlist deleted.")
            break
        total_moved = total_freed = 0
        for x in chosen:
            out(f"  Deleting {stem_of(x['file'])}...")
            moved, redundant, freed = delete_modlist(state, x)
            total_moved += moved
            total_freed += freed
            out(f"    {moved} archive(s) moved to Master, "
                f"{redundant} redundant copy(ies) removed, "
                f"manifest freed {human(freed)}")
        A.save_state(state)
        deleted_any = True
        out(f"  Manifests freed {human(total_freed)}; "
            f"{total_moved} archive(s) now sit in Master.")

        # Rebuild the pools so Master holds only what nothing needs. Without
        # this, 4b would be choosing from a Master that still contains other
        # lists' archives.
        games = sorted({x["game"] for x in chosen if x["game"]})
        for game in games:
            out(f"  Rebuilding {A.WJ.game_label(game)} pools so nothing still "
                f"needed is left in Master...")
            A.run_script(A.SORTER_SCRIPT, ["--game", game, "--cleanup-only", "--yes"],
                         "sorter")
        break

    if deleted_any:
        A.WJ.invalidate_existing_cache()
        records = load_records()
        _lists, masters = gather_space(records)
        out()
        out("  MASTER POOLS after the rebuild")
        gw = fit([A.WJ.game_label(m["game"]) for m in masters], 4) + 2
        for m in masters:
            out(f"    {A.WJ.game_label(m['game']):<{gw}}"
                f"{m['files']:>9}{human(m['bytes']):>12}")

    # ---- 4b: clear Master --------------------------------------------
    out()
    rule()
    out("  4b. Clear Master for which games?")
    out("      Master holds archives no remaining modlist needs. Anything a")
    out("      surviving list still wants is re-checked and kept, whatever")
    out("      you select here.")
    masters = [m for m in masters if m["free_files"]]
    if not masters:
        out("  Nothing in any Master pool is reclaimable - every file there is")
        out("  still needed by a modlist you have. Nothing to do.")
        return deleted_any

    out()
    gw = fit([A.WJ.game_label(m["game"]) for m in masters], 4) + 2
    out(f"    {'#':>3}  {'game':<{gw}}{'reclaimable':>13}{'files':>8}"
        f"{'protected':>12}")
    for i, m in enumerate(masters, 1):
        out(f"    {i:>3}  {A.WJ.game_label(m['game']):<{gw}}"
            f"{human(m['free_bytes']):>13}{m['free_files']:>8}"
            f"{m['kept_files']:>7} file(s)")

    while True:
        picks = choose(len(masters), "games")
        if picks is None:
            out("  Skipped. Master untouched.")
            return deleted_any
        chosen = [masters[i] for i in picks]
        lines = [f"{A.WJ.game_label(m['game'])}  -  {m['free_files']} file(s), "
                 f"{human(m['free_bytes'])}   "
                 f"({m['kept_files']} still needed, kept)" for m in chosen]
        lines.append("")
        lines.append("A record of every deleted file is written to a CSV first.")
        verdict = confirm_destructive(
            f"About to clear Master for {len(chosen)} game(s)", lines,
            total_bytes=sum(m["free_bytes"] for m in chosen))
        if verdict == "change":
            continue
        if verdict == "no":
            out("  Skipped. Master untouched.")
            return deleted_any

        grand = 0
        for m in chosen:
            game = m["game"]
            keep = wanted_builds(records, game)
            out(f"  Clearing Master for {A.WJ.game_label(game)} "
                f"({len(keep)} archive name(s) still demanded and protected)...")
            deleted, kept, freed = clear_master(game, keep)
            grand += freed
            rec = write_deletion_record(game, deleted) if deleted else None
            out(f"    deleted {len(deleted)} file(s), freed {human(freed)}; "
                f"kept {kept} still needed")
            if rec:
                out(f"    record: {os.path.basename(rec)}")
        out()
        out(f"  Freed {human(grand)} in total.")
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
        lock_fd = A.acquire_lock(os.path.join(A.HERE, A.LOCK_FILE))
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

        stage_updates(state, tracked)
        # The cache is read after stage 1 so a manifest fetched just now is
        # parsed with its new contents rather than served from a stale entry.
        records = load_records()
        stage_incomplete(records, assist)
        stage_prepare(records)
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
