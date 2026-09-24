"""Nexus collections as lists: naming one, turning collection.json into a list
file, the rest of the suite reading that file as a modlist, the MD5 that pins
each archive, and the hand-over to collections2mo2.

No network: a local server stands in for Nexus's CDN, and a stub stands in for
its API. Nothing here runs collections2mo2 or builds an instance.
"""
import hashlib
import io
import json
import os
import shutil
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOXROOT = os.path.join(os.environ.get("TEMP", "/tmp"), "wj_tests")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Sandbox, check, summary

sys.path.insert(0, REPO)
os.chdir(REPO)
import logging
import wj_config as CFG
import wj_downloader as WJ
import wj_collection as COL
WJ.LOG.setLevel(logging.CRITICAL)

BOX = os.path.join(BOXROOT, "sb17")
TMP = os.path.join(BOXROOT, "sb17_unit")
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)
G = "SkyrimSpecialEdition"
POOL = CFG.game_folder(G)


def md5(b):
    return hashlib.md5(b).hexdigest()


ALPHA = b"alpha archive bytes" * 7
BETA = b"beta skse" * 11
DELTA = b"delta direct download" * 5
ETA = b"eta optional" * 3
FILES = {"/files/Delta%20File.zip": DELTA}
HITS = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        HITS[path] = HITS.get(path, 0) + 1
        body = FILES.get(path)
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


def mod(name, kind, **src):
    m = {"name": name, "version": "1.0", "optional": False, "domainName": "skyrimspecialedition",
         "author": "someone", "details": {"category": "Utilities", "type": ""}, "phase": 0,
         "source": dict({"type": kind, "updatePolicy": "exact", "tag": name[:4]}, **src)}
    return m


MANIFEST = {
    "info": {"author": "Tester", "name": "Fixture", "domainName": "skyrimspecialedition",
             "gameVersions": ["1.7.104.0"], "description": "a fixture"},
    "mods": [
        dict(mod("Alpha", "nexus", modId=101, fileId=1001, md5=md5(ALPHA).upper(),
                 fileSize=len(ALPHA), logicalFilename="Alpha"),
             choices={"type": "fomod", "options": [{"name": "Main", "groups": [
                 {"name": "DLL", "choices": [{"name": "AE", "idx": 0}]}]}]}),
        dict(mod("Beta SKSE", "nexus", modId=102, fileId=1002, md5=md5(BETA), fileSize=len(BETA)),
             details={"category": "Utilities", "type": "dinput"}),
        mod("Gamma removed", "nexus", modId=103, fileId=1003, md5="0" * 32, fileSize=10),
        mod("Delta direct", "direct", url=BASE + "/files/Delta%20File.zip", md5=md5(DELTA),
            fileSize=len(DELTA)),
        mod("Epsilon bundled", "bundle", fileExpression="Bundled - Epsilon"),
        mod("Zeta browse", "browse", url="https://example.invalid/zeta"),
        dict(mod("Eta optional", "nexus", modId=104, fileId=1004, md5=md5(ETA), fileSize=len(ETA)),
             optional=True, phase=666, patches={"Eta.esp": "ABCDEF12"},
             hashes=[{"path": "Eta.esp", "md5": "1" * 32}]),
    ],
    "modRules": [{"type": "before", "source": {"logicalFileName": "Alpha"},
                  "reference": {"logicalFileName": "Beta SKSE"}}],
    "plugins": [{"name": "Alpha.esp", "enabled": True}, {"name": "Eta.esp", "enabled": True}],
    "pluginRules": {"plugins": [{"name": "Alpha.esp", "after": ["Eta.esp"]}], "groups": []},
    "tools": [],
}
NAMES = {(101, 1001): ("Alpha-101-1-0.7z", len(ALPHA)),
         (102, 1002): ("Skyrim Script Extender (SKSE64) Steam 102 2.3.1 x.7z", len(BETA)),
         (104, 1004): ("Eta-104-1.zip", len(ETA))}


class FakeNexus:
    """The two NexusClient calls a fetch makes, answered from NAMES."""

    def __init__(self, archive_url=None):
        self.asked = []
        self.archive_url = archive_url

    def file_info(self, domain, mod_id, file_id):
        self.asked.append((domain, mod_id, file_id))
        if (mod_id, file_id) in NAMES:
            name, size = NAMES[(mod_id, file_id)]
            return {"file_name": name, "size_in_bytes": size, "category_name": "MAIN"}, "ok"
        return None, "notfound"

    def collection_archive_url(self, link_path):
        return (self.archive_url, "ok") if self.archive_url else (None, "notfound")


INFO = {"slug": "abcd12", "name": "Fixture", "domain": "skyrimspecialedition",
        "author": "Tester", "revision": 3, "latest": 3, "status": "published",
        "mod_count": 7, "total_size": 12345, "download_link": "/v2/x/download_link",
        "adult": False}

print("=" * 70)
print("  T17  NEXUS COLLECTIONS AS LISTS   (local server on %d)" % srv.server_port)
print("=" * 70)

# ---------------------------------------------------------------- 17.1
print("\n 17.1  naming a collection")
cases = (
    ("https://www.nexusmods.com/games/skyrimspecialedition/collections/mi01mo",
     {"game": "skyrimspecialedition", "slug": "mi01mo", "revision": None}),
    ("nexusmods.com/games/skyrimspecialedition/collections/MI01MO/revisions/4",
     {"game": "skyrimspecialedition", "slug": "mi01mo", "revision": 4}),
    ("https://next.nexusmods.com/skyrimspecialedition/collections/mi01mo",
     {"game": "skyrimspecialedition", "slug": "mi01mo", "revision": None}),
    ("nxm://skyrimspecialedition/collections/mi01mo/revisions/2",
     {"game": "skyrimspecialedition", "slug": "mi01mo", "revision": 2}),
    ("nxm://skyrimspecialedition/collections/mi01mo/revisions/latest",
     {"game": "skyrimspecialedition", "slug": "mi01mo", "revision": None}),
    ('  "mi01mo"  ', {"game": None, "slug": "mi01mo", "revision": None}),
)
for text, want in cases:
    check("%-66s read" % text.strip()[:66], COL.parse_ref(text) == want, str(COL.parse_ref(text)))
for junk in ("", "hello world", "https://www.nexusmods.com/skyrimspecialedition/mods/12604",
             "a/b"):
    check("not a collection: %r" % junk, COL.parse_ref(junk) is None, str(COL.parse_ref(junk)))
check("the list stem is name then slug",
      COL.list_stem("TESTING, 1 2 3", "MI01MO") == "TESTING, 1 2 3_@@_mi01mo",
      COL.list_stem("TESTING, 1 2 3", "MI01MO"))
check("  characters a folder cannot hold are replaced",
      COL.list_stem('A/B: "c"?', "x1") == "A_B_ _c_@@_x1", COL.list_stem('A/B: "c"?', "x1"))
check("  a name with nothing left still gets a stem",
      COL.list_stem(" ?? ", "x1") == "collection_@@_x1", COL.list_stem(" ?? ", "x1"))
check("  and never a reserved pool name", COL.list_stem("Master", "x1").lower()
      not in CFG.reserved_list_names())
check("the address c2mo2 is handed",
      COL.collection_url("skyrimspecialedition", "mi01mo")
      == "https://www.nexusmods.com/games/skyrimspecialedition/collections/mi01mo")

# ---------------------------------------------------------------- 17.2
print("\n 17.2  a direct source is kept under the name c2mo2 looks for")
check("the URL's last path part, decoded",
      COL.direct_file_name("https://h/a/My%20Mod%20v2.7z?x=1", {}, {}) == "My Mod v2.7z")
check("else the logical file name",
      COL.direct_file_name("https://h/", {"logicalFilename": "Logic"}, {}) == "Logic")
check("else the mod name when it has an extension",
      COL.direct_file_name("https://h/", {}, {"name": "thing.zip"}) == "thing.zip")
check("else nothing", COL.direct_file_name("https://h/", {}, {"name": "thing"}) is None)

# ---------------------------------------------------------------- 17.3
print("\n 17.3  collection.json becomes Archives[] entries")
nx = FakeNexus()
entries, left = COL.archive_entries(nx, MANIFEST)
by_name = {e["Name"]: e for e in entries}
check("one entry per downloadable file",
      sorted(by_name) == sorted(["Alpha-101-1-0.7z", NAMES[(102, 1002)][0], "Delta File.zip",
                                 "Eta-104-1.zip"]), str(sorted(by_name)))
check("the name is the v1 file name, not the display name", "Alpha-101-1-0.7z" in by_name)
a = by_name.get("Alpha-101-1-0.7z", {})
check("  its size is the collection's", a.get("Size") == len(ALPHA))
check("  its MD5 is kept, lower-cased", a.get("MD5") == md5(ALPHA))
check("  it has no xxHash (a collection pins none)", a.get("Hash") is None)
check("  its State is a Nexus one with the pool's GameType",
      (a.get("State") or {}).get("$type", "").startswith("NexusDownloader")
      and a["State"].get("GameName") == G and a["State"].get("ModID") == 101
      and a["State"].get("FileID") == 1001, str(a.get("State")))
check("  its .meta names the Nexus domain and ids",
      "gameName=skyrimspecialedition" in a.get("Meta", "") and "modID=101" in a.get("Meta", "")
      and "fileID=1001" in a.get("Meta", ""))
d = by_name.get("Delta File.zip", {})
check("a direct source is an HttpDownloader with its URL",
      (d.get("State") or {}).get("$type", "").startswith("HttpDownloader")
      and d["State"].get("Url") == BASE + "/files/Delta%20File.zip")
why = {x["mod"]: x for x in left}
check("a removed Nexus file is left out, and says so",
      "Gamma removed" in why and "no longer serves" in why["Gamma removed"]["why"])
check("a bundled mod is noted as c2mo2's to unpack",
      "Epsilon bundled" in why and "collections2mo2 unpacks it" in why["Epsilon bundled"]["why"])
check("a browse source is noted as not installed yet",
      "Zeta browse" in why and "does not install" in why["Zeta browse"]["why"])
check("Nexus was asked once per Nexus file", len(nx.asked) == 4, str(nx.asked))
nx2 = FakeNexus()
known = {(101, 1001): NAMES[(101, 1001)], (102, 1002): NAMES[(102, 1002)]}
entries2, _left2 = COL.archive_entries(nx2, MANIFEST, known=known)
check("a later revision asks only about files not already known",
      sorted(x[1] for x in nx2.asked) == [103, 104], str(nx2.asked))
check("  and the answer is the same", sorted(e["Name"] for e in entries2) == sorted(by_name))

# ---------------------------------------------------------------- 17.4
print("\n 17.4  the list file is read like a .wabbajack")
mdir = os.path.join(TMP, "modlists")
os.makedirs(mdir)
members = ["collection.json", "bundled/Bundled - Epsilon/a.esp", "patches/Eta optional/Eta.esp.diff",
           "Ini Tweaks/Fast [Skyrim].ini", "bundled/"]
stem = COL.list_stem(INFO["name"], INFO["slug"])
path = COL.write_list_file(os.path.join(mdir, stem + CFG.COLLECTION_EXT), INFO, MANIFEST,
                           entries, left, members)
with zipfile.ZipFile(os.path.join(mdir, "Other.wabbajack"), "w") as z:
    z.writestr("modlist", json.dumps({"GameType": "Fallout4", "Archives": []}))
io.open(os.path.join(mdir, "notes.txt"), "w").write("not a list")
names = WJ.list_manifest_files(mdir)
check("the downloader lists both kinds, and nothing else",
      names == sorted(["Other.wabbajack", stem + ".collection"]), str(names))
check("CFG tells them apart", CFG.is_collection(stem + ".collection")
      and not CFG.is_collection("Other.wabbajack") and CFG.is_manifest_name("x.COLLECTION"))
rec = WJ.parse_manifest(path)
check("parse_manifest reads it", rec["readable"] and rec["game"] == G, str(rec)[:200])
check("  with every archive", len(rec["archives"]) == 4, str(len(rec["archives"])))
check("  each carrying its MD5",
      all(x["md5"] for x in rec["archives"])
      and {x["name"]: x["md5"] for x in rec["archives"]}["Alpha-101-1-0.7z"] == md5(ALPHA))
check("  and a kind the planner knows",
      sorted(WJ.state_type(x["state"]) for x in rec["archives"]) == ["http", "nexus", "nexus",
                                                                      "nexus"])
check("  and the Nexus ids the assist window matches a click by",
      WJ.nexus_ids(next(x for x in rec["archives"] if x["name"] == "Alpha-101-1-0.7z"))
      == (101, 1001))
cache = os.path.join(TMP, "cache.json")
light = WJ.load_light_records(mdir, cache)
check("the light cache the sorter plans from carries it",
      (light.get(stem + ".collection") or {}).get("game") == G
      and len(light[stem + ".collection"]["archives"]) == 4)
got = COL.read_list_file(path)
check("read_list_file gives back collection.json as fetched", got["collection"] == MANIFEST)
check("  and wizard.json with the slug, revision and address",
      got["wizard"]["slug"] == "abcd12" and got["wizard"]["revision"] == 3
      and got["wizard"]["url"].endswith("/skyrimspecialedition/collections/abcd12"))
check("  and what the archive carried besides",
      got["wizard"]["members"] == {"bundled": 1, "patches": 1, "ini_tweaks": 1},
      str(got["wizard"]["members"]))
check("  and what was left out", len(got["wizard"]["left_out"]) == 3)
try:
    COL.read_list_file(os.path.join(mdir, "Other.wabbajack"))
    check("a .wabbajack is not read as a collection", False)
except COL.CollectionError:
    check("a .wabbajack is not read as a collection", True)
for text in (stem, stem + ".collection", "abcd12",
             "https://www.nexusmods.com/games/skyrimspecialedition/collections/abcd12"):
    check("resolve_list finds it from %r" % text[:50], COL.resolve_list(mdir, text)
          == stem + ".collection")
check("resolve_list does not find a Wabbajack list", COL.resolve_list(mdir, "Other") is None)

# ---------------------------------------------------------------- 17.5
print("\n 17.5  what the collection asks for, counted")
feat = COL.features(MANIFEST)
check("mods and sources", feat["mods"] == 7 and feat["sources"] == {
    "nexus": 4, "direct": 1, "bundle": 1, "browse": 1}, str(feat["sources"]))
check("FOMODs, exact-file installs, patches, optional",
      (feat["fomod"], feat["replicate"], feat["patches"], feat["optional"]) == (1, 1, 1, 1))
check("game-root mods by type", feat["types"] == {"dinput": 1}, str(feat["types"]))
check("rules, plugins, plugin rules",
      (feat["rules"], feat["plugins"], feat["plugin_rules"]) == (1, 2, 1))
check("versions", feat["versions"] == ["1.7.104.0"])
check("three version parts decide", COL.version_matches("1.7.104.0", ["1.7.104.9"]) is True)
check("  a different one is a mismatch", COL.version_matches("1.7.104.0", ["1.6.1170.0"]) is False)
check("  nothing to compare is None", COL.version_matches(None, ["1.7"]) is None
      and COL.version_matches("1.7.104.0", []) is None)
v = COL.exe_version(sys.executable)
check("an executable's version is read", bool(v) and v.count(".") == 3, str(v))
check("  and a missing one is None", COL.exe_version(os.path.join(TMP, "nope.exe")) is None)

# ---------------------------------------------------------------- 17.6
print("\n 17.6  the MD5 a collection pins is checked after a download")
p = os.path.join(TMP, "delta.bin")
io.open(p, "wb").write(DELTA)
check("the right MD5 passes", WJ.verify_file(p, len(DELTA), None, md5(DELTA)) == (True, "ok"))
ok, why = WJ.verify_file(p, len(DELTA), None, md5(b"other"))
check("the wrong one fails, and says it was the MD5", not ok and "md5" in why, why)
check("  upper case in the manifest is the same MD5",
      WJ.verify_file(p, len(DELTA), None, md5(DELTA).upper())[0])
ok, why = WJ.verify_file(os.path.join(TMP, "gone.bin"), 5, None, md5(DELTA))
check("a missing file fails", not ok, why)

# ---------------------------------------------------------------- 17.7
print("\n 17.7  fetch, end to end, against a stub API and a local CDN")
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("collection.json", json.dumps(MANIFEST))
    z.writestr("bundled/Bundled - Epsilon/a.esp", b"x")
    z.writestr("Ini Tweaks/Fast [Skyrim].ini", b"[General]\n")
FILES["/collection.zip"] = buf.getvalue()
fdir = os.path.join(TMP, "fetched")
os.makedirs(fdir)
real_info = COL.revision_info
answers = {"info": dict(INFO)}
COL.revision_info = lambda slug, revision=None: dict(answers["info"])
try:
    nx = FakeNexus(BASE + "/collection.zip")
    fn, changed = COL.fetch("https://www.nexusmods.com/games/skyrimspecialedition/collections/abcd12",
                            fdir, nx, say=lambda s: None)
    check("a list file is written", changed and fn == "Fixture_@@_abcd12.collection", fn)
    check("  and nothing else is left in the manifest folder",
          os.listdir(fdir) == [fn], str(os.listdir(fdir)))
    rec = WJ.parse_manifest(os.path.join(fdir, fn))
    check("  it holds the four downloadable archives", len(rec["archives"]) == 4)
    check("  and what the archive carried",
          COL.read_list_file(os.path.join(fdir, fn))["wizard"]["members"]
          == {"bundled": 1, "patches": 0, "ini_tweaks": 1})
    before = HITS.get("/collection.zip", 0)
    fn2, changed2 = COL.fetch("abcd12", fdir, FakeNexus(BASE + "/collection.zip"),
                              say=lambda s: None)
    check("the same revision again changes nothing",
          fn2 == fn and changed2 is False and HITS.get("/collection.zip", 0) == before)
    answers["info"].update(revision=4, name="Fixture renamed")
    nx3 = FakeNexus(BASE + "/collection.zip")
    fn3, changed3 = COL.fetch("abcd12", fdir, nx3, say=lambda s: None)
    check("a new revision rewrites the same file, whatever the curator renamed it",
          changed3 and fn3 == fn, fn3)
    check("  asking Nexus only about the file it could not name before",
          [x[1] for x in nx3.asked] == [103], str(nx3.asked))
    check("  and records the new revision",
          COL.read_list_file(os.path.join(fdir, fn))["wizard"]["revision"] == 4)
    answers["info"].update(domain="cyberpunk2077")
    try:
        COL.fetch("abcd12", fdir, FakeNexus(BASE + "/collection.zip"), force=True,
                  say=lambda s: None)
        check("a game collections2mo2 cannot build is refused", False)
    except COL.CollectionError as e:
        check("a game collections2mo2 cannot build is refused", "cyberpunk2077" in str(e), str(e))
    answers["info"].update(domain="skyrimspecialedition", revision=5)
    try:
        COL.fetch("abcd12", fdir, FakeNexus(None), say=lambda s: None)
        check("an archive Nexus will not hand out stops the fetch", False)
    except COL.CollectionError:
        check("an archive Nexus will not hand out stops the fetch", True)
    check("  leaving the list file as it was",
          COL.read_list_file(os.path.join(fdir, fn))["wizard"]["revision"] == 4)
finally:
    COL.revision_info = real_info

# ---------------------------------------------------------------- 17.8
print("\n 17.8  the downloader and the sorter treat it as a modlist")
s = Sandbox(BOX)
spath = os.path.join(s.manifests, stem + CFG.COLLECTION_EXT)
COL.write_list_file(spath, INFO, MANIFEST, entries, left, members)
s.pool_file(POOL, "Master", "Alpha-101-1-0.7z", len(ALPHA))
s.pool_file(POOL, "Shared", NAMES[(102, 1002)][0], 3)          # the wrong build
io.open(os.path.join(s.archives, POOL, "Master", "Alpha-101-1-0.7z"), "wb").write(ALPHA)
rc, out = s.run("wj_downloader.py", ["--game", G, "--list", stem, "--dry-run", "--yes"],
                expect=0)
plan = out.split("FILES THAT WOULD BE DOWNLOADED", 1)[-1]
check("the dry run plans it", "FILES THAT WOULD BE DOWNLOADED" in out, out[-400:])
check("  a pooled archive of the right size is not fetched again",
      "Alpha-101-1-0.7z" not in plan)
check("  the wrong build in Shared is fetched", NAMES[(102, 1002)][0] in plan)
check("  as are the absent ones", "Delta File.zip" in plan and "Eta-104-1.zip" in plan)
rc, out = s.run("wj_downloader.py", ["--game", G, "--list", stem, "--skip-nexus", "--yes"])
dest = os.path.join(s.archives, POOL, stem, "Delta File.zip")
check("the direct source downloads into the collection's own folder",
      os.path.isfile(dest) and io.open(dest, "rb").read() == DELTA, out[-400:])
check("  with its .meta", os.path.isfile(dest + ".meta"))
FILES["/files/Delta%20File.zip"] = b"Z" * len(DELTA)           # same size, other bytes
os.remove(dest)
rc, out = s.run("wj_downloader.py", ["--game", G, "--list", stem, "--skip-nexus", "--yes"])
check("bytes that are not the collection's are discarded", not os.path.exists(dest),
      out[-400:])
check("  and the MD5 is named as the reason", "md5" in out.lower(), out[-400:])
FILES["/files/Delta%20File.zip"] = DELTA
rc, out = s.run("wj_downloader.py", ["--game", G, "--list", stem, "--skip-nexus", "--yes"])
rc, out = s.run("wj_sorter.py", ["--game", G, "--list", stem, "--yes"], expect=0)
staged = os.path.join(s.archives, POOL, stem)
check("staging gathers the pooled archive into the collection's folder",
      os.path.isfile(os.path.join(staged, "Alpha-101-1-0.7z")), str(s.tree().get(POOL)))

# ---------------------------------------------------------------- 17.9
print("\n 17.9  the staged folder is checked archive by archive")
for name, body in ((NAMES[(102, 1002)][0], BETA), ("Eta-104-1.zip", ETA)):
    io.open(os.path.join(staged, name), "wb").write(body)
ok_n, problems = COL.check_staged(spath, staged)
check("every archive present and right", ok_n == 4 and problems == [], str(problems))
io.open(os.path.join(staged, "Eta-104-1.zip"), "wb").write(b"E" * len(ETA))
os.remove(os.path.join(staged, "Delta File.zip"))
io.open(os.path.join(staged, NAMES[(102, 1002)][0]), "wb").write(b"short")
ok_n, problems = COL.check_staged(spath, staged)
reasons = dict(problems)
check("a missing one is named", "not in the staged folder" in reasons.get("Delta File.zip", ""),
      str(problems))
check("a wrong-size one is named", "bytes" in reasons.get(NAMES[(102, 1002)][0], ""))
check("a same-size, wrong-bytes one is named by its MD5",
      "MD5" in reasons.get("Eta-104-1.zip", ""))
check("  and the rest still count", ok_n == 1, str(ok_n))

# ---------------------------------------------------------------- 17.10
print("\n 17.10  the Wabbajack feeds are never asked about a collection")
import wj_autoupdate as A
saved = A.MANIFESTS
saved_wj = A.WJ
A.WJ = WJ                                  # what bootstrap() would have loaded
only = os.path.join(TMP, "only_collection")
os.makedirs(only)
shutil.copy2(spath, os.path.join(only, os.path.basename(spath)))
real_feeds = A.load_feeds
A.load_feeds = lambda *a, **k: (_ for _ in ()).throw(AssertionError("feeds were loaded"))
try:
    A.MANIFESTS = only
    state = {}
    try:
        tracked = A.resolve_tracking(state)
        check("resolving tracking loads no feed", True)
    except AssertionError:
        tracked = None
        check("resolving tracking loads no feed", False)
    check("  and records nothing about the collection",
          not any(k.endswith(".collection") for k in state.get("map", {})), str(state))
    check("  and does not follow it", not any(str(f or "").endswith(".collection")
                                              for f in (tracked or {}).values()))
finally:
    A.MANIFESTS = saved
    A.WJ = saved_wj
    A.load_feeds = real_feeds

# ---------------------------------------------------------------- 17.11
print("\n 17.11  collections2mo2: found, pinned, and handed the right command")
real_dir = COL.c2mo2_dir
c2root = os.path.join(TMP, "c2mo2")
COL.c2mo2_dir = lambda: c2root
try:
    check("absent is None", COL.find_c2mo2() is None)
    os.makedirs(os.path.join(c2root, "src", "collections2mo2"))
    io.open(os.path.join(c2root, "src", "collections2mo2", "cli.py"), "w").write("")
    io.open(os.path.join(c2root, COL.C2MO2_STAMP), "w").write(json.dumps({"commit": "0" * 40}))
    c2 = COL.find_c2mo2()
    check("a copy of another commit is found but not current",
          c2 is not None and c2["current"] is False)
    io.open(os.path.join(c2root, COL.C2MO2_STAMP), "w").write(
        json.dumps({"commit": COL.C2MO2_COMMIT}))
    c2 = COL.find_c2mo2()
    check("the pinned commit is current", c2["current"] is True)
    check("  run by a console Python", c2["python"] and
          not os.path.basename(c2["python"]).lower().startswith("pythonw"))
    args = COL.c2mo2_args(c2, "https://www.nexusmods.com/games/skyrimspecialedition/collections/abcd12",
                          4, r"D:\Mods\Fixture", r"C:\Games\Skyrim", r"Z:\Mod Archive\Skyrim\F")
    tail = args[args.index("create"):]
    check("it is a create, of the collection's address",
          tail[:2] == ["create",
                       "https://www.nexusmods.com/games/skyrimspecialedition/collections/abcd12"],
          str(tail))
    for flag, value in (("--revision", "4"), ("--out", r"D:\Mods\Fixture"),
                        ("--game-path", r"C:\Games\Skyrim"),
                        ("--downloads-dir", r"Z:\Mod Archive\Skyrim\F")):
        check("  %-15s is passed" % flag, flag in tail and tail[tail.index(flag) + 1] == value,
              str(tail))
    check("  with the Stock Game copy, and without the survey",
          "--stock-game" in tail and "--skip-survey" in tail)
    check("  as a module, in UTF-8", args[1:6] == ["-X", "utf8", "-s", "-m",
                                                    "collections2mo2.cli"], str(args[:6]))
    env = COL.c2mo2_env(c2, "KEY123")
    check("its environment puts its source and libraries first",
          env["PYTHONPATH"].split(os.pathsep)[:2] == [c2["src"], c2["site"]])
    check("  and hands it the key for this run only", env.get("NEXUS_API_KEY") == "KEY123"
          and os.environ.get("NEXUS_API_KEY") != "KEY123")
    check("  and no PYTHONHOME", "PYTHONHOME" not in env)

    game = os.path.join(TMP, "Game")
    os.makedirs(game)
    plan = COL.install_plan(spath, game_path=game)
    check("a plan with an incomplete staged folder is blocked",
          any("missing or wrong" in b for b in plan["blockers"]), str(plan["blockers"]))
    for name, body in (("Delta File.zip", DELTA), (NAMES[(102, 1002)][0], BETA),
                       ("Eta-104-1.zip", ETA)):
        io.open(os.path.join(staged, name), "wb").write(body)
    real_dirs = WJ.resolve_game_dirs
    COL.WJ.resolve_game_dirs = lambda g: (os.path.join(s.archives, POOL), None, None)
    try:
        plan = COL.install_plan(spath, game_path=game)
    finally:
        COL.WJ.resolve_game_dirs = real_dirs
    check("a complete one is not", plan["blockers"] == [] and plan["staged"][0] == 4,
          str(plan["blockers"]))
    for where, words in ((os.path.join(game, "inst"), "inside the game"),
                         (staged, "IS the downloads"),
                         (os.path.join(staged, "inst"), "inside the downloads")):
        COL.set_instance(plan, where)
        check("an instance %s is refused" % words,
              any(words in p for p in plan["path_problems"]), str(plan["path_problems"]))
    COL.set_instance(plan, os.path.join(TMP, "instance"))
    check("a folder of its own is accepted", plan["path_problems"] == [],
          str(plan["path_problems"]))
    check("the plan says what it will do",
          any(line.startswith("Instance") for line in COL.plan_lines(plan)))
    try:
        COL.install(dict(plan, blockers=["stop"]), "k")
        check("install refuses a blocked plan", False)
    except COL.CollectionError:
        check("install refuses a blocked plan", True)
    try:
        COL.install(dict(plan, out=None), "k")
        check("install refuses a plan with no instance folder", False)
    except COL.CollectionError:
        check("install refuses a plan with no instance folder", True)
finally:
    COL.c2mo2_dir = real_dir

# ---------------------------------------------------------------- 17.12
print("\n 17.12  setting collections2mo2 up refuses an archive it did not pin")
FILES["/c2mo2.zip"] = FILES["/collection.zip"]
real = (COL.C2MO2_ZIP_URL, COL.c2mo2_dir)
setup_root = os.path.join(TMP, "setup", ".c2mo2")
os.makedirs(os.path.dirname(setup_root))
COL.C2MO2_ZIP_URL = BASE + "/c2mo2.zip"
COL.c2mo2_dir = lambda: setup_root
try:
    try:
        COL.setup_c2mo2(say=lambda s: None)
        check("a wrong checksum stops it", False)
    except COL.CollectionError as e:
        check("a wrong checksum stops it", "SHA-256" in str(e), str(e))
    check("  and leaves nothing behind", os.listdir(os.path.dirname(setup_root)) == [],
          str(os.listdir(os.path.dirname(setup_root))))
finally:
    COL.C2MO2_ZIP_URL, COL.c2mo2_dir = real

# ---------------------------------------------------------------- 17.13
print("\n 17.13  reading a collection archive")
ztest = os.path.join(TMP, "c.zip")
io.open(ztest, "wb").write(FILES["/collection.zip"])
man, mem = COL.read_archive(ztest)
check("a zip's collection.json is read", man == MANIFEST)
check("  and its members listed", "Ini Tweaks/Fast [Skyrim].ini" in mem)
with zipfile.ZipFile(os.path.join(TMP, "empty.zip"), "w") as z:
    z.writestr("readme.txt", "no manifest here")
for label, target in (("an archive with no collection.json", os.path.join(TMP, "empty.zip")),
                      ("a file that is no archive", p)):
    try:
        COL.read_archive(target)
        check("%s is refused" % label, False)
    except COL.CollectionError:
        check("%s is refused" % label, True)

# ---------------------------------------------------------------- 17.14
print("\n 17.14  which rules count, and the game folder a build copies")
ruled = json.loads(json.dumps(MANIFEST))
ruled["modRules"] = [
    {"type": "before", "source": {"fileMD5": md5(ALPHA)}, "reference": {"fileMD5": md5(BETA)}},
    {"type": "after", "source": {"fileMD5": md5(ETA).upper()}, "reference": {"fileMD5": md5(ALPHA)}},
    {"type": "after", "source": {"fileMD5": md5(ALPHA)}, "reference": {"fileMD5": "f" * 32}},
    {"type": "conflicts", "source": {"fileMD5": md5(ALPHA)}, "reference": {"fileMD5": md5(BETA)}},
    {"type": "before", "source": {"logicalFileName": "Alpha"}, "reference": {"logicalFileName": "Beta"}},
]
check("a rule counts when both of its mods are in the collection, by MD5",
      COL.internal_rules(ruled) == 2, str(COL.internal_rules(ruled)))
check("  and the features say how many of all its rules those are",
      COL.features(ruled)["rules_internal"] == 2 and COL.features(ruled)["rules"] == 5)
report = {"wizard": {"name": "F", "author": "A", "revision": 1, "url": "u", "members": {},
                     "left_out": []},
          "features": COL.features(MANIFEST), "latest": None, "game": None,
          "pools": {"have": [], "need": [], "md5_wrong": [], "md5_checked": False}}
text = "\n".join(COL.report_lines(report))
check("a collection whose rules all name outside mods says so",
      "no mod rule between its own mods" in text, text)
report["features"] = COL.features(ruled)
check("  and one with rules between its own mods counts those",
      "2 mod rule(s) between its own mods" in "\n".join(COL.report_lines(report)))

gdir = os.path.join(TMP, "Games", "SSE")
os.makedirs(gdir)
COL.set_game(plan, gdir)
check("a game folder holding no game executable is refused",
      any("holds no SkyrimSE.exe" in p for p in plan["game_problems"]), str(plan["game_problems"]))
check("  and that stops the build", any("SkyrimSE.exe" in p for p in COL.stops(plan)))
shutil.copy2(sys.executable, os.path.join(gdir, "SkyrimSE.exe"))
COL.set_game(plan, gdir)
check("a real one is accepted", plan["game_problems"] == [] and plan["game"]["path"] == gdir,
      str(plan["game_problems"]))
check("  and remembered as chosen, not found", plan["game"]["how"] == "chosen for this collection")
check("  its version is read and compared with the collection's",
      plan["game"]["version"] == COL.exe_version(sys.executable)
      and plan["game"]["matches"] is False and plan["wanted"] == ["1.7.104.0"],
      str(plan["game"]))
check("  and an instance chosen before is checked against the new folder",
      plan["out"] and plan["path_problems"] == [], str(plan["path_problems"]))

print("\n      a game folder at another version, from Steam's depots")
steam = os.path.join(TMP, "Steam")
content = os.path.join(steam, "steamapps", "content", "app_489830")
for depot, files in (("489831", {"Data/Skyrim - Textures0.bsa": b"t0", "Data/Skyrim.esm": b"old"}),
                     ("489832", {"Data/Skyrim - Voices_en0.bsa": b"v"}),
                     ("489833", {"Data/Skyrim.esm": b"newest"})):
    for rel, body in files.items():
        p = os.path.join(content, "depot_" + depot, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        io.open(p, "wb").write(body)
shutil.copy2(sys.executable, os.path.join(content, "depot_489833", "SkyrimSE.exe"))
out_game = os.path.join(TMP, "Games", "Skyrim 1.5.97")
v = COL.assemble_game(G, out_game, steam=steam, say=lambda s: None)
check("the depots are laid into one folder", sorted(os.listdir(os.path.join(out_game, "Data")))
      == ["Skyrim - Textures0.bsa", "Skyrim - Voices_en0.bsa", "Skyrim.esm"])
check("  in order, so a later depot's file wins",
      io.open(os.path.join(out_game, "Data", "Skyrim.esm"), "rb").read() == b"newest")
check("  and the executable's version is what it returns", v == COL.exe_version(sys.executable))
check("  with a record of what it was made from",
      json.load(io.open(os.path.join(out_game, COL.ASSEMBLED_MARK)))["depots"]
      == ["489831", "489832", "489833"])
check("the same folder can be assembled again",
      COL.assemble_game(G, out_game, steam=steam, say=lambda s: None) == v)
for label, target, words in (
        ("a folder inside a Steam library", os.path.join(steam, "steamapps", "common", "X"),
         "Steam library"),
        ("a folder that holds something else", gdir, "already holds")):
    try:
        COL.assemble_game(G, target, steam=steam, say=lambda s: None)
        check("%s is refused" % label, False)
    except COL.CollectionError as e:
        check("%s is refused" % label, words in str(e), str(e))
shutil.rmtree(os.path.join(content, "depot_489832"))
try:
    COL.assemble_game(G, os.path.join(TMP, "Games", "other"), steam=steam, say=lambda s: None)
    check("a depot not downloaded yet stops it, by name", False)
except COL.CollectionError as e:
    check("a depot not downloaded yet stops it, by name", "depot_489832" in str(e), str(e))

srv.shutdown()
sys.exit(summary())
