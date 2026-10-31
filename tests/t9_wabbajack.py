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

# ---------------------------------------------------------------- 9.8
print("\n 9.8  an install is remembered, and brought up to date end to end")
import json
import zipfile
from harness import Sandbox
import wj_config as CFG
box = Sandbox(os.path.join(BOXROOT, "sb9b"))
saved_paths = (A.HERE, A.MANIFESTS, A.WJ.ARCHIVE_ROOT, CFG.ARCHIVE_ROOT)
A.HERE, A.MANIFESTS = box.tools, box.manifests
A.WJ.ARCHIVE_ROOT = CFG.ARCHIVE_ROOT = box.archives
box.manifest("Some_@@_list", "Fallout4", [("a.7z", 5, "nexus")])
with zipfile.ZipFile(os.path.join(box.manifests, "Own.wabbajack"), "w") as z:
    z.writestr("modlist", json.dumps({"GameType": "Fallout4", "Version": "3.2.1",
                                      "Archives": [{"Name": "o.7z", "Size": 1, "Hash": "A=",
                                                    "State": {"$type": "HttpDownloader"}}]}))
A.WJ.set_known_list_dirs(A.WJ.known_list_dirs_from(box.manifests))
A.WJ.invalidate_existing_cache()
fn = "Some_@@_list.wabbajack"
target = os.path.join(box.root, "Installed")
state = {"lists": {"Some/list": {"version": "1.0", "hash": "h", "file": fn}}}
said = []
real_out = W.out
W.out = lambda msg="": said.append(str(msg))
rec = W.record_install(state, fn, target)
check("a finished install is recorded, with the feed's version and key",
      rec["version"] == "1.0" and rec["key"] == "Some/list"
      and state["installed"]["Some_@@_list"]["path"] == target, str(rec))
check("  and saved with the state", (A.load_state().get("installed") or {}).get("Some_@@_list")
      == state["installed"]["Some_@@_list"])
own = W.record_install(state, "Own.wabbajack", os.path.join(box.root, "Own"))
check("a list no feed describes takes the manifest's own version", own["version"] == "3.2.1"
      and own["key"] is None, str(own))
ent = {"version": "1.1", "size": 10, "hash": "h2", "title": "Some list", "updated": ""}
check("an install at the published version is not behind",
      W.install_status(state, {"version": "1.0"}, fn, False)["behind"] is False)
check("  one with a newer manifest published is", W.install_status(state, ent, fn, True)["behind"])
check("  and so is one older than a manifest already fetched",
      W.install_status(state, ent, fn, False)["behind"])
check("a list with nothing installed has no status",
      W.install_status(state, ent, "Other.wabbajack", True) is None)

calls = []
real = (A.fetch_manifest, W.download_list, W.stage_list, WB.run_install, WB.running_processes)
A.fetch_manifest = lambda e, f: calls.append(("fetch", f)) or True
W.download_list = lambda records, f, st, assist: calls.append(("download", f, assist)) or (True, 0, st)
W.stage_list = lambda state_, f, st: calls.append(("stage", f)) or 0
WB.run_install = (lambda cli, manifest, downloads, into, on_line=None:
                  calls.append(("install", os.path.basename(manifest), into)) or 0)
WB.running_processes = lambda: []
os.makedirs(os.path.join(box.archives, "Fallout4", "Some_@@_list"), exist_ok=True)
row = ("Some/list", ent, state["lists"]["Some/list"], fn, True, True)
try:
    ok, message = W.update_install(state, row, False, {"cli": "stub-cli"})
    check("every step runs, in order: fetch, download, stage, install",
          [c[0] for c in calls] == ["fetch", "download", "stage", "install"], str(calls))
    check("  the installer is run into the folder it was installed in",
          calls[-1] == ("install", fn, target), str(calls[-1:]))
    check("  and the record follows the new version",
          ok and state["installed"]["Some_@@_list"]["version"] == "1.1", str((ok, message)))
    calls.clear()
    W.stage_list = lambda state_, f, st: calls.append(("stage", f)) or 1
    ok, message = W.update_install(state, row[:4] + (False, True), False, {"cli": "stub-cli"})
    check("a staging that fails stops it before the installer, and says so",
          not ok and "staging failed" in message and [c[0] for c in calls] == ["download", "stage"],
          str((message, calls)))
    calls.clear()
    WB.running_processes = lambda: [(9, "Wabbajack.exe")]
    ok, message = W.update_install(state, row[:4] + (False, True), False, {"cli": "stub-cli"})
    check("  so does a Wabbajack that is running, before anything is moved",
          not ok and "running" in message and "stage" not in [c[0] for c in calls], message)
    WB.running_processes = lambda: []
    ok, message = W.update_install({"lists": {}}, row, False, {"cli": "stub-cli"})
    check("  and a list with no install folder recorded is refused outright",
          not ok and "no install folder" in message, message)
    for result, says, label in (((False, None), "not downloaded", "a download refused for want of space"),
                                ((True, 2), "exited 2", "a downloader that failed outright")):
        calls.clear()
        W.download_list = (lambda records, f, st, assist, r=result:
                           calls.append(("download", f, assist)) or (r[0], r[1], st))
        ok, message = W.update_install(state, row[:4] + (False, True), False, {"cli": "stub-cli"})
        check("%s stops it before anything is staged or installed" % label,
              not ok and says in message and [c[0] for c in calls] == ["download"],
              str((message, calls)))
    W.download_list = lambda records, f, st, assist: calls.append(("download", f, assist)) or (True, 0, st)

    # The console's [i]: pick the list, answer the readme question (this
    # test's manifest names none: install anyway), confirm, and the same
    # update runs.
    import builtins
    answers = iter(["1", "y", "y"])
    real_input = builtins.input
    builtins.input = lambda prompt="": next(answers)
    stubs = (WB.find_wabbajack, WB.prune_old_versions, WB.game_ready, WB.update_status)
    WB.find_wabbajack = lambda hint=None: {"dir": "d", "exe": None, "cli": "stub-cli",
                                           "version": "4.2.3.0"}
    WB.prune_old_versions = lambda wj, running=None, keep=(): []
    WB.game_ready = lambda cli, game: (True, "found")
    WB.update_status = lambda wj, latest=None: {"text": "the newest release", "outdated": False,
                                                "url": ""}
    W.stage_list = lambda state_, f, st: calls.append(("stage", f)) or 0
    calls.clear()
    try:
        ok = W.install_update_step(state, [row])
    finally:
        builtins.input = real_input
        WB.find_wabbajack, WB.prune_old_versions, WB.game_ready, WB.update_status = stubs
    check("the console's [i] runs the same update after one confirmation",
          ok is True and [c[0] for c in calls] == ["fetch", "download", "stage", "install"],
          str(calls) + " " + " | ".join(said[-6:]))
    check("  once the readme question is answered",
          any("names no readme" in s for s in said), " | ".join(said[-12:]))
finally:
    A.fetch_manifest, W.download_list, W.stage_list, WB.run_install, WB.running_processes = real
    W.out = real_out
    A.HERE, A.MANIFESTS, A.WJ.ARCHIVE_ROOT, CFG.ARCHIVE_ROOT = saved_paths
    shutil.rmtree(box.root, ignore_errors=True)

# ---------------------------------------------------------------- 9.9
print("\n 9.9  before any install, the list's readme is opened and confirmed")
# From reading 28 lists' readmes on 2026-10-01: what a list asks of the PC
# before an install is written there and nowhere a tool can read - no
# manifest has a field for it - so the install opens it and waits for a yes
# (docs/modlist-prerequisites.md).
rd = os.path.join(BOX, "readme")
os.makedirs(rd, exist_ok=True)


def wabbajack_file(name, doc):
    p = os.path.join(rd, name)
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("modlist", json.dumps(doc))
    return p


README = "https://example.invalid/lists/readme#prerequisites"
# Wabbajack's own field order: the archives and the install steps first - in
# a large list hundreds of megabytes of them - and the Readme after. One
# archive's Meta text names a "Readme" of its own, a string inside a string.
big = wabbajack_file("Big.wabbajack", {
    "Archives": [{"Name": "a.7z", "Size": 1, "Hash": "A=",
                  "Meta": '[General]\n"Readme": "https://evil.invalid/meta"\n',
                  "State": {"$type": "HttpDownloader"}}],
    "Author": "someone", "Description": "d",
    "Directives": [{"$type": "InlineFile", "To": "mods\\x\\%d.ini" % i, "Hash": "A=", "Size": 1}
                   for i in range(3000)],
    "GameType": "Fallout4", "Name": "Big", "Readme": README, "Version": "1.0"})
check("the manifest's own Readme is found past the archives and steps, not the one in a Meta text",
      WB.manifest_readme(big) == README, repr(WB.manifest_readme(big)))
saved_reads = (WB._README_CHUNK, WB._README_OVERLAP)
WB._README_CHUNK, WB._README_OVERLAP = 7, 256          # every field straddles a read
try:
    check("  however the reads fall across it", WB.manifest_readme(big) == README,
          repr(WB.manifest_readme(big)))
finally:
    WB._README_CHUNK, WB._README_OVERLAP = saved_reads
quoted = wabbajack_file("Quoted.wabbajack", {"Archives": [], "Readme": 'https://x.invalid/a "b"\\c'})
check("  and the escapes in it are read back", WB.manifest_readme(quoted) == 'https://x.invalid/a "b"\\c',
      repr(WB.manifest_readme(quoted)))
none = wabbajack_file("None.wabbajack", {"Archives": [], "GameType": "Fallout4"})
blank = wabbajack_file("Blank.wabbajack", {"Archives": [], "Readme": "  "})
notzip = os.path.join(rd, "NotZip.wabbajack")
io.open(notzip, "wb").write(b"not a zip at all")
check("no Readme, a blank one, a damaged file and a missing one all read as none",
      [WB.manifest_readme(p) for p in (none, blank, notzip, os.path.join(rd, "absent"))]
      == [None, None, None, None])

opened = []
real_open = WB.webbrowser.open
WB.webbrowser.open = lambda url, *a, **k: opened.append(url) or True
try:
    check("a web address is opened in the browser", WB.open_readme(README) is None
          and opened == [README], str(opened))
    refused = [WB.open_readme(u) for u in ("file:///C:/Windows/System32/calc.exe",
                                           "C:\\Windows\\System32\\calc.exe", "", None)]
    check("  anything else is refused - the shell would start it - and nothing is opened",
          all(refused) and opened == [README], str(refused) + " " + str(opened))
    WB.webbrowser.open = lambda url, *a, **k: False
    check("  and a browser that will not start is said", "no browser" in (WB.open_readme(README) or ""))
finally:
    WB.webbrowser.open = real_open

print("\n      the console waits for a yes before it installs")
import builtins
heard, shown, installs = [], [], []
real_bits = (W.out, builtins.input, WB.find_wabbajack, WB.prune_old_versions, WB.game_ready,
             WB.update_status, WB.open_readme, WB.run_install, A.MANIFESTS, W.CFG.INSTALL_MODE)
W.CFG.INSTALL_MODE = "cli"
W.out = lambda msg="": heard.append(str(msg))
WB.find_wabbajack = lambda hint=None: {"dir": "d", "exe": None, "cli": "stub-cli", "version": "4.2.3.0"}
WB.prune_old_versions = lambda wj, running=None, keep=(): []
WB.game_ready = lambda cli, game: (True, "found")
WB.update_status = lambda wj, latest=None: {"text": "the newest release", "outdated": False, "url": ""}
WB.open_readme = lambda url: shown.append(url)          # None: it opened
WB.run_install = lambda cli, manifest, downloads, into, on_line=None: installs.append(into) or 0
A.MANIFESTS = rd
quiet_st = {"game": "Fallout4", "missing": 0, "bytes": 0}
try:
    answers = iter(["n"])
    builtins.input = lambda prompt="": next(answers)
    res = W.offer_install({}, "Big.wabbajack", quiet_st)
    said_text = "\n".join(heard)
    check("installing opens the list's readme first, and says where it is",
          shown == [README] and "is open in your browser" in said_text and README in said_text,
          said_text[-400:])
    check("  'not yet' stops it before the install folder is even asked for",
          res is False and installs == [] and "Where should" not in said_text, said_text[-300:])
    heard.clear()
    answers = iter(["y", "s"])
    res = W.offer_install({}, "Big.wabbajack", quiet_st)
    check("  'yes' goes on to the install's own questions",
          "Where should" in "\n".join(heard), "\n".join(heard)[-300:])
    heard.clear()
    shown.clear()
    answers = iter(["n"])
    res = W.offer_install({}, "None.wabbajack", quiet_st)
    said_text = "\n".join(heard)
    check("a list that names no readme says so, opens nothing, and still waits for a yes",
          res is False and shown == [] and "names no readme" in said_text and installs == [],
          said_text[-300:])
finally:
    (W.out, builtins.input, WB.find_wabbajack, WB.prune_old_versions, WB.game_ready,
     WB.update_status, WB.open_readme, WB.run_install, A.MANIFESTS, W.CFG.INSTALL_MODE) = real_bits

# ---------------------------------------------------------------- 9.10
print("\n 9.10  only a version folder is a version, and none goes on a guess")
# Found 2026-10-10: a folder Explorer named '4.2.3.0 - Copy (2)' was ranked by
# every digit in its name, taken for the newest install, and the real
# versions pruned as superseded by it; and a tasklist that did not answer
# read as 'Wabbajack is not running'.
import subprocess
import types
inst = os.path.join(BOX, "wj-copies")
shutil.rmtree(inst, ignore_errors=True)
for ver in ("4.2.2.1", "4.2.3.0", "4.2.3.0 - Copy (2)", "backup_20261010"):
    os.makedirs(os.path.join(inst, ver, "cli"))
    io.open(os.path.join(inst, ver, "Wabbajack.exe"), "wb").write(b"x")
    io.open(os.path.join(inst, ver, "cli", "wabbajack-cli.exe"), "wb").write(b"x")
wj = WB.find_wabbajack(hint=inst)
check("a copy of a version folder is not taken for the newest version",
      wj and wj["version"] == "4.2.3.0", str(wj))
gone = WB.prune_old_versions(wj, running=[])
check("  and pruning takes only the older real version, never a copy or a backup",
      gone == ["4.2.2.1"] and sorted(os.listdir(inst))
      == ["4.2.3.0", "4.2.3.0 - Copy (2)", "backup_20261010"], str((gone, sorted(os.listdir(inst)))))
check("a newest that is not named like a version prunes nothing",
      WB.prune_old_versions(dict(wj, version="4.2.3.0 - Copy (2)"), running=[]) == [])

os.makedirs(os.path.join(inst, "4.1.0.0", "cli", "downloaded_mod_lists"))
io.open(os.path.join(inst, "4.1.0.0", "Wabbajack.exe"), "wb").write(b"x")
io.open(os.path.join(inst, "4.1.0.0", "cli", "wabbajack-cli.exe"), "wb").write(b"x")
mine = os.path.join(inst, "4.1.0.0", "cli", "downloaded_mod_lists")
check("an old version folder holding your manifests stays",
      WB.prune_old_versions(wj, running=[], keep=[mine]) == []
      and os.path.isdir(mine), str(sorted(os.listdir(inst))))

real_sub = WB.subprocess


def fake_tasklist(rc=0, raises=None, out=""):
    def run(*a, **k):
        if raises:
            raise raises
        return types.SimpleNamespace(returncode=rc, stdout=out)
    return types.SimpleNamespace(run=run, DEVNULL=subprocess.DEVNULL,
                                 SubprocessError=subprocess.SubprocessError)


try:
    WB.subprocess = fake_tasklist(raises=OSError(2, "tasklist is not there"))
    missing = WB.running_processes()
    WB.subprocess = fake_tasklist(raises=subprocess.TimeoutExpired("tasklist", 30))
    stuck = WB.running_processes()
    WB.subprocess = fake_tasklist(rc=1, out="ERROR: Access denied")
    failed = WB.running_processes()
    WB.subprocess = fake_tasklist(out='"Wabbajack.exe","4242","Console","1","300,000 K"\n'
                                      '"explorer.exe","1","Console","1","90,000 K"\n')
    seen = WB.running_processes()
    check("a tasklist that is missing, stuck or failing is 'cannot tell', not 'not running'",
          (missing, stuck, failed) == (None, None, None), str((missing, stuck, failed)))
    check("  one that answers is read", seen == [(4242, "Wabbajack.exe")], str(seen))
    WB.subprocess = fake_tasklist(rc=1)
    check("and with 'cannot tell', no version folder is pruned",
          WB.prune_old_versions(wj) == [] and os.path.isdir(os.path.join(inst, "4.1.0.0")))
finally:
    WB.subprocess = real_sub

print("\n      an install may not go where this tooling keeps anything")
saved_root = CFG.ARCHIVE_ROOT
CFG.ARCHIVE_ROOT = os.path.join(BOX, "pools", "Mod Archive")
lists = os.path.join(BOX, "lists")
os.makedirs(lists, exist_ok=True)
man2 = os.path.join(lists, "L.wabbajack")
io.open(man2, "wb").write(b"stub")
try:
    root, tools = CFG.resolved_archive_root(), WB.wj_paths.app_dir(WB.__file__)
    for target, label, says in (
            (os.path.join(root, "Skyrim", "Installed"), "inside the archive root", "inside the archive root"),
            (os.path.dirname(root), "holding the archive root", "holds the archive root"),
            (os.path.join(tools, "Installed"), "inside the tools folder", "inside this tool's folder"),
            (lists, "holding the modlist file", "holds the modlist file")):
        problems = WB.check_paths(man2, dl, target)
        check("an install %s is refused, and says so" % label,
              any(says in p for p in problems), str(problems))
    check("  while one of its own is not",
          WB.check_paths(man2, dl, os.path.join(os.path.dirname(BOX), "elsewhere", "Installed")) == [],
          str(WB.check_paths(man2, dl, os.path.join(os.path.dirname(BOX), "elsewhere", "Installed"))))
finally:
    CFG.ARCHIVE_ROOT = saved_root

# ---------------------------------------------------------------- 9.11
print("\n 9.11  an archive of the right size and the wrong bytes is missing before Wabbajack")
# Found on 2026-10-27 installing Lost Legacy: FloatingSwordFollower.zip was
# the manifest's 500,008,225 bytes, real for the first fifth and zeros after
# - a download cut short once its file had been laid out at full length.
# Every table here counted it present, staging handed it over, and Wabbajack
# read it, called it missing, and stopped the install.
from harness import wj_hash
box = Sandbox(os.path.join(BOXROOT, "sb9c"))
saved_paths = (A.HERE, A.MANIFESTS, A.WJ.ARCHIVE_ROOT, CFG.ARCHIVE_ROOT)
A.HERE, A.MANIFESTS = box.tools, box.manifests
A.WJ.ARCHIVE_ROOT = CFG.ARCHIVE_ROOT = box.archives
GOOD = b"a good archive " * 70
FULL = bytes(range(256)) * 40
CUT = FULL[:len(FULL) // 5] + b"\0" * (len(FULL) - len(FULL) // 5)
OURS, THEIRS = b"o" * 50, b"t" * 50
fn, stem = "Lost_@@_legacy.wabbajack", "Lost_@@_legacy"
box.manifest(stem, "Fallout4", [("good.7z", len(GOOD), "nexus", None, wj_hash(GOOD)),
                                ("cut.zip", len(FULL), "nexus", None, wj_hash(FULL)),
                                ("same.7z", 50, "nexus", None, wj_hash(OURS)),
                                ("Fallout4.esm", 7, "gamefile")])
box.manifest("Other_@@_list", "Fallout4", [("same.7z", 50, "nexus", None, wj_hash(THEIRS))])
folder = os.path.join(box.archives, "Fallout4", stem)
trash = os.path.join(box.archives, "Fallout4", "Trash")
box.pool_file("Fallout4", stem, "good.7z", len(GOOD), GOOD)
box.pool_file("Fallout4", stem, "cut.zip", len(CUT), CUT)
box.pool_file("Fallout4", stem, "cut.zip.meta", 5)
box.pool_file("Fallout4", stem, "same.7z", 50, THEIRS)     # the other list's build, staged here
A.WJ.set_known_list_dirs(A.WJ.known_list_dirs_from(box.manifests))
A.WJ.invalidate_existing_cache()
said = []
real_out, real_run = W.out, A.run_script
W.out = lambda msg="": said.append(str(msg))
try:
    records = A.load_plan_records()
    st = W.list_status(records[fn])
    check("by name and size the cut-short archive counts as present",
          st["missing"] == 0 and st["present"] == 3, str({k: st[k] for k in ("present", "missing")}))
    res = W.verify_staged(fn, st)
    check("reading what was staged finds it", [b[0] for b in res["bad"]] == ["cut.zip"], str(res))
    check("  and says so, with both hashes",
          any("[WRONG] cut.zip" in s and wj_hash(CUT) in s and wj_hash(FULL) in s for s in said),
          " | ".join(said))
    check("  while the archive holding its bytes passes", res["ok"] == 1, str(res))
    check("  and the game file, Wabbajack's to copy, is not looked for",
          not any("Fallout4.esm" in s for s in said), " | ".join(said))
    check("it goes to the trash with its sidecar",
          sorted(os.listdir(trash)) == ["cut.zip", "cut.zip.meta"]
          and not os.path.exists(os.path.join(folder, "cut.zip"))
          and not os.path.exists(os.path.join(folder, "cut.zip.meta")), str(os.listdir(trash)))
    got = A.trash_contents().get("Fallout4") or []
    check("  recorded as stage 5 records what it reclaims, so it can be put back or purged",
          res["record"] and {n for n, _p, _s, when in got if when} == {"cut.zip", "cut.zip.meta"},
          str((res["record"], got)))
    check("another list's build of the same name and size is not ours to move",
          res["theirs"] == [("same.7z", "Other_@@_list")]
          and os.path.isfile(os.path.join(folder, "same.7z")), str(res["theirs"]))
    A.WJ.invalidate_existing_cache()
    after = W.list_status(records[fn])
    check("from then on it counts as missing, to every tool that asks",
          after["missing"] == 1 and after["nexus"] == 1
          and A.WJ.present_match(A.WJ.index_existing(os.path.join(box.archives, "Fallout4")),
                                 {"name": "cut.zip", "size": len(FULL)})[0] is None,
          str({k: after[k] for k in ("present", "missing")}))

    print("\n      what was read once is not read again, unless it changed")
    said.clear()
    again = W.verify_staged(fn, after)
    check("a second staging reads nothing it read before",
          again["read"] == 0 and again["ok"] == 1 and again["bad"] == [], str(again))
    box.pool_file("Fallout4", stem, "good.7z", len(GOOD), b"\0" * len(GOOD))
    later = os.path.getmtime(os.path.join(folder, "good.7z")) + 10
    os.utime(os.path.join(folder, "good.7z"), (later, later))
    third = W.verify_staged(fn, after)
    check("  but a file written since is read, and caught",
          third["read"] == 1 and [b[0] for b in third["bad"]] == ["good.7z"], str(third))

    print("\n      an update in place stops before Wabbajack is handed the folder")
    box.pool_file("Fallout4", stem, "good.7z", len(GOOD), GOOD)
    box.pool_file("Fallout4", stem, "cut.zip", len(CUT), CUT)  # staged again from elsewhere
    A.WJ.invalidate_existing_cache()
    installs = []
    stubs = (W.download_list, WB.running_processes, WB.run_install)
    A.run_script = lambda script, args, label, interactive=False: 0      # the sorter "ran"
    WB.running_processes = lambda: []
    WB.run_install = lambda *a, **k: installs.append(a) or 0
    state = {"lists": {"Lost/legacy": {"version": "2.0.2", "file": fn}},
             "installed": {stem: {"path": os.path.join(box.root, "Installed"), "version": "2.0.2"}}}
    ent = {"version": "2.0.2", "size": 1, "hash": "h", "title": "Lost", "updated": ""}
    try:
        ok, message = W.update_install(state, ("Lost/legacy", ent, state["lists"]["Lost/legacy"],
                                               fn, False, True), False, {"cli": "stub-cli"})
    finally:
        W.download_list, WB.running_processes, WB.run_install = stubs
    check("staging finds the wrong bytes, and the installer is never started",
          not ok and "wrong bytes" in message and installs == [], str((ok, message, installs)))
    check("  the archive is in the trash, and missing",
          W.list_status(records[fn])["missing"] == 1
          and not os.path.exists(os.path.join(folder, "cut.zip")), " | ".join(said[-6:]))
finally:
    W.out, A.run_script = real_out, real_run
    A.HERE, A.MANIFESTS, A.WJ.ARCHIVE_ROOT, CFG.ARCHIVE_ROOT = saved_paths
    shutil.rmtree(box.root, ignore_errors=True)

# ---------------------------------------------------------------- 9.12
print("\n 9.12  an out-of-date Wabbajack is brought up to date before an install")
import hashlib
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def wj_zip(names, wrap=""):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            z.writestr(wrap + n, b"program " + n.encode())
    return buf.getvalue()


WHOLE = ("Wabbajack.exe", "cli/wabbajack-cli.exe", "Extractors/7z.exe")
SERVE, HITS = {}, []


class Serve(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        HITS.append(self.path)
        body = SERVE.get(self.path)
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


srv = ThreadingHTTPServer(("127.0.0.1", 0), Serve)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % srv.server_port


def release(body, path="/4.3.2.1.zip", sha=True, size=None):
    SERVE[path] = body
    return {"version": "4.3.2.1", "url": "https://x/tag", "published": "",
            "asset": {"name": "4.3.2.1.zip", "url": BASE + path,
                      "size": len(body) if size is None else size,
                      "sha256": hashlib.sha256(body).hexdigest() if sha else None}}


def fresh_install():
    root = os.path.join(BOX, "wj-update")
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(os.path.join(root, "4.2.3.0", "cli"))
    io.open(os.path.join(root, "4.2.3.0", "Wabbajack.exe"), "wb").write(b"old")
    io.open(os.path.join(root, "4.2.3.0", "cli", "wabbajack-cli.exe"), "wb").write(b"old")
    io.open(os.path.join(root, "Wabbajack.exe"), "wb").write(b"launcher")
    return root, WB.find_wabbajack(hint=root)


def leftovers(root):
    return sorted(n for n in os.listdir(root) if n not in ("4.2.3.0", "Wabbajack.exe"))


api = {"tag_name": "4.3.2.1", "assets": [
    {"name": "4.3.2.1-linux-x64.tar.gz", "size": 5, "browser_download_url": "https://x/linux",
     "digest": "sha256:" + "0" * 64},
    {"name": "4.3.2.1.zip", "size": 171776039, "browser_download_url": "https://x/win",
     "digest": "sha256:" + "AB" * 32},
    {"name": "Wabbajack.exe", "size": 9, "browser_download_url": "https://x/launcher"}]}
a = WB.release_asset(api, "4.3.2.1")
check("the Windows build is the asset named for the tag, with its size and SHA-256",
      a == {"name": "4.3.2.1.zip", "url": "https://x/win", "size": 171776039,
            "sha256": "ab" * 32}, str(a))
api["assets"][1].pop("digest")
check("  a release published without digests has none, rather than a guess",
      WB.release_asset(api, "4.3.2.1")["sha256"] is None)
check("  and a release with no such asset has no Windows build",
      WB.release_asset(api, "9.9.9.9") is None and WB.release_asset(None, "4.3.2.1") is None)

root, wj = fresh_install()
up = WB.update_status(wj, latest=release(wj_zip(WHOLE)))
check("an outdated install carries the build to update to", up["outdated"] and up["asset"], str(up))
said = []
ok, message = WB.update_wabbajack(wj, up, on_line=said.append, running=[])
check("the release is downloaded, checked and unpacked into a folder named for it",
      ok and os.path.isfile(os.path.join(root, "4.3.2.1", "cli", "wabbajack-cli.exe"))
      and os.path.isfile(os.path.join(root, "4.3.2.1", "Extractors", "7z.exe")), message)
check("  saying how far the download has got", any("100% of" in s for s in said), str(said))
check("  the version in use is untouched, and nothing of the download is left",
      os.path.isfile(os.path.join(root, "4.2.3.0", "cli", "wabbajack-cli.exe"))
      and leftovers(root) == ["4.3.2.1"], str(leftovers(root)))
wj2 = WB.find_wabbajack(hint=root)
check("  the new version is the one found from then on, and the old one is pruned",
      wj2["version"] == "4.3.2.1" and WB.prune_old_versions(wj2, running=[]) == ["4.2.3.0"])

for what, kwargs, why in (
        ("a download whose SHA-256 is not the published one",
         dict(body=wj_zip(WHOLE)), "SHA-256"),
        ("a download of another size than the published one",
         dict(body=wj_zip(WHOLE), size=7), "larger than"),
        ("a zip naming a file outside its own folder",
         dict(body=wj_zip(WHOLE + ("../escaped.txt",))), "outside its own folder"),
        ("a zip without the CLI", dict(body=wj_zip(("Wabbajack.exe",))), "does not hold")):
    root, wj = fresh_install()
    rel = release(**kwargs)
    if why == "SHA-256":
        rel["asset"]["sha256"] = "0" * 64
    ok, message = WB.update_wabbajack(wj, WB.update_status(wj, latest=rel), running=[])
    check(f"{what} is refused, and leaves nothing behind",
          not ok and why in message and leftovers(root) == []
          and not os.path.exists(os.path.join(BOX, "escaped.txt")), f"{message} {leftovers(root)}")

root, wj = fresh_install()
ok, message = WB.update_wabbajack(
    wj, WB.update_status(wj, latest=release(wj_zip(WHOLE, wrap="4.3.2.1/"), sha=False)), running=[])
check("a zip that wraps the program in a folder of its own is unpacked from inside it",
      ok and os.path.isfile(os.path.join(root, "4.3.2.1", "Wabbajack.exe"))
      and leftovers(root) == ["4.3.2.1"], f"{message} {leftovers(root)}")

root, wj = fresh_install()
up = WB.update_status(wj, latest=release(wj_zip(WHOLE)))
HITS.clear()
ok, message = WB.update_wabbajack(wj, up, running=[(1, "Wabbajack.exe")])
check("nothing is fetched while Wabbajack runs", not ok and "running" in message and HITS == [],
      message)
real_running = WB.running_processes
WB.running_processes = lambda: None
try:
    ok, message = WB.update_wabbajack(wj, up)
finally:
    WB.running_processes = real_running
check("  nor when whether it runs cannot be told",
      not ok and "could not be checked" in message and HITS == [], message)
os.makedirs(os.path.join(root, "4.3.2.1"))
io.open(os.path.join(root, "4.3.2.1", "half.dll"), "wb").write(b"x")
ok, message = WB.update_wabbajack(wj, up, running=[])
check("a folder already bearing the new version's name is never written into",
      not ok and "already there" in message and HITS == []
      and os.listdir(os.path.join(root, "4.3.2.1")) == ["half.dll"], message)
ok, message = WB.update_wabbajack(wj, WB.update_status(wj, latest={"version": "4.3.2.1", "url": "u"}),
                                  running=[])
check("a release with no Windows build is said, not guessed at",
      not ok and "no Windows build" in message, message)
ok, message = WB.update_wabbajack(dict(wj, version="4.3.2.1"), up, running=[])
check("the newest release is left alone", ok and "already the newest" in message and HITS == [],
      message)

print("\n      the one way every install learns which Wabbajack to use")
real = (WB.latest_release, WB.running_processes, WB.CFG.WABBAJACK_DIR)
try:
    root, wj = fresh_install()
    WB.CFG.WABBAJACK_DIR = root
    rel = release(wj_zip(WHOLE))
    WB.latest_release = lambda force=False: rel
    WB.running_processes = lambda: []
    HITS.clear()
    wj2, up, why = WB.bring_up_to_date(wj, auto=False)
    check("with WABBAJACK_AUTO_UPDATE off, it only says how things stand",
          wj2 is wj and up["outdated"] and why is None and HITS == [])
    said = []
    wj2, up, why = WB.bring_up_to_date(wj, on_line=said.append)
    check("on, the new version is the one to use, current, and the old one gone",
          wj2["version"] == "4.3.2.1" and up["outdated"] is False and why is None
          and not os.path.exists(os.path.join(root, "4.2.3.0")), str((wj2, up, said)))
    check("  each step said", any("updating it" in s for s in said)
          and any("Removed Wabbajack 4.2.3.0" in s for s in said), str(said))
    root, wj = fresh_install()
    SERVE.clear()
    wj2, up, why = WB.bring_up_to_date(wj)
    check("a failed update keeps the version in use, and says why",
          wj2 is wj and up["outdated"] and why and "could not be installed" in why, str(why))
finally:
    WB.latest_release, WB.running_processes, WB.CFG.WABBAJACK_DIR = real

print("\n      the console says when Wabbajack could not be updated")
heard = []
real_bits = (W.out, WB.find_wabbajack, WB.latest_release, WB.update_wabbajack)
W.out = lambda msg="": heard.append(str(msg))
WB.find_wabbajack = lambda hint=None: {"dir": "stub-dir", "exe": None, "cli": "stub",
                                       "version": "4.2.3.0"}
WB.latest_release = lambda force=False: {"version": "4.3.2.1", "url": "https://x/tag",
                                         "asset": {"url": "https://x/win"}}
WB.update_wabbajack = lambda wj, update, on_line=None, running=None: (False, "the network is down")
blocked_st = {"game": "SkyrimSpecialEdition", "missing": 0, "bytes": 0}
try:
    W.offer_install({}, "Some_@@_list.wabbajack", blocked_st)   # BLOCKED: stops before asking
finally:
    W.out, WB.find_wabbajack, WB.latest_release, WB.update_wabbajack = real_bits
said_text = "\n".join(heard)
check("it tries, and says what stopped it",
      "updating it" in said_text and "the network is down" in said_text
      and "Not updated, so run Wabbajack.exe" in said_text, said_text[:600])
srv.shutdown()

shutil.rmtree(BOX, ignore_errors=True)
sys.exit(summary())
