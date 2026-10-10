"""The settings file: what a value may be, and what a bad one costs.

Every setting is checked on the way in, and a bad value keeps its default and
is reported. The folder settings are checked for what they would name,
because the trash is emptied for good on a timer: TRASH_FOLDER set to Master
emptied Master of every archive older than TRASH_KEEP_DAYS, reproduced in a
sandbox on 2026-09-25, and an emptied field saved from the Settings tab
stopped every tool from starting. The purge and stage 5c then ask again, in
case a bad name ever gets past the settings.

Runs against a sandbox copy of the scripts, imported from the sandbox, so
the settings file they read is the sandbox's and the real one beside the
scripts is never read or written.
"""
import io
import json
import logging
import os
import subprocess
import sys
import time

BOXROOT = os.path.join(os.environ.get("TEMP", "/tmp"), "wj_tests")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness
from harness import Sandbox, check, summary

BOX = os.path.join(BOXROOT, "sb17")
GAME = "Fallout4"
OLD = time.time() - 60 * 86400

print("=" * 70)
print("  T17  THE SETTINGS FILE")
print("=" * 70)

box = Sandbox(BOX)
sys.path.insert(0, box.tools)
os.chdir(box.tools)
import wj_config as CFG


def fresh():
    CFG.apply_settings(CFG._DEFAULTS)


def aged(path):
    os.utime(path, (OLD, OLD))
    return path


# ---------------------------------------------------------------- 17.1
print("\n 17.1  a plain name, and what is not one")
for good in ("Trash", "Recycle Bin", ".45 Auto Pistol-21142-1-2-1.zip", "Name~1.7z",
             "DARK SOULS 3"):
    check("17.1 %r is a plain name" % good, CFG.plain_name_problem(good) is None,
          str(CFG.plain_name_problem(good)))
for bad in ("", ".", "..", "...", "   ", "a\\b", "a/b", "..\\..\\escaped.txt", "C:", "x:y",
            "Trash.", "Trash ", "a\x01b", "a?b", "a*b", None):
    check("17.1b %r is refused" % (bad,), bool(CFG.plain_name_problem(bad)))

# ---------------------------------------------------------------- 17.2
print("\n 17.2  a folder this tooling manages")
for bad in ("Master", "master", "Shared", "NUL", "com1", "Con.txt"):
    check("17.2 %r cannot be one" % bad, bool(CFG.folder_setting_problem(bad)))
check("17.2b an ordinary name can", CFG.folder_setting_problem("Recycled") is None)

# ---------------------------------------------------------------- 17.3
print("\n 17.3  TRASH_FOLDER: the folder the purge empties")
for bad in ("Master", "Shared", "", None, "..", "..\\Master", "Master.", "C:\\", 7):
    fresh()
    problems = CFG.apply_settings({"TRASH_FOLDER": bad})
    check("17.3 %r keeps the default" % (bad,),
          CFG.TRASH_FOLDER == "Trash" and any(p.startswith("TRASH_FOLDER") for p in problems),
          str((CFG.TRASH_FOLDER, problems)))
fresh()
check("17.3b a plain name is taken", CFG.apply_settings({"TRASH_FOLDER": "Recycled"}) == []
      and CFG.TRASH_FOLDER == "Recycled", CFG.TRASH_FOLDER)
check("17.3c and joins the folders every tool leaves alone",
      "recycled" in CFG.excluded_dirs() and "trash" not in CFG.excluded_dirs(),
      str(CFG.excluded_dirs()))

# ---------------------------------------------------------------- 17.4
print("\n 17.4  UNMANAGED_FOLDER: where strays are parked")
for bad in ("Master", "", None, "a/b", "Unmanaged "):
    fresh()
    problems = CFG.apply_settings({"UNMANAGED_FOLDER": bad})
    check("17.4 %r keeps the default" % (bad,),
          CFG.UNMANAGED_FOLDER == "Unmanaged"
          and any(p.startswith("UNMANAGED_FOLDER") for p in problems), str(problems))

# ---------------------------------------------------------------- 17.5
print("\n 17.5  one folder may not be two things")
fresh()
p = CFG.apply_settings({"TRASH_FOLDER": "Unmanaged"})
check("17.5 the trash may not be the unmanaged store",
      CFG.TRASH_FOLDER == "Trash" and CFG.UNMANAGED_FOLDER == "Unmanaged" and p, str(p))
fresh()
p = CFG.apply_settings({"UNMANAGED_FOLDER": "trash"})
check("17.5b nor the store the trash: the setting that moved goes back",
      CFG.UNMANAGED_FOLDER == "Unmanaged" and CFG.TRASH_FOLDER == "Trash"
      and any(x.startswith("UNMANAGED_FOLDER") for x in p), str(p))
fresh()
p = CFG.apply_settings({"TRASH_FOLDER": "Nolvus"})
check("17.5c nor another installer's folder", CFG.TRASH_FOLDER == "Trash" and p, str(p))
fresh()
p = CFG.apply_settings({"EXCLUDE_POOL_DIRS": ["nolvus", "Trash"]})
check("17.5d excluding the trash itself is dropped, not obeyed",
      CFG.EXCLUDE_POOL_DIRS == {"nolvus"} and CFG.TRASH_FOLDER == "Trash" and p,
      str((CFG.EXCLUDE_POOL_DIRS, p)))
fresh()
p = CFG.apply_settings({"TRASH_FOLDER": "Nolvus", "UNMANAGED_FOLDER": "Trash",
                        "EXCLUDE_POOL_DIRS": ["nolvus", "trash"]})
check("17.5e all three at once still settle",
      CFG.TRASH_FOLDER == "Trash" and CFG.UNMANAGED_FOLDER == "Unmanaged"
      and CFG.EXCLUDE_POOL_DIRS == {"nolvus"} and len(p) == 3,
      str((CFG.TRASH_FOLDER, CFG.UNMANAGED_FOLDER, CFG.EXCLUDE_POOL_DIRS, p)))

# ---------------------------------------------------------------- 17.6
print("\n 17.6  EXCLUDE_POOL_DIRS entries, one at a time")
fresh()
p = CFG.apply_settings({"EXCLUDE_POOL_DIRS": ["nolvus", "Master", "a/b", "MO2"]})
check("17.6 the good entries stay, the others are named and dropped",
      CFG.EXCLUDE_POOL_DIRS == {"nolvus", "mo2"} and len(p) == 2, str((CFG.EXCLUDE_POOL_DIRS, p)))

# ---------------------------------------------------------------- 17.7
print("\n 17.7  GAME_FOLDERS values, one at a time")
fresh()
p = CFG.apply_settings({"GAME_FOLDERS": {"Fallout4VR": "Fallout4", "Evil": "..\\..\\x",
                                         "Worse": "Master", "Odd": None}})
check("17.7 the good entry is taken", CFG.game_folder("Fallout4VR") == "Fallout4")
check("17.7b each bad one is ignored and named",
      CFG.game_folder("Evil") == "Evil" and CFG.game_folder("Worse") == "Worse"
      and CFG.game_folder("Odd") == "Odd" and len(p) == 3, str(p))
check("17.7c and the shipped mapping survives", CFG.same_pool("SkyrimVR", "Skyrim"))

# ---------------------------------------------------------------- 17.8
print("\n 17.8  text that has to say something")
fresh()
root = CFG.ARCHIVE_ROOT
p = CFG.apply_settings({"ARCHIVE_ROOT": None, "MANIFEST_DIR": "  "})
check("17.8 ARCHIVE_ROOT and MANIFEST_DIR may not be emptied",
      CFG.ARCHIVE_ROOT == root and CFG.MANIFEST_DIR == "modlists" and len(p) == 2, str(p))
fresh()
p = CFG.apply_settings({"STAGE_LIST": None, "WABBAJACK_DIR": None, "NOTIFY_URL": None})
check("17.8b the three that mean something when null may be null", p == [], str(p))

# ---------------------------------------------------------------- 17.9
print("\n 17.9  a bad settings file at start-up")
settings = os.path.join(box.tools, CFG.SETTINGS_FILE)
with io.open(settings, "w", encoding="utf-8") as f:
    json.dump({"TRASH_FOLDER": None, "UNMANAGED_FOLDER": "Master", "THEME": "dark"}, f)
rc, out = box.run("wj_downloader.py", ["--help"])
check("17.9 a null trash folder no longer stops the tools starting", rc == 0, out[-400:])
probe = subprocess.run(
    [harness.PY, "-c", "import json, wj_config as C; print(json.dumps([C.SETTINGS_PROBLEMS, "
                       "C.TRASH_FOLDER, C.UNMANAGED_FOLDER, C.THEME]))"],
    cwd=box.tools, capture_output=True, text=True, stdin=subprocess.DEVNULL)
try:
    problems, trash, unmanaged, theme = json.loads(probe.stdout.strip().splitlines()[-1])
except (ValueError, IndexError):
    problems, trash, unmanaged, theme = [], None, None, None
check("17.9b both bad values keep their defaults", (trash, unmanaged) == ("Trash", "Unmanaged"),
      probe.stdout + probe.stderr)
check("  and each is reported", len(problems) == 2, str(problems))
check("  while the good one is applied", theme == "dark", str(theme))
os.remove(settings)

# ---------------------------------------------------------------- 17.10
print("\n 17.10  a refused value is never written")
fresh()
vals = CFG.current_settings()
vals.update({"TRASH_FOLDER": "Master", "UNMANAGED_FOLDER": None, "THEME": "dark"})
path, problems = CFG.save_settings(vals, path=os.path.join(box.tools, "saved.json"))
with io.open(path, encoding="utf-8") as f:
    saved = json.load(f)
check("17.10 the good change is saved", saved.get("THEME") == "dark", str(saved))
check("17.10b the refused ones are not written at all",
      "TRASH_FOLDER" not in saved and "UNMANAGED_FOLDER" not in saved, str(saved))
check("17.10c each is named", sorted(p.split(":")[0] for p in problems)
      == ["TRASH_FOLDER", "UNMANAGED_FOLDER"], str(problems))
check("17.10d the running settings are the ones saved",
      CFG.TRASH_FOLDER == "Trash" and CFG.UNMANAGED_FOLDER == "Unmanaged" and CFG.THEME == "dark")
fresh()

# ---------------------------------------------------------------- 17.11
print("\n 17.11  the purge asks again before deleting")
import wj_autoupdate as A
A.setup_logging(False)
A.LOG.setLevel(logging.CRITICAL)
A.WJ = A.load_downloader_module()
A.MANIFESTS = box.manifests
box.manifest("LoreRim", GAME, [("keep.7z", 64, "nexus")])
master_old = aged(box.pool_file(GAME, "Master", "old.7z", 64))
list_old = aged(box.pool_file(GAME, "LoreRim", "keep.7z", 64))
trash_old = aged(box.pool_file(GAME, "Trash", "gone.7z", 64))
fresh()
CFG.TRASH_FOLDER = "Master"                   # past the settings, as a bug might put it
rows, rec = A.purge_trash()
check("17.11 a trash folder called Master is not emptied",
      rows == [] and rec is None and os.path.isfile(master_old), str(rows))
CFG.TRASH_FOLDER = "LoreRim"
rows, rec = A.purge_trash()
check("17.11b nor one named after a modlist held here",
      rows == [] and os.path.isfile(list_old), str(rows))
check("  and the reason is a problem the helper names",
      "modlist" in (A.trash_name_problem() or ""), str(A.trash_name_problem()))
CFG.TRASH_FOLDER = "Trash"
rows, rec = A.purge_trash()
check("17.11c a file in the real trash that no cleanup recorded is not deleted",
      rows == [] and os.path.exists(trash_old), str(rows))
box.trash_record(GAME, "gone.7z", 64, days_ago=40)
rows, rec = A.purge_trash()
check("17.11d the real trash still is emptied on time, of what a cleanup put there",
      [r[1] for r in rows] == ["gone.7z"] and not os.path.exists(trash_old), str(rows))

# ---------------------------------------------------------------- 17.12
print("\n 17.12  so does clearing Master")
import wj_wizard as W
W.out = lambda *a, **k: None
CFG.TRASH_FOLDER = "LoreRim"
deleted, kept, freed = W.clear_master(GAME, {})
check("17.12 Master is not cleared into a modlist's folder",
      deleted == [] and os.path.isfile(master_old) and os.listdir(os.path.dirname(list_old))
      == ["keep.7z"], str(deleted))
CFG.TRASH_FOLDER = "Trash"

# ---------------------------------------------------------------- 17.13
print("\n 17.13  a new pool's folder name follows the same rules")
for bad in ("Starfield.", "NUL", "a/b", "Master"):
    check("17.13 %r is refused" % bad, bool(W.pool_folder_problem("Starfield", bad)))
check("17.13b an ordinary one is not", W.pool_folder_problem("Starfield", "Starfield") is None)

# ---------------------------------------------------------------- 17.14
print("\n 17.14  a saved change reaches the downloader's own copies")
# The downloader copies part of wj_config when it loads; the window then
# plans with that copy in the same process. Every copied setting is changed
# here and must follow, so a copy added later without a refresh shows up.
fresh()
vals = CFG.current_settings()
vals.update({"GAME_FOLDERS": {"Fallout4VR": "Fallout4"}, "GAME_DISPLAY": {"Fallout4VR": "F4 VR"},
             "EXCLUDE_POOL_DIRS": ["nolvus", "MO2"], "TRASH_FOLDER": "Recycled",
             "REQUIRE_HASH": False, "DOWNLOAD_ORDER": "largest",
             "PARALLEL_SMALL_DOWNLOADS": 3, "SMALL_DOWNLOAD_MB": 20,
             "ASSIST_CLICK_AHEAD": True, "ASSIST_QUEUE_DEPTH": 4, "ASSIST_WINDOW": False})
CFG.save_settings(vals, path=os.path.join(box.tools, "saved.json"))
A.reload_settings()
W_ = A.WJ
followed = {
    "GAME_FOLDERS": W_.game_folder("Fallout4VR") == "Fallout4",
    "GAME_DISPLAY": W_.game_label("Fallout4VR") == "F4 VR",
    "EXCLUDE_POOL_DIRS": {"mo2", "recycled"} <= W_.EXCLUDE_POOL_DIRS,
    "REQUIRE_HASH": W_.REQUIRE_HASH is False,
    "DOWNLOAD_ORDER": W_.DOWNLOAD_ORDER == "largest",
    "PARALLEL_SMALL_DOWNLOADS": W_.PARALLEL_SMALL == 3,
    "SMALL_DOWNLOAD_MB": W_.SMALL_BYTES == 20 << 20,
    "ASSIST_CLICK_AHEAD": W_.CLICK_AHEAD is True,
    "ASSIST_QUEUE_DEPTH": W_.QUEUE_DEPTH == 4,
    "ASSIST_WINDOW": W_.ASSIST_WINDOW is False,
}
for key, ok in followed.items():
    check("17.14 %s follows" % key, ok)
check("17.14b and the pool index is rebuilt for the new exclusions",
      not W_._EXISTING_CACHE)
fresh()
A.reload_settings()
check("17.14c going back reaches it too", W_.game_folder("Fallout4VR") == "Fallout4VR"
      and W_.PARALLEL_SMALL == 1 and W_.ASSIST_WINDOW is True)

# ---------------------------------------------------------------- 17.15
print("\n 17.15  a modlist's name, judged as Windows reads it")
# Windows drops a trailing dot: 'Master.' opens Master, and a manifest called
# Master..wabbajack slipped past every check that compared 'master.' with
# 'master' - deleting it emptied the Master pool. And a device name is no
# folder at all.
for bad in ("Master", "master.", "Master. ", "Shared", "Trash", "Unmanaged", "Nolvus",
            "CON", "nul", "CONIN$", "conout$", "COM1", "COM\u00b9", "LPT\u00b3", "com1.backup",
            "AUX .old", "a/b", "..", ""):
    check("17.15 a list called %r is refused" % bad, bool(CFG.list_name_problem(bad)),
          str(CFG.list_name_problem(bad)))
for good in ("LoreRim", "Lorerim_@@_LoreRim", "Fallout Anomaly 2.0", "Comet", "Console Tweaks",
             "COM10", "Masterpiece"):
    check("17.15b %r may be one" % good, CFG.list_name_problem(good) is None,
          str(CFG.list_name_problem(good)))
check("17.15c a pool's name is called reserved, saying why",
      "reserved for a pool folder" in CFG.list_name_problem("Shared"))
check("17.15d a GameType is held to the same rule: no trailing dot, no device",
      [CFG.valid_game_type(g) for g in ("Fallout4", "Skyrim.", "NUL", "a\\b", "", None)]
      == [True, False, False, False, False, False])

# ---------------------------------------------------------------- 17.16
print("\n 17.16  ARCHIVE_ROOT: a folder of pools, and nothing more")
# Everything under the archive root is judged by these tools: each folder in
# it read as a game's pools, a stray in one offered for parking, a Trash in
# any of them emptied on a timer. A drive's root, or the folder holding the
# tools, would make the tools' own files - or the rest of the disk - pools.
tools = os.path.normcase(os.path.abspath(box.tools))
for bad, label in (("C:\\", "a drive's root"), ("D:/", "another drive's root"),
                   ("\\\\server\\share", "a share's root"), (".", "the tools folder itself"),
                   ("..", "the folder holding the tools"),
                   (os.path.dirname(box.root), "a folder further up"),
                   (box.tools, "the tools folder, spelled out"), ("", "empty"), ("   ", "blank")):
    check("17.16 %s is refused" % label, bool(CFG.archive_root_problem(bad)),
          "%r: %s" % (bad, CFG.archive_root_problem(bad)))
for good in ("../Mod Archive", box.archives, "\\\\server\\share\\Mod Archive", "Z:\\Mod Archive"):
    check("17.16b %r may be one" % good, CFG.archive_root_problem(good) is None,
          str(CFG.archive_root_problem(good)))
fresh()
before = CFG.ARCHIVE_ROOT
p = CFG.apply_settings({"ARCHIVE_ROOT": "C:\\"})
check("17.16c a refused root keeps the default, and is reported",
      CFG.ARCHIVE_ROOT == before and any(x.startswith("ARCHIVE_ROOT") for x in p), str(p))

# ---------------------------------------------------------------- 17.17
print("\n 17.17  TRASH_KEEP_DAYS: a number of days that means something")
for bad in (0, -1, 3651, True, "30", 2.5, None):
    fresh()
    p = CFG.apply_settings({"TRASH_KEEP_DAYS": bad})
    check("17.17 %r keeps the default" % (bad,),
          CFG.TRASH_KEEP_DAYS == 30 and any(x.startswith("TRASH_KEEP_DAYS") for x in p), str(p))
for good in (1, 7, 3650):
    fresh()
    check("17.17b %d is taken" % good, CFG.apply_settings({"TRASH_KEEP_DAYS": good}) == []
          and CFG.TRASH_KEEP_DAYS == good)
fresh()

# ---------------------------------------------------------------- 17.18
print("\n 17.18  a change is made to the file as it is now, and to nothing else")
# Found 2026-10-10: the window wrote its whole form, filled hours before, so
# a value typed into the file since went back to what the window started
# with - and saving moved this process's ARCHIVE_ROOT while the downloader
# and the sorter in it planned with the old one.
sp = os.path.join(box.tools, "update.json")
with io.open(sp, "w", encoding="utf-8") as f:
    json.dump({"THEME": "dark", "KEEP_BACKUP_DAYS": 99, "A_FUTURE_SETTING": {"x": 1}}, f)
fresh()
path, problems = CFG.update_settings({"HIDE_NSFW": True}, path=sp)
with io.open(sp, encoding="utf-8") as f:
    saved = json.load(f)
check("17.18 the change is written", saved.get("HIDE_NSFW") is True, str(saved))
check("  what the file already said is kept, though this process never loaded it",
      saved.get("THEME") == "dark" and saved.get("KEEP_BACKUP_DAYS") == 99, str(saved))
check("  and a setting this version does not know is kept as it was",
      saved.get("A_FUTURE_SETTING") == {"x": 1}, str(saved))
check("  the running settings are now the file's", CFG.THEME == "dark" and CFG.HIDE_NSFW is True
      and CFG.KEEP_BACKUP_DAYS == 99)
CFG.update_settings(lambda now: {"HIDDEN": now["HIDDEN"] + ["r/one"]}, path=sp)
CFG.update_settings(lambda now: {"HIDDEN": now["HIDDEN"] + ["r/two"]}, path=sp)
with io.open(sp, encoding="utf-8") as f:
    check("17.18b a change made relative to what is there builds on it",
          json.load(f).get("HIDDEN") == ["r/one", "r/two"])
with io.open(sp, "w", encoding="utf-8") as f:
    f.write('{"THEME": "dark", ')                                   # cut short
CFG.update_settings({"HIDE_NSFW": False}, path=sp)
check("17.18c a file that cannot be read is kept aside, not overwritten",
      os.path.isfile(sp + ".unreadable")
      and io.open(sp + ".unreadable", encoding="utf-8").read() == '{"THEME": "dark", ')
os.remove(sp + ".unreadable")
fresh()
root_before, resolved_before = CFG.ARCHIVE_ROOT, CFG.resolved_archive_root()
manifests_before = CFG.MANIFEST_DIR
elsewhere = os.path.join(box.root, "Archive Elsewhere")
CFG.update_settings({"ARCHIVE_ROOT": elsewhere, "MANIFEST_DIR": "lists elsewhere"}, path=sp)
with io.open(sp, encoding="utf-8") as f:
    saved = json.load(f)
check("17.18d the two paths are written", saved.get("ARCHIVE_ROOT") == elsewhere
      and saved.get("MANIFEST_DIR") == "lists elsewhere", str(saved))
check("  but this process keeps the ones it started with: they take effect at the next start",
      CFG.ARCHIVE_ROOT == root_before and CFG.resolved_archive_root() == resolved_before
      and CFG.MANIFEST_DIR == manifests_before, CFG.ARCHIVE_ROOT)
os.remove(sp)
fresh()

print("\n      two changes at once, from two threads, both reach the file")
# Found 2026-10-10 in review: the window's switches change the file on its
# main thread while a job - a pool fold - changes it from a worker. Each read
# the file, changed it and wrote it back, and one change could be lost.
import threading


def slow(key, value):
    def change(now):
        time.sleep(0.3)                        # the other writer reads the file meanwhile
        return {key: value}
    return change


first_ = threading.Thread(target=CFG.update_settings, args=(slow("THEME", "dark"),), kwargs={"path": sp})
second = threading.Thread(target=CFG.update_settings, args=(slow("HIDE_NSFW", True),), kwargs={"path": sp})
first_.start()
time.sleep(0.05)
second.start()
first_.join()
second.join()
with io.open(sp, encoding="utf-8") as f:
    saved = json.load(f)
check("17.18e neither change is lost", saved.get("THEME") == "dark" and saved.get("HIDE_NSFW") is True,
      str(saved))
os.remove(sp)
fresh()

# ---------------------------------------------------------------- 17.19
print("\n 17.19  a child works under its parent's archive root until the parent restarts")
# Found 2026-10-10 in review: a new ARCHIVE_ROOT saved in the window applies at
# the next start, but a download or a sort the window started read it from the
# file at once - two roots again, one planning and one moving.
fresh()
A.reload_settings()
here = os.path.join(box.tools, CFG.SETTINGS_FILE)
if os.path.exists(here):
    os.remove(here)
check("17.19 with nothing new saved, a child works its root out for itself",
      A.archive_root_for_children() is None, str(A.archive_root_for_children()))
elsewhere = os.path.join(box.root, "Archive Elsewhere")
CFG.update_settings({"ARCHIVE_ROOT": elsewhere})
check("  with a new root waiting in the file, a child is told this process's own",
      A.archive_root_for_children() == A.WJ.ARCHIVE_ROOT == box.archives,
      str((A.archive_root_for_children(), A.WJ.ARCHIVE_ROOT)))


def child_root(env_root=None):
    env = dict(os.environ)
    env.pop("WJ_ARCHIVE_ROOT", None)
    if env_root is not None:
        env["WJ_ARCHIVE_ROOT"] = env_root
    p = subprocess.run([harness.PY, "-c", "import json, wj_config as C; "
                        "print(json.dumps([C.resolved_archive_root(), C.SETTINGS_PROBLEMS]))"],
                       cwd=box.tools, env=env, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return [None, [p.stderr[-300:]]]


root, _p = child_root(box.archives)
check("  and works under it, not the one in the file",
      os.path.normcase(root or "") == os.path.normcase(box.archives), str(root))
root, _p = child_root()
check("  while a process started afresh takes the file's",
      os.path.normcase(root or "") == os.path.normcase(elsewhere), str(root))
root, problems = child_root("C:\\")
check("  and a root no setting could be is not taken from the environment either",
      os.path.normcase(root or "") == os.path.normcase(elsewhere)
      and any("WJ_ARCHIVE_ROOT" in p for p in problems), str((root, problems)))
os.remove(here)
fresh()
A.reload_settings()

# ---------------------------------------------------------------- 17.20
print("\n 17.20  one game, every name it goes by")
check("17.20 the feeds' vtmb and a manifest's VampireTheMasqueradeBloodlines are one game",
      CFG.game_aliases("vtmb") == CFG.game_aliases("VampireTheMasqueradeBloodlines")
      == {"vtmb", "vampirethemasqueradebloodlines"}, str(CFG.game_aliases("vtmb")))
check("  a game with one name has just that one", CFG.game_aliases("Fallout4") == {"fallout4"})
check("  one the tooling does not know is still itself", CFG.game_aliases("NewGame") == {"newgame"})
check("  and nothing names nothing", CFG.game_aliases(None) == set())

fresh()
sys.exit(summary())
