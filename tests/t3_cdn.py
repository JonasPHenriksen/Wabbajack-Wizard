"""Wabbajack CDN multi-part protocol: failure and resume handling."""
import os
import sys

# Derive the repo root from this file so the suite runs from anywhere, rather
# than being pinned to whatever drive letter the share happens to have.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOXROOT = os.path.join(os.environ.get("TEMP", "/tmp"), "wj_tests")
import gzip
import io
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)
from harness import check, summary
import logging
import wj_cdn as C
C.LOG.setLevel(logging.CRITICAL)

TMP = os.path.join(os.environ["TEMP"], "wj_t3")
os.makedirs(TMP, exist_ok=True)

PART = 1000
NPARTS = 4
WHOLE = b"".join(bytes([65 + i]) * PART for i in range(NPARTS))
BROKEN = {"parts": set(), "shortparts": set(), "cutparts": set(), "slowbad": set(),
          "definition": "ok"}
HITS = {}
# A file of many small parts, served under /many: enough of them that a
# download holding every part at once, or fetching on after a stop, shows.
MANY, MANY_PART = 40, 100
WHOLE_MANY = b"".join(bytes([48 + i % 40]) * MANY_PART for i in range(MANY))
STOP_AT = [None]            # the /many part whose request stands for Ctrl+C


def layout(whole, part):
    entries = []
    for i in range(len(whole) // part):
        chunk = whole[i * part:(i + 1) * part]
        entries.append({"Index": i, "Offset": i * part, "Size": part,
                        "Hash": C.wj_hash(chunk)})
    return {"Size": len(whole), "Hash": C.wj_hash(whole), "MungedName": "test.bin",
            "Parts": entries}


def definition(size=None, parts=NPARTS, whole_hash=None):
    d = layout(WHOLE[:parts * PART], PART)
    d["Size"] = len(WHOLE) if size is None else size
    d["Hash"] = C.wj_hash(WHOLE) if whole_hash is None else whole_hash
    return d


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send_body(self, body, cut=False):
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if cut:
            # The body stops short and the connection closes: IncompleteRead.
            self.wfile.write(body[:5])
            self.close_connection = True
            return
        self.wfile.write(body)

    def do_GET(self):
        HITS[self.path] = HITS.get(self.path, 0) + 1
        if self.path.startswith("/many/"):
            if self.path.endswith("/definition.json.gz"):
                return self.send_body(gzip.compress(json.dumps(layout(WHOLE_MANY, MANY_PART)).encode()))
            idx = int(self.path.rsplit("/", 1)[1])
            if idx == STOP_AT[0]:
                C.STOP.set()
            return self.send_body(WHOLE_MANY[idx * MANY_PART:(idx + 1) * MANY_PART])
        if self.path.endswith("/definition.json.gz"):
            mode = BROKEN["definition"]
            if mode == "404":
                return self.send_error(404)
            if mode == "notgzip":
                body = b"plain nonsense, not gzip and not json"
            elif mode == "badjson":
                body = gzip.compress(b'{"Size": 1, "Parts": ')
            elif mode == "noparts":
                body = gzip.compress(json.dumps(
                    {"Size": 10, "Hash": "x", "Parts": []}).encode())
            elif mode == "badwholehash":
                body = gzip.compress(json.dumps(
                    definition(whole_hash="AAAAAAAAAAA=")).encode())
            elif mode == "badlayout":
                d = definition()
                d["Parts"][1]["Offset"] = -1000      # somewhere that is not this file
                body = gzip.compress(json.dumps(d).encode())
            else:
                body = gzip.compress(json.dumps(definition()).encode())
            return self.send_body(body, cut=(mode == "cutdef" and HITS[self.path] == 1))
        if "/parts/" in self.path:
            idx = int(self.path.rsplit("/", 1)[1])
            data = WHOLE[idx * PART:(idx + 1) * PART]
            if idx in BROKEN["parts"]:
                data = b"Z" * PART          # right length, wrong bytes
            if idx in BROKEN["shortparts"]:
                data = data[:10]            # wrong length
            if idx in BROKEN["slowbad"]:
                time.sleep(2)               # slow, and wrong when it comes
                data = b"Y" * PART
            return self.send_body(data, cut=(idx in BROKEN["cutparts"] and HITS[self.path] == 1))
        self.send_error(404)


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d/file" % srv.server_port
C.PART_RETRIES = 2          # keep the test quick

print("=" * 70)
print("  T3  CDN MULTI-PART")
print("=" * 70)


def clean(name="cdn.bin"):
    p = os.path.join(TMP, name)
    for suffix in ("", ".part", ".parts.json", ".meta"):
        try:
            os.remove(p + suffix)
        except OSError:
            pass
    return p


def reset():
    BROKEN["parts"] = set()
    BROKEN["shortparts"] = set()
    BROKEN["cutparts"] = set()
    BROKEN["slowbad"] = set()
    BROKEN["definition"] = "ok"
    HITS.clear()


# ---------------------------------------------------------------- 3.1
print("\n 3.1  happy path")
reset()
p = clean()
ok, h = C.download(BASE, p, progress=False, expected_size=len(WHOLE),
                   expected_hash=C.wj_hash(WHOLE), detail=True)
check("returns True", ok is True)
check("file assembled byte-exact", io.open(p, "rb").read() == WHOLE)
check("returns the hash it computed", h == C.wj_hash(WHOLE))
check("resume sidecar cleaned up", not os.path.exists(p + ".parts.json"))
check("no .part left behind", not os.path.exists(p + ".part"))

# ---------------------------------------------------------------- 3.2
print("\n 3.2  a broken definition is refused, in every shape")
for mode, label in (("404", "HTTP 404"), ("notgzip", "not gzip or JSON"),
                    ("badjson", "truncated JSON"), ("noparts", "no parts listed")):
    reset()
    BROKEN["definition"] = mode
    p = clean()
    ok = C.download(BASE, p, progress=False)
    check("refuses definition: %-18s" % label, ok is False)
    check("  and writes no output file", not os.path.exists(p))

# ---------------------------------------------------------------- 3.3
print("\n 3.3  a part with the wrong BYTES is caught by its own hash")
reset()
BROKEN["parts"] = {2}
p = clean()
ok = C.download(BASE, p, progress=False)
check("download fails", ok is False)
check("the bad part was retried before giving up",
      HITS.get("/file/parts/2", 0) > 1, "attempts=%s" % HITS.get("/file/parts/2"))
check("no file published under the real name", not os.path.exists(p))
check("the .part is kept for a resume", os.path.exists(p + ".part"))
check("progress recorded in a sidecar", os.path.exists(p + ".parts.json"))

# ---------------------------------------------------------------- 3.4
print("\n 3.4  ...and it resumes from what it already had")
reset()
sidecar = json.load(io.open(p + ".parts.json", encoding="utf-8"))
kept = len(sidecar.get("done", []))
check("sidecar remembers the good parts (%d of %d)" % (kept, NPARTS),
      0 < kept < NPARTS)
ok, h = C.download(BASE, p, progress=False, detail=True)
check("second run completes", ok is True)
check("assembled file is byte-exact", io.open(p, "rb").read() == WHOLE)
check("only the missing parts were refetched",
      HITS.get("/file/parts/2", 0) >= 1 and
      sum(v for k, v in HITS.items() if "/parts/" in k) < NPARTS + 2,
      "fetches=%s" % {k: v for k, v in HITS.items() if "/parts/" in k})

# ---------------------------------------------------------------- 3.5
print("\n 3.5  a part with the wrong LENGTH is caught too")
reset()
BROKEN["shortparts"] = {1}
p = clean()
ok = C.download(BASE, p, progress=False)
check("download fails", ok is False)
check("no file published", not os.path.exists(p))

# ---------------------------------------------------------------- 3.6
print("\n 3.6  a whole-file hash mismatch discards everything")
reset()
BROKEN["definition"] = "badwholehash"
p = clean()
ok = C.download(BASE, p, progress=False)
check("download fails on the assembled hash", ok is False)
check("the .part is discarded (not resumable - the bytes are suspect)",
      not os.path.exists(p + ".part"))
check("the resume sidecar is cleared too",
      not os.path.exists(p + ".parts.json"))
check("nothing published under the real name", not os.path.exists(p))

# ---------------------------------------------------------------- 3.7
print("\n 3.7  a sidecar from a different file must be ignored")
reset()
p = clean()
io.open(p + ".part", "wb").write(b"?" * len(WHOLE))
json.dump({"url": "http://somewhere.else/other", "size": len(WHOLE),
           "done": [0, 1, 2, 3]},
          io.open(p + ".parts.json", "w", encoding="utf-8"))
ok = C.download(BASE, p, progress=False)
check("download still succeeds", ok is True)
check("every part was refetched, not trusted",
      sum(v for k, v in HITS.items() if "/parts/" in k) == NPARTS,
      "fetches=%s" % sum(v for k, v in HITS.items() if "/parts/" in k))
check("file is byte-exact", io.open(p, "rb").read() == WHOLE)

# ---------------------------------------------------------------- 3.8
print("\n 3.8  a sidecar claiming more than the .part actually holds")
reset()
p = clean()
io.open(p + ".part", "wb").write(b"A" * 500)       # far too short
json.dump({"url": BASE, "size": len(WHOLE), "done": [0, 1, 2, 3]},
          io.open(p + ".parts.json", "w", encoding="utf-8"))
ok = C.download(BASE, p, progress=False)
check("the impossible resume claim is rejected", ok is True)
check("all parts refetched",
      sum(v for k, v in HITS.items() if "/parts/" in k) == NPARTS,
      "fetches=%s" % sum(v for k, v in HITS.items() if "/parts/" in k))
check("file is byte-exact", io.open(p, "rb").read() == WHOLE)

# ---------------------------------------------------------------- 3.9
print("\n 3.9  detail= keeps its contract on every exit")
reset()
BROKEN["definition"] = "404"
p = clean()
r = C.download(BASE, p, progress=False, detail=True)
check("failure with detail=True is a 2-tuple",
      isinstance(r, tuple) and len(r) == 2 and r[0] is False, str(r))
r = C.download(BASE, p, progress=False)
check("failure with detail=False is a bare bool", r is False, str(r))

# ---------------------------------------------------------------- 3.10
print("\n 3.10  strict_hash refuses a replaced build before fetching a part")
reset()
p = clean()
ok = C.download(BASE, p, progress=False, expected_hash="AAAAAAAAAAA=",
                strict_hash=True)
check("a definition whose hash differs from the expected one is refused",
      ok is False)
check("  before any part was requested",
      not any("/parts/" in k for k in HITS), str(HITS))
check("  and nothing was written", not os.path.exists(p) and
      not os.path.exists(p + ".part"))
ok = C.download(BASE, p, progress=False, expected_hash="AAAAAAAAAAA=")
check("without strict_hash the same case is a warning and a download",
      ok is True and os.path.exists(p))
reset()
p = clean()
r = C.download(BASE, p, progress=False, expected_hash="AAAAAAAAAAA=",
               strict_hash=True, detail=True)
check("the refusal keeps the detail= contract", r == (False, None), str(r))

# ---------------------------------------------------------------- 3.11
print("\n 3.11  a CDN archive is verified against the MANIFEST, not just the CDN")
import wj_downloader as D
D.LOG.setLevel(logging.CRITICAL)
reset()
p = clean("arch.bin")
archive = {"name": "arch.bin", "size": len(WHOLE), "hash": "AAAAAAAAAAA=",
           "meta_text": "[General]\n", "_cdn": True}
results = {"done": 0, "failed": 0, "failures": []}
ok = D.download_one(BASE, archive, TMP, results)
check("a build whose hash differs from the manifest's is refused", ok is False)
check("  counted as a failure", results["failed"] == 1 and results["done"] == 0,
      str(results))
check("  and no file or sidecar is left in the pool",
      not os.path.exists(p) and not os.path.exists(p + ".meta"))
reset()
archive["hash"] = C.wj_hash(WHOLE)
results = {"done": 0, "failed": 0, "failures": []}
ok = D.download_one(BASE, archive, TMP, results)
check("the build the manifest pins downloads and verifies", ok is True
      and results["done"] == 1, str(results))
check("  byte-exact", io.open(p, "rb").read() == WHOLE)
check("  with its .meta written", os.path.exists(p + ".meta"))

print("\n      on its own, the module keeps the same rule for what it fetches")
# wj_cdn is usable without the rest of the suite, so it carries its own copy
# of wj_paths.web_url_problem for that case. The two must agree.
import importlib.util
import wj_paths
spec = importlib.util.spec_from_file_location("wj_cdn_alone", C.__file__)
alone = importlib.util.module_from_spec(spec)
saved = sys.modules.get("wj_paths")
sys.modules["wj_paths"] = None                  # makes `from wj_paths import` fail
try:
    spec.loader.exec_module(alone)
finally:
    sys.modules["wj_paths"] = saved
urls = ("https://x.invalid/a", "HTTP://x.invalid/a", "file:///C:/x", "ftp://x.invalid/a",
        "data:,x", "C:\\x", "", None)
check("its inline copy is in use and agrees with wj_paths on every address",
      alone.web_url_problem is not wj_paths.web_url_problem
      and [alone.web_url_problem(u) is None for u in urls]
      == [wj_paths.web_url_problem(u) is None for u in urls])


def read(p):
    try:
        return io.open(p, "rb").read()
    except OSError:
        return None


def part_fetches(prefix="/file/parts/"):
    return sum(v for k, v in HITS.items() if k.startswith(prefix))


# ---------------------------------------------------------------- 3.12
print("\n 3.12  the assembled file never replaces the one already under its name")
reset()
p = clean()
aside = os.path.join(TMP, "cdn~1.bin")
for leftover in (aside, aside + ".meta"):
    if os.path.exists(leftover):
        os.remove(leftover)
io.open(p, "wb").write(b"another list's build")
io.open(p + ".meta", "w", encoding="utf-8").write("[General]\nwhose=the other list\n")
ok = C.download(BASE, p, progress=False)
check("the download lands under its name", ok is True and read(p) == WHOLE)
check("  and the file that was there is kept beside it, with its sidecar",
      read(aside) == b"another list's build" and os.path.isfile(aside + ".meta"),
      str(sorted(n for n in os.listdir(TMP) if n.startswith("cdn"))))
os.remove(aside)
os.remove(aside + ".meta")
ok = C.download(BASE, p, progress=False)
check("the very same bytes already there are simply replaced: nothing set aside",
      ok is True and read(p) == WHOLE and not os.path.exists(aside))

# ---------------------------------------------------------------- 3.13
print("\n 3.13  an assembled file that cannot be read to hash it is not published")
reset()
p = clean()
real_hash_file = C.hash_file
C.hash_file = lambda path, chunk=1 << 20: None      # held open elsewhere; the share dropped
try:
    ok = C.download(BASE, p, progress=False)
finally:
    C.hash_file = real_hash_file
check("the download reports failure rather than an unchecked file",
      ok is False and not os.path.exists(p))
check("  and keeps what it fetched for the next try",
      os.path.exists(p + ".part") and os.path.exists(p + ".parts.json"))
ok = C.download(BASE, p, progress=False)
check("  which then finishes without fetching a part again",
      ok is True and read(p) == WHOLE and part_fetches() == NPARTS, str(HITS))

# ---------------------------------------------------------------- 3.14
print("\n 3.14  only a few parts are in hand at once, however many the file has")
# Every finished part used to stay referenced until the last arrived: the
# whole file in memory, 4.7 GB for the largest manifest here.
import concurrent.futures as cf


class CountingPool(cf.ThreadPoolExecutor):
    """Counts the parts submitted and not yet taken back."""
    lock = threading.Lock()
    held = peak = 0

    def submit(self, fn, *a, **k):
        fut = super().submit(fn, *a, **k)
        with CountingPool.lock:
            CountingPool.held += 1
            CountingPool.peak = max(CountingPool.peak, CountingPool.held)
        real = fut.result

        def result(timeout=None):
            with CountingPool.lock:
                CountingPool.held -= 1
            return real(timeout)
        fut.result = result
        return fut


reset()
p = clean("many.bin")
real_pool = cf.ThreadPoolExecutor
cf.ThreadPoolExecutor = CountingPool
try:
    ok = C.download(BASE.replace("/file", "/many"), p, progress=False, workers=2)
finally:
    cf.ThreadPoolExecutor = real_pool
check("a %d-part file assembles byte-exact" % MANY, ok is True and read(p) == WHOLE_MANY)
check("  with never more than %d parts in hand at once" % (C.WINDOW_PER_WORKER * 2),
      0 < CountingPool.peak <= C.WINDOW_PER_WORKER * 2, "peak=%d" % CountingPool.peak)

# ---------------------------------------------------------------- 3.15
print("\n 3.15  a stop ends the download within a part, and keeps what it has")
reset()
p = clean("many.bin")
STOP_AT[0] = 5
t0 = time.time()
try:
    ok = C.download(BASE.replace("/file", "/many"), p, progress=False, workers=2)
    took = time.time() - t0
    # A request already on the wire when the stop came still lands, after the
    # download has returned; it is let land before anything is counted.
    time.sleep(1.0)
finally:
    stopped = C.STOP.is_set()
    C.STOP.clear()
    STOP_AT[0] = None
first = part_fetches("/many/parts/")
kept = json.load(io.open(p + ".parts.json", encoding="utf-8")).get("done", []) \
    if os.path.exists(p + ".parts.json") else []
check("the download stops, unfinished, in moments", stopped and ok is False and took < 10,
      "%.1fs" % took)
check("  nothing queued is fetched after it", first <= 5 + 1 + C.WINDOW_PER_WORKER * 2,
      "fetched %d of %d" % (first, MANY))
check("  and what was written is recorded for the resume", 0 < len(kept) <= first,
      str(kept))
HITS.clear()
ok = C.download(BASE.replace("/file", "/many"), p, progress=False, workers=2)
check("the next run fetches only the rest, and finishes",
      ok is True and read(p) == WHOLE_MANY and part_fetches("/many/parts/") == MANY - len(kept),
      "fetched %d, had %d" % (part_fetches("/many/parts/"), len(kept)))

# ---------------------------------------------------------------- 3.16
print("\n 3.16  a disk that fills mid-download is a failure, not a crash")
import builtins


class FullDisk:
    """A file that takes `room` writes and then reports the disk full."""
    def __init__(self, f, room):
        self.f, self.room = f, room

    def write(self, data):
        if self.room <= 0:
            raise OSError(28, "There is not enough space on the disk")
        self.room -= 1
        return self.f.write(data)

    def __getattr__(self, name):
        return getattr(self.f, name)


def full_disk_open(path, mode="r", *a, **k):
    f = builtins.open(path, mode, *a, **k)
    writing = "b" in mode and ("w" in mode or "+" in mode)
    return FullDisk(f, 2) if str(path).endswith(".part") and writing else f


reset()
p = clean("many.bin")
C.open = full_disk_open
try:
    try:
        ok = C.download(BASE.replace("/file", "/many"), p, progress=False, workers=2)
        raised = None
    except Exception as e:
        ok, raised = None, e
finally:
    del C.open
kept = json.load(io.open(p + ".parts.json", encoding="utf-8")).get("done", []) \
    if os.path.exists(p + ".parts.json") else None
check("the download returns failure; nothing is raised", ok is False and raised is None,
      repr(raised))
check("  no part queued behind the failure is fetched",
      part_fetches("/many/parts/") <= 3 + C.WINDOW_PER_WORKER * 2, str(part_fetches("/many/parts/")))
check("  and the two parts written are recorded for the resume", kept is not None and len(kept) == 2,
      str(kept))
ok = C.download(BASE.replace("/file", "/many"), p, progress=False, workers=2)
check("  from which the next run finishes", ok is True and read(p) == WHOLE_MANY)

print("\n      and a part still in flight when a download fails does not retry on for nobody")
reset()
BROKEN["slowbad"] = {1}                           # 2 s to arrive, and wrong when it does
p = clean()


def no_room_open(path, mode="r", *a, **k):
    f = builtins.open(path, mode, *a, **k)
    writing = "b" in mode and ("w" in mode or "+" in mode)
    return FullDisk(f, 0) if str(path).endswith(".part") and writing else f


C.open = no_room_open                             # the first write fails: the disk is full
try:
    ok = C.download(BASE, p, progress=False, workers=2)
finally:
    del C.open
returned = HITS.get("/file/parts/1", 0)
time.sleep(6)                                     # past the slow part's retry, had it come
check("the download fails at once", ok is False)
check("  and the slow part, failing too, is not asked for again after the download has ended",
      HITS.get("/file/parts/1", 0) == returned == 1, str(HITS))

# ---------------------------------------------------------------- 3.17
print("\n 3.17  a part or a definition cut short is fetched again, not the end of the file")
reset()
BROKEN["cutparts"] = {1}
p = clean()
ok = C.download(BASE, p, progress=False)
check("a part whose body stops short is retried", ok is True and read(p) == WHOLE
      and HITS.get("/file/parts/1") == 2, str(HITS))
reset()
BROKEN["definition"] = "cutdef"
p = clean()
ok = C.download(BASE, p, progress=False)
check("so is a definition", ok is True and read(p) == WHOLE
      and HITS.get("/file/definition.json.gz") == 2, str(HITS))

# ---------------------------------------------------------------- 3.18
print("\n 3.18  a definition whose parts do not lay out as one file is refused")
check("a definition that does lay out passes", C.parts_problem(definition()) is None)
spoilers = {
    "a negative offset": lambda d: d["Parts"][1].update(Offset=-1000),
    "a fractional size": lambda d: d["Parts"][1].update(Size=999.5),
    "one index given twice": lambda d: d["Parts"][2].update(Index=1),
    "a part reaching past the end": lambda d: d["Parts"][3].update(Offset=3500),
    "a file size that is not a count": lambda d: d.update(Size="4000"),
    "true for an index": lambda d: d["Parts"][0].update(Index=True),
}
for label, spoil in spoilers.items():
    d = definition()
    spoil(d)
    check("refused: %s" % label, bool(C.parts_problem(d)), str(C.parts_problem(d)))
reset()
BROKEN["definition"] = "badlayout"
p = clean()
ok = C.download(BASE, p, progress=False)
check("such a definition from the server: no part fetched, nothing written",
      ok is False and part_fetches() == 0 and not os.path.exists(p)
      and not os.path.exists(p + ".part"), str(HITS))

# ---------------------------------------------------------------- 3.19
print("\n 3.19  a build of another size is refused before it is fetched, and nothing is lost")
reset()
p = clean()
ok = C.download(BASE, p, progress=False, expected_size=len(WHOLE) - 1, strict_hash=True)
check("strict: a definition of another size than asked for is refused before any part",
      ok is False and part_fetches() == 0 and not os.path.exists(p + ".part"), str(HITS))
reset()
p = clean("there.bin")
io.open(p, "wb").write(WHOLE)               # the very bytes the CDN serves: another list's build
results = {"done": 0, "failed": 0, "failures": []}
ok = D.download_one(BASE, {"name": "there.bin", "size": len(WHOLE) - 1, "hash": "",
                           "meta_text": "", "_cdn": True}, TMP, results)
check("a manifest pinning another size: the download fails, and the file already there stays",
      ok is False and read(p) == WHOLE and part_fetches() == 0, str((results, HITS)))

srv.shutdown()
sys.exit(summary())
