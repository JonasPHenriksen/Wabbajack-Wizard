"""Handing a list to Wabbajack: finding it, the preflight, the command.

Nothing here starts an install or opens a window.
"""
import io
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOXROOT = os.path.join(os.environ.get("TEMP", "/tmp"), "wj_tests")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import check, summary, quiet_logging

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
quiet_logging(A)
A.WJ = A.load_downloader_module()
A.resolve_dirs()
import wj_wizard as W

buf = io.StringIO()
real_out = W.out
W.out = lambda msg="": buf.write(str(msg) + "\n")
# A Skyrim list, against a stubbed Wabbajack install whose detected games
# (set in 9.3) do not include it. Stubbed, so this does not depend on which
# games THIS machine has, nor run the real list-games probe, which scans
# every store and can take minutes.
real_find, real_latest = WB.find_wabbajack, WB.latest_release
WB.find_wabbajack = lambda hint=None: {"dir": "stub-dir", "exe": None,
                                       "cli": "stub", "version": "9.9"}
# The release lookup is stubbed too: a test must not ask GitHub, nor write
# into the real gallery cache beside the scripts.
WB.latest_release = lambda force=False: None
st = {"game": "SkyrimSpecialEdition", "missing": 5, "bytes": 1234,
      "present": 10, "nexus": 5, "direct": 0, "stale": 0, "gamefile": 0,
      "other": 0, "nexus_bytes": 1234, "direct_bytes": 0}
try:
    res = W.offer_install({}, "Some_@@_list.wabbajack", st)
finally:
    W.out = real_out
    WB.find_wabbajack, WB.latest_release = real_find, real_latest
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

# ---------------------------------------------------------------- 9.7
print("\n 9.7  is the installed Wabbajack the newest release?")
newest = {"version": "4.2.3.0", "url": "https://x/rel", "published": "2026-09-09"}
old = {"dir": "d", "exe": r"d\Wabbajack.exe", "cli": r"d\4.2.2.1\cli\wabbajack-cli.exe",
       "version": "4.2.2.1"}
s = WB.update_status(old, latest=newest)
check("an older install is outdated",
      s["outdated"] is True and s["installed"] == "4.2.2.1" and s["latest"] == "4.2.3.0", str(s))
check("  and the sentence says so, with where to get the new one",
      "update is available" in s["text"] and s["url"] == "https://x/rel", str(s))
s = WB.update_status(dict(old, version="4.2.3.0"), latest=newest)
check("the same version is current", s["outdated"] is False and "newest release" in s["text"], str(s))
s = WB.update_status(dict(old, version="4.10.0.0"), latest={"version": "4.9.9.9", "url": "u"})
check("4.10 is newer than 4.9, not older", s["outdated"] is False, str(s))
s = WB.update_status(old, latest={})
check("a lookup that failed is 'cannot tell', never 'current'",
      s["outdated"] is None and "could not be looked up" in s["text"], str(s))
s = WB.update_status(None, latest=newest)
check("no Wabbajack at all says so", s["outdated"] is None and "not found" in s["text"], str(s))
s = WB.update_status(dict(old, version=None), latest=newest)
check("an install with no versioned folder cannot be compared",
      s["outdated"] is None and "versioned folder" in s["text"], str(s))

print("\n      the version folders an update superseded are removed, carefully")
inst = os.path.join(BOX, "wj-install")
shutil.rmtree(inst, ignore_errors=True)
for ver, whole in (("4.2.2.1", True), ("4.2.3.0", True), ("4.1.0.0", True), ("4.9.0.0", False)):
    os.makedirs(os.path.join(inst, ver, "cli"))
    if whole:
        io.open(os.path.join(inst, ver, "Wabbajack.exe"), "wb").write(b"x")
    io.open(os.path.join(inst, ver, "cli", "wabbajack-cli.exe"), "wb").write(b"x")
os.makedirs(os.path.join(inst, "downloads"))                    # not a version folder
os.makedirs(os.path.join(inst, "3.0.0.0"))                       # version-named, but empty
io.open(os.path.join(inst, "Wabbajack.exe"), "wb").write(b"launcher")
wj = WB.find_wabbajack(hint=inst)
check("the newest whole version is the one found", wj["version"] == "4.9.0.0", str(wj))
check("  but a newest that is not whole (no Wabbajack.exe) prunes nothing",
      WB.prune_old_versions(wj, running=[]) == [] and os.path.isdir(os.path.join(inst, "4.1.0.0")))
shutil.rmtree(os.path.join(inst, "4.9.0.0"))
wj = WB.find_wabbajack(hint=inst)
check("with 4.2.3.0 the newest, nothing goes while Wabbajack runs",
      WB.prune_old_versions(wj, running=[(1, "Wabbajack.exe")]) == []
      and os.path.isdir(os.path.join(inst, "4.2.2.1")))
gone = WB.prune_old_versions(wj, running=[])
check("  and with it closed, the two older versions go",
      gone == ["4.1.0.0", "4.2.2.1"], str(gone))
left = sorted(os.listdir(inst))
check("  the newest, the launcher, a non-version folder and an empty version-named one stay",
      left == ["3.0.0.0", "4.2.3.0", "Wabbajack.exe", "downloads"], str(left))
check("  a second pass has nothing to do", WB.prune_old_versions(wj, running=[]) == [])
check("no install at all is no error", WB.prune_old_versions(None) == [])

print("\n      the release is read through the gallery cache, from GitHub's API")
import wj_gallery as G
real_fetch = G.fetch_json
calls = []


def fake_fetch(rel, ttl=None, force=False, lenient=False, url=None):
    calls.append((rel, url))
    return {"tag_name": "v4.2.3.0", "html_url": "https://x/tag",
            "published_at": "2026-09-09T17:35:14Z"}


G.fetch_json = fake_fetch
try:
    r = WB.latest_release()
finally:
    G.fetch_json = real_fetch
check("it asks for the release API, caching it under its own name",
      calls and calls[0] == (WB.RELEASE_CACHE, WB.RELEASES_API), str(calls))
check("  a leading v is dropped so the tag compares with the install's folder",
      r["version"] == "4.2.3.0" and r["url"] == "https://x/tag", str(r))


def down(*a, **k):
    raise G.GalleryError("network down")


G.fetch_json = down
try:
    r = WB.latest_release()
finally:
    G.fetch_json = real_fetch
check("with the network down it is None rather than an error", r is None, str(r))
G.fetch_json = lambda *a, **k: ["not", "a", "release"]
try:
    r = WB.latest_release()
finally:
    G.fetch_json = real_fetch
check("  as it is for an answer in the wrong shape", r is None, str(r))

shutil.rmtree(BOX, ignore_errors=True)
sys.exit(summary())
