"""Laying out a pool for a game that has none yet.

Browsing the gallery for a game you keep no archives for is only useful if
the tooling can then make that game a home. This drives create_game_pool()
against a sandbox: the layout it produces, the names it refuses, the folder
it reuses rather than duplicating, and the settings it writes when one game
is folded into another game's pool. The real settings file beside the
scripts is never touched - CFG.settings_path is redirected into the sandbox
before anything is written.
"""
import io
import json
import logging
import os
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

BOX = os.path.join(BOXROOT, "sb16")
SAVED_SETTINGS = CFG.current_settings()
REAL_WOUT = W.out
REAL_SETTINGS_PATH = CFG.settings_path

print("=" * 70)
print("  T16  LAYING OUT A POOL FOR A NEW GAME")
print("=" * 70)

box = Sandbox(BOX)
A.MANIFESTS = box.manifests
A.HERE = box.tools
A.WJ.ARCHIVE_ROOT = box.archives
CFG.ARCHIVE_ROOT = box.archives
G.HERE = box.tools
CFG.settings_path = lambda: os.path.join(box.tools, "wj_settings.json")
W.out = lambda *a, **k: None


def subdirs(path):
    return sorted(n for n in os.listdir(path) if os.path.isdir(os.path.join(path, n)))


def roots():
    return sorted(n for n in os.listdir(box.archives)
                  if os.path.isdir(os.path.join(box.archives, n)))


def saved_mapping():
    p = CFG.settings_path()
    if not os.path.isfile(p):
        return {}
    with io.open(p, encoding="utf-8") as f:
        return (json.load(f) or {}).get("GAME_FOLDERS") or {}


# -- 16.1  the layout ---------------------------------------------------------
print("\n 16.1  a new game gets the same layout as every other game")
path, created, mapped = W.create_game_pool("Starfield")
check("16.1 the folder is made, named after the game",
      created and os.path.basename(path) == "Starfield", os.path.basename(path))
check("16.1b Master, Shared, Unmanaged and Trash are all there",
      subdirs(path) == ["Master", "Shared", "Trash", "Unmanaged"], str(subdirs(path)))
check("16.1c a game under its own name needs no GAME_FOLDERS entry", not mapped)
check("16.1d and nothing was written to the settings file",
      "Starfield" not in saved_mapping(), str(saved_mapping()))

# -- 16.2  running it twice ---------------------------------------------------
print("\n 16.2  running it again is not a second pool")
before = roots()
path2, created2, mapped2 = W.create_game_pool("Starfield")
check("16.2 the same folder comes back", os.path.normcase(path2) == os.path.normcase(path))
check("16.2b it does not claim to have created it", not created2)
check("16.2c no second folder appeared", roots() == before, str(roots()))

# -- 16.3  the share owns the spelling ----------------------------------------
print("\n 16.3  an existing folder is reused, whatever its case")
os.makedirs(os.path.join(box.archives, "morrowind", "Master"))
p3, created3, _ = W.create_game_pool("Morrowind")
check("16.3 the folder already on disk is used, not a second one",
      os.path.basename(p3) == "morrowind", os.path.basename(p3))
check("16.3b it is not reported as created", not created3)
check("16.3c and it is completed to the full layout",
      subdirs(p3) == ["Master", "Shared", "Trash", "Unmanaged"], str(subdirs(p3)))
check("16.3d no 'Morrowind' appeared beside 'morrowind'",
      "Morrowind" not in roots(), str(roots()))

# -- 16.4  names that would collide -------------------------------------------
print("\n 16.4  a game folder may not be named after a pool folder")
for bad in ("Master", "Shared", "Trash", "Unmanaged", "Nolvus"):
    problem = W.pool_folder_problem("Starfield", bad)
    check("16.4 %s is refused" % bad, bool(problem), "allowed it")
    raised = False
    try:
        W.create_game_pool("Starfield", bad)
    except ValueError:
        raised = True
    check("16.4b create_game_pool refuses %s too" % bad, raised)
check("16.4c and none of them was created",
      not any(n in roots() for n in ("Master", "Shared", "Nolvus")), str(roots()))

# -- 16.5  names that would escape the root -----------------------------------
print("\n 16.5  a folder name may not carry a path")
for bad in ("a/b", "a\\b", "..", "C:x", ""):
    check("16.5 %r is refused" % bad, bool(W.pool_folder_problem("Starfield", bad)))
check("16.5b an empty GameType is refused", bool(W.pool_folder_problem("", "Starfield")))
check("16.5c a GameType carrying a separator is refused",
      bool(W.pool_folder_problem("a/b", None)))

# -- 16.6  folding one game into another --------------------------------------
print("\n 16.6  folding a game into a pool it should share")
W.create_game_pool("Fallout4")
before = roots()
p6, created6, mapped6 = W.create_game_pool("Fallout4VR", "Fallout4")
check("16.6 it lands in the existing pool",
      os.path.basename(p6) == "Fallout4", os.path.basename(p6))
check("16.6b no separate folder was made for it",
      roots() == before and "Fallout4VR" not in roots(), str(roots()))
check("16.6c the mapping is reported", mapped6)
check("16.6d and written to the settings file",
      saved_mapping().get("Fallout4VR") == "Fallout4", str(saved_mapping()))
check("16.6e the config now folds the two games onto one folder",
      CFG.folder_key("Fallout4VR") == CFG.folder_key("Fallout4"))
check("16.6f so they read as one pool, and protect each other",
      CFG.same_pool("Fallout4VR", "Fallout4"))
check("16.6g the Skyrim family it shipped with is still mapped",
      CFG.same_pool("SkyrimVR", "SkyrimSpecialEdition"))
# The window plans with the downloader module, which keeps copies of the
# layout. Before refresh_settings() it went on looking in Fallout4VR until
# the next start, while wj_config already said Fallout4.
check("16.6i the downloader module this process plans with agrees at once",
      A.WJ.game_folder("Fallout4VR") == "Fallout4"
      and os.path.basename(A.WJ.resolve_game_dirs("Fallout4VR")[0]) == "Fallout4",
      str((A.WJ.game_folder("Fallout4VR"), A.WJ.resolve_game_dirs("Fallout4VR")[0])))

_, _, mapped6b = W.create_game_pool("Fallout4VR", "Fallout4")
check("16.6h writing the same mapping twice is not a second write", not mapped6b)

# -- 16.7  measuring a game you own nothing for -------------------------------
print("\n 16.7  browsing a game with no pool measures against nothing")
G._write_cache(G.report_rel("r/new"), json.dumps(
    {"MachineURL": "new", "Name": "New List", "Version": "1.0", "Status": "Available",
     "Failures": 0, "Archives": [
         {"Status": "Valid", "Original": {"Name": "one.7z", "Size": 100, "Hash": "A=",
                                          "Meta": "", "State": {"$type": "NexusDownloader, Wabbajack.Lib"}}},
         {"Status": "Valid", "Original": {"Name": "two.7z", "Size": 400, "Hash": "B=",
                                          "Meta": "", "State": {"$type": "NexusDownloader, Wabbajack.Lib"}}},
     ]}).encode())
rows = [{"key": "r/new", "title": "New List", "game": "Starfield",
         "game_key": "starfield", "retired": False}]
crashed = None
try:
    W.evaluate_rows(rows, None, {}, "Starfield")
except Exception as e:                                   # noqa: BLE001 - the point
    crashed = e
check("16.7 a missing pool does not raise", crashed is None, repr(crashed))
gap = rows[0].get("gap")
check("16.7b the list is measured, not skipped", bool(gap), str(gap))
if gap:
    check("16.7c every archive reads as still to fetch",
          gap["total"] == 2 and gap["missing"] == 2, str(gap))
    check("16.7d and the whole download is the size to fetch",
          gap["missing_bytes"] == gap["total_bytes"] == 500, str(gap))

# -- restore ------------------------------------------------------------------
W.out = REAL_WOUT
CFG.settings_path = REAL_SETTINGS_PATH
CFG.apply_settings(SAVED_SETTINGS)
sys.exit(summary())
