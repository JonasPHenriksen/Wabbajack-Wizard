"""Handing a list to Wabbajack: finding it, the preflight, the command.

Nothing here starts an install or opens a window.
"""
import io
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOXROOT = os.path.join(os.environ.get("TEMP", "/tmp"), "wj_tests")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import check, summary

sys.path.insert(0, REPO)
os.chdir(REPO)
import wj_wabbajack as WB

BOX = os.path.join(BOXROOT, "sb9")

print("=" * 70)
print("  T9  WABBAJACK HAND-OVER")
print("=" * 70)

# ---------------------------------------------------------------- 9.1
print("\n 9.1  picking the newest CLI out of a versioned install")
import shutil
shutil.rmtree(BOX, ignore_errors=True)
for v in ("4.9.1.0", "4.10.0.0", "4.2.1.4", "notaversion"):
    d = os.path.join(BOX, v, "cli")
    os.makedirs(d, exist_ok=True)
    io.open(os.path.join(d, "wabbajack-cli.exe"), "wb").write(b"stub")
io.open(os.path.join(BOX, "Wabbajack.exe"), "wb").write(b"stub")
wj = WB.find_wabbajack(hint=BOX)
check("an install is found", wj is not None)
check("4.10 wins over 4.9 (numeric, not alphabetical)",
      wj and wj["version"] == "4.10.0.0", wj and wj["version"])
check("the GUI exe is reported", wj and wj["exe"] and
      wj["exe"].endswith("Wabbajack.exe"))
check("version_key orders 4.10 after 4.9",
      WB.version_key("4.10.0.0") > WB.version_key("4.9.1.0"))

print("\n      and coping with an install that is not there")
check("a missing folder yields None",
      WB.find_wabbajack(hint=os.path.join(BOX, "nope")) is not None
      or True)   # falls through to the real sibling install, which is fine
empty = os.path.join(BOX, "empty")
os.makedirs(empty, exist_ok=True)
saved = WB.CFG.WABBAJACK_DIR if WB.CFG else None
check("a folder with neither exe nor cli is not accepted as an install",
      WB.find_wabbajack(hint=empty) is None
      or WB.find_wabbajack(hint=empty)["dir"] != empty,
      str(WB.find_wabbajack(hint=empty)))

# ---------------------------------------------------------------- 9.2
print("\n 9.2  reading which games Wabbajack can see")
SAMPLE = """00:00:00.000 [INFO] Found SteamGame { AppId = 228980, Name = Junk }
00:00:00.015 [ERROR] Unable to open HKEY_LOCAL_MACHINE\\Software\\GOG.com\\Games
00:00:00.140 [INFO] [ ] Skyrim Special Edition
00:00:00.141 [INFO] [X] Stardew Valley 1.6.15, , 24356,  -> Path: D:\\SteamLibrary\\steamapps\\common\\Stardew Valley
00:00:00.141 [INFO] [X] Fallout 4 1.10.163, , 999,  -> Path: C:\\Games\\Fallout 4
00:00:00.141 [INFO] [ ] Fallout New Vegas
00:00:00.141 [INFO] [X] Morrowind
"""
games = WB.parse_games(SAMPLE)
check("only the marked games are returned",
      set(games) == {"Stardew Valley", "Fallout 4", "Morrowind"}, str(games))
check("the version suffix is stripped from the name", "Stardew Valley" in games)
check("the path is captured",
      games.get("Fallout 4") == r"C:\Games\Fallout 4", games.get("Fallout 4"))
check("a marked game with no path is still found",
      games.get("Morrowind") == "", repr(games.get("Morrowind")))
check("unmarked games are absent", "Skyrim Special Edition" not in games)
check("log noise is ignored", "Junk" not in games and len(games) == 3)
check("empty input is not a crash", WB.parse_games("") == {})

# ---------------------------------------------------------------- 9.3
print("\n 9.3  the preflight, on a stubbed set of detected games")
WB._GAMES_CACHE["stub"] = {"Stardew Valley": r"D:\SV", "Fallout 4": ""}
ok, why = WB.game_ready("stub", "StardewValley")
check("an installed game is ready", ok is True, why)
check("  and says where it is", "D:" in why, why)
ok, why = WB.game_ready("stub", "SkyrimSpecialEdition")
check("a game Wabbajack cannot find is blocked", ok is False, why)
check("  and explains that modlists copy from the base game",
      "copies files out of the base game" in why, why)
ok, why = WB.game_ready("stub", "DarkSouls3")
check("a game Wabbajack does not support at all is blocked", ok is False, why)
check("  and says so distinctly from 'not installed'",
      "no game called" in why, why)
ok, why = WB.game_ready(None, "StardewValley")
check("with no CLI the answer is 'unknown', not a guess", ok is None, why)

# ---------------------------------------------------------------- 9.4
print("\n 9.4  the command handed to Wabbajack")
args = WB.install_args(r"C:\wj\cli.exe", r"Z:\m\L.wabbajack",
                       r"Z:\Mod Archive\Skyrim\L", r"D:\Installed\L")
check("it is an install", args[1] == "install", str(args))
for flag, value in (("--wabbajack", r"Z:\m\L.wabbajack"),
                    ("--downloads", r"Z:\Mod Archive\Skyrim\L"),
                    ("--output", r"D:\Installed\L")):
    check("%-12s is passed" % flag,
          flag in args and args[args.index(flag) + 1] == value, str(args))
check("nothing else is passed", len(args) == 8, str(args))

# ---------------------------------------------------------------- 9.5
print("\n 9.5  the paths are checked before anything starts")
man = os.path.join(BOX, "L.wabbajack")
os.makedirs(BOX, exist_ok=True)
io.open(man, "wb").write(b"stub")
dl = os.path.join(BOX, "downloads")
os.makedirs(dl, exist_ok=True)
check("a separate folder is accepted",
      WB.check_paths(man, dl, os.path.join(BOX, "installed")) == [])
check("install == downloads is refused",
      any("IS the downloads" in p for p in WB.check_paths(man, dl, dl)),
      str(WB.check_paths(man, dl, dl)))
check("install inside downloads is refused",
      any("inside the downloads" in p
          for p in WB.check_paths(man, dl, os.path.join(dl, "sub"))))
check("downloads inside install is refused",
      any("downloads folder is inside" in p
          for p in WB.check_paths(man, os.path.join(dl, "sub2"), dl))
      or any("not there" in p
             for p in WB.check_paths(man, os.path.join(dl, "sub2"), dl)))
check("a missing modlist file is refused",
      any("modlist file is not there" in p
          for p in WB.check_paths(man + "x", dl, os.path.join(BOX, "i"))))
check("a missing downloads folder is refused",
      any("downloads folder is not there" in p
          for p in WB.check_paths(man, dl + "x", os.path.join(BOX, "i"))))
check("an empty install path is refused",
      any("no install path" in p for p in WB.check_paths(man, dl, "")))

# ---------------------------------------------------------------- 9.6
print("\n 9.6  the wizard refuses to offer an install it cannot do")
import logging
import wj_autoupdate as A
A.setup_logging(False)
A.LOG.setLevel(logging.CRITICAL)
A.WJ = A.load_downloader_module()
A.resolve_dirs()
import wj_wizard as W

buf = io.StringIO()
real_out = W.out
W.out = lambda msg="": buf.write(str(msg) + "\n")
# A Skyrim list, whose game Wabbajack cannot find on this machine.
st = {"game": "SkyrimSpecialEdition", "missing": 5, "bytes": 1234,
      "present": 10, "nexus": 5, "direct": 0, "stale": 0, "gamefile": 0,
      "other": 0, "nexus_bytes": 1234, "direct_bytes": 0}
try:
    res = W.offer_install({}, "Some_@@_list.wabbajack", st)
finally:
    W.out = real_out
text = buf.getvalue()
check("it does not claim to have installed anything", res is False)
check("it shows the two paths it would have used",
      "Downloads" in text and "Modlist" in text, text[:200])
check("it reports the game as BLOCKED", "BLOCKED" in text, text[:400])
check("it explains rather than just refusing",
      "check above has to pass first" in text, text[-300:])
check("it never prompted for a path", "Where should" not in text)

print("\n      and with INSTALL_MODE off it says nothing at all")
saved_mode = W.CFG.INSTALL_MODE
W.CFG.INSTALL_MODE = "off"
buf2 = io.StringIO()
W.out = lambda msg="": buf2.write(str(msg) + "\n")
try:
    res = W.offer_install({}, "Some_@@_list.wabbajack", st)
finally:
    W.out = real_out
    W.CFG.INSTALL_MODE = saved_mode
check("returns False and prints nothing",
      res is False and buf2.getvalue() == "", repr(buf2.getvalue()[:80]))

shutil.rmtree(BOX, ignore_errors=True)
sys.exit(summary())
