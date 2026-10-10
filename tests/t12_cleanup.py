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
from harness import Sandbox, check, summary, quiet_logging

sys.path.insert(0, REPO)
os.chdir(REPO)
import wj_autoupdate as A
quiet_logging(A)
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
rc, out = box.run("wj_downloader.py",
                  ["--game", GAME, "--list", "Target", "--dry-run", "--yes"],
                  expect=0)
check("a file in Unmanaged does NOT count as already downloaded",
      "parked.7z" in out.split("FILES THAT WOULD BE DOWNLOADED", 1)[-1],
      out.strip()[-200:])
check("  and the store is named as skipped, not as a foreign installer",
      "unmanaged store" in out.lower(), out.strip()[-200:])

rc, sout = box.run("wj_sorter.py",
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
rc, out2 = box2.run("wj_sorter.py",
                    ["--game", GAME, "--cleanup-only", "--yes"], expect=1)
check("a list called Unmanaged is refused outright",
      "reserved for a pool folder" in out2, out2.strip()[-200:])
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
      list(unchecked) == ["r/pulled"], str(unchecked))
check("  with the permanent reason", unchecked.get("r/pulled") == G.PULLED,
      str(unchecked))
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

print("\n      a hidden list protects nothing, and blocks nothing")
CFG.HIDDEN = ["r/one"]
want, note, unchecked = quiet(W.gallery_protection, {"fallout4"})[0]
check("its archives are not in the protected set", want == {}, str(want))
check("  and the note says it was left out", "1 hidden list(s) ignored (r/one)" in (note or ""), str(note))
os.remove(G.cache_file(G.report_rel("r/one")))
want, note, unchecked = quiet(W.gallery_protection, {"fallout4"})[0]
check("with its report missing the rule still answers, since it is never read",
      want == {}, str(note))
CFG.HIDDEN = []
want, note, unchecked = quiet(W.gallery_protection, {"fallout4"})[0]
check("  and unhidden, the missing report blocks again", want is None and "sync-reports" in (note or ""),
      str(note))
A.reset_feed_cache()

# ---------------------------------------------------------------- 12.7
print("\n 12.7  a stray folder is a tree, not a flat pool")
box = Sandbox(BOX)
wire(box)
box.manifest("Mine", GAME, [("m.7z", 10, "nexus")])
A.WJ.set_known_list_dirs(A.WJ.known_list_dirs_from(box.manifests))
base, _m, _s = A.WJ.resolve_game_dirs(GAME)

# A hand-built install: a few small files at the top, the bulk nested below.
stray = os.path.join(base, "Workbase")
os.makedirs(os.path.join(stray, "PatchedBa2", "deeper"))
io.open(os.path.join(stray, "README.md"), "wb").write(b"x" * 100)
io.open(os.path.join(stray, "README.md.meta"), "wb").write(b"x" * 7)
io.open(os.path.join(stray, "PatchedBa2", "big.ba2"), "wb").write(b"x" * 5000)
io.open(os.path.join(stray, "PatchedBa2", "deeper", "more.ba2"),
        "wb").write(b"x" * 2000)

flat_b, flat_f = W.dir_size(stray)
deep_b, deep_f = W.tree_size(stray)
check("dir_size stays non-recursive, as the pools need",
      (flat_f, flat_b) == (1, 100), str((flat_f, flat_b)))
check("tree_size counts the nested files too",
      (deep_f, deep_b) == (3, 100 + 5000 + 2000), str((deep_f, deep_b)))
check("tree_size ignores .meta sidecars, as dir_size does",
      deep_f == 3, str(deep_f))
check("tree_size on a path that is not there returns zero",
      W.tree_size(os.path.join(base, "nope")) == (0, 0))

rows = W.strays_in(base)
check("a stray is reported at its TRUE size, not its top level",
      rows == [("Workbase", 3, 7100)], str(rows))
check("  which is what the old flat count got wrong",
      rows[0][2] != flat_b, str((rows[0][2], flat_b)))

print("\n      and it is found even for a game with no manifest at all")
other = os.path.join(box.archives, "DarkSouls3")
os.makedirs(os.path.join(other, "ModEngine"))
io.open(os.path.join(other, "ModEngine", "tool.exe"), "wb").write(b"x" * 42)
allrows = W.all_stray_folders()
check("all_stray_folders walks the archive root, not the manifests",
      ("DarkSouls3", "ModEngine", 1, 42) in allrows, str(allrows))
check("  and still reports the one for a game that does have manifests",
      ("Fallout4", "Workbase", 3, 7100) in allrows, str(allrows))
check("  finding exactly the two", len(allrows) == 2, str(allrows))

print("\n      parking works from a game folder with no GameType")
(dst, _o) = quiet(W.park_unmanaged_dir, other, "ModEngine")
check("park_unmanaged_dir moves it", bool(dst), str(dst))
check("  intact",
      os.path.isfile(os.path.join(other, CFG.UNMANAGED_FOLDER, "ModEngine",
                                  "tool.exe")), str(dst))
check("nothing is a stray in that game any more",
      [r for r in W.all_stray_folders() if r[0] == "DarkSouls3"] == [],
      str(W.all_stray_folders()))

print("\n      the store is reported in the space overview")
rep_rows = W.unmanaged_report()
check("unmanaged_report names the game folder and its true size",
      rep_rows == [("DarkSouls3", 42, 1)], str(rep_rows))
(_r, spoke) = quiet(W.park_unmanaged_dir, base, "Workbase")
rep_rows = W.unmanaged_report()
check("both stores are reported once both exist",
      sorted(g for g, _b, _f in rep_rows) == ["DarkSouls3", "Fallout4"],
      str(rep_rows))
check("  with the nested bytes counted",
      dict((g, b) for g, b, _f in rep_rows)["Fallout4"] == 7100,
      str(rep_rows))
os.makedirs(os.path.join(box.archives, "Oblivion", CFG.UNMANAGED_FOLDER))
check("an empty store is not reported as costing anything",
      "Oblivion" not in [g for g, _b, _f in W.unmanaged_report()],
      str(W.unmanaged_report()))

# ---------------------------------------------------------------------------
# A stray that is a modlist whose paperwork went missing. Parking one of these
# hides archives you own from a list that still wants them, so the stray
# tables have to tell the two kinds apart.
# ---------------------------------------------------------------------------
print("\n 12.x  a stray that is a list with its manifest missing")

SAVED_FEEDS = A._FEED_CACHE.get("all")


def sidecar(name, title):
    p = os.path.join(box.manifests, name + ".wabbajack.metadata")
    with io.open(p, "w", encoding="utf-8") as f:
        json.dump({"title": title, "game": "Fallout4"}, f)
    return p


A._FEED_CACHE["all"] = None
check("a hand-built folder has nothing to explain",
      W.stray_origin("TTW") is None, str(W.stray_origin("TTW")))
check("  so its note is empty", W.stray_note(W.stray_origin("TTW")) == "")

sidecar("Gone_@@_gone", "A List That Went Missing")
o = W.stray_origin("Gone_@@_gone")
check("a sidecar with no manifest is recognised", bool(o) and o["sidecar"], str(o))
check("  the list is named from the sidecar",
      o and o["title"] == "A List That Went Missing", str(o))
check("  the folder name resolves back to the feed key",
      o and o["key"] == "Gone/gone", str(o))
check("  and with no feeds loaded, published is unknown, not False",
      o and o["published"] is None, str(o))
check("  the note says only what is known",
      W.stray_note(o) == "manifest gone, sidecar remains", W.stray_note(o))

A._FEED_CACHE["all"] = {"Gone/gone": {"title": "Back In The Gallery", "repo": "Gone"}}
o = W.stray_origin("Gone_@@_gone")
check("with feeds loaded, a still-published list is spotted",
      o and o["published"] is True, str(o))
check("  and the note says to adopt rather than park",
      "adopt it" in W.stray_note(o), W.stray_note(o))

A._FEED_CACHE["all"] = {"Other/other": {"title": "Something Else", "repo": "Other"}}
o = W.stray_origin("Gone_@@_gone")
check("a list the gallery no longer carries is said to be gone",
      o and o["published"] is False, str(o))
check("  without suggesting an adoption that cannot happen",
      "adopt" not in W.stray_note(o), W.stray_note(o))

o = W.stray_origin("Never_@@_seen")
check("a manifest-shaped folder that was never ours is not invented",
      o is None, str(o))

rows = W.strays_with_notes([("Fallout4", "Gone_@@_gone", 3, 30),
                            ("Fallout4", "TTW", 1, 10)])
check("the table rows carry the note alongside the size",
      len(rows) == 2 and rows[0][4] and rows[1][4] is None, str(rows))

A._FEED_CACHE["all"] = SAVED_FEEDS

# ---------------------------------------------------------------------------
# Debris no other step reaches. Stage 5 judges archives, the trash runs on a
# timer and backups on theirs; none of that touches what an operating system
# or an editor drops beside them. What matters most here is the second list:
# what the sweep must never pick up.
# ---------------------------------------------------------------------------
print("\n 12.y  leftovers, and everything the sweep must not take")

box = Sandbox(BOX)
wire(box)
box.manifest("Live", GAME, [("a.7z", 10, "nexus")])
box.pool_file(GAME, "Live", "a.7z", 10)


def put(path, body=b"x"):
    full = os.path.join(box.tools, path) if not os.path.isabs(path) else path
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with io.open(full, "wb") as f:
        f.write(body)
    return full


junk = [put(".DS_Store"), put("._run.bat"), put("Thumbs.db"),
        put(os.path.join("runtime", "Thumbs.db")),
        put(os.path.join(box.manifests, "Gone_@@_gone.wabbajack.metadata"), b"{}"),
        put(os.path.join(box.archives, GAME, "Live", ".DS_Store"))]

# Live state, records and managed content. None of this may ever be swept -
# in the runtime folder where it lives now, or beside the scripts where an
# older version left it.
keep = [put("nexus_api_key.txt", b"secret"), put("wj_settings.json", b"{}"),
        put(".wj_autoupdate.lock", b"pid"), put(".nxm_listener_port", b"53443"),
        put(".nxm_prev_handler", b""), put("wj_autoupdate_state.json", b"{}"),
        put("deleted_Fallout4_20260903_165237.csv", b"a,b"),
        put("purged_trash_20260903_165352.csv", b"a,b"),
        put(os.path.join("runtime", ".wj_autoupdate.lock"), b"pid"),
        put(os.path.join("runtime", "wj_autoupdate_state.json"), b"{}"),
        put(os.path.join("runtime", "deleted_Fallout4_20260925_000000.csv"), b"a,b"),
        put(os.path.join("manifest_backups", "Live.wabbajack.bak"), b"z" * 20),
        put(os.path.join(".gallery-cache", "summary.json"), b"[]"),
        put(os.path.join(".venv-win", "Scripts", "._python.exe")),
        put(os.path.join(box.archives, GAME, "Live", "b.7z.part"), b"partial"),
        os.path.join(box.archives, GAME, "Live", "a.7z"),
        os.path.join(box.manifests, "Live.wabbajack")]

rows = W.leftover_report()
found = {os.path.normcase(p) for _c, p, _b in rows}
missed = [p for p in junk if os.path.normcase(os.path.abspath(p)) not in found]
check("every kind of leftover is found", not missed, str(missed))
taken = [p for p in keep if os.path.normcase(os.path.abspath(p)) in found]
check("and nothing live, recorded or managed is", not taken, str(taken))
check("  the API key is never in the list",
      not any("nexus_api_key" in p for _c, p, _b in rows))
check("  nor a resumable .part", not any(p.endswith(".part") for _c, p, _b in rows))
check("  nor anything under .venv-win or .gallery-cache",
      not any(".venv-win" in p or ".gallery-cache" in p for _c, p, _b in rows))
check("  nor a deletion record, which is the only account of what stage 5 took",
      not any("deleted_" in p or "purged_trash_" in p for _c, p, _b in rows))
put(os.path.join("__pycache__", "wj_wizard.pyc"), b"xx")
put(os.path.join("__pycache__", "wj_wizard.pyc"), b"xx")
put("wj_autoupdate_sorter.out", b"y" * 50)
check("  nor what the next run rewrites anyway: __pycache__, captured output",
      not any("__pycache__" in p or p.endswith(".out")
              for _c, p, _b in W.leftover_report()), str(W.leftover_report()))
check("the sidecar of a modlist that is gone is included",
      any(c == "orphaned sidecar" for c, _p, _b in rows), str(rows))
check("sizes are numbers, folders included",
      all(isinstance(b, int) for _c, _p, b in rows), str(rows))

count, freed, failures = W.remove_leftovers(rows)
check("they all delete", count == len(rows) and not failures, str(failures))
check("  and the bytes are reported", freed > 0, str(freed))
check("  every leftover is gone", not any(os.path.exists(p) for p in junk))
check("  everything else is untouched", all(os.path.exists(p) for p in keep),
      str([p for p in keep if not os.path.exists(p)]))
check("a second sweep finds nothing", W.leftover_report() == [], str(W.leftover_report()))

# ---------------------------------------------------------------- 12.8
print("\n 12.8  stage 5e asks twice, and only a yes deletes")
# Found 2026-10-10: the second question passed a keyword its confirmation
# does not take, so the step crashed - and behind the crash it tested the
# answer for truth, which "no" and "change" both are.
import builtins


def drive(fn, answers, *a, **k):
    it = iter(answers)

    def fake(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    real_input = builtins.input
    builtins.input = fake
    try:
        return quiet(fn, *a, **k)
    except W.Quit:
        return None, "(quit)"
    finally:
        builtins.input = real_input


def litter():
    return [put("Thumbs.db"), put(os.path.join("runtime", ".DS_Store")),
            put(os.path.join(box.manifests, "Gone_@@_gone.wabbajack.metadata"), b"{}")]


left = litter()
(res, text) = drive(W.leftover_step, ["d", "n"])
check("'no' at the confirmation deletes nothing", res is False
      and all(os.path.exists(p) for p in left) and "Left alone" in text, text[-300:])
(res, text) = drive(W.leftover_step, ["d", "c", "1", "y"])
check("'change' narrows it to the kinds picked, and only those go",
      res is True and not os.path.exists(left[0]) and not os.path.exists(left[1])
      and os.path.exists(left[2]), text[-400:])
(res, text) = drive(W.leftover_step, ["d", "y"])
check("'yes' deletes what is left", res is True and not any(os.path.exists(p) for p in left),
      text[-300:])

# ---------------------------------------------------------------- 12.9
print("\n 12.9  the sweep stays in its own folders, and takes files only")
outside = os.path.join(BOXROOT, "sb12-outside")
shutil.rmtree(outside, ignore_errors=True)
os.makedirs(outside)
theirs = [os.path.join(outside, n) for n in ("Thumbs.db", "desktop.ini")]
for p in theirs:
    io.open(p, "wb").write(b"someone else's")
link = os.path.join(box.tools, "linked")
import subprocess
made = subprocess.run(["cmd", "/c", "mklink", "/J", link, outside], capture_output=True,
                      text=True, stdin=subprocess.DEVNULL).returncode == 0
check("(a junction can be made here)", made)
if made:
    rows = W.leftover_report()
    check("a junction is not followed: nothing beyond it is listed",
          not any(os.path.normcase(outside) in os.path.normcase(p) for _c, p, _b in rows), str(rows))
    W.remove_leftovers(rows)
    check("  and nothing beyond it is deleted", all(os.path.exists(p) for p in theirs))
    os.rmdir(link)                                   # the junction only, never its target
folder = os.path.join(box.manifests, "Folder_@@_x.wabbajack.metadata")
os.makedirs(os.path.join(folder, "inside"))
io.open(os.path.join(folder, "inside", "keep.txt"), "wb").write(b"mine")
check("a folder named like a sidecar is not an orphaned sidecar",
      not any("Folder_@@_x" in p for _c, p, _b in W.leftover_report()), str(W.leftover_report()))
count, freed, failures = W.remove_leftovers([("orphaned sidecar", folder, 4)])
check("  and handed to the delete anyway, it is left alone, contents and all",
      count == 0 and failures and os.path.isfile(os.path.join(folder, "inside", "keep.txt")),
      str(failures))
shutil.rmtree(outside, ignore_errors=True)

# ---------------------------------------------------------------- 12.10
print("\n 12.10  parking a folder never merges it into one already parked, nor splits it")
box = Sandbox(BOX)
wire(box)
base = os.path.join(box.archives, GAME)
box.pool_file(GAME, os.path.join(CFG.UNMANAGED_FOLDER, "TTW"), "parked-before.7z", 5)
box.pool_file(GAME, "TTW", "new.7z", 6)
(dst, _o) = quiet(W.park_unmanaged_dir, base, "TTW")
store = os.path.join(base, CFG.UNMANAGED_FOLDER)
check("a second TTW is parked beside the first, not into it",
      dst and os.path.basename(dst) == "TTW~1"
      and sorted(os.listdir(os.path.join(store, "TTW"))) == ["parked-before.7z"]
      and sorted(os.listdir(os.path.join(store, "TTW~1"))) == ["new.7z"], str(dst))
box.pool_file(GAME, "Busy", "a.7z", 7)
box.pool_file(GAME, os.path.join("Busy", "sub"), "b.7z", 8)
held_open = io.open(os.path.join(base, "Busy", "a.7z"), "rb")   # in use, as by an install
try:
    (dst, _o) = quiet(W.park_unmanaged_dir, base, "Busy")
finally:
    held_open.close()
src_whole = (os.path.isfile(os.path.join(base, "Busy", "a.7z"))
             and os.path.isfile(os.path.join(base, "Busy", "sub", "b.7z")))
dst_whole = bool(dst) and os.path.isfile(os.path.join(dst, "a.7z")) \
    and os.path.isfile(os.path.join(dst, "sub", "b.7z"))
check("a folder with a file in use moves whole or stays whole, never half and half",
      (src_whole and not dst) or (dst_whole and not os.path.exists(os.path.join(base, "Busy"))),
      str((dst, src_whole, dst_whole)))

shutil.rmtree(BOX, ignore_errors=True)
sys.exit(summary())
