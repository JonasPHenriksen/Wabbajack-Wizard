"""Deleting archives nothing wants, and the folders that are nobody's.

Two questions here. First, what protects an archive from stage 5c: only the
modlists you hold, or every list the gallery publishes as well? Second, what
happens to a folder under a game root that is neither a pool nor a modlist -
a hand-built install like Tale of Two Wastelands - because every file in one
of those reads as unwanted to a cleanup that judges by demand.

Nothing here reaches the network; the gallery half is served from a cache the
suite writes itself.
"""
import io
import json
import logging
import os
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOXROOT = os.path.join(os.environ.get("TEMP", "/tmp"), "wj_tests")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Sandbox, check, summary

sys.path.insert(0, REPO)
os.chdir(REPO)
import wj_autoupdate as A
A.setup_logging(False)
A.LOG.setLevel(logging.CRITICAL)
A.WJ = A.load_downloader_module()
import wj_config as CFG
import wj_gallery as G
import wj_wizard as W

BOX = os.path.join(BOXROOT, "sb12")
GAME = "Fallout4"

print("=" * 70)
print("  T12  CLEANUP: WHAT NOTHING WANTS, AND WHAT IS NOT OURS")
print("=" * 70)

REAL_HTTP = G.http_bytes


def quiet(fn, *a, **k):
    buf = io.StringIO()
    real = W.out
    W.out = lambda msg="": buf.write(str(msg) + "\n")
    try:
        return fn(*a, **k), buf.getvalue()
    finally:
        W.out = real


def wire(box):
    """Point every module at the sandbox instead of the live share."""
    A.MANIFESTS = box.manifests
    A.HERE = box.tools
    A.WJ.ARCHIVE_ROOT = box.archives
    CFG.ARCHIVE_ROOT = box.archives
    G.HERE = box.tools
    A.WJ.set_known_list_dirs(A.WJ.known_list_dirs_from(box.manifests))


# ---------------------------------------------------------------- 12.1
print("\n 12.1  the unmanaged store is not ours to read or touch")
check("excluded_dirs covers the foreign installer",
      "nolvus" in CFG.excluded_dirs(), str(sorted(CFG.excluded_dirs())))
check("excluded_dirs covers the unmanaged store",
      CFG.UNMANAGED_FOLDER.lower() in CFG.excluded_dirs(),
      str(sorted(CFG.excluded_dirs())))
check("the downloader and the sorter share one exclusion set",
      A.WJ.EXCLUDE_POOL_DIRS == CFG.excluded_dirs())

box = Sandbox(BOX)
wire(box)
box.manifest("Target", GAME, [("parked.7z", 500, "nexus")])
A.WJ.set_known_list_dirs(A.WJ.known_list_dirs_from(box.manifests))
box.pool_file(GAME, CFG.UNMANAGED_FOLDER, "parked.7z", 500)
rc, out = box.run("wabbajack_archive_downloader.py",
                  ["--game", GAME, "--list", "Target", "--dry-run", "--yes"],
                  expect=0)
check("a file in Unmanaged does NOT count as already downloaded",
      "parked.7z" in out.split("FILES THAT WOULD BE DOWNLOADED", 1)[-1],
      out.strip()[-200:])
check("  and the store is named as skipped, not as a foreign installer",
      "unmanaged store" in out.lower(), out.strip()[-200:])

rc, sout = box.run("wabbajack_archive_sorter_v2.py",
                   ["--game", GAME, "--cleanup-only", "--yes", "--dry-run"],
                   expect=0)
check("the sorter never indexes Unmanaged",
      "Indexed" in sout and CFG.UNMANAGED_FOLDER not in
      "".join(l for l in sout.split("\n") if "Indexed" in l),
      str([l.strip() for l in sout.split("\n") if "Indexed" in l]))
check("  so it cannot propose moving anything out of it",
      "parked.7z" not in sout.replace("would be", ""))

print("\n      a modlist may not claim a reserved pool name")
box2 = Sandbox(BOX + "r")
A.WJ.set_known_list_dirs(None)
box2.manifest(CFG.UNMANAGED_FOLDER, GAME, [("a.7z", 10, "nexus")])
rc, out2 = box2.run("wabbajack_archive_sorter_v2.py",
                    ["--game", GAME, "--cleanup-only", "--yes"], expect=1)
check("a list called Unmanaged is refused outright",
      "clashes with a reserved pool folder name" in out2, out2.strip()[-200:])
shutil.rmtree(BOX + "r", ignore_errors=True)

# ---------------------------------------------------------------- 12.2
print("\n 12.2  a folder that is neither a pool nor a modlist")
box = Sandbox(BOX)
wire(box)
box.manifest("Target", GAME, [("wanted.7z", 100, "nexus")])
A.WJ.set_known_list_dirs(A.WJ.known_list_dirs_from(box.manifests))
box.pool_file(GAME, "Target", "wanted.7z", 100)
box.pool_file(GAME, "TTW", "hand-built.7z", 700)
box.pool_file(GAME, "Master", "junk.7z", 300)

A.WJ.invalidate_existing_cache()
base, _m, _s = A.WJ.resolve_game_dirs(GAME)
idx = A.WJ.index_existing(base)
check("a stray folder's contents are NOT indexed",
      "hand-built.7z" not in idx, str(sorted(idx)))
check("  while the pools and real list folders still are",
      {"wanted.7z", "junk.7z"} <= set(idx), str(sorted(idx)))

strays = W.stray_folders(GAME)
check("stray_folders finds it", [s[0] for s in strays] == ["TTW"], str(strays))
check("  with its file count and size",
      strays and strays[0][1] == 1 and strays[0][2] == 700, str(strays))
box.pool_file(GAME, CFG.UNMANAGED_FOLDER, "already-parked.7z", 10)
check("Unmanaged itself is not reported as a stray",
      [s[0] for s in W.stray_folders(GAME)] == ["TTW"],
      str(W.stray_folders(GAME)))

print("\n      parking it moves the folder intact")
(dst, _o) = quiet(W.park_unmanaged, GAME, "TTW")
check("park_unmanaged reports where it went", bool(dst), str(dst))
check("the folder is gone from the game root",
      not os.path.isdir(os.path.join(base, "TTW")))
check("  and its file is intact inside Unmanaged",
      os.path.isfile(os.path.join(base, CFG.UNMANAGED_FOLDER, "TTW",
                                  "hand-built.7z")),
      str(dst))
check("  at its original size",
      os.path.getsize(os.path.join(base, CFG.UNMANAGED_FOLDER, "TTW",
                                   "hand-built.7z")) == 700)
check("nothing is a stray any more", W.stray_folders(GAME) == [],
      str(W.stray_folders(GAME)))

print("\n      an empty known-set must not exclude every list folder")
A.WJ.set_known_list_dirs(set())
check("an empty declaration means 'unknown', not 'none are legitimate'",
      A.WJ.KNOWN_LIST_DIRS is None)
A.WJ.invalidate_existing_cache()
idx = A.WJ.index_existing(base)
check("  so a real list folder is still indexed",
      "wanted.7z" in idx, str(sorted(idx)))
A.WJ.set_known_list_dirs(A.WJ.known_list_dirs_from(box.manifests))
check("known_list_dirs_from reads the manifest names",
      A.WJ.KNOWN_LIST_DIRS == frozenset({"target"}),
      str(A.WJ.KNOWN_LIST_DIRS))
check("  and a missing manifest folder yields nothing, not a crash",
      A.WJ.known_list_dirs_from(os.path.join(BOX, "nope")) == set())

# ---------------------------------------------------------------- 12.3
print("\n 12.3  merging two bodies of demand")
check("union of sizes for one name",
      G.merge_builds({"a": {1}}, {"a": {2}})["a"] == {1, 2})
check("names from both sides survive",
      set(G.merge_builds({"a": {1}}, {"b": {2}})) == {"a", "b"})
check("an unstated size (None) wins over a specific one",
      G.merge_builds({"a": {1}}, {"a": None})["a"] is None)
check("  in either order",
      G.merge_builds({"a": None}, {"a": {1}})["a"] is None)
check("merging with nothing is harmless",
      G.merge_builds({"a": {1}}, None)["a"] == {1})
check("the inputs are not mutated",
      (lambda m: (G.merge_builds(m, {"a": {9}}), m["a"] == {1})[1])({"a": {1}}))

# ---------------------------------------------------------------- 12.4
print("\n 12.4  what the published gallery wants")
box = Sandbox(BOX)
wire(box)


def rep(archives, status="Available"):
    return {"MachineURL": "x", "Name": "x", "Version": "1", "Status": status,
            "Failures": 0,
            "Archives": [{"Status": "Valid",
                          "Original": {"Name": n, "Size": s,
                                       "Hash": "AAAAAAAAAAA=", "Meta": "",
                                       "State": {"$type": "NexusDownloader, "
                                                          "Wabbajack.Lib"}}}
                         for n, s in archives]}


feeds = {"r/one": {"repo": "r", "machine": "one", "game": "fallout4",
                   "title": "one", "version": "1", "updated": "",
                   "download": "u", "hash": "h", "size": 1, "archives": 2,
                   "arch_size": 3, "force_down": False},
         "r/two": {"repo": "r", "machine": "two", "game": "Fallout4",
                   "title": "two", "version": "1", "updated": "",
                   "download": "u", "hash": "h", "size": 1, "archives": 1,
                   "arch_size": 1, "force_down": False},
         "r/pulled": {"repo": "r", "machine": "pulled", "game": "fallout4",
                      "title": "pulled", "version": "1", "updated": "",
                      "download": "u", "hash": "h", "size": 1,
                      "archives": 1, "arch_size": 1, "force_down": True},
         "r/other": {"repo": "r", "machine": "other", "game": "morrowind",
                     "title": "other", "version": "1", "updated": "",
                     "download": "u", "hash": "h", "size": 1, "archives": 1,
                     "arch_size": 1, "force_down": False}}
summ = {k: {"machineURL": k, "failed": 0, "passed": 1, "updating": 0,
            "mirrored": 0, "modlist_missing": False, "has_failures": False}
        for k in feeds}
G._write_cache(G.report_rel("r/one"),
               json.dumps(rep([("shared.7z", 10), ("only-one.7z", 20)])).encode())
G._write_cache(G.report_rel("r/two"),
               json.dumps(rep([("shared.7z", 99)])).encode())
G._write_cache(G.report_rel("r/pulled"),
               json.dumps(rep([], status="ForcedDown")).encode())
G._write_cache(G.report_rel("r/other"),
               json.dumps(rep([("elsewhere.7z", 1)])).encode())

want, checked, unchecked = G.published_builds(
    {"fallout4"}, feeds, summ, A.WJ)
check("only lists for the games asked about are consulted",
      "elsewhere.7z" not in want, str(sorted(want)))
check("both case spellings of the game are consulted",
      checked == 2, "checked=%s" % checked)
check("two lists wanting one name at two sizes protect BOTH builds",
      want.get("shared.7z") == {10, 99}, str(want.get("shared.7z")))
check("a name only one list wants is still protected",
      want.get("only-one.7z") == {20}, str(want.get("only-one.7z")))
check("a ForcedDown report with no archives is reported as unchecked",
      unchecked == ["r/pulled"], str(unchecked))
check("  and contributes nothing to the protected set", len(want) == 2,
      str(sorted(want)))

# ---------------------------------------------------------------- 12.5
print("\n 12.5  Master, measured under both protection rules")
box = Sandbox(BOX)
wire(box)
box.manifest("Mine", GAME, [("mine.7z", 100, "nexus")])
A.WJ.set_known_list_dirs(A.WJ.known_list_dirs_from(box.manifests))
box.pool_file(GAME, "Master", "mine.7z", 100)          # my list wants it
box.pool_file(GAME, "Master", "gallery.7z", 200)       # only the gallery does
box.pool_file(GAME, "Master", "nobody.7z", 300)        # nothing does
box.pool_file(GAME, "Master", "wrongbuild.7z", 444)    # wanted, other size
A.WJ.invalidate_existing_cache()
records = A.load_plan_records()
gallery_want = {"gallery.7z": {200}, "wrongbuild.7z": {999}}

narrow = W.master_report(GAME, records)
check("under 'my lists only' three files are reclaimable",
      narrow["free_files"] == 3, str(narrow))
check("  totalling their exact bytes",
      narrow["free_bytes"] == 200 + 300 + 444, str(narrow["free_bytes"]))

wide = W.master_report(GAME, records, gallery_want)
check("under 'any known list' the gallery's file is protected",
      wide["wide_free_files"] == 2, str(wide))
check("  and only what nothing wants is reclaimable",
      wide["wide_free_bytes"] == 300 + 444, str(wide["wide_free_bytes"]))
check("a wanted NAME at an unwanted SIZE is still reclaimable",
      wide["wide_free_bytes"] == 300 + 444)
check("both rules are reported from one scan, so the difference is a fact",
      wide["free_bytes"] - wide["wide_free_bytes"] == 200,
      str((wide["free_bytes"], wide["wide_free_bytes"])))

print("\n      and the deletion honours the merged rule")
keep = W.merge_want(W.wanted_builds(records, GAME), gallery_want)
(res, _o) = quiet(W.clear_master, GAME, keep)
deleted, kept, freed = res
names = sorted(n for n, _s in deleted)
check("only the unwanted files are deleted",
      names == ["nobody.7z", "wrongbuild.7z"], str(names))
check("  freeing exactly their bytes", freed == 300 + 444, str(freed))
check("the gallery's file is still on disk",
      os.path.isfile(os.path.join(box.archives, GAME, "Master", "gallery.7z")))
check("my list's file is still on disk",
      os.path.isfile(os.path.join(box.archives, GAME, "Master", "mine.7z")))
check("  and both are counted as kept", kept == 2, str(kept))

# ---------------------------------------------------------------- 12.6
print("\n 12.6  an incomplete picture must refuse, not delete less")
box = Sandbox(BOX)
wire(box)


def boom(rel):
    raise OSError("network down")


G.http_bytes = boom
try:
    want, note, unchecked = quiet(W.gallery_protection, {"fallout4"})[0]
    check("with no gallery index at all it refuses", want is None, str(note))
    check("  and says why", "could not be read" in (note or ""), str(note))
finally:
    G.http_bytes = REAL_HTTP

G._write_cache(G.SUMMARY_PATH, json.dumps(
    [{"machineURL": "r/one", "failed": 0, "passed": 1, "updating": 0,
      "mirrored": 0, "modlist_missing": False, "has_failures": False}]).encode())
A.reset_feed_cache()
A._FEED_CACHE["all"] = {"r/one": {"repo": "r", "machine": "one",
                                  "game": "fallout4", "title": "one",
                                  "version": "1", "updated": "",
                                  "download": "u", "hash": "h", "size": 1,
                                  "archives": 1, "arch_size": 1,
                                  "force_down": False}}
A._FEED_CACHE["complete"] = False
want, note, unchecked = quiet(W.gallery_protection, {"fallout4"})[0]
check("with some feeds unread it refuses rather than guessing",
      want is None, str(note))
check("  naming the incomplete feed list as the reason",
      "did not respond" in (note or ""), str(note))

A._FEED_CACHE["complete"] = True
want, note, unchecked = quiet(W.gallery_protection, {"fallout4"})[0]
check("with a feed but no cached report it still refuses",
      want is None, str(note))
check("  and points at the fix", "sync-reports" in (note or ""), str(note))

G._write_cache(G.report_rel("r/one"),
               json.dumps(rep([("g.7z", 7)])).encode())
want, note, unchecked = quiet(W.gallery_protection, {"fallout4"})[0]
check("with everything present it answers", want == {"g.7z": {7}}, str(want))
check("  and says how many lists it consulted",
      "1 published list(s) consulted" in (note or ""), str(note))
A.reset_feed_cache()

shutil.rmtree(BOX, ignore_errors=True)
sys.exit(summary())
