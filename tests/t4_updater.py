"""Updater: state handling, matching helpers, and the guards that stop a run."""
import os
import sys

# Derive the repo root from this file so the suite runs from anywhere, rather
# than being pinned to whatever drive letter the share happens to have.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOXROOT = os.path.join(os.environ.get("TEMP", "/tmp"), "wj_tests")
import io
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Sandbox, check, summary, quiet_logging

sys.path.insert(0, REPO)
os.chdir(REPO)
import wj_autoupdate as A
quiet_logging(A)
A.WJ = A.load_downloader_module()

BOX = os.path.join(BOXROOT, "sb4")
G = "Fallout4"

print("=" * 70)
print("  T4  UPDATER: STATE, MATCHING, GUARDS")
print("=" * 70)

# ---------------------------------------------------------------- 4.1
print("\n 4.1  filename -> published feed key")
cases = [
    ("wj-featured_@@_living_skyrim.wabbajack", "wj-featured/living_skyrim"),
    ("A_@@_B.wabbajack", "A/B"),
    ("repo_@@_a_@@_b.wabbajack", "repo/a_@@_b"),   # only the FIRST separator
    ("NoSeparator.wabbajack", None),
    ("_@@_orphan.wabbajack", None),                # empty repo
    ("orphan_@@_.wabbajack", None),                # empty machine
    ("", None),
]
for name, want in cases:
    got = A.feed_key_from_filename(name) if name else None
    check("%-38s -> %s" % (name or "(empty)", got), got == want,
          "expected %s" % want)

# ---------------------------------------------------------------- 4.2
print("\n 4.2  a remembered 'matches nothing' must expire, and legacy ones retry")
future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
past = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
check("legacy '' is always retried", A.negative_expired("") is True)
check("a future retry_after is respected",
      A.negative_expired({"retry_after": future}) is False)
check("a past retry_after has expired",
      A.negative_expired({"retry_after": past}) is True)
check("garbage retry_after retries rather than sticking",
      A.negative_expired({"retry_after": "not a date"}) is True)
check("missing retry_after retries", A.negative_expired({}) is True)
check("an unexpected type retries", A.negative_expired(12345) is True)

print("\n      and a no-match is only recorded when every feed was readable")
cache = {}
A.remember_no_match(cache, "X.wabbajack", feeds_complete=False)
check("incomplete feeds -> nothing cached", cache == {}, str(cache))
A.remember_no_match(cache, "X.wabbajack", feeds_complete=True)
check("complete feeds -> cached with an expiry",
      "x.wabbajack" in cache and "retry_after" in cache["x.wabbajack"],
      str(cache))

# ---------------------------------------------------------------- 4.3
print("\n 4.3  naming a manifest we do not yet have")
ent = {"title": "Some List: With/Bad*Chars?"}
check("follows the _@@_ convention from the feed key",
      A.manifest_filename_for("LoreRim/LoreRim", ent)
      == "LoreRim_@@_LoreRim.wabbajack",
      A.manifest_filename_for("LoreRim/LoreRim", ent))
n = A.manifest_filename_for("Repo/With:Bad*Chars", ent)
check("falls back to the title when the key is not a legal filename",
      n.endswith(".wabbajack") and ":" not in n and "*" not in n, n)

# ---------------------------------------------------------------- 4.4
print("\n 4.4  a damaged state file must not stop a run")
box = Sandbox(BOX)
os.makedirs(box.runtime, exist_ok=True)
for bad, label in (("{ not json at all", "invalid JSON"),
                   ("[1,2,3]", "a list instead of an object"),
                   ("null", "null"),
                   ("", "empty file")):
    sf = os.path.join(box.runtime, A.STATE_FILE)
    io.open(sf, "w", encoding="utf-8").write(bad)
    saved_here, A.HERE = A.HERE, box.tools
    try:
        read_from = A.runtime_path(A.STATE_FILE)
        st = A.load_state()
    finally:
        A.HERE = saved_here
    check("recovers from %-28s" % label,
          os.path.normcase(read_from) == os.path.normcase(sf)
          and isinstance(st, dict) and isinstance(st.get("lists"), dict), str(st)[:60])

# ---------------------------------------------------------------- 4.5
print("\n 4.5  verify_manifest reuses a hash instead of re-reading the file")
p = os.path.join(box.tools, "probe.bin")
io.open(p, "wb").write(b"Q" * 1234)
real = A.WJ.wabbajack_hash(p)
ok, why = A.verify_manifest(p, 1234, real, known_hash=real)
check("matching known_hash passes without re-hashing", ok and "download" in why, why)
ok, why = A.verify_manifest(p, 1234, real, known_hash="AAAAAAAAAAA=")
check("mismatching known_hash fails", not ok and "mismatch" in why, why)
ok, why = A.verify_manifest(p, 9999, real, known_hash=real)
check("a wrong size fails before any hashing", not ok and "size" in why, why)
ok, why = A.verify_manifest(p, 1234, real)
check("with no known_hash it hashes for itself", ok, why)

# ---------------------------------------------------------------- 4.6
print("\n 4.6  odd feed shapes must not crash the parser")
shapes = [
    ([], "empty list"),
    ([{"links": {"machineURL": "m"}, "title": "T"}], "entry with links only"),
    ({"lists": [{"machineURL": "m", "title": "T"}]}, "dict wrapping a list"),
    ({"machineURL": "m", "title": "T"}, "a single bare entry"),
    ([None, 5, "x", {"machineURL": "ok"}], "list with junk in it"),
    ({"junk": "nothing useful"}, "dict with nothing usable"),
]
for data, label in shapes:
    try:
        got = list(A.iter_entries(data))
        norm = [A.normalise(e, "repo") for e in got]
        check("survives %-26s (%d usable)" % (label, len([n for n in norm if n])),
              True)
    except Exception as e:
        check("survives %-26s" % label, False, repr(e))

check("normalise drops an entry with no machineURL",
      A.normalise({"title": "T"}, "repo") is None)
n = A.normalise({"machineURL": "m", "title": "T",
                 "download_metadata": {"Size": "not an int"}}, "repo")
check("a non-integer Size becomes None rather than propagating",
      n is not None and n["size"] is None, str(n))

# ---------------------------------------------------------------- 4.7
print("\n 4.7  guards that must stop a run before it touches anything")
box = Sandbox(BOX)
box.manifest("ListA", G, [("a1.7z", 100, "nexus")])

# wj_autoupdate SETS WJ_MANIFEST_DIR for its children; the guard against a
# bad value lives in the two scripts that READ it.
bogus = {"WJ_MANIFEST_DIR": os.path.join(box.tools, "does-not-exist")}
for script in ("wj_downloader.py",
               "wj_sorter.py"):
    rc, out = box.run(script, ["--game", G, "--list", "ListA", "--dry-run",
                               "--yes"], env_extra=bogus)
    check("%-34s refuses a bad WJ_MANIFEST_DIR" % script.split("_")[-1], rc == 2,
          "exit %d" % rc)
    check("  and names the variable rather than guessing",
          "WJ_MANIFEST_DIR" in out)

# GAME_FOLDERS disagreeing between the two scripts. Both now take it from
# wj_config.py, so the way it can still go wrong is a local override added
# back into one of them - which is exactly what the check exists to catch.
sp = os.path.join(box.tools, "wj_sorter.py")
s = io.open(sp, encoding="utf-8").read()
anchor = "MANIFEST_SUBDIR = CFG.MANIFEST_SUBDIR"
assert anchor in s, "config import block not found in the sorter"
override = ("GAME_FOLDERS = dict(GAME_FOLDERS, "
            "SkyrimSpecialEdition=" + chr(34) + "SkyrimSE" + chr(34) + ")")
io.open(sp, "w", encoding="utf-8", newline="").write(
    s.replace(anchor, anchor + chr(10) + override, 1))
rc, out = box.run("wj_autoupdate.py", ["--check", "--nexus-mode", "skip"])
check("mismatched GAME_FOLDERS stops the run", rc == 2, "exit %d" % rc)
check("  and names the setting", "GAME_FOLDERS" in out)
shutil.copy2(os.path.join(REPO, "wj_sorter.py"), sp)

# xxhash missing -> refuse rather than silently weaken verification.
# An interpreter without xxhash: another one on the machine if there is one,
# else this one started with -S, which leaves site-packages - and so xxhash -
# off the path. The scripts need nothing else from there, so that is the same
# machine-without-xxhash the guard exists for, and the check no longer
# depends on what happens to be installed.
nohash = None
for cand in ("py", "python"):
    try:
        r = subprocess.run([cand, "-c", "import xxhash"], capture_output=True,
                           timeout=60)
        if r.returncode != 0:
            nohash = [cand]
            break
    except Exception:
        continue
if nohash is None:
    nohash = [sys.executable, "-S"]
proc = subprocess.run(nohash + [os.path.join(box.tools, "wj_autoupdate.py"),
                                "--check", "--nexus-mode", "skip"],
                      cwd=box.tools, capture_output=True, text=True,
                      errors="replace", timeout=300,
                      env=dict(os.environ, WJ_MANIFEST_DIR=box.manifests),
                      stdin=subprocess.DEVNULL)
o = proc.stdout + proc.stderr
check("without xxhash the run refuses to start (exit 2)",
      proc.returncode == 2, "exit %d" % proc.returncode)
check("  and explains why", "xxhash" in o)
proc = subprocess.run(nohash + [os.path.join(box.tools, "wj_autoupdate.py"),
                                "--check", "--nexus-mode", "skip",
                                "--allow-size-only"],
                      cwd=box.tools, capture_output=True, text=True,
                      errors="replace", timeout=300,
                      env=dict(os.environ, WJ_MANIFEST_DIR=box.manifests),
                      stdin=subprocess.DEVNULL)
check("  --allow-size-only is a deliberate way past it",
      proc.returncode != 2, "exit %d" % proc.returncode)

# ---------------------------------------------------------------- 4.8
print("\n 4.8  what an older version left beside the scripts moves into runtime")
import wj_paths as WP
box = Sandbox(BOX)
old = {"wj_autoupdate.log": b"old log\n", "wj_autoupdate_state.json": b'{"lists": {}}',
       "deleted_Skyrim_20260924_210617.csv": b"archive,size_bytes\n",
       "wj_sorter_20260924_210445.log": b"sorter\n", "wj_autoupdate_sorter.out": b"out\n",
       ".nxm_prev_handler": b"", ".wj_autoupdate.lock": b"1234 then\n"}
for name, data in old.items():
    io.open(os.path.join(box.tools, name), "wb").write(data)
os.makedirs(box.runtime)
io.open(os.path.join(box.runtime, "wj_overview.json"), "w", encoding="utf-8").write("{}")
io.open(os.path.join(box.tools, "wj_overview.json"), "w", encoding="utf-8").write('{"old": 1}')
io.open(os.path.join(box.tools, "notes.csv"), "w", encoding="utf-8").write("mine")
moved = WP.adopt_runtime(box.tools)
check("every runtime file is moved", sorted(moved) == sorted(old), str(sorted(moved)))
check("  and arrives intact", all(
    io.open(os.path.join(box.runtime, n), "rb").read() == d for n, d in old.items()))
check("  none of them is left beside the scripts",
      not any(os.path.exists(os.path.join(box.tools, n)) for n in old))
check("one already in the runtime folder is not overwritten, and the old copy is kept",
      io.open(os.path.join(box.runtime, "wj_overview.json"), encoding="utf-8").read() == "{}"
      and os.path.isfile(os.path.join(box.tools, "wj_overview.json")))
check("anything else beside the scripts is left alone",
      os.path.isfile(os.path.join(box.tools, "notes.csv"))
      and os.path.isfile(os.path.join(box.tools, "wj_config.py")))
check("a second pass moves nothing", WP.adopt_runtime(box.tools) == [])
io.open(os.path.join(box.tools, "wj_downloader_20260101_000000.log"), "w").write("x")
rc, out = box.run("wj_sorter.py", ["--game", G, "--yes", "--dry-run"])
check("a tool moves them itself as it starts",
      "wj_downloader_20260101_000000.log" in box.runtime_files()
      and not os.path.exists(os.path.join(box.tools, "wj_downloader_20260101_000000.log")),
      str(box.runtime_files()))
check("  and writes its own log in the runtime folder",
      len([n for n in box.runtime_files() if n.startswith("wj_sorter_")]) == 2,
      str(box.runtime_files()))

# A web of our own on localhost: the feed registry, the feeds, the manifests
# and archives they name, and a notification endpoint that writes down what
# it is sent. Nothing below reaches beyond it.
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from harness import wj_hash

SITE = {}           # path -> (status, body)
SEEN = {}           # path -> requests
NOTES = []          # (path, headers, body) of every POST


class Site(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        SEEN[path] = SEEN.get(path, 0) + 1
        status, body = SITE.get(path, (404, b"not here"))
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        NOTES.append((self.path, {k.lower(): v for k, v in self.headers.items()},
                      self.rfile.read(n).decode("utf-8", "replace")))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


srv = ThreadingHTTPServer(("127.0.0.1", 0), Site)
threading.Thread(target=srv.serve_forever, daemon=True).start()
WEB = "http://127.0.0.1:%d" % srv.server_port
REGISTRY = WEB + "/repositories.json"


def serve_json(path, data, status=200):
    SITE[path] = (status, json.dumps(data).encode())


def feeds(**named):
    """Serve the registry and these feeds: name -> entries (or a status)."""
    reg = {}
    for name, body in named.items():
        reg[name] = WEB + "/feeds/%s.json" % name
        if isinstance(body, int):
            SITE["/feeds/%s.json" % name] = (body, b"<html>down</html>")
        else:
            serve_json("/feeds/%s.json" % name, body)
    serve_json("/repositories.json", reg)


def entry(machine, title, version="1.0", size=None, h=None, download=None, game="fallout4"):
    return {"machineURL": machine, "title": title, "game": game, "version": version,
            "download_metadata": {"Size": size, "Hash": h},
            "links": {"download": download or WEB + "/m/%s.wabbajack" % machine}}


def manifest_bytes(name, archives=(), version="1.0", game=G):
    """A .wabbajack: archives are (filename, size, url, hash), fetched over http."""
    doc = {"Name": name, "Version": version, "GameType": game, "Archives": [
        {"Name": fn, "Size": size, "Hash": h, "Meta": "[General]\n",
         "State": {"$type": "HttpDownloader, Wabbajack.Lib", "Url": url}}
        for fn, size, url, h in archives]}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("modlist", json.dumps(doc))
    return buf.getvalue()


def read(p):
    try:
        return io.open(p, "rb").read()
    except OSError:
        return None


class Heard(logging.Handler):
    """What wj_autoupdate logs, while it is listened to - here, and not on
    the console or in the log the other handlers keep."""
    def __init__(self):
        super().__init__(logging.INFO)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())

    def __enter__(self):
        self.others = [(h, h.level) for h in A.LOG.handlers]
        for h, _lvl in self.others:
            h.setLevel(logging.CRITICAL)
        A.LOG.addHandler(self)
        self.level_was = A.LOG.level
        A.LOG.setLevel(logging.INFO)
        return self

    def __exit__(self, *a):
        A.LOG.removeHandler(self)
        A.LOG.setLevel(self.level_was)
        for h, lvl in self.others:
            h.setLevel(lvl)

    def said(self, text):
        return any(text in line for line in self.lines)


# ---------------------------------------------------------------- 4.9
print("\n 4.9  a feed that answered is not a feed that was read")
# Every negative conclusion - 'this file is no published list', '[a] no list
# wants this' - is drawn only from a complete picture. A feed whose shape or
# entries this version cannot read used to count as read, with nothing in it.
A.REPOSITORIES_URL = REGISTRY
good = [entry("ListA", "List A")]
feeds(good=good, empty=[])
A.reset_feed_cache()
got, complete = A.load_feeds()
check("every feed read, an empty one included: the picture is complete",
      complete and set(got) == {"good/ListA"}, str((complete, sorted(got))))
for name, body, label in (
        ("drift", [{"title": "no machineURL any more", "links": {"download": "x"}}],
         "an entry this version cannot read"),
        ("odd", "a string where a feed should be", "a shape this version does not read"),
        ("junk", [entry("ok", "OK"), 5], "something in it that is not an entry"),
        ("down", 500, "no answer at all")):
    feeds(good=good, **{name: body})
    A.reset_feed_cache()
    got, complete = A.load_feeds()
    check("a feed with %s leaves the picture incomplete" % label,
          not complete and "good/ListA" in got, str((complete, sorted(got))))
serve_json("/repositories.json", {"good": WEB + "/feeds/good.json", "..\\evil": WEB + "/feeds/good.json"})
A.reset_feed_cache()
got, complete = A.load_feeds()
check("so does one the registry names with a path in its name, which is left out",
      not complete and set(got) == {"good/ListA"}, str((complete, sorted(got))))
check("the shapes a feed may have",
      [A._feed_shape_ok(d) for d in ([], [{}], {"machineURL": "m"}, {"lists": [{}]},
                                     [{}, 1], "x", 5, None, {"x": 1}, {"lists": [1]})]
      == [True, True, True, True, False, False, False, False, False, False])

print("\n      a snapshot of the feeds is reused for a while, then fetched afresh")
feeds(good=good)
A.reset_feed_cache()
A.load_feeds()
asked = SEEN.get("/repositories.json", 0)
A.load_feeds({"good"})
check("a fresh snapshot answers the next ask", SEEN.get("/repositories.json") == asked)
A._FEED_CACHE["at"] -= A.FEED_SNAPSHOT_TTL + 1
A.load_feeds()
check("one older than FEED_SNAPSHOT_TTL is fetched again",
      SEEN.get("/repositories.json") == asked + 1)
feeds(good=good, down=500)
A.reset_feed_cache()
A.load_feeds()
asked = SEEN.get("/repositories.json", 0)
A.load_feeds()
check("an incomplete one is reused only a little while", SEEN.get("/repositories.json") == asked)
got, complete = A.load_feeds({"good"})
check("  and asked for feeds that all answered, it says they did",
      complete is True and set(got) == {"good/ListA"}, str((complete, sorted(got))))
got, complete = A.load_feeds({"good", "down"})
check("  and for one that did not, that it did not", complete is False and SEEN.get("/repositories.json") == asked)
A._FEED_CACHE["at"] -= A.FEED_RETRY_S + 1
A.load_feeds()
check("  and then the missing feed is tried again", SEEN.get("/repositories.json") == asked + 1)
A._FEED_CACHE["at"] = None                      # put there by hand, as the suites do
check("a snapshot put there by hand is kept", A._snapshot_fresh())
A.reset_feed_cache()

# ---------------------------------------------------------------- 4.10
print("\n 4.10  nothing to compare, or nothing read, is not a match")
p = os.path.join(Sandbox(BOX).tools, "probe.bin")
io.open(p, "wb").write(b"Q" * 1234)
real = A.WJ.wabbajack_hash(p)
ok, why = A.verify_manifest(p, None, None)
check("a feed entry with neither size nor hash adopts nothing", not ok and "no size or hash" in why, why)
real_hash = A.WJ.wabbajack_hash
A.WJ.wabbajack_hash = lambda path: None             # held open elsewhere; the share dropped
try:
    ok, why = A.verify_manifest(p, 1234, real)
finally:
    A.WJ.wabbajack_hash = real_hash
check("a file that could not be read to hash it does not match", not ok and "could not be read" in why,
      why)
io.open(p, "wb").write(b"<html>Too many requests</html>")
check("an error page is not a .wabbajack", bool(A.manifest_problem(p)))
io.open(p, "wb").write(manifest_bytes("X"))
check("  a zip holding a modlist is", A.manifest_problem(p) is None)
with zipfile.ZipFile(p, "w") as z:
    z.writestr("readme.txt", "no modlist in here")
check("  and a zip without one is not", "modlist" in (A.manifest_problem(p) or ""))

# ---------------------------------------------------------------- 4.11
print("\n 4.11  a manifest from a feed that states nothing is checked as a manifest")
box = Sandbox(BOX)
A.MANIFESTS = box.manifests
A.BACKUPS = os.path.join(box.tools, "manifest_backups")
os.makedirs(A.BACKUPS)
mine = os.path.join(box.manifests, "T.wabbajack")
OLD_M, NEW_M = manifest_bytes("T", version="1"), manifest_bytes("T", version="2")
io.open(mine, "wb").write(OLD_M)
SITE["/m/errorpage"] = (200, b"<html>Rate limited. Try again later.</html>")
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("readme.txt", "no modlist in here")
SITE["/m/nomodlist.wabbajack"] = (200, buf.getvalue())
SITE["/m/T.wabbajack"] = (200, NEW_M)
ent = {"title": "T", "version": "2", "size": None, "hash": None}
for path, label in (("/m/errorpage", "an error page served with a 200"),
                    ("/m/nomodlist.wabbajack", "a zip with no modlist in it")):
    ok = A.fetch_manifest(dict(ent, download=WEB + path), "T.wabbajack")
    check("%s is refused, and the manifest you have stays" % label,
          ok is False and read(mine) == OLD_M)
io.open(mine + ".new", "wb").write(b"half a manifest from a run that was stopped")
ok = A.fetch_manifest(dict(ent, download=WEB + "/m/T.wabbajack"), "T.wabbajack")
check("a real one is taken", ok is True and read(mine) == NEW_M)
check("  over a staging copy an interrupted run left, which is cleared, not kept beside it",
      sorted(os.listdir(box.manifests)) == ["T.wabbajack"], str(os.listdir(box.manifests)))

print("\n      the manifest you have is kept before it is replaced, on another volume too")
# Found 2026-10-10 in review: MANIFEST_DIR may point into Wabbajack's own
# folder on C: while manifest_backups sits beside the tools on a share. The
# backup was a rename, which fails between volumes, and the update went on to
# replace the manifest with no backup at all.
import errno


class TwoVolumes:
    """os, with the manifests and manifest_backups on two volumes: a rename
    from one to the other fails as it does on Windows."""
    def __getattr__(self, name):
        return getattr(os, name)

    def rename(self, src, dst):
        def backups(p):
            return os.path.normcase(os.path.abspath(p)).startswith(os.path.normcase(A.BACKUPS))
        if backups(src) != backups(dst):
            raise OSError(errno.EXDEV, "The system cannot move the file to a different disk drive")
        return os.rename(src, dst)


V3 = manifest_bytes("T", version="3")
SITE["/m/T3.wabbajack"] = (200, V3)
real_os = A.wj_paths.os
A.wj_paths.os = TwoVolumes()
try:
    ok = A.fetch_manifest(dict(ent, version="3", download=WEB + "/m/T3.wabbajack"), "T.wabbajack")
finally:
    A.wj_paths.os = real_os
check("with the backups on another volume, the update keeps the manifest it replaces",
      ok is True and read(mine) == V3
      and read(os.path.join(A.BACKUPS, "T.wabbajack.bak")) == NEW_M, str(os.listdir(A.BACKUPS)))
SITE["/m/T4.wabbajack"] = (200, manifest_bytes("T", version="4"))
real_move = A.wj_paths.move_file


def cannot_move(src, dst):
    raise OSError(13, "Access is denied")


A.wj_paths.move_file = cannot_move
try:
    ok = A.fetch_manifest(dict(ent, version="4", download=WEB + "/m/T4.wabbajack"), "T.wabbajack")
finally:
    A.wj_paths.move_file = real_move
check("  and one that cannot be kept is not replaced at all",
      ok is False and read(mine) == V3 and sorted(os.listdir(box.manifests)) == ["T.wabbajack"],
      str(os.listdir(box.manifests)))

# ---------------------------------------------------------------- 4.12
print("\n 4.12  a notification goes only to a web address, and only from a real run")
A._NOTIFIED = False
A.NOTIFY_URL = "file:///C:/Windows/win.ini"
A.notify("t", "b")
check("a NOTIFY_URL that is not a web address is not used", NOTES == [] and A._NOTIFIED is False)
A.NOTIFY_URL, A.DRY_RUN = WEB + "/notify", True
A.notify("t", "b")
check("a dry run sends nothing", NOTES == [] and A._NOTIFIED is False)
A.DRY_RUN = False
A.notify("Title here", "Body here")
check("a real run sends it", NOTES and NOTES[-1][1].get("title") == "Title here"
      and NOTES[-1][2] == "Body here" and A._NOTIFIED is True, str(NOTES))
A.NOTIFY_URL, A._NOTIFIED = None, False
NOTES.clear()
url = "https://user:s3cret@ntfy.example.invalid/topic"
check("an error about the address never quotes its password",
      A._without_credentials("URLError: %s refused" % url, url)
      == "URLError: https://***@ntfy.example.invalid/topic refused")

# ---------------------------------------------------------------- 4.13
print("\n 4.13  a file is followed when its name is the feed key, or when it is pinned")
# A title is a guess: an author's beta of a list carries the list's own Name,
# and a file followed by its title was replaced by the published manifest at
# the next update. Now the guess is said, and followed once it is pinned.
box = Sandbox(BOX)
A.MANIFESTS = box.manifests
for fn, name in (("repo_@@_Exact.wabbajack", "Exact"), ("My Beta.wabbajack", "Some List"),
                 ("Old Title Match.wabbajack", "Other")):
    io.open(os.path.join(box.manifests, fn), "wb").write(manifest_bytes(name))
A.reset_feed_cache()
A._FEED_CACHE.update(all={e["key"]: e for e in (
    A.normalise(entry("Exact", "Exact"), "repo"), A.normalise(entry("SomeList", "Some List"), "repo"),
    A.normalise(entry("Other", "Other"), "repo"))}, complete=True, at=None)
saved = (A.HERE, A.TRACK, A.AUTO_TRACK)
A.HERE, A.TRACK, A.AUTO_TRACK = box.tools, [], True
state = {"lists": {}, "map": {"old title match.wabbajack": "repo/Other"}}   # an older version's match
try:
    with Heard() as log:
        tracked = A.resolve_tracking(state)
    check("the file named after its feed key is followed",
          tracked == {"repo/Exact": "repo_@@_Exact.wabbajack"}, str(tracked))
    check("  one that only looks like a list by its name is not, and the guess is said",
          log.said("[SUGGEST] My Beta.wabbajack") and "repo/SomeList" in " ".join(log.lines))
    check("  one an older version followed by its title is no longer followed, and that is said",
          log.said("[UNTRACK] Old Title Match.wabbajack")
          and state["map"].get("old title match.wabbajack") != "repo/Other", str(state["map"]))
    A.TRACK = ["repo/SomeList"]
    with Heard() as log:
        tracked = A.resolve_tracking(state)
    check("pinned in TRACK, the guess is followed", tracked.get("repo/SomeList") == "My Beta.wabbajack"
          and log.said("pinned in TRACK"), str(tracked))
    # Found 2026-10-10 in review: with the published list's own file there as
    # well, whichever file was met last stood for the list.
    for fn in ("repo_@@_SomeList.wabbajack", "zz Some List beta.wabbajack"):
        io.open(os.path.join(box.manifests, fn), "wb").write(manifest_bytes("Some List"))
    with Heard() as log:
        tracked = A.resolve_tracking(state)
    check("  and with the published list's own file there too, that file stands for it",
          tracked.get("repo/SomeList") == "repo_@@_SomeList.wabbajack"
          and log.said("both stand for repo/SomeList"), str(tracked))
finally:
    A.HERE, A.TRACK, A.AUTO_TRACK = saved
    A.reset_feed_cache()
    A.MANIFESTS = A.HERE

# ---------------------------------------------------------------- 4.14
print("\n 4.14  a whole unattended run, against feeds of our own")
ARCH = b"n" * 50
V1 = manifest_bytes("ListA", [("old.7z", 40, WEB + "/a/old.7z", wj_hash(b"o" * 40))], version="1.0")
LOCAL = "repo_@@_ListA.wabbajack"


def unattended(settings=None, archives=True, extra=(), again=None):
    """A sandbox holding ListA v1, and a run of `wj_autoupdate.py --run` -
    or, given `again`, another run in that sandbox, state and all."""
    b = again or Sandbox(BOX)
    if again is None:
        io.open(os.path.join(b.manifests, LOCAL), "wb").write(V1)
    # No margin: the run's free-space check is about the disk the suite runs
    # on, which is not what is being tested.
    io.open(os.path.join(b.tools, "wj_settings.json"), "w", encoding="utf-8").write(
        json.dumps(dict(settings or {}, FREE_SPACE_MARGIN_GB=0)))
    if not archives:
        shutil.rmtree(b.archives)
    NOTES.clear()
    rc, out = b.run("wj_autoupdate.py", ["--run"] + list(extra),
                    env_extra={"WJ_FEED_REGISTRY": REGISTRY})
    return b, rc, out


def notes():
    return [h.get("title", "") for _p, h, _b in NOTES]


current = [entry("ListA", "List A", "1.0", len(V1), wj_hash(V1))]
feeds(repo=current)
first, rc, out = unattended({"NOTIFY_URL": WEB + "/notify"})
check("--run is a run of its own: every list current, exit 0, nothing to report",
      rc == 0 and "All 1 tracked list(s) are current" in out and notes() == [], out[-600:])
check("  and the list it adopted is recorded", A.STATE_FILE in first.runtime_files(),
      str(first.runtime_files()))

feeds(repo=500)                                  # the next run in the same folder
b, rc, out = unattended({"NOTIFY_URL": WEB + "/notify", "NEXUS_MODE": "assist"}, again=first)
check("with the tracked list's own feed not answering, 'all current' is not claimed: exit 1",
      rc == 1 and "not every feed answered" in out.lower(), out[-600:])
check("  and the run says so to NOTIFY_URL", any("exit 1" in t for t in notes()), str(notes()))
check("  with nobody at a console, assist mode from the settings is not waited out",
      "Nobody is at a console" in out, out[-600:])

feeds(repo=current, other=500)
b, rc, out = unattended({"NOTIFY_URL": WEB + "/notify"})
check("a feed this run tracks nothing in that does not answer changes nothing: exit 0",
      rc == 0 and "All 1 tracked list(s) are current" in out and notes() == [], out[-600:])

feeds(repo=500)
b, rc, out = unattended({"NOTIFY_URL": WEB + "/notify"}, extra=["--dry-run"])
check("a dry run that fails the same way: exit 1, and nothing sent or written",
      rc == 1 and notes() == [] and A.STATE_FILE not in b.runtime_files(),
      str((rc, notes(), b.runtime_files())))

b, rc, out = unattended({"NOTIFY_URL": WEB + "/notify"}, archives=False)
check("a run that cannot start exits 2, and says so to NOTIFY_URL",
      rc == 2 and any("could not start (exit 2)" in t for t in notes()), str((rc, notes(), out[-400:])))

V2 = manifest_bytes("ListA", [("new.7z", 50, WEB + "/a/new.7z", wj_hash(ARCH))], version="2.0")
SITE["/m/ListA.wabbajack"] = (200, V2)
SITE["/a/new.7z"] = (200, ARCH)
feeds(repo=[entry("ListA", "List A", "2.0", len(V2), wj_hash(V2))])
b, rc, out = unattended({"NOTIFY_URL": WEB + "/notify"}, extra=["--no-sort", "--order", "largest"])
landed = os.path.join(b.archives, G, "repo_@@_ListA", "new.7z")
check("an update: the new manifest, and the archive it adds, fetched: exit 0",
      rc == 0 and read(os.path.join(b.manifests, LOCAL)) == V2 and read(landed) == ARCH,
      out[-800:])
check("  the run's order reaches the downloader", "--order largest" in out)
check("  and the run is reported", any("1 list(s) processed" in t for t in notes()), str(notes()))

V3 = manifest_bytes("ListA", [("gone.7z", 60, WEB + "/a/gone.7z", wj_hash(b"g" * 60))], version="3.0")
SITE["/m/ListA.wabbajack"] = (200, V3)
feeds(repo=[entry("ListA", "List A", "3.0", len(V3), wj_hash(V3))])
b, rc, out = unattended({"NOTIFY_URL": WEB + "/notify"}, extra=["--no-sort"])
check("an archive that could not be fetched: exit 1, and the report says what is left",
      rc == 1 and "still missing" in out and any("problem" in t for t in notes()),
      str((rc, notes(), out[-800:])))
state = json.loads(read(os.path.join(b.runtime, A.STATE_FILE)) or b"{}")
check("  and the list is remembered as needing another pass",
      state.get("lists", {}).get("repo/ListA", {}).get("needs_archives") is True, str(state)[:300])

srv.shutdown()
sys.exit(summary())
