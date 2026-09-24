"""The Wizard's own MO2 build, for collections collections2mo2 cannot build:
bsdiff patches, the Stardew layout, SMAPI into Stock Game, the order the
rules make, MO2's own files, and a whole build from a staged collection.

No network: a local server stands in for GitHub, a stub for Nexus, a zip for
MO2's release archive, and a folder of stubs for the game.
"""
import binascii
import bz2
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOXROOT = os.path.join(os.environ.get("TEMP", "/tmp"), "wj_tests")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import check, summary

sys.path.insert(0, REPO)
os.chdir(REPO)
import logging
import wj_config as CFG
import wj_downloader as WJ
import wj_collection as COL
import wj_mo2 as MO2
WJ.LOG.setLevel(logging.CRITICAL)

TMP = os.path.join(BOXROOT, "sb18")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)
QUIET = lambda s: None                                  # noqa: E731
SEVEN = MO2.seven_zip()


def md5(b):
    return hashlib.md5(b).hexdigest()


def crc(b):
    return "%08X" % (binascii.crc32(b) & 0xFFFFFFFF)


def zbytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def offtout(v):
    b = bytearray(abs(v).to_bytes(8, "little"))
    if v < 0:
        b[7] |= 0x80
    return bytes(b)


def make_patch(old, new, triples):
    """A BSDIFF40 patch taking old to new along the given control triples."""
    ctrl, diff, extra = bytearray(), bytearray(), bytearray()
    op = np = 0
    for x, y, z in triples:
        ctrl += offtout(x) + offtout(y) + offtout(z)
        for i in range(x):
            o = old[op + i] if 0 <= op + i < len(old) else 0
            diff.append((new[np + i] - o) & 0xFF)
        np += x
        op += x
        extra += new[np:np + y]
        np += y
        op += z
    c, d, e = bz2.compress(bytes(ctrl)), bz2.compress(bytes(diff)), bz2.compress(bytes(extra))
    return b"BSDIFF40" + offtout(len(c)) + offtout(len(d)) + offtout(len(new)) + c + d + e


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode("utf-8"))


def read(path):
    with open(path, "rb") as f:
        return f.read()


def tree(root):
    return sorted(MO2.list_files(root)) if os.path.isdir(root) else []


FILES = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = FILES.get(self.path.split("?")[0])
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % srv.server_port

print("=" * 70)
print("  T18  THE WIZARD'S OWN MO2 BUILD   (local server on %d, 7-Zip %s)"
      % (srv.server_port, "found" if SEVEN else "NOT found"))
print("=" * 70)

# ---------------------------------------------------------------- 18.1
print("\n 18.1  a BSDIFF40 patch makes the new file from the old")
old = bytes(range(256)) * 40 + b"the tail of the old file"
same = make_patch(old, old, [(len(old), 0, 0)])
check("a patch that changes nothing gives the old file back", MO2.bspatch(old, same) == old)
edited = bytearray(old)
edited[100:110] = b"0123456789"
edited[5000] ^= 0xFF
edited = bytes(edited)
check("bytes changed in place are made", MO2.bspatch(old, make_patch(old, edited, [(len(old), 0, 0)]))
      == edited)
inserted = old[:300] + b"INSERTED HERE" + old[300:]
check("bytes inserted come from the extra block",
      MO2.bspatch(old, make_patch(old, inserted, [(300, 13, 0), (len(old) - 300, 0, 0)])) == inserted)
moved = old[2000:2600] + old[0:100]
check("a backwards seek (negative offset) is followed",
      MO2.bspatch(old, make_patch(old, moved, [(0, 0, 2000), (600, 0, -2600), (100, 0, 0)]))
      == moved)
grown = old + b"beyond the old end"
check("bytes past the old file's end are the diff bytes alone",
      MO2.bspatch(old, make_patch(old, grown, [(len(grown), 0, 0)])) == grown)
check("offtin reads the sign from the top bit", MO2._offtin(offtout(-5)) == -5
      and MO2._offtin(offtout(1 << 40)) == 1 << 40)
good = make_patch(old, edited, [(len(old), 0, 0)])
for label, bad in (("a file that is not a patch", b"PK\x03\x04" + good[4:]),
                   ("a patch cut short", good[:40]),
                   ("a patch whose control block is not bzip2",
                    good[:32] + b"garbage!" + good[40:]),
                   ("a patch whose control runs past the new size",
                    make_patch(old, edited, [(len(old), 0, 0)])[:24] + offtout(10)
                    + make_patch(old, edited, [(len(old), 0, 0)])[32:])):
    try:
        MO2.bspatch(old, bad)
        check("%s is refused" % label, False)
    except MO2.PatchError:
        check("%s is refused" % label, True)

# ---------------------------------------------------------------- 18.2
print("\n 18.2  a patch is applied only to the file it was made from")
pdir = os.path.join(TMP, "patching")
target = os.path.join(pdir, "data.json")
write(target, old)
check("the right file is patched", MO2.apply_patch(target, crc(old), good) == "applied"
      and read(target) == edited)
check("  and nothing is left beside it", os.listdir(pdir) == ["data.json"], str(os.listdir(pdir)))
result = MO2.apply_patch(target, crc(old), good)
check("another version of the file is left as it is",
      result.startswith("not the file") and read(target) == edited, result)
check("a lower-case or short CRC32 still compares",
      MO2.apply_patch(target, crc(edited).lower().lstrip("0"), make_patch(edited, old, [(len(edited), 0, 0)]))
      == "applied" and read(target) == old)
check("a file the mod does not have says so",
      MO2.apply_patch(os.path.join(pdir, "gone.json"), crc(old), good) == "the file is not in the mod")

# ---------------------------------------------------------------- 18.3
print("\n 18.3  a Stardew mod is laid out as Vortex's Stardew extension lays it")
lay = os.path.join(TMP, "layout")
nested = ["Ridgeside Village/[CP] Ridgeside Village/manifest.json",
          "Ridgeside Village/[CP] Ridgeside Village/Assets/Maps/x.tmx",
          "Ridgeside Village/[SMAPI] Ridgeside Village/manifest.json",
          "Ridgeside Village/[SMAPI] Ridgeside Village/RSV.dll",
          "Ridgeside Village/README.txt", "screenshot.png"]
for f in nested:
    write(os.path.join(lay, "n", *f.split("/")), b"{}")
pairs, how = MO2.stardew_layout(os.path.join(lay, "n"), MO2.list_files(os.path.join(lay, "n")))
dests = [d for _s, d in pairs]
check("every manifest folder keeps its path in the archive",
      "Ridgeside Village/[CP] Ridgeside Village/Assets/Maps/x.tmx" in dests
      and "Ridgeside Village/[SMAPI] Ridgeside Village/RSV.dll" in dests, str(dests))
check("  and what lies outside one is dropped",
      "Ridgeside Village/README.txt" not in dests and "screenshot.png" not in dests, str(dests))
check("  and every file goes where it came from", all(s == d for s, d in pairs))
write(os.path.join(lay, "r", "manifest.json"),
      '\ufeff{\n  // SMAPI allows comments\n  "Name": "Beta: The Mod?",\n  "UniqueID": "b.b",\n}')
write(os.path.join(lay, "r", "assets", "b.png"), b"png")
pairs, how = MO2.stardew_layout(os.path.join(lay, "r"), MO2.list_files(os.path.join(lay, "r")),
                                fallback="Beta")
check("a manifest at the top gets a folder named after its Name (made safe)",
      sorted(d for _s, d in pairs) == ["Beta_ The Mod_/assets/b.png", "Beta_ The Mod_/manifest.json"],
      str(pairs))
write(os.path.join(lay, "l", "Pack", "i18n", "locale", "manifest.json"), b"{}")
write(os.path.join(lay, "l", "Pack", "config.json"), b"{}")
pairs, how = MO2.stardew_layout(os.path.join(lay, "l"), MO2.list_files(os.path.join(lay, "l")))
check("a manifest under a locale folder is no mod's, so the archive goes in as it is",
      how.startswith("as is") and len(pairs) == 2 and all(s == d for s, d in pairs), how)
write(os.path.join(lay, "u", "manifest.json"), b"{ not json")
write(os.path.join(lay, "u", "x.txt"), b"x")
pairs, how = MO2.stardew_layout(os.path.join(lay, "u"), MO2.list_files(os.path.join(lay, "u")),
                                fallback="My Mod")
check("a top manifest that does not parse falls back to the mod's own name",
      sorted(d for _s, d in pairs) == ["My Mod/manifest.json", "My Mod/x.txt"], str(pairs))
check("loose JSON: comments, trailing commas, and '//' inside a string survive",
      MO2.loose_json('{"a": "http://x", /* c */ "b": [1,2,],}') == {"a": "http://x", "b": [1, 2]})
check("folder names are made safe and unique",
      MO2.unique_names([MO2.safe_name("A/B"), "a_B", MO2.safe_name(" SMAPI "), "x."])
      == ["A_B", "a_B (2)", "SMAPI", "x."], str(MO2.unique_names(["A_B", "a_B", "SMAPI", "x."])))
check("  a name of nothing but dots and spaces gets the fallback", MO2.safe_name(" .. ", "m") == "m")
check("a profile name keeps to what MO2 stores plainly",
      MO2.safe_profile("Stardew Valley VERY Expanded") == "Stardew Valley VERY Expanded"
      and MO2.safe_profile("🐔 Farm: Life") == "Farm_ Life" and MO2.safe_profile("") == "Default",
      MO2.safe_profile("🐔 Farm: Life"))
rep = os.path.join(lay, "rep")
write(os.path.join(rep, "00 Core", "a.txt"), b"alpha")
write(os.path.join(rep, "01 Option", "a.txt"), b"other")
pairs, unmatched = MO2.replicate_layout(rep, MO2.list_files(rep),
                                        [{"path": "Mod\\a.txt", "md5": md5(b"other")},
                                         {"path": "Mod\\gone.txt", "md5": "0" * 32}])
check("a file-for-file mod takes each listed path from the file with its MD5",
      pairs == [("01 Option/a.txt", "Mod/a.txt")] and unmatched == ["Mod/gone.txt"], str(pairs))

# ---------------------------------------------------------------- 18.4
print("\n 18.4  the rules decide the order; a cycle is broken, and said")
order, aside = MO2.order_by_rules(4, [])
check("with no rules the collection's order stands", order == [0, 1, 2, 3] and aside == [])
order, aside = MO2.order_by_rules(4, [(3, 0), (2, 1)])
check("a rule moves a mod below the one it loads before", order.index(3) < order.index(0)
      and order.index(2) < order.index(1), str(order))
check("  and otherwise the earliest mod free to go goes first, the same every time",
      order == [2, 1, 3, 0] and MO2.order_by_rules(4, [(2, 1), (3, 0)])[0] == order, str(order))
order, aside = MO2.order_by_rules(3, [(0, 1), (1, 2), (2, 0)])
check("a cycle still gives every mod once", sorted(order) == [0, 1, 2], str(order))
check("  and names the rule it set aside", aside == [(2, 0)], str(aside))
order, aside = MO2.order_by_rules(2, [(0, 0), (0, 1), (0, 1), (5, 1)])
check("a rule about one mod, a repeat, or a mod not there is harmless",
      order == [0, 1] and aside == [], str((order, aside)))

# ---------------------------------------------------------------- 18.5
print("\n 18.5  SMAPI's loader into the game folder, its bundled mods into its mod")
INSTALL_DAT = zbytes({"StardewModdingAPI.exe": b"MZ smapi 4.5.2",
                      "StardewModdingAPI.dll": b"smapi dll",
                      "StardewModdingAPI.runtimeconfig.json": b"{}",
                      "smapi-internal/SMAPI.Toolkit.dll": b"toolkit",
                      "Mods/ConsoleCommands/manifest.json": b'{"Name": "Console Commands"}',
                      "Mods/SaveBackup/manifest.json": b'{"Name": "Save Backup"}'})
SMAPI_ZIP = zbytes({"SMAPI 4.5.2 installer/install on Windows.bat": b"@echo off",
                    "SMAPI 4.5.2 installer/internal/windows/install.dat": INSTALL_DAT,
                    "SMAPI 4.5.2 installer/internal/windows/SMAPI.Installer.exe": b"MZ inst",
                    "SMAPI 4.5.2 installer/internal/linux/install.dat": zbytes(
                        {"StardewModdingAPI": b"linux elf"})})
sm = os.path.join(TMP, "smapi")
write(os.path.join(sm, "SMAPI.zip"), SMAPI_ZIP)
MO2.unpack(os.path.join(sm, "SMAPI.zip"), os.path.join(sm, "unpacked"))
files = MO2.list_files(os.path.join(sm, "unpacked"))
check("the Windows payload is found, not Linux's",
      MO2.smapi_payload(files) == "SMAPI 4.5.2 installer/internal/windows/install.dat",
      str(MO2.smapi_payload(files)))
stock = os.path.join(sm, "Stock Game")
write(os.path.join(stock, "Stardew Valley.deps.json"), b'{"game": "deps"}')
game_files, mod_files = MO2.install_smapi(os.path.join(sm, "unpacked"), files, stock,
                                          os.path.join(sm, "mod"), os.path.join(sm, "dat"))
check("the loader goes into the game folder",
      read(os.path.join(stock, "StardewModdingAPI.exe")) == b"MZ smapi 4.5.2"
      and os.path.isfile(os.path.join(stock, "smapi-internal", "SMAPI.Toolkit.dll")))
check("  with the game's deps.json copied as its own, as SMAPI's installer does",
      read(os.path.join(stock, "StardewModdingAPI.deps.json")) == b'{"game": "deps"}'
      and "StardewModdingAPI.deps.json" in game_files)
check("  and the bundled mods into its MO2 mod, out of the game folder",
      sorted(mod_files) == ["ConsoleCommands/manifest.json", "SaveBackup/manifest.json"]
      and not os.path.exists(os.path.join(stock, "Mods")), str(mod_files))
check("  and its scratch folder is gone", not os.path.exists(os.path.join(sm, "dat")))
try:
    MO2.install_smapi(os.path.join(sm, "unpacked"), ["readme.txt"], stock, os.path.join(sm, "m2"),
                      os.path.join(sm, "dat2"))
    check("an archive that is not SMAPI's installer is refused", False)
except MO2.BuildError as e:
    check("an archive that is not SMAPI's installer is refused", "install.dat" in str(e), str(e))

# ---------------------------------------------------------------- 18.6
print("\n 18.6  MO2's own files")
game = MO2.GAMES["stardewvalley"]
ini = MO2.mo2_ini(game, r"D:\Modding\SVVE", "Stardew Valley VERY Expanded",
                  "//nas/Mod Archive/StardewValley/List, 1_@@_abc")
check("the game and its Stock Game, as MO2 stores them",
      "gameName=Stardew Valley\r\n" in ini
      and "gamePath=@ByteArray(D:\\\\Modding\\\\SVVE\\\\Stock Game)\r\n" in ini, ini[:200])
check("  SMAPI is the first executable, and the chosen one",
      "1\\title=SMAPI\r\n1\\binary=D:/Modding/SVVE/Stock Game/StardewModdingAPI.exe\r\n" in ini
      and "MainWindow_executablesListBox_index=1" in ini)
check("  run as the game's Steam app", "1\\steamAppID=413150" in ini and "2\\steamAppID=413150" in ini)
check("  with a way to look into the virtual Mods folder",
      '3\\arguments=\\"D:\\\\Modding\\\\SVVE\\\\Stock Game\\\\Mods\\"' in ini, ini)
check("  and a downloads folder with a comma quoted, so Qt does not read a list",
      'download_directory="//nas/Mod Archive/StardewValley/List, 1_@@_abc"' in ini)
check("  and the profile", "selected_profile=@ByteArray(Stardew Valley VERY Expanded)" in ini)
ml = MO2.modlist_txt(["Low", "Mid", "High"], disabled=["mid"])
check("modlist.txt puts the highest priority first, and keeps a switched-off mod off",
      ml.splitlines()[1:] == ["+High", "-Mid", "+Low"], ml)
meta = MO2.meta_ini(game, {"mod_id": 1000, "file_id": 1001, "version": "1.2", "archive": "A-1000.zip"})
check("meta.ini ties the mod to its Nexus file and its archive",
      "modid=1000" in meta and "1\\fileid=1001" in meta and "installationFile=A-1000.zip" in meta
      and "gameName=stardewvalley" in meta and "repository=Nexus" in meta)
check("  and a mod from nowhere on Nexus lists no file",
      "size=0" in MO2.meta_ini(game, {}) and "repository=\r\n" in MO2.meta_ini(game, {}))
check("profiles leave the saves where the game keeps them",
      "LocalSaves=false" in MO2.profile_settings())

# ---------------------------------------------------------------- 18.7
print("\n 18.7  MO2's release archive: pinned, cached, fetched only when missing")
FAKE_MO2 = zbytes({"ModOrganizer.exe": b"MZ mo2", "explorer++/Explorer++.exe": b"MZ exp",
                   "plugins/basic_games/games/game_stardewvalley.py": b"# plugin"})
real_mo2 = (MO2._mo2_places, MO2.MO2_URL, MO2.MO2_SIZE, MO2.MO2_SHA256)
cache = os.path.join(TMP, "mo2cache")
MO2._mo2_places = lambda: (os.path.join(cache, "mo2.7z"), os.path.join(TMP, "borrowed", "mo2.7z"))
MO2.MO2_SIZE, MO2.MO2_SHA256 = len(FAKE_MO2), hashlib.sha256(FAKE_MO2).hexdigest()
FILES["/mo2.7z"] = FAKE_MO2
MO2.MO2_URL = BASE + "/mo2.7z"
real_cache_dir = MO2.cache_dir
MO2.cache_dir = lambda: cache
try:
    check("nothing cached says so", MO2.mo2_cached() is False)
    got = MO2.mo2_archive(say=QUIET)
    check("it is downloaded and checked", got == os.path.join(cache, "mo2.7z")
          and read(got) == FAKE_MO2)
    check("  leaving only the archive in the cache", os.listdir(cache) == ["mo2.7z"],
          str(os.listdir(cache)))
    FILES["/mo2.7z"] = b"tampered" + FAKE_MO2[8:]
    check("  and then served from there", MO2.mo2_archive(say=QUIET) == got)
    os.remove(got)
    write(os.path.join(TMP, "borrowed", "mo2.7z"), FAKE_MO2)
    check("collections2mo2's cached copy is used when it is the pinned one",
          MO2.mo2_archive(say=QUIET) == got and read(got) == FAKE_MO2)
    os.remove(got)
    os.remove(os.path.join(TMP, "borrowed", "mo2.7z"))
    try:
        MO2.mo2_archive(say=QUIET)
        check("a download that is not the pinned archive is refused", False)
    except MO2.BuildError as e:
        check("a download that is not the pinned archive is refused", "pinned" in str(e), str(e))
    check("  and leaves nothing behind", os.listdir(cache) == [], str(os.listdir(cache)))
    FILES["/mo2.7z"] = FAKE_MO2
    MO2.mo2_archive(say=QUIET)
finally:
    MO2.MO2_URL = real_mo2[1]

# ---------------------------------------------------------------- 18.8
print("\n 18.8  a collection for Stardew, fetched, staged, planned and built")
DATA = b'{"Entries": {"x": "the original text"}}' * 20
DATA2 = DATA.replace(b"original", b"curated!")
ALPHA = zbytes({"[CP] Alpha/manifest.json": b'{"Name": "Alpha"}',
                "[CP] Alpha/content.json": b'{"Changes": "alpha"}',
                "[CP] Alpha/assets/data.json": DATA, "README.txt": b"not a mod file"})
CONFIG = zbytes({"[CP] Alpha/content.json": b'{"Changes": "configured"}'})
BETA = zbytes({"manifest.json": b'{"Name": "Beta Mod", "UniqueID": "b",}', "Beta.dll": b"dll"})
PACKED = zbytes({"[CP] Packed/manifest.json": b'{"Name": "Packed"}'})
SMAPI_MD5 = md5(SMAPI_ZIP)
DOMAIN = "stardewvalley"


def sv_mod(name, mod_id, body, **extra):
    m = {"name": name, "version": "1.0", "optional": False, "domainName": DOMAIN,
         "author": "someone", "details": {"category": "", "type": ""}, "phase": 0,
         "source": {"type": "nexus", "modId": mod_id, "fileId": mod_id + 1, "md5": md5(body),
                    "fileSize": len(body), "logicalFilename": name, "updatePolicy": "exact",
                    "tag": "t%d" % mod_id}}
    m.update(extra)
    return m


SV_MANIFEST = {
    "info": {"author": "Curator", "name": "Fixture Valley", "domainName": DOMAIN,
             "gameVersions": ["1.6.15.24356"], "description": "a Stardew fixture"},
    "mods": [
        sv_mod("SMAPI - Stardew Modding API ", 2400, SMAPI_ZIP,
               details={"category": "Modding Tools", "type": "SMAPI"}),
        sv_mod("Config for Alpha", 3000, CONFIG),
        sv_mod("Alpha", 1000, ALPHA, patches={"[CP] Alpha\\assets\\data.json": crc(DATA),
                                              "[CP] Alpha\\content.json": "DEADBEEF"}),
        sv_mod("Beta", 2000, BETA),
        {"name": "Marker", "version": "2.0.0", "optional": False, "domainName": DOMAIN,
         "details": {"category": "", "type": ""}, "phase": 0,
         "source": {"type": "bundle", "fileSize": 10, "tag": "mk",
                    "logicalFilename": "Bundled - Marker (v2.0.0).7z",
                    "fileExpression": "Bundled - Marker (v2.0.0)"}},
        {"name": "Packed", "version": "1", "optional": True, "domainName": DOMAIN,
         "details": {"category": "", "type": ""}, "phase": 0,
         "source": {"type": "bundle", "fileSize": len(PACKED), "tag": "pk",
                    "logicalFilename": "Packed.zip"}},
        {"name": "Elsewhere", "version": "1", "optional": False, "domainName": DOMAIN,
         "details": {"category": "", "type": ""}, "phase": 0,
         "source": {"type": "browse", "url": "https://example.invalid/elsewhere", "tag": "el"}},
    ],
    "modRules": [
        {"type": "before", "source": {"fileMD5": md5(ALPHA), "logicalFileName": "Alpha"},
         "reference": {"fileMD5": md5(CONFIG), "logicalFileName": "Config for Alpha"}},
        {"type": "after", "source": {"fileExpression": "Bundled - Marker (v2.0.0)"},
         "reference": {"fileMD5": md5(BETA)}},
        {"type": "before", "source": {"fileMD5": "f" * 32}, "reference": {"fileMD5": md5(BETA)}},
        {"type": "requires", "source": {"fileMD5": md5(BETA)}, "reference": {"fileMD5": SMAPI_MD5}},
    ],
    "collectionConfig": {"recommendNewProfile": True}, "tools": [],
}
DIFF = make_patch(DATA, DATA2, [(len(DATA), 0, 0)])
EXTRAS = {"bundled/Bundled - Marker (v2.0.0)/Marker/[CP] Marker/manifest.json": b'{"Name": "M"}',
          "bundled/Bundled - Marker (v2.0.0)/Marker/[CP] Marker/content.json": b"{}",
          "bundled/Packed.zip": PACKED,
          "patches/Alpha/[CP] Alpha/assets/data.json.diff": DIFF,
          "patches/Alpha/[CP] Alpha/content.json.diff": DIFF}
SV_NAMES = {(2400, 2401): "SMAPI 4.5.2-2400-4-5-2-1.zip", (3000, 3001): "Config-3000-1-0.zip",
            (1000, 1001): "Alpha-1000-1-0.zip", (2000, 2001): "Beta-2000-1-0.zip"}
BODIES = {2400: SMAPI_ZIP, 3000: CONFIG, 1000: ALPHA, 2000: BETA}


class FakeNexus:
    def __init__(self, archive_url=None):
        self.archive_url = archive_url

    def file_info(self, domain, mod_id, file_id):
        name = SV_NAMES.get((mod_id, file_id))
        if not name:
            return None, "notfound"
        return {"file_name": name, "size_in_bytes": len(BODIES[mod_id])}, "ok"

    def collection_archive_url(self, link_path):
        return (self.archive_url, "ok") if self.archive_url else (None, "notfound")


SV_INFO = {"slug": "svfix1", "name": "Fixture Valley", "domain": DOMAIN, "author": "Curator",
           "revision": 7, "latest": 7, "status": "published", "mod_count": 7,
           "total_size": 999, "download_link": "/v2/x/download_link", "adult": False}
coll_zip = zbytes(dict({"collection.json": json.dumps(SV_MANIFEST)}, **EXTRAS))
FILES["/sv-collection.zip"] = coll_zip
mdir = os.path.join(TMP, "modlists")
os.makedirs(mdir)
real_info = COL.revision_info
COL.revision_info = lambda slug, revision=None: dict(SV_INFO)
try:
    fn, changed = COL.fetch("https://www.nexusmods.com/games/stardewvalley/collections/svfix1", mdir,
                            FakeNexus(BASE + "/sv-collection.zip"), say=QUIET)
finally:
    COL.revision_info = real_info
spath = os.path.join(mdir, fn)
check("a Stardew collection is fetched, not refused", changed and fn.endswith(".collection"), fn)
rec = WJ.parse_manifest(spath)
check("  as a Stardew Valley list of its four downloads",
      rec["game"] == "StardewValley" and len(rec["archives"]) == 4, str(rec["game"]))
check("  keeping its bundled mods and patches in the list file",
      COL.list_extras(spath) == EXTRAS, str(sorted(COL.list_extras(spath) or {})))
check("  and saying so in wizard.json",
      COL.read_list_file(spath)["wizard"]["extras"] is True
      and COL.read_list_file(spath)["wizard"]["format"] == 2)
lines = COL.report_lines(COL.inspect(spath, look_up_game=False, look_up_latest=False))
check("the report says SMAPI goes into Stock Game and the patches are applied",
      any("SMAPI (into Stock Game)" in l for l in lines)
      and any("patched with the curator's binary patches" in l for l in lines)
      and not any("does not apply yet" in l for l in lines), "\n".join(lines))

pool = os.path.join(TMP, "Mod Archive", "StardewValley")
staged = os.path.join(pool, os.path.splitext(fn)[0])
os.makedirs(staged)
for (mod_id, _f), name in SV_NAMES.items():
    write(os.path.join(staged, name), BODIES[mod_id])
gdir = os.path.join(TMP, "Stardew Valley")
for rel, body in (("Stardew Valley.exe", b"MZ game"), ("Stardew Valley.dll", b"game dll"),
                  ("Stardew Valley.deps.json", b'{"game": "deps"}'),
                  ("Content/Data/Objects.xnb", b"xnb"), ("Mods/OldMod/manifest.json", b"{}"),
                  ("StardewModdingAPI.exe", b"MZ an old SMAPI"),
                  ("smapi-internal/old.dll", b"old")):
    write(os.path.join(gdir, *rel.split("/")), body)
game_before = {f: read(os.path.join(gdir, *f.split("/"))) for f in tree(gdir)}
real_dirs = WJ.resolve_game_dirs
COL.WJ.resolve_game_dirs = lambda g: (pool, None, None)
try:
    plan = COL.install_plan(spath, game_path=gdir)
finally:
    COL.WJ.resolve_game_dirs = real_dirs
check("the plan is the Wizard's own build", plan["engine"] == "wizard" and plan["c2"] is None
      and not COL.needs_setup(plan))
check("  with nothing in its way", plan["blockers"] == [] and plan["staged"][0] == 4,
      str(plan["blockers"]))
check("  and says who builds it",
      any(l.startswith("Installer   the Wizard's own build for Stardew Valley")
          for l in COL.plan_lines(plan)), "\n".join(COL.plan_lines(plan)))
check("  and why a game version matters here",
      COL.version_note(plan).startswith("SMAPI and its mods"), COL.version_note(plan))
check("  and what to know once it is built",
      any("%APPDATA%" in l for l in COL.done_lines(plan)))
busy = os.path.join(TMP, "busy")
write(os.path.join(busy, "something.txt"), b"x")
COL.set_instance(plan, busy)
check("an instance folder with anything in it is refused",
      any("already holds something" in p for p in plan["path_problems"]), str(plan["path_problems"]))
out = os.path.join(TMP, "instances", "Fixture")
COL.set_instance(plan, out)
check("  and a new one accepted", plan["path_problems"] == [], str(plan["path_problems"]))

scratch = os.path.join(TMP, "wm")
os.makedirs(scratch)
mods, left, aside = COL.wizard_mods(dict(plan, unavailable=[("Beta-2000-1-0.zip", "gone")]),
                                    SV_MANIFEST, EXTRAS, scratch)
check("a mod Nexus no longer serves is left out, and named",
      ("Beta", "Nexus no longer serves the file the collection pins") in left, str(left))
check("  as is one from another site", any(n == "Elsewhere" for n, _w in left), str(left))
mods, left, aside = COL.wizard_mods(plan, SV_MANIFEST, EXTRAS, scratch)
names = [m["name"] for m in mods]
check("the rules order the build: Alpha below the config that overrides it",
      names.index("Alpha") < names.index("Config for Alpha"), str(names))
check("  and the marker above Beta, matched by its file expression",
      names.index("Beta") < names.index("Marker"), str(names))
check("  and a rule naming a file outside the collection changes nothing", aside == [], str(aside))
by = {m["name"]: m for m in mods}
check("SMAPI is the loader, in a folder without its trailing space",
      by["SMAPI - Stardew Modding API"]["kind"] == "loader"
      and by["SMAPI - Stardew Modding API"]["folder"] == "SMAPI - Stardew Modding API")
check("each download is its staged archive, tied to its Nexus file",
      by["Alpha"]["source"] == os.path.join(staged, "Alpha-1000-1-0.zip")
      and by["Alpha"]["meta"]["mod_id"] == 1000 and by["Alpha"]["meta"]["file_id"] == 1001)
check("a patch carries its CRC32 and its diff",
      sorted((r, c, d is not None) for r, c, d in by["Alpha"]["patches"])
      == [("[CP] Alpha/assets/data.json", crc(DATA), True), ("[CP] Alpha/content.json", "DEADBEEF", True)])
check("a bundled folder is laid out of the list file",
      os.path.isfile(os.path.join(by["Marker"]["source"], "Marker", "[CP] Marker", "manifest.json")))
check("  and a bundled archive is written out to be unpacked",
      by["Packed"]["source"].endswith("Packed.zip") and read(by["Packed"]["source"]) == PACKED)
check("an optional mod is built switched off", by["Packed"]["enabled"] is False)
check("a patch folder named with other spacing or case is still found",
      COL.mod_patches({"patches/alpha /x.json.diff": b"d"}, {"name": "Alpha", "patches": {"x.json": "1"}})
      == [("x.json", "1", b"d")])

say_lines = []
rc = COL.install(plan, "KEY", on_line=say_lines.append)
check("the build finishes", rc == 0, "\n".join(say_lines[-12:]))
check("  in the folder asked for, with no build folder left beside it",
      os.path.isfile(os.path.join(out, "ModOrganizer.exe"))
      and not os.path.exists(out + MO2.BUILDING_SUFFIX)
      and os.path.isfile(os.path.join(out, "portable.txt")))
stock = os.path.join(out, MO2.STOCK_GAME)
check("Stock Game is the game, less its old mods and old SMAPI",
      read(os.path.join(stock, "Content", "Data", "Objects.xnb")) == b"xnb"
      and not os.path.exists(os.path.join(stock, "Mods", "OldMod"))
      and not os.path.exists(os.path.join(stock, "smapi-internal", "old.dll")))
check("  with the collection's SMAPI laid in, and its own deps.json",
      read(os.path.join(stock, "StardewModdingAPI.exe")) == b"MZ smapi 4.5.2"
      and read(os.path.join(stock, "StardewModdingAPI.deps.json")) == b'{"game": "deps"}')
check("  and an empty Mods folder for MO2 to map onto",
      os.path.isdir(os.path.join(stock, "Mods")) and os.listdir(os.path.join(stock, "Mods")) == [])
check("the store's install is exactly as it was",
      {f: read(os.path.join(gdir, *f.split("/"))) for f in tree(gdir)} == game_before)
mods_dir = os.path.join(out, "mods")
check("every mod has its folder",
      sorted(os.listdir(mods_dir)) == sorted(["SMAPI - Stardew Modding API", "Config for Alpha",
                                              "Alpha", "Beta", "Marker", "Packed"]),
      str(os.listdir(mods_dir)))
check("SMAPI's mod holds its bundled mods and no loader",
      tree(os.path.join(mods_dir, "SMAPI - Stardew Modding API"))
      == ["ConsoleCommands/manifest.json", "SaveBackup/manifest.json", "meta.ini"],
      str(tree(os.path.join(mods_dir, "SMAPI - Stardew Modding API"))))
check("Alpha is laid out by its manifest, the readme beside it dropped",
      tree(os.path.join(mods_dir, "Alpha")) == ["[CP] Alpha/assets/data.json", "[CP] Alpha/content.json",
                                                "[CP] Alpha/manifest.json", "meta.ini"],
      str(tree(os.path.join(mods_dir, "Alpha"))))
check("  its data patched by the curator's patch",
      read(os.path.join(mods_dir, "Alpha", "[CP] Alpha", "assets", "data.json")) == DATA2)
check("  and the file the other patch was not made from left alone",
      read(os.path.join(mods_dir, "Alpha", "[CP] Alpha", "content.json")) == b'{"Changes": "alpha"}')
check("Beta, with its manifest at the top, sits in a folder named for it",
      tree(os.path.join(mods_dir, "Beta")) == ["Beta Mod/Beta.dll", "Beta Mod/manifest.json", "meta.ini"],
      str(tree(os.path.join(mods_dir, "Beta"))))
check("the bundled marker and the bundled archive are both laid out",
      os.path.isfile(os.path.join(mods_dir, "Marker", "Marker", "[CP] Marker", "manifest.json"))
      and os.path.isfile(os.path.join(mods_dir, "Packed", "[CP] Packed", "manifest.json")))
profile = os.path.join(out, "profiles", "Fixture Valley")
listed = read(os.path.join(profile, "modlist.txt")).decode("utf-8").splitlines()[1:]
check("modlist.txt is the rules' order, highest first, the optional mod off",
      listed == ["-Packed", "+Marker", "+Beta", "+Config for Alpha", "+Alpha",
                 "+SMAPI - Stardew Modding API"], str(listed))
ini = read(os.path.join(out, "ModOrganizer.ini")).decode("utf-8")
check("ModOrganizer.ini points at this instance's Stock Game and the staged pool",
      ("gamePath=@ByteArray(%s)" % os.path.join(out, "Stock Game").replace("\\", "\\\\")) in ini
      and ("download_directory=" + staged.replace("\\", "/")) in ini, ini[:400])
alpha_meta = read(os.path.join(mods_dir, "Alpha", "meta.ini")).decode("utf-8")
check("a mod's meta.ini names its archive and Nexus file",
      "installationFile=Alpha-1000-1-0.zip" in alpha_meta and "1\\fileid=1001" in alpha_meta
      and "comments=collection:svfix1@7" in alpha_meta)
with open(os.path.join(out, MO2.MARK), encoding="utf-8") as f:
    mark = json.load(f)
check("wj-instance.json records the collection, what was left out, and every patch",
      mark["collection"]["slug"] == "svfix1" and mark["kind"] == "wj-mo2-instance"
      and [x["mod"] for x in mark["left_out"]] == ["Elsewhere"]
      and sum(1 for m in mark["mods"] for p in m["patches"] if p["result"] == "applied") == 1)
check("the build said what it did, patch by patch",
      any("1 of 2 patch(es) applied" in l for l in say_lines)
      and any("not the file the patch was made from" in l for l in say_lines)
      and any(l.startswith("built: 6 mod(s)") for l in say_lines), "\n".join(say_lines))

# ---------------------------------------------------------------- 18.9
print("\n 18.9  a build never lands on anything, and a failed one leaves nothing")
COL.set_instance(plan, out)
check("the built instance is refused as the folder for another",
      any("already holds something" in p for p in plan["path_problems"]))
try:
    MO2.build("stardewvalley", out, gdir, "P", [], say=QUIET)
    check("  by the build itself too", False)
except MO2.BuildError:
    check("  by the build itself too", True)
out2 = os.path.join(TMP, "instances", "Second")
write(os.path.join(out2 + MO2.BUILDING_SUFFIX, MO2.BUILDING_MARK), b"")
write(os.path.join(out2 + MO2.BUILDING_SUFFIX, "half.txt"), b"left from before")
bad = os.path.join(TMP, "bad.zip")
write(bad, b"PK\x03\x04 this is no zip at all")
try:
    MO2.build("stardewvalley", out2, gdir, "P",
              [{"name": "Broken", "folder": "Broken", "source": bad, "kind": "mod"}], say=QUIET)
    check("a mod that will not unpack stops the build", False)
except MO2.BuildError as e:
    check("a mod that will not unpack stops the build", "bad.zip" in str(e), str(e))
check("  leaving neither an instance nor a build folder",
      not os.path.exists(out2) and not os.path.exists(out2 + MO2.BUILDING_SUFFIX))
os.makedirs(out2 + MO2.BUILDING_SUFFIX)
write(os.path.join(out2 + MO2.BUILDING_SUFFIX, "theirs.txt"), b"not ours")
check("a folder in the way that the tool did not make is refused, not cleared",
      "is in the way" in (MO2.check_out(out2) or "")
      and os.path.isfile(os.path.join(out2 + MO2.BUILDING_SUFFIX, "theirs.txt")))
try:
    MO2.build("stardewvalley", os.path.join(TMP, "instances", "Third"), pool, "P", [], say=QUIET)
    check("a game folder without the game is refused", False)
except MO2.BuildError as e:
    check("a game folder without the game is refused", "Stardew Valley.exe" in str(e), str(e))
MO2._mo2_places, MO2.MO2_URL, MO2.MO2_SIZE, MO2.MO2_SHA256 = real_mo2
MO2.cache_dir = real_cache_dir

# ---------------------------------------------------------------- 18.10
print("\n 18.10  what a collection archive carries, from a 7z and from an older list")
check("a 7z listing's folders are not counted as its files",
      COL._slt_files("Path = bundled\nAttributes = D\n\nPath = bundled/a.txt\nAttributes = A\n\n"
                     "Path = z\nFolder = +\n") == ["bundled/a.txt"])
if SEVEN:
    src7 = os.path.join(TMP, "seven")
    for rel, body in (("collection.json", json.dumps(SV_MANIFEST).encode()),
                      ("bundled/Bundled - Marker (v2.0.0)/Marker/m.json", b"{}"),
                      ("patches/Alpha/x.json.diff", b"diff"), ("Ini Tweaks/a.ini", b"[x]")):
        write(os.path.join(src7, *rel.split("/")), body)
    arc = os.path.join(TMP, "coll.7z")
    subprocess.run([SEVEN, "a", "-t7z", arc, "."], cwd=src7, capture_output=True,
                   stdin=subprocess.DEVNULL)
    man, members = COL.read_archive(arc)
    check("a 7z collection archive's members are its files only",
          sorted(members) == sorted(["collection.json", "bundled/Bundled - Marker (v2.0.0)/Marker/m.json",
                                     "patches/Alpha/x.json.diff", "Ini Tweaks/a.ini"]), str(members))
    check("  and its extras are read", COL.read_extras(arc) == {
        "bundled/Bundled - Marker (v2.0.0)/Marker/m.json": b"{}", "patches/Alpha/x.json.diff": b"diff",
        "Ini Tweaks/a.ini": b"[x]"})
else:
    check("a 7z collection archive (skipped: no 7-Zip here)", True)
old_list = os.path.join(TMP, "old", "Old_@@_svfix1.collection")
os.makedirs(os.path.dirname(old_list))
entries, lo = COL.archive_entries(FakeNexus(), SV_MANIFEST, say=QUIET)
COL.write_list_file(old_list, SV_INFO, SV_MANIFEST, entries, lo, [])
check("a list that does not keep the extras says so", COL.list_extras(old_list) is None)
with zipfile.ZipFile(old_list) as z:
    wiz = json.loads(z.read("wizard.json"))
check("  and is marked as not keeping them", wiz["extras"] is False)
COL.revision_info = lambda slug, revision=None: dict(SV_INFO)
try:
    got = COL.download_extras(SV_INFO | {"url": "u"}, FakeNexus(BASE + "/sv-collection.zip"), say=QUIET)
    check("a build of such a list fetches the archive again for them", got == EXTRAS)
    try:
        COL.download_extras(SV_INFO, FakeNexus(None), say=QUIET)
        check("  and says so when Nexus will not hand it out", False)
    except COL.CollectionError:
        check("  and says so when Nexus will not hand it out", True)
finally:
    COL.revision_info = real_info

sys.exit(summary())
