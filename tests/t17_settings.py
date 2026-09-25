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
check("17.11c the real trash still is emptied on time",
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

fresh()
sys.exit(summary())
