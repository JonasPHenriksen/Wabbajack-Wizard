"""Stage 5 end to end: the deletion path, and the rules it deletes under.

Every case here was first reproduced against the code as it stood on
2026-09-02, when three of them deleted or lost something they should not have:
a wanted build renamed away and then removed, a Master pool emptied by
deleting a manifest named after it, and a freshly adopted list reading as
absent. The rest are the guards added alongside. Nothing here touches the
network or the live share.
"""
import builtins
import csv
import io
import json
import logging
import os
import shutil
import sys
import time

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

BOX = os.path.join(BOXROOT, "sb13")
GAME = "Fallout4"

# The stages that move files first ask whether Wabbajack is running, via
# tasklist. Stubbed to "no" so the suite does not depend on what is open on
# the machine running it; 13.16 exercises the real check's parser and the
# waiting itself.
REAL_RUNNING = W.WB.running_processes
W.WB.running_processes = lambda: []

print("=" * 70)
print("  T13  STAGE 5: DELETING, AND WHAT MUST SURVIVE IT")
print("=" * 70)


def quiet(fn, *a, **k):
    buf = io.StringIO()
    real = W.out
    W.out = lambda msg="": buf.write(str(msg) + "\n")
    try:
        return fn(*a, **k), buf.getvalue()
    finally:
        W.out = real


def wire(box):
    A.MANIFESTS = box.manifests
    A.HERE = box.tools
    A.BACKUPS = os.path.join(box.tools, "manifest_backups")
    A.WJ.ARCHIVE_ROOT = box.archives
    CFG.ARCHIVE_ROOT = box.archives
    G.HERE = box.tools
    A.WJ.set_known_list_dirs(A.WJ.known_list_dirs_from(box.manifests))
    A.WJ.invalidate_existing_cache()


def scripted(answers):
    """Replace input() with a fixed script. Exhausting it reads as EOF,
    which every prompt in the wizard treats as 'stop', never as 'yes'."""
    it = iter(answers)

    def fake(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    return fake


def drive(fn, answers, *a, **k):
    """Run an interactive stage against scripted answers, quietly."""
    real_input = builtins.input
    builtins.input = scripted(answers)
    try:
        return quiet(fn, *a, **k)
    except W.Quit:
        return None, "(quit)"
    finally:
        builtins.input = real_input


def master_of(box, game=GAME):
    d = os.path.join(box.archives, game, "Master")
    return {n: os.path.getsize(os.path.join(d, n))
            for n in os.listdir(d)} if os.path.isdir(d) else None


# ---------------------------------------------------------------- 13.1
print("\n 13.1  the '~N' suffix is storage, not identity")
for raw, want in (("X.7z", "X.7z"), ("X~1.7z", "X.7z"), ("X~12.7z", "X.7z"),
                  ("X~1~1.7z", "X.7z"), ("X.tar~1.gz", "X.tar.gz"),
                  ("X~1", "X"), (".45 Auto~2.zip", ".45 Auto.zip")):
    check("%-16s reads as %s" % (raw, want), CFG.canonical_name(raw) == want,
          CFG.canonical_name(raw))
check("a name with no suffix is left alone",
      CFG.canonical_name("Mod v2.zip") == "Mod v2.zip")
check("'~a' is not a suffix", CFG.canonical_name("X~a.7z") == "X~a.7z")
check("is_suffixed tells them apart",
      CFG.is_suffixed("X~1.7z") and not CFG.is_suffixed("X.7z"))
want = {"synthesis.zip": {200}}
check("is_wanted sees a suffixed copy at a wanted size",
      W.is_wanted(want, "Synthesis~1.zip", 200))
check("  but not at an unwanted size", not W.is_wanted(want, "Synthesis~1.zip", 999))
check("the downloader re-exports the same rule",
      A.WJ.canonical_name("a~3.rar") == "a.rar")

# ---------------------------------------------------------------- 13.2
print("\n 13.2  deleting a list must not lose a build another list needs")
# A was staged last, so it holds shared.7z@200 - which B also wants. Master
# already holds a 999-byte junk build under the same name.
box = Sandbox(BOX)
wire(box)
box.manifest("A", GAME, [("shared.7z", 200, "nexus"), ("only-a.7z", 10, "nexus")])
box.manifest("B", GAME, [("shared.7z", 200, "nexus"), ("only-b.7z", 20, "nexus")])
box.pool_file(GAME, "A", "shared.7z", 200)
box.pool_file(GAME, "A", "shared.7z.meta", 30)
box.pool_file(GAME, "A", "only-a.7z", 10)
box.pool_file(GAME, "B", "only-b.7z", 20)
box.pool_file(GAME, "Master", "shared.7z", 999)
box.pool_file(GAME, "Master", "shared.7z.meta", 31)
wire(box)
records = A.load_plan_records()
survivors = {fn: e for fn, e in records.items() if fn != "A.wabbajack"}
keep = W.wanted_builds(survivors, GAME)
(res, _o) = quiet(W.delete_modlist, {"lists": {}, "map": {}},
                  {"file": "A.wabbajack", "game": GAME}, keep=keep)
m = master_of(box)
check("the build B wants keeps the real name in Master",
      m.get("shared.7z") == 200, str(m))
check("  and its sidecar came with it", m.get("shared.7z.meta") == 30, str(m))
check("the junk build is the one parked under a suffix",
      m.get("shared~1.7z") == 999, str(m))
check("  with ITS sidecar renamed alongside", m.get("shared~1.7z.meta") == 31, str(m))
check("A's own archive went to Master too", m.get("only-a.7z") == 10, str(m))
check("A's folder is gone", not os.path.isdir(os.path.join(box.archives, GAME, "A")))

print("\n      the rebuild then hands B the right build")
rc, out = box.run("wabbajack_archive_sorter_v2.py",
                  ["--game", GAME, "--cleanup-only", "--yes"], expect=0)
check("cleanup exits 0", rc == 0)
check("B receives shared.7z at 200 bytes",
      dict(box.where("shared.7z")).get("B") == 200, str(box.where("shared.7z")))
check("the junk build stays in Master under its suffix",
      box.where("shared~1.7z") == [("Master", 999)], str(box.where("shared~1.7z")))

print("\n      and 5c removes only what nothing wants")
A.WJ.invalidate_existing_cache()
records = A.load_plan_records()
keep = W.wanted_builds(records, GAME)
(res, _o) = quiet(W.clear_master, GAME, keep)
deleted, kept, freed = res
names = sorted(n for n, _s in deleted)
check("deleted: A's leftover and the junk build (and their sidecars)",
      names == ["only-a.7z", "shared~1.7z", "shared~1.7z.meta"], str(names))
check("a 200-byte shared.7z still exists, in B's folder",
      dict(box.where("shared.7z")).get("B") == 200, str(box.where("shared.7z")))

# ---------------------------------------------------------------- 13.3
print("\n 13.3  a wanted build parked under a suffix is protected and restored")
box = Sandbox(BOX)
wire(box)
box.manifest("L", GAME, [("syn.zip", 200, "nexus"), ("other.7z", 5, "nexus")])
box.manifest("M", GAME, [("syn.zip", 111, "nexus")])
box.pool_file(GAME, "Master", "syn.zip", 111)        # M's build, canonical name
box.pool_file(GAME, "Master", "syn~1.zip", 200)      # L's build, parked
box.pool_file(GAME, "Master", "syn~1.zip.meta", 9)
box.pool_file(GAME, "Master", "junk~1.zip", 42)      # nobody's
wire(box)
records = A.load_plan_records()
r = W.master_report(GAME, records)
check("master_report protects the suffixed wanted build",
      r["kept_files"] == 2 and r["free_files"] == 1, str(r))
check("  and counts only the nobody's file as reclaimable", r["free_bytes"] == 42)

A.WJ.invalidate_existing_cache()
idx = A.WJ.index_existing(os.path.join(box.archives, GAME))
check("the downloader's index files the suffixed copy under the real name",
      sorted(s for _p, s in idx.get("syn.zip", [])) == [111, 200], str(idx.get("syn.zip")))
hit, why = A.WJ.present_match(idx, {"name": "syn.zip", "size": 200})
check("  so L's build counts as present (%s)" % why, bool(hit))

rc, out = box.run("wabbajack_archive_sorter_v2.py",
                  ["--game", GAME, "--list", "L", "--yes"], expect=0)
check("staging L exits 0", rc == 0)
hits = dict(box.where("syn.zip"))
check("L receives its build under the name the manifest uses",
      hits.get("L") == 200, str(box.where("syn.zip") + box.where("syn~1.zip")))
check("  with the sidecar renamed to match",
      os.path.isfile(os.path.join(box.archives, GAME, "L", "syn.zip.meta")))
# Two lists want the name, so the rebuild parks M's build in Shared: that is
# the sorter's standing rule for a shared name, and the point here is only
# that the 111-byte build survives the round trip.
check("M's build survives, under the real name",
      111 in dict(box.where("syn.zip")).values(), str(box.where("syn.zip")))
check("the nobody's suffixed file is not churned by the rebuild",
      box.where("junk~1.zip") == [("Master", 42)], str(box.where("junk~1.zip")))

# ---------------------------------------------------------------- 13.4
print("\n 13.4  a manifest named after a pool folder cannot empty that pool")
box = Sandbox(BOX)
wire(box)
box.manifest("Master", GAME, [("x.7z", 1, "nexus")])
box.manifest("Keep", GAME, [("k.7z", 5, "nexus")])
box.pool_file(GAME, "Master", "precious1.7z", 111)
box.pool_file(GAME, "Master", "precious2.7z", 222)
box.pool_file(GAME, "Keep", "k.7z", 5)
wire(box)
before = master_of(box)
(res, spoke) = quiet(W.delete_modlist, {"lists": {}, "map": {}},
                     {"file": "Master.wabbajack", "game": GAME})
check("delete_modlist refuses", res == (0, 0, 0), str(res))
check("  and says why", "named after a pool folder" in spoke, spoke[-200:])
check("Master is untouched", master_of(box) == before, str(master_of(box)))
check("  and the manifest is still there",
      os.path.isfile(os.path.join(box.manifests, "Master.wabbajack")))
for nm in ("Shared", CFG.UNMANAGED_FOLDER, "Nolvus"):
    check("'%s' is a reserved name too" % nm, nm.lower() in A.WJ.reserved_list_names())

print("\n      the downloader refuses to download INTO a pool")
rc, out = box.run("wabbajack_archive_downloader.py",
                  ["--game", GAME, "--list", "Master", "--dry-run", "--yes"],
                  expect=2)
check("--list Master exits 2", rc == 2)
check("  naming the clash", "reserved pool folder" in out, out.strip()[-200:])

print("\n      and stage 5a refuses to offer it")
records = A.load_plan_records()
state = {"lists": {}, "map": {}}
# lists sort by filename: Keep=1, Master=2. Pick 2, get refused, skip 5a;
# then decline to clear Master.
(res, text) = drive(W.stage_space, ["2", "s", "y", "s"], state, records)
check("5a names the refused manifest", "Cannot delete Master" in text, text[-600:])
check("  and Master is still intact", master_of(box) == before, str(master_of(box)))
check("the space table flags it", "named after a pool folder" in text)

# ---------------------------------------------------------------- 13.5
print("\n 13.5  a list adopted mid-run is a real list, not a stray")
box = Sandbox(BOX)
wire(box)
box.manifest("Old", GAME, [("o.7z", 1, "nexus")])
wire(box)                                   # the start-up snapshot
box.manifest("New", GAME, [("n.7z", 7, "nexus")])     # adopted in stage 1
box.pool_file(GAME, "New", "n.7z", 7)                 # fetched in stage 3
A.WJ.invalidate_existing_cache()
recs = A.load_plan_records()
st = W.list_status(recs["New.wabbajack"])
check("with a stale snapshot the download reads as missing (the old bug)",
      st["missing"] == 1 and st["present"] == 0, str(st))
W.refresh_known_dirs()
st = W.list_status(recs["New.wabbajack"])
check("refresh_known_dirs() makes it present",
      st["missing"] == 0 and st["present"] == 1, str(st))
check("  and the set now names the new folder",
      A.WJ.KNOWN_LIST_DIRS == frozenset({"old", "new"}), str(A.WJ.KNOWN_LIST_DIRS))

# ---------------------------------------------------------------- 13.6
print("\n 13.6  when the rebuild after a deletion fails, 5c is not offered")
box = Sandbox(BOX)
wire(box)
box.manifest("Doomed", GAME, [("d.7z", 50, "nexus"), ("both.7z", 60, "nexus")])
box.manifest("Keep", GAME, [("both.7z", 60, "nexus")])
box.pool_file(GAME, "Doomed", "d.7z", 50)
box.pool_file(GAME, "Doomed", "both.7z", 60)         # Keep needs this
box.pool_file(GAME, "Master", "junk.7z", 999)
wire(box)
records = A.load_plan_records()
real_run = A.run_script
A.run_script = lambda script, args, label, interactive=False: 2   # the sorter "fails"
try:
    state = {"lists": {}, "map": {}, "staged": {}}
    (res, text) = drive(W.stage_space, ["1", "y", "y", "a", "y"], state, records)
finally:
    A.run_script = real_run
m = master_of(box)
check("the list was deleted and its archives moved to Master",
      m.get("d.7z") == 50 and m.get("both.7z") == 60, str(m))
check("the failure is reported", "FAILED (sorter exited 2)" in text, text[-900:])
check("Master is not offered for clearing",
      "not offered - its rebuild failed" in text, text[-900:])
check("  so nothing in Master was deleted, junk included",
      m.get("junk.7z") == 999 and len(m) == 3, str(m))
check("  and no deletion record was written",
      not [f for f in os.listdir(box.tools) if f.startswith("deleted_")])

# ---------------------------------------------------------------- 13.7
print("\n 13.7  a manifest whose demand cannot be trusted disarms 5c")
box = Sandbox(BOX)
wire(box)
box.manifest("Fine", GAME, [("f.7z", 1, "nexus")])
box.manifest("Empty", GAME, [])                       # reads fine, wants nothing
box.manifest("Other", "Morrowind", [("m.7z", 2, "nexus")])
box.pool_file(GAME, "Master", "junk.7z", 999)
box.pool_file("Morrowind", "Master", "mjunk.7z", 888)
wire(box)
records = A.load_plan_records()
unreadable, empty = W.demand_problems(records)
check("an archive-less manifest is reported against its folder",
      empty == {"fallout4": ["Empty.wabbajack"]}, str(empty))
check("  and nothing is unreadable", unreadable == [])
(res, text) = drive(W.stage_space, ["s", "y", "a", "y", "s"], {"lists": {}, "map": {}},
                    records)
check("Fallout 4's Master is not offered", "Fallout 4: not offered" in text
      and "lists no archives" in text, text[-700:])
check("  and its junk survives", master_of(box).get("junk.7z") == 999)
check("Morrowind, unaffected, IS cleared",
      master_of(box, "Morrowind") == {}, str(master_of(box, "Morrowind")))

print("\n      an unreadable manifest disarms every Master")
box.manifest("Broken", GAME, [], corrupt="notzip")
records = A.load_plan_records()
unreadable, _e = W.demand_problems(records)
check("it is reported as unreadable", unreadable == ["Broken.wabbajack"], str(unreadable))
box.pool_file("Morrowind", "Master", "mjunk2.7z", 777)
(res, text) = drive(W.stage_space, ["s", "y", "a", "y", "s"], {"lists": {}, "map": {}},
                    records)
check("5c refuses outright", "Not clearing any Master" in text, text[-500:])
check("  and Morrowind's junk is still there",
      master_of(box, "Morrowind").get("mjunk2.7z") == 777)

print("\n      the sorter refuses an empty in-scope list, like an unreadable one")
box = Sandbox(BOX)
wire(box)
box.manifest("Fine", GAME, [("f.7z", 1, "nexus")])
box.manifest("Empty", GAME, [])
box.manifest("OtherEmpty", "Morrowind", [])
box.pool_file(GAME, "Fine", "f.7z", 1)
before = box.files()
rc, out = box.run("wabbajack_archive_sorter_v2.py",
                  ["--game", GAME, "--cleanup-only", "--yes"], expect=2)
check("exit 2 with an empty in-scope manifest", rc == 2)
check("  naming it", "Empty" in out and "no archives" in out, out.strip()[-200:])
check("  and moving nothing", box.files() == before)
os.remove(os.path.join(box.manifests, "Empty.wabbajack"))
rc, out = box.run("wabbajack_archive_sorter_v2.py",
                  ["--game", GAME, "--cleanup-only", "--yes"], expect=0)
check("an empty list for ANOTHER game does not block this one", rc == 0)

# ---------------------------------------------------------------- 13.8
print("\n 13.8  two GameTypes, one folder: both protect it")
box = Sandbox(BOX)
wire(box)
box.manifest("SE", "SkyrimSpecialEdition", [("se.7z", 10, "nexus")])
box.manifest("LE", "Skyrim", [("le.7z", 20, "nexus")])
box.pool_file("Skyrim", "Master", "se.7z", 10)
box.pool_file("Skyrim", "Master", "le.7z", 20)
box.pool_file("Skyrim", "Master", "nobody.7z", 30)
wire(box)
records = A.load_plan_records()
want = W.wanted_builds(records, "SkyrimSpecialEdition")
check("asking for SE's pool includes what the LE list wants",
      "le.7z" in want and "se.7z" in want, str(sorted(want)))
masters = W.measure_masters(records)
check("the shared folder is measured once, not per GameType",
      len(masters) == 1, str([m["label"] for m in masters]))
check("  under a label naming both games",
      "Legendary" in masters[0]["label"] and "Special" in masters[0]["label"],
      masters[0]["label"])
check("  and only the nobody's file is reclaimable",
      masters[0]["free_files"] == 1 and masters[0]["kept_files"] == 2, str(masters[0]))
(res, _o) = quiet(W.clear_master, "SkyrimSpecialEdition", want)
deleted, kept, freed = res
check("clearing for SE deletes only nobody.7z",
      [n for n, _s in deleted] == ["nobody.7z"], str(deleted))
check("  and keeps the LE list's archive",
      os.path.isfile(os.path.join(box.archives, "Skyrim", "Master", "le.7z")))

print("\n      and the sorter's scope is the folder too")
box = Sandbox(BOX)
wire(box)
box.manifest("SE", "SkyrimSpecialEdition", [("se.7z", 10, "nexus")])
box.manifest("LE", "Skyrim", [("le.7z", 20, "nexus")])
box.pool_file("Skyrim", "SE", "se.7z", 10)
box.pool_file("Skyrim", "LE", "le.7z", 20)
rc, out = box.run("wabbajack_archive_sorter_v2.py",
                  ["--game", "SkyrimSpecialEdition", "--cleanup-only", "--yes"],
                  expect=0)
check("cleanup for SE leaves the LE list's archive in ITS folder",
      box.where("le.7z") == [("LE", 20)], str(box.where("le.7z")))
check("  and says the folder is shared", "share this folder" in out)

print("\n      the gallery groups by folder the same way")
check("pool_key folds LE and SE onto one pool",
      G.pool_key("skyrim") == G.pool_key("SkyrimSpecialEdition") == "skyrim")
check("  while a game with its own folder keeps its own key",
      G.pool_key("fallout4") == "fallout4")
check("Skyrim VR draws from the Skyrim pool as well",
      G.pool_key("skyrimvr") == "skyrim" and A.WJ.folder_key("SkyrimVR") == "skyrim")
check("  and Enderal SE, whose lists are three-quarters Skyrim SE mods",
      G.pool_key("enderalspecialedition") == "skyrim"
      and A.WJ.folder_key("EnderalSpecialEdition") == "skyrim")

# ---------------------------------------------------------------- 13.9
print("\n 13.9  the 'any known modlist' rule refuses every non-permanent gap")
box = Sandbox(BOX)
wire(box)


def rep(archives, status="Available"):
    return {"MachineURL": "x", "Name": "x", "Version": "1", "Status": status,
            "Failures": 0,
            "Archives": [{"Status": "Valid",
                          "Original": {"Name": n, "Size": s, "Hash": "A=",
                                       "Meta": "", "State": {
                                           "$type": "NexusDownloader, Wabbajack.Lib"}}}
                         for n, s in archives]}


def feed(machine, game="fallout4", force_down=False):
    return {"repo": "r", "machine": machine, "game": game, "title": machine,
            "version": "1", "updated": "", "download": "u", "hash": "h",
            "size": 1, "archives": 1, "arch_size": 1, "force_down": force_down}


def row(key):
    return {"machineURL": key, "failed": 0, "passed": 1, "updating": 0,
            "mirrored": 0, "modlist_missing": False, "has_failures": False}


feeds = {"r/ok": feed("ok"), "r/pulled": feed("pulled", force_down=True),
         "r/new": feed("new"), "r/odd": feed("odd"),
         # Pulled lists that have dropped out of the summary altogether, as
         # LegacyRim did on 2026-09-03: known from the feed's flag, or from a
         # report cached before they vanished.
         "r/gone": feed("gone", force_down=True),
         "r/gone2": feed("gone2")}
summ = {k: row(k) for k in ("r/ok", "r/pulled", "r/odd")}     # the rest: no row
G._write_cache(G.report_rel("r/ok"), json.dumps(rep([("g.7z", 7)])).encode())
G._write_cache(G.report_rel("r/pulled"), json.dumps(rep([], "ForcedDown")).encode())
G._write_cache(G.report_rel("r/odd"), json.dumps(rep([], "Available")).encode())
G._write_cache(G.report_rel("r/gone2"), json.dumps(rep([], "ForcedDown")).encode())
want, checked, unchecked = G.published_builds({"fallout4"}, feeds, summ, A.WJ)
check("one list contributed", checked == 1 and want == {"g.7z": {7}}, str(want))
check("a pulled list is named with the permanent reason",
      unchecked.get("r/pulled") == G.PULLED, str(unchecked))
check("a list with no summary row is named as not yet validated",
      unchecked.get("r/new") == G.UNVALIDATED, str(unchecked))
check("an empty report that is NOT ForcedDown is named as such",
      "status Available" in unchecked.get("r/odd", ""), str(unchecked))
check("a pulled list with no summary row is still pulled, by its feed flag",
      unchecked.get("r/gone") == G.PULLED, str(unchecked))
check("  or by the ForcedDown report cached before it vanished",
      unchecked.get("r/gone2") == G.PULLED, str(unchecked))

G._write_cache(G.SUMMARY_PATH, json.dumps(list(summ.values())).encode())
A.reset_feed_cache()
A._FEED_CACHE["all"] = dict(feeds)
A._FEED_CACHE["complete"] = True
want, note, pulled = quiet(W.gallery_protection, {"fallout4"})[0]
check("gallery_protection refuses on the non-permanent gaps",
      want is None, str(note))
check("  naming them and their reasons",
      "r/new" in (note or "") and "r/odd" in (note or ""), str(note))
check("  and not blaming the pulled lists",
      not any(k in (note or "") for k in ("r/pulled", "r/gone", "r/gone2")), str(note))

A._FEED_CACHE["all"] = {k: feeds[k] for k in ("r/ok", "r/pulled", "r/gone", "r/gone2")}
want, note, pulled = quiet(W.gallery_protection, {"fallout4"})[0]
check("with only the permanent gaps left it answers",
      want == {"g.7z": {7}}, str(want))
check("  naming every pulled list separately",
      pulled == ["r/gone", "r/gone2", "r/pulled"], str(pulled))
check("  and saying so in the note", "pulled by their authors" in note, note)
A.reset_feed_cache()

# ---------------------------------------------------------------- 13.10
print("\n 13.10  the deletion record is written as it goes")
box = Sandbox(BOX)
wire(box)
box.manifest("L", GAME, [("keep.7z", 1, "nexus")])
for n, s in (("a.7z", 10), ("b.7z", 20), ("c.7z", 30), ("keep.7z", 1)):
    box.pool_file(GAME, "Master", n, s)
wire(box)
records = A.load_plan_records()
keep = W.wanted_builds(records, GAME)
_b, master, _s = A.WJ.resolve_game_dirs(GAME)
record = W.DeletionRecord(GAME, "your modlists only", master)


class Interrupted(Exception):
    pass


def flaky(name, size, reason="", to=""):
    record.note(name, size, reason, to)
    if record.rows == 2:
        raise Interrupted()        # the run dies after the second deletion


try:
    quiet(W.clear_master, GAME, keep, on_delete=flaky)
    check("the interruption reached us", False)
except Interrupted:
    check("the interruption reached us", True)
rows = list(csv.reader(io.open(record.path, encoding="utf-8")))
check("the record already holds the two deletions",
      len(rows) == 3 and rows[0] == ["archive", "size_bytes", "reason",
                                     "trashed_to", "master", "rule"],
      str(rows))
check("  naming the files that are actually gone",
      [r[0] for r in rows[1:]] == ["a.7z", "b.7z"]
      and not os.path.exists(os.path.join(master, "a.7z"))
      and os.path.exists(os.path.join(master, "c.7z")), str(rows))
check("  with the rule and the folder on every row",
      all(r[4] == master and r[5] == "your modlists only" for r in rows[1:]))
check("  and where each one went",
      all(os.path.isfile(r[3]) and os.sep + "Trash" + os.sep in r[3]
          for r in rows[1:]), str([r[3] for r in rows[1:]]))
check("close() keeps a record that has rows", record.close() == record.path)
empty_rec = W.DeletionRecord(GAME, "any known modlist", master)
check("  and drops one that has none",
      empty_rec.close() is None and not os.path.exists(empty_rec.path))

# ---------------------------------------------------------------- 13.11
print("\n 13.11  stage 5a says when its rebuild will un-stage stage 4's list")
box = Sandbox(BOX)
wire(box)
box.manifest("Staged", GAME, [("s.7z", 1, "nexus"), ("both.7z", 2, "nexus")])
box.manifest("Doomed", GAME, [("d.7z", 3, "nexus"), ("both.7z", 2, "nexus")])
box.pool_file(GAME, "Staged", "s.7z", 1)
box.pool_file(GAME, "Staged", "both.7z", 2)          # staged: holds the shared file
box.pool_file(GAME, "Doomed", "d.7z", 3)
wire(box)
records = A.load_plan_records()
state = {"lists": {}, "map": {}, "staged": {"fallout4": "Staged"}}
# lists sort: Doomed=1, Staged=2. Delete Doomed; decline 5c.
(res, text) = drive(W.stage_space, ["1", "y", "y", "s"], state, records)
check("the confirmation warns that Staged will be un-staged",
      "UN-STAGES Staged" in text, text[-1200:])
check("after the rebuild the staged marker is cleared",
      state.get("staged") == {}, str(state.get("staged")))
check("  and the shared file is still exactly where the rebuild put it",
      box.where("both.7z") in ([("Shared", 2)], [("Staged", 2)]),
      str(box.where("both.7z")))

print("\n      stage 4 is what records the marker")
state = {"lists": {}, "map": {}}
records = A.load_plan_records()
# Doomed is gone, so Staged is row 1 now. Install offers are switched off:
# offer_install() would otherwise look for the real Wabbajack next to the
# real scripts and run its list-games probe from inside a test.
saved_mode = W.CFG.INSTALL_MODE
W.CFG.INSTALL_MODE = "off"
try:
    (res, text) = drive(W.stage_prepare, ["1"], records, state)
finally:
    W.CFG.INSTALL_MODE = saved_mode

check("staging records which list the folder is arranged for",
      state.get("staged", {}).get("fallout4") == "Staged", str(state.get("staged")))

# ---------------------------------------------------------------- 13.12
print("\n 13.12  a Master copy of a build held elsewhere is a duplicate")
box = Sandbox(BOX)
wire(box)
box.manifest("L", GAME, [("held.7z", 10, "nexus"), ("shared.7z", 20, "nexus"),
                         ("twice.7z", 30, "nexus"), ("lonely.7z", 40, "nexus"),
                         ("stray.7z", 50, "nexus"), ("wrongsize.7z", 5, "nexus")])
box.pool_file(GAME, "L", "held.7z", 10)             # L holds this build
box.pool_file(GAME, "Shared", "shared.7z", 20)      # Shared holds this one
box.pool_file(GAME, "Master", "held.7z", 10)        # so this is a duplicate
box.pool_file(GAME, "Master", "held.7z.meta", 1)    #   and its sidecar goes too
box.pool_file(GAME, "Master", "shared.7z", 20)      # duplicate of Shared's
box.pool_file(GAME, "Master", "twice.7z", 30)       # two copies inside Master:
box.pool_file(GAME, "Master", "twice~1.7z", 30)     #   the suffixed one goes
box.pool_file(GAME, "Master", "twice~1.7z.meta", 3)
box.pool_file(GAME, "Master", "lonely.7z", 40)      # wanted, the only copy: kept
box.pool_file(GAME, "Master", "stray.7z", 50)       # wanted; its other copy is
box.pool_file(GAME, "TTW", "stray.7z", 50)          #   in a STRAY folder: kept
box.pool_file(GAME, "Master", "wrongsize.7z", 99)   # a wanted name, wrong size
wire(box)
records = A.load_plan_records()
_b, master, _s = A.WJ.resolve_game_dirs(GAME)
r = W.master_report(GAME, records)
check("three duplicates are counted", r["dup_files"] == 3 and r["dup_bytes"] == 60,
      str(r))
check("  and included in what is reclaimable, with the unwanted build",
      r["free_files"] == 4 and r["free_bytes"] == 60 + 99, str(r))
check("  while the only copies of wanted builds are kept",
      r["kept_files"] == 3, str(r))
wide = W.master_report(GAME, records, {"wrongsize.7z": {99}})
check("under the wide rule the duplicates are still reclaimable",
      wide["wide_free_files"] == 3 and wide["wide_kept_files"] == 4, str(wide))

idx = A.WJ.index_existing(os.path.join(box.archives, GAME))
here = os.path.join(master, "held.7z")
other = W.duplicate_of(idx, master, here, "held.7z", 10)
check("duplicate_of names the copy in L's folder",
      other and os.path.normcase(other) == os.path.normcase(
          os.path.join(box.archives, GAME, "L", "held.7z")), str(other))
check("  the canonical-named copy inside Master is the one that stays",
      W.duplicate_of(idx, master, os.path.join(master, "twice.7z"), "twice.7z", 30)
      is None
      and W.duplicate_of(idx, master, os.path.join(master, "twice~1.7z"),
                         "twice~1.7z", 30) is not None)
check("  a copy in a stray folder does not count",
      W.duplicate_of(idx, master, os.path.join(master, "stray.7z"), "stray.7z", 50)
      is None)
os.remove(os.path.join(box.archives, GAME, "L", "held.7z"))
check("  a copy that has vanished since the index was built does not count",
      W.duplicate_of(idx, master, here, "held.7z", 10) is None)
io.open(os.path.join(box.archives, GAME, "L", "held.7z"), "wb").write(b"x" * 10)

record = W.DeletionRecord(GAME, "your modlists only", master)
(res, _o) = quiet(W.clear_master, GAME, W.wanted_builds(records, GAME),
                  on_delete=record.note)
deleted, kept, freed = res
rec_path = record.close()
names = sorted(n for n, _s in deleted)
check("deleted: the duplicates, their sidecars, and the unwanted build",
      names == ["held.7z", "held.7z.meta", "shared.7z", "twice~1.7z",
                "twice~1.7z.meta", "wrongsize.7z"], str(names))
check("  freeing exactly their bytes", freed == 10 + 1 + 20 + 30 + 3 + 99, str(freed))
check("  keeping the three only copies", kept == 3, str(kept))
for n in ("twice.7z", "lonely.7z", "stray.7z"):
    check("  %s is still in Master" % n, os.path.isfile(os.path.join(master, n)))
check("the copies elsewhere are untouched",
      os.path.isfile(os.path.join(box.archives, GAME, "L", "held.7z"))
      and os.path.isfile(os.path.join(box.archives, GAME, "Shared", "shared.7z")))
rows = {r[0]: r for r in list(csv.reader(io.open(rec_path, encoding="utf-8")))[1:]}
check("the record says which copy made each duplicate redundant, and its hash",
      rows["held.7z"][2].startswith("duplicate of ")
      and os.path.join("L", "held.7z") in rows["held.7z"][2]
      and "xxh64" in rows["held.7z"][2],
      str(rows.get("held.7z")))
check("  including a duplicate inside Master",
      os.path.join("Master", "twice.7z") in rows["twice~1.7z"][2],
      str(rows.get("twice~1.7z")))
check("  and gives the plain reason for the rest",
      rows["wrongsize.7z"][2] == "wanted by no list"
      and rows["held.7z.meta"][2] == "sidecar of held.7z",
      str((rows.get("wrongsize.7z"), rows.get("held.7z.meta"))))
trash = os.path.join(box.archives, GAME, "Trash")
check("everything reclaimed is in the trash, sidecars beside their archives",
      sorted(os.listdir(trash)) == ["held.7z", "held.7z.meta", "shared.7z",
                                    "twice~1.7z", "twice~1.7z.meta", "wrongsize.7z"],
      str(sorted(os.listdir(trash))))

# ---------------------------------------------------------------- 13.13
print("\n 13.13  a duplicate is proven by hash before it goes")
box = Sandbox(BOX)
wire(box)
box.manifest("L", GAME, [("same.7z", 8, "nexus"), ("real.7z", 8, "nexus")])
box.pool_file(GAME, "L", "real.7z", 8)
box.pool_file(GAME, "Master", "real.7z", 8)               # identical bytes
box.pool_file(GAME, "Master", "same.7z", 8)               # same size as L's...
io.open(os.path.join(box.archives, GAME, "L", "same.7z"), "wb").write(b"y" * 8)
wire(box)
records = A.load_plan_records()
r = W.master_report(GAME, records)
check("the estimate counts both as duplicates (it does not hash)",
      r["dup_files"] == 2, str(r))
(res, _o) = quiet(W.clear_master, GAME, W.wanted_builds(records, GAME))
deleted, kept, freed = res
check("only the hash-identical copy goes",
      [n for n, _s in deleted] == ["real.7z"], str(deleted))
check("  the same-size pair with different bytes is kept",
      kept == 1 and os.path.isfile(os.path.join(box.archives, GAME, "Master", "same.7z")))

# ---------------------------------------------------------------- 13.14
print("\n 13.14  reclaimed files wait in the trash, then go for good")
box = Sandbox(BOX)
wire(box)
box.manifest("L", GAME, [("keep.7z", 1, "nexus")])
box.pool_file(GAME, "Master", "keep.7z", 1)
box.pool_file(GAME, "Master", "junk.7z", 50)
box.pool_file(GAME, "Master", "junk.7z.meta", 5)
os.utime(os.path.join(box.archives, GAME, "Master", "junk.7z"),
         (1_000_000, 1_000_000))                          # an ancient archive
wire(box)
records = A.load_plan_records()
(res, _o) = quiet(W.clear_master, GAME, W.wanted_builds(records, GAME))
trash = os.path.join(box.archives, GAME, "Trash")
check("the unwanted file is in the trash, not deleted",
      os.path.isfile(os.path.join(trash, "junk.7z")))
check("  with its sidecar beside it", os.path.isfile(os.path.join(trash, "junk.7z.meta")))
check("  stamped with its arrival, not its own old mtime",
      time.time() - os.path.getmtime(os.path.join(trash, "junk.7z")) < 300)
check("  while the wanted file stays in Master",
      os.path.isfile(os.path.join(box.archives, GAME, "Master", "keep.7z")))
A.WJ.invalidate_existing_cache()
idx = A.WJ.index_existing(os.path.join(box.archives, GAME))
check("the trash is invisible to the index", "junk.7z" not in idx, str(sorted(idx)))
check("  and is not a stray", W.stray_folders(GAME) == [], str(W.stray_folders(GAME)))
check("  and a modlist may not be called Trash", "trash" in A.WJ.reserved_list_names())
purged, rec = A.purge_trash()
check("a fresh arrival survives the 30-day purge",
      purged == [] and rec is None and os.path.isfile(os.path.join(trash, "junk.7z")))
stale = time.time() - 31 * 86400
os.utime(os.path.join(trash, "junk.7z"), (stale, stale))
purged, rec = A.purge_trash()
check("a file 31 days in the trash is purged",
      [p[1] for p in purged] == ["junk.7z"]
      and not os.path.exists(os.path.join(trash, "junk.7z")), str(purged))
check("  its sidecar, younger, is not yet",
      os.path.isfile(os.path.join(trash, "junk.7z.meta")))
check("  and the purge is recorded",
      bool(rec) and os.path.isfile(rec)
      and "junk.7z" in io.open(rec, encoding="utf-8").read(), str(rec))
purged, rec = A.purge_trash(days=0)
check("days=0 empties the trash",
      [p[1] for p in purged] == ["junk.7z.meta"] and os.listdir(trash) == [],
      str(purged))

# ---------------------------------------------------------------- 13.15
print("\n 13.15  stale to-do CSVs are removed")
box = Sandbox(BOX)
wire(box)
box.manifest("Done", GAME, [("d.7z", 1, "nexus")])
box.manifest("Busy", GAME, [("b.7z", 2, "nexus")])
box.pool_file(GAME, "Done", "d.7z", 1)                     # complete
wire(box)
for n in ("nexus_todo_Done.csv", "nexus_pages_Done.csv",
          "download_failures_Done.csv", "nexus_todo_Busy.csv",
          "nexus_todo_Gone.csv", "deleted_Fallout4_20260101_000000.csv"):
    io.open(os.path.join(box.tools, n), "w").write("x")
records = A.load_plan_records()
(res, text) = drive(W.stage_incomplete, ["s"], records, False)
left = sorted(n for n in os.listdir(box.tools) if n.endswith(".csv"))
check("CSVs for a complete list and a vanished list are gone; a record stays",
      left == ["deleted_Fallout4_20260101_000000.csv", "nexus_todo_Busy.csv"],
      str(left))
check("  and the stage said so", "stale to-do CSV" in text, text[-300:])

# ---------------------------------------------------------------- 13.16
print("\n 13.16  nothing moves while Wabbajack is running")
sample = ('"Wabbajack.exe","1234","Console","1","500,000 K"\n'
          '"notepad.exe","9","Console","1","1 K"\n'
          '"wabbajack-cli.exe","77","Console","1","2 K"\n')
check("tasklist output is parsed for both Wabbajack processes",
      W.WB.parse_tasklist(sample) == [(1234, "Wabbajack.exe"), (77, "wabbajack-cli.exe")],
      str(W.WB.parse_tasklist(sample)))
check("  and nothing else", W.WB.parse_tasklist('"notepad.exe","9","Console","1","1 K"') == [])
calls = {"n": 0}


def fake_running():
    calls["n"] += 1
    return [(1234, "Wabbajack.exe")] if calls["n"] <= 2 else []


W.WB.running_processes = fake_running
try:
    (res, text) = drive(W.wait_for_wabbajack, ["r", "r"], "Staging")
finally:
    W.WB.running_processes = lambda: []
check("it waits, re-checks on request, and proceeds once Wabbajack is gone",
      res is True and calls["n"] == 3, str((res, calls)))
check("  saying what it is waiting for",
      "Wabbajack is running" in text and "Staging" in text, text[-300:])
W.WB.running_processes = lambda: [(1, "Wabbajack.exe")]
try:
    (res, text) = drive(W.wait_for_wabbajack, ["q"], "Staging")
finally:
    W.WB.running_processes = lambda: []
check("  and 'q' stops the wizard instead", res is None and text == "(quit)")
check("the real check answers without error on this machine",
      isinstance(REAL_RUNNING(), list))

# ---------------------------------------------------------------- 13.17
print("\n 13.17  stage 5d empties the trash on request")
box = Sandbox(BOX)
wire(box)
box.manifest("L", GAME, [("k.7z", 1, "nexus")])
box.pool_file(GAME, "Master", "k.7z", 1)
box.pool_file(GAME, "Trash", "old.7z", 40)
wire(box)
records = A.load_plan_records()
(res, text) = drive(W.stage_space, ["s", "e", "y"], {"lists": {}, "map": {}}, records)
check("5d shows the trash", "5d. Trash" in text, text[-500:])
check("  and empties it on request",
      os.listdir(os.path.join(box.archives, GAME, "Trash")) == []
      and "Purged 1 file(s)" in text, text[-300:])
box.pool_file(GAME, "Trash", "again.7z", 4)
(res, text) = drive(W.stage_space, ["s", "s"], {"lists": {}, "map": {}}, records)
check("  or leaves it alone",
      os.path.isfile(os.path.join(box.archives, GAME, "Trash", "again.7z")), text[-300:])

W.WB.running_processes = REAL_RUNNING
shutil.rmtree(BOX, ignore_errors=True)
sys.exit(summary())
