"""Download failure handling, against a local server that misbehaves on demand."""
import os
import sys

# Derive the repo root from this file so the suite runs from anywhere, rather
# than being pinned to whatever drive letter the share happens to have.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOXROOT = os.path.join(os.environ.get("TEMP", "/tmp"), "wj_tests")
import base64
import io
import os
import re
import socket
import struct
import sys
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)
from harness import check, summary
import logging
import wj_downloader as D
D.LOG.setLevel(logging.CRITICAL)

TMP = os.path.join(os.environ["TEMP"], "wj_t2")
os.makedirs(TMP, exist_ok=True)
BODY = b"A" * 5000
BIG = b"B" * ((1 << 20) + 5)          # over the pool's 1 MiB 'small' threshold
# 2.10 runs the pool with a 2 s stream timeout: /slow must go silent for
# longer than that, so it is a stall; /hold must answer within it, so it is
# merely a large file that takes a moment.
STALL = 3.5                           # how long /slow goes silent for
HOLD = 1.0                            # how long /hold takes before it answers
HITS = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        HITS[path] = HITS.get(path, 0) + 1
        if path == "/ok":
            self.send_response(200)
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY)
        elif path == "/404":
            self.send_error(404)
        elif path == "/500":
            self.send_error(500)
        elif path == "/short":
            # Claims the full length, sends half, then hangs up.
            self.send_response(200)
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY[:len(BODY) // 2])
            self.close_connection = True
        elif path == "/flaky":
            # Fails the first attempt, succeeds afterwards.
            if HITS[path] == 1:
                self.send_response(200)
                self.send_header("Content-Length", str(len(BODY)))
                self.end_headers()
                self.wfile.write(BODY[:100])
                self.close_connection = True
            else:
                start = 0
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    start = int(rng.split("=")[1].split("-")[0])
                    self.send_response(206)
                    self.send_header("Content-Range", "bytes %d-%d/%d"
                                     % (start, len(BODY) - 1, len(BODY)))
                else:
                    self.send_response(200)
                self.send_header("Content-Length", str(len(BODY) - start))
                self.end_headers()
                self.wfile.write(BODY[start:])
        elif path == "/spaces":
            self.send_response(200)
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY)
        elif path == "/slow":
            # Answers, sends a little, then goes silent: a stalled connection.
            # The retry, with its Range, gets the rest at once.
            if HITS[path] == 1:
                self.send_response(200)
                self.send_header("Content-Length", str(len(BODY)))
                self.end_headers()
                self.wfile.write(BODY[:100])
                self.wfile.flush()
                time.sleep(STALL)
                self.close_connection = True
            else:
                start = 0
                rng = self.headers.get("Range")
                if rng and rng.startswith("bytes="):
                    start = int(rng.split("=")[1].split("-")[0])
                    self.send_response(206)
                    self.send_header("Content-Range", "bytes %d-%d/%d"
                                     % (start, len(BODY) - 1, len(BODY)))
                else:
                    self.send_response(200)
                self.send_header("Content-Length", str(len(BODY) - start))
                self.end_headers()
                self.wfile.write(BODY[start:])
        elif path == "/hold":
            # A large file that takes its time to start: nothing else may
            # start beside it.
            time.sleep(HOLD)
            self.send_response(200)
            self.send_header("Content-Length", str(len(BIG)))
            self.end_headers()
            self.wfile.write(BIG)
        else:
            self.send_error(404)


srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % srv.server_port
print("=" * 70)
print("  T2  DOWNLOAD FAILURE HANDLING   (local server on %d)" % srv.server_port)
print("=" * 70)


def dest(name):
    p = os.path.join(TMP, name)
    for suffix in ("", ".part"):
        try:
            os.remove(p + suffix)
        except OSError:
            pass
    return p


# ---------------------------------------------------------------- 2.1
print("\n 2.1  a clean success")
p = dest("ok.bin")
ok = D.stream_download(BASE + "/ok", p, expected_size=len(BODY))
check("returns True", ok is True)
check("file is the right length", os.path.getsize(p) == len(BODY))
check("no .part left behind", not os.path.exists(p + ".part"))

# ---------------------------------------------------------------- 2.2
print("\n 2.2  a hard HTTP error must NOT be retried")
for code in ("404", "500"):
    p = dest("e%s.bin" % code)
    HITS.clear()
    ok = D.stream_download(BASE + "/" + code, p, expected_size=len(BODY))
    check("HTTP %s returns False" % code, ok is False)
    check("  attempted exactly once (no pointless retries)",
          HITS.get("/" + code) == 1, "attempts=%s" % HITS.get("/" + code))
    check("  no file created", not os.path.exists(p))

# ---------------------------------------------------------------- 2.3
print("\n 2.3  a transient drop IS retried, and resumes rather than restarting")
p = dest("flaky.bin")
HITS.clear()
ok = D.stream_download(BASE + "/flaky", p, expected_size=len(BODY))
check("eventually succeeds", ok is True)
check("final file is correct length",
      os.path.exists(p) and os.path.getsize(p) == len(BODY))
check("took more than one attempt", HITS.get("/flaky", 0) > 1,
      "attempts=%s" % HITS.get("/flaky"))

# ---------------------------------------------------------------- 2.4
print("\n 2.4  a truncated transfer must not be published as complete")
p = dest("short.bin")
ok = D.stream_download(BASE + "/short", p, expected_size=len(BODY))
short_on_disk = os.path.exists(p) and os.path.getsize(p) != len(BODY)
check("either fails, or leaves a file verify_file will reject",
      ok is False or short_on_disk)
good, why = D.verify_file(p, len(BODY), None) if os.path.exists(p) else (False, "absent")
check("verify_file rejects it", not good, "verify said: %s" % why)

# ---------------------------------------------------------------- 2.5
print("\n 2.5  download_one deletes what fails verification")
res = {"done": 0, "failed": 0, "failures": []}
archive = {"name": "short2.bin", "size": len(BODY), "hash": None,
           "meta_text": "[General]\ngameName=fallout4\n", "meta": {},
           "state": {"$type": "HttpDownloader"}}
dest("short2.bin")
ok = D.download_one(BASE + "/short", archive, TMP, res)
check("download_one reports failure", ok is False)
check("counted as failed", res["failed"] == 1 and res["done"] == 0)
check("the bad file is gone, not left in the pool",
      not os.path.exists(os.path.join(TMP, "short2.bin")))
check("no .meta sidecar written for it",
      not os.path.exists(os.path.join(TMP, "short2.bin.meta")))
check("failure reason recorded", bool(res["failures"]))

# ---------------------------------------------------------------- 2.6
print("\n 2.6  a good download writes its .meta sidecar")
res = {"done": 0, "failed": 0, "failures": []}
archive2 = dict(archive, name="good.bin")
dest("good.bin")
ok = D.download_one(BASE + "/ok", archive2, TMP, res)
check("download_one reports success", ok is True)
check("counted as done", res["done"] == 1 and res["failed"] == 0)
sidecar = os.path.join(TMP, "good.bin.meta")
check(".meta written beside it", os.path.exists(sidecar))
check(".meta holds the manifest's Meta block",
      "gameName=fallout4" in io.open(sidecar, encoding="utf-8").read())

# ---------------------------------------------------------------- 2.7
print("\n 2.7  hash verification actually rejects the wrong bytes")
p = dest("hash.bin")
D.stream_download(BASE + "/ok", p, expected_size=len(BODY))
real = D.wabbajack_hash(p)
check("xxhash available in this interpreter", D._HAVE_XXHASH)
check("correct hash verifies", D.verify_file(p, len(BODY), real)[0])
wrong = base64.b64encode(struct.pack("<Q", 12345)).decode()
good, why = D.verify_file(p, len(BODY), wrong)
check("wrong hash is rejected", not good, why)
check("  and the reason names the hash", "hash" in why.lower())

# ---------------------------------------------------------------- 2.8
print("\n 2.8  URLs with raw spaces are encoded, not rejected")
p = dest("sp.bin")
ok = D.stream_download(BASE + "/spaces?name=a file with spaces.7z", p,
                       expected_size=len(BODY))
check("a URL containing spaces downloads", ok is True)
check("sanitize_url encodes the space",
      "%20" in D.sanitize_url("http://x/a b.7z"))
check("sanitize_url does not double-encode",
      D.sanitize_url("http://x/a%20b.7z").count("%") == 1)

# ---------------------------------------------------------------- 2.9
print("\n 2.9  a stale .part from an interrupted run is resumed, not restarted")
p = dest("resume.bin")
io.open(p + ".part", "wb").write(BODY[:1000])       # pretend we got 1000 bytes
HITS.clear()
ok = D.stream_download(BASE + "/flaky", p, expected_size=len(BODY))
check("resumes to a complete file", ok is True and
      os.path.getsize(p) == len(BODY))
check("file contents are correct after resume",
      io.open(p, "rb").read() == BODY)

# ---------------------------------------------------------------- 2.10
print("\n 2.10  several small files at once: a stalled slot holds up nothing else")
events = []
real_one = D.download_one


def spy(url, a, dest_dir, results, quiet=False):
    events.append(("start", a["name"], time.time()))
    ok = real_one(url, a, dest_dir, results, quiet=quiet)
    events.append(("end", a["name"], time.time()))
    return ok


def when(kind, name):
    return next(t for k, n, t in events if k == kind and n == name)


def item(name, path, size):
    return ("http", {"name": name, "size": size, "hash": None, "meta_text": "",
                     "meta": {}, "state": {"Url": BASE + path}})


check("a file of unknown size counts as large", not D.is_small_download({"size": None}, 100))
check("  one at the threshold counts as small, one over it does not",
      D.is_small_download({"size": 100}, 100) and not D.is_small_download({"size": 101}, 100))

saved_pool = (D.PARALLEL_SMALL, D.SMALL_BYTES, D.STREAM_TIMEOUT)
D.download_one = spy
D.PARALLEL_SMALL, D.SMALL_BYTES, D.STREAM_TIMEOUT = 3, 1 << 20, 2
for n in ("s1.bin", "s2.bin", "s3.bin", "big.bin", "after.bin"):
    dest(n)
HITS.clear()
results = {"done": 0, "failed": 0, "failures": []}
items = [item("s1.bin", "/slow", len(BODY)), item("s2.bin", "/ok", len(BODY)),
         item("s3.bin", "/ok", len(BODY)), item("big.bin", "/hold", len(BIG)),
         item("after.bin", "/ok", len(BODY))]
try:
    D.download_direct(items, TMP, results)
finally:
    D.PARALLEL_SMALL, D.SMALL_BYTES, D.STREAM_TIMEOUT = saved_pool
check("every file landed", results["done"] == 5 and results["failed"] == 0, str(results))
check("  with the right bytes",
      all(os.path.getsize(os.path.join(TMP, n)) == len(BODY) for n in ("s1.bin", "s2.bin", "s3.bin", "after.bin"))
      and os.path.getsize(os.path.join(TMP, "big.bin")) == len(BIG))
check("the stalled file was retried and resumed", HITS.get("/slow", 0) >= 2, str(HITS))
check("  and the two beside it finished long before it did",
      when("end", "s2.bin") < when("end", "s1.bin") and when("end", "s3.bin") < when("end", "s1.bin"))
check("the small files ran together", when("start", "s3.bin") < when("end", "s1.bin"))
check("the large file started as soon as a slot freed, while the stall was still going",
      when("start", "big.bin") < when("end", "s1.bin"))
check("  and nothing new started beside it",
      when("start", "after.bin") >= when("end", "big.bin"))

print("\n      at the default of 1 the pool is not used")
events.clear()
HITS.clear()
for n in ("q1.bin", "q2.bin"):
    dest(n)
D.PARALLEL_SMALL = 1
results = {"done": 0, "failed": 0, "failures": []}
try:
    D.download_direct([item("q1.bin", "/ok", len(BODY)), item("q2.bin", "/ok", len(BODY))], TMP, results)
finally:
    D.PARALLEL_SMALL = saved_pool[0]
check("two files, one after the other",
      results["done"] == 2 and when("start", "q2.bin") >= when("end", "q1.bin"))

# ---------------------------------------------------------------- 2.11
print("\n 2.11  assist mode with click-ahead: the next page opens while the file downloads")


class FakeNexus:
    daily_remaining = None
    gone = {}          # (mod_id, file_id) the author has taken down -> replacement id, or None

    def mod_files(self, domain, mod_id):
        ids = []
        for a in nx_items:
            m, f = D.nexus_ids(a)
            if m != mod_id:
                continue
            if (m, f) in self.gone:
                if self.gone[(m, f)]:
                    ids.append(self.gone[(m, f)])
            else:
                ids.append(f)
        return [{"file_id": i} for i in ids], "ok"

    def recover_file_id(self, domain, mod_id, archive):
        return self.gone.get(D.nexus_ids(archive))

    def resolve_link(self, a, key=None, expires=None, forced_domain=None,
                     allow_fileid_fallback=True):
        return a["state"]["Url"], None


def nx_item(name, path, mod_id, size):
    return {"name": name, "size": size, "hash": None, "meta_text": "",
            "meta": {"gamename": "skyrimspecialedition", "modid": str(mod_id),
                     "fileid": str(mod_id * 10)},
            "state": {"Url": BASE + path}}


opened = []
pages = []
nx_items = []


def fake_open(page):
    """The browser: a moment later, 'you' click, and the handler forwards the
    nxm link to the listener exactly as the real one does - for the file the
    page is for."""
    opened.append(time.time())
    pages.append(page)
    m = re.search(r"mods/(\d+)\?tab=files&file_id=(\d+)", page)
    mod, fid = int(m.group(1)), int(m.group(2))

    def click():
        time.sleep(0.2)
        port = int(io.open(os.path.join(TMP, D.NXM_PORT_FILE)).read())
        with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
            s.sendall(f"nxm://skyrimspecialedition/mods/{mod}/files/{fid}"
                      f"?key=k&expires=1\n".encode())
    threading.Thread(target=click, daemon=True).start()
    return True


saved_assist = (D.webbrowser, D.register_nxm, D.unregister_nxm, D.resolve_browser_domain,
                D.CLICK_AHEAD, D.NXM_CLICK_TIMEOUT, D.ASSIST_WINDOW, D.QUEUE_DEPTH)
D.webbrowser = types.SimpleNamespace(open=fake_open)
D.register_nxm = lambda sd: True                 # the registry is not touched by a test
D.unregister_nxm = lambda sd: None
D.resolve_browser_domain = lambda nexus, a, learned: ("skyrimspecialedition", None)
D.NXM_CLICK_TIMEOUT = 10
D.ASSIST_WINDOW = False                          # the browser path: a test opens no window
args = types.SimpleNamespace(limit=0)


def run_assist(ahead):
    events.clear()
    opened.clear()
    HITS.clear()
    for n in ("nx1.bin", "nx2.bin"):
        dest(n)
    nx_items[:] = [nx_item("nx1.bin", "/hold", 1, len(BIG)), nx_item("nx2.bin", "/ok", 2, len(BODY))]
    D.CLICK_AHEAD = ahead
    res = {"done": 0, "failed": 0, "failures": []}
    D.download_nexus_assist(nx_items, FakeNexus(), TMP, res, TMP, args)
    return res


try:
    res = run_assist(True)
    check("both files landed through the capture flow", res["done"] == 2 and res["failed"] == 0, str(res))
    check("  the second page opened while the first file was still downloading",
          len(opened) == 2 and opened[1] < when("end", "nx1.bin"),
          str((len(opened), opened[1:] and opened[1] - when("end", "nx1.bin"))))
    check("  and its download waited for the first to finish: still one at a time",
          when("start", "nx2.bin") >= when("end", "nx1.bin"))
    check("  the listener's port file was cleaned up",
          not os.path.exists(os.path.join(TMP, D.NXM_PORT_FILE)))
    res = run_assist(False)
    check("with click-ahead off, the second page waits for the first download",
          res["done"] == 2 and opened[1] >= when("end", "nx1.bin"))

    print("\n      the queue can hold more than one clicked file (ASSIST_QUEUE_DEPTH)")
    for depth in (1, 2):
        events.clear()
        opened.clear()
        pages.clear()
        for n in ("nx1.bin", "nx2.bin", "nx3.bin"):
            dest(n)
        nx_items[:] = [nx_item("nx1.bin", "/hold", 1, len(BIG)), nx_item("nx2.bin", "/ok", 2, len(BODY)),
                       nx_item("nx3.bin", "/ok", 3, len(BODY))]
        D.CLICK_AHEAD, D.QUEUE_DEPTH = True, depth
        res = {"done": 0, "failed": 0, "failures": []}
        D.download_nexus_assist(nx_items, FakeNexus(), TMP, res, TMP, args)
        third_early = len(opened) == 3 and opened[2] < when("end", "nx1.bin")
        if depth == 1:
            check("depth 1: the third page waits for the first download to end, one file already waiting",
                  res["done"] == 3 and not third_early, str((res, len(opened))))
        else:
            check("depth 2: the third page opens while the first file still downloads, two waiting behind it",
                  res["done"] == 3 and third_early, str((res, len(opened))))
            check("  and the downloads still ran one after another",
                  when("start", "nx2.bin") >= when("end", "nx1.bin")
                  and when("start", "nx3.bin") >= when("end", "nx2.bin"))
    D.QUEUE_DEPTH = 1

    print("\n      a download URL that aged out in the queue is minted afresh from the click's key")

    class AgingNexus(FakeNexus):
        """Mints a URL already past its time the first time, a good one after."""
        def __init__(self):
            self.minted = []

        def resolve_link(self, a, key=None, expires=None, forced_domain=None,
                         allow_fileid_fallback=True):
            self.minted.append((D.nexus_ids(a), key))
            stale = len(self.minted) == 1
            return a["state"]["Url"] + ("?expires=1" if stale else ""), None

    events.clear()
    opened.clear()
    pages.clear()
    dest("nx1.bin")
    nx_items[:] = [nx_item("nx1.bin", "/ok", 1, len(BODY))]
    aging = AgingNexus()
    D.CLICK_AHEAD = True
    res = {"done": 0, "failed": 0, "failures": []}
    D.download_nexus_assist(nx_items, aging, TMP, res, TMP, args)
    check("the stale URL was seen before the download and a fresh one minted with the click's key",
          aging.minted == [((1, 10), "k"), ((1, 10), "k")] and res["done"] == 1 and res["failed"] == 0,
          str((aging.minted, res)))
    D.CLICK_AHEAD = False

    print("\n      the pages can be shown by a window of our own, and closing it ends the run")

    class FakeWindow:
        """Shows pages like the window would, and 'closes' after the first."""
        def __init__(self):
            self.closed = threading.Event()
            self.shown = []

        def show(self, url, banner=""):
            self.shown.append((url, banner))
            fake_open(url)                       # 'you' click this one
            threading.Timer(0.4, self.closed.set).start()   # then shut the window
            return True

    events.clear()
    opened.clear()
    for n in ("nx1.bin", "nx2.bin"):
        dest(n)
    # The first file takes a second to arrive, so the window is shut while it
    # is still downloading and the loop must notice before the second page.
    nx_items[:] = [nx_item("nx1.bin", "/hold", 1, len(BIG)), nx_item("nx2.bin", "/ok", 2, len(BODY))]
    D.CLICK_AHEAD = False
    fw = FakeWindow()
    res = {"done": 0, "failed": 0, "failures": []}
    t0 = time.time()
    D.download_nexus_assist(nx_items, FakeNexus(), TMP, res, TMP, args, opener=fw)
    check("the page went to the window, with a banner naming the file and its place",
          len(fw.shown) == 1 and "file 1 of 2" in fw.shown[0][1] and "nx1.bin" in fw.shown[0][1],
          str(fw.shown))
    check("  the clicked file landed", res["done"] == 1, str(res))
    check("  closing the window stopped the run before the second page, within seconds",
          len(fw.shown) == 1 and time.time() - t0 < 8, f"{time.time() - t0:.1f}s")
    check("  and the listener was cleaned up", not os.path.exists(os.path.join(TMP, D.NXM_PORT_FILE)))

    print("\n      a mod Nexus says is gone is passed over at once, not waited out")
    GONE = "Nexus lists mod 2 as removed on skyrimspecialedition; there is no page to click"
    D.resolve_browser_domain = lambda nexus, a, learned: (
        (None, GONE) if a["name"] == "nx2.bin" else ("skyrimspecialedition", None))
    events.clear()
    opened.clear()
    for n in ("nx1.bin", "nx2.bin"):
        dest(n)
    nx_items[:] = [nx_item("nx1.bin", "/ok", 1, len(BODY)), nx_item("nx2.bin", "/ok", 2, len(BODY))]
    D.CLICK_AHEAD = False
    res = {"done": 0, "failed": 0, "failures": []}
    t0 = time.time()
    D.download_nexus_assist(nx_items, FakeNexus(), TMP, res, TMP, args)
    check("the file that is there landed; the gone one is a recorded failure carrying Nexus's verdict",
          res["done"] == 1 and res["failed"] == 1 and ("nx2.bin", GONE) in res["failures"], str(res))
    check("  no page was opened for it, and no click was waited for",
          len(opened) == 1 and time.time() - t0 < 8, f"{len(opened)} page(s), {time.time() - t0:.1f}s")
    D.resolve_browser_domain = lambda nexus, a, learned: ("skyrimspecialedition", None)

    print("\n      a pinned file the author has since replaced: its replacement's page, or nothing to click")
    events.clear()
    opened.clear()
    pages.clear()
    for n in ("nx1.bin", "nx2.bin"):
        dest(n)
    nx_items[:] = [nx_item("nx1.bin", "/ok", 1, len(BODY)), nx_item("nx2.bin", "/ok", 2, len(BODY))]
    fn = FakeNexus()
    fn.gone = {(1, 10): 77, (2, 20): None}      # nx1 has a same-name replacement, nx2 has nothing
    D.CLICK_AHEAD = False
    res = {"done": 0, "failed": 0, "failures": []}
    t0 = time.time()
    D.download_nexus_assist(nx_items, fn, TMP, res, TMP, args)
    check("the replaced file: its replacement's page was opened, and the click there landed the file",
          len(pages) == 1 and "mods/1?tab=files&file_id=77" in pages[0] and res["done"] == 1,
          str((pages, res)))
    check("  the file with no replacement was recorded with the reason, and not waited for",
          res["failed"] == 1 and any("no longer offers file 20 of mod 2" in (r or "") for _n, r in res["failures"])
          and time.time() - t0 < 8, str((res, f"{time.time() - t0:.1f}s")))

    print("\n      the wait for a click ends the moment the window is closed")
    lst = D.NxmListener(TMP)
    stop = threading.Event()
    threading.Timer(0.5, stop.set).start()
    t0 = time.time()
    try:
        got = D._wait_click(lst, 30, stop)
    finally:
        lst.close()
    check("no click and a closed window: None, promptly, not after the timeout",
          got is None and time.time() - t0 < 5, f"{time.time() - t0:.1f}s")

    print("\n      the window's Skip button ends the wait without a click")
    skip = threading.Event()
    threading.Timer(0.3, skip.set).start()
    lst = D.NxmListener(TMP)
    t0 = time.time()
    try:
        got = D._wait_click(lst, 30, None, skip)
    finally:
        lst.close()
    check("the skip flag: the SKIPPED verdict, promptly, and the flag spent for the next page",
          got is D.SKIPPED and time.time() - t0 < 5 and not skip.is_set(), f"{time.time() - t0:.1f}s")
    lst = D.NxmListener(TMP)
    try:
        threading.Timer(0.2, lambda: D.forward_nxm("nxm://wabbajackwizard/skip", TMP)).start()
        got = D._wait_click(lst, 30, None, None)
    finally:
        lst.close()
    check("  the same button by way of the protocol handler is the same verdict", got is D.SKIPPED, repr(got))

    import wj_assistwin as AW
    caught = []
    w = AW.AssistWindow(on_link=caught.append)

    class EngineArgs:
        def __init__(self, uri):
            self.Uri = uri
            self.Cancel = False

    ev = EngineArgs(AW.SKIP_URI)
    took = w._take(ev)
    check("in the window, the Skip link is cancelled in the engine and raises the flag, and is not a click",
          took and ev.Cancel and w.skip.is_set() and not caught)
    ev = EngineArgs("nxm://skyrimspecialedition/mods/1/files/2?key=k&expires=1")
    w._take(ev)
    check("  a real nxm link is cancelled in the engine and handed to the run",
          ev.Cancel and caught == ["nxm://skyrimspecialedition/mods/1/files/2?key=k&expires=1"], str(caught))
    ev = EngineArgs("https://www.nexusmods.com/skyrimspecialedition/mods/1")
    check("  an ordinary page is neither", not w._take(ev) and not ev.Cancel)
    check("  the banner offers the button, under the host the downloader knows",
          "Skip this file" in AW.BANNER_JS and AW.SKIP_URI in AW.BANNER_JS
          and AW.SKIP_URI.startswith(D.NXM_SKIP_PREFIX))
    check("  and the banner text still formats in", "= \"file 3 of 9\";" in (AW.BANNER_JS % '"file 3 of 9"'))

    print("\n      how long a click's key and its minted URL stay good is read off the links")
    now = 1_700_000_000
    key_left, url_left = D._link_windows(
        {"expires": str(now + 300)},
        f"https://cf-files.nexusmods.com/cdn/1704/x.7z?md5=abc&expires={now + 3600}&user_id=1&rip=2", now)
    check("five minutes on the key, an hour on the URL", key_left == 300 and url_left == 3600, str((key_left, url_left)))
    check("  missing or odd timestamps read as unknown, not as an error",
          D._link_windows({"expires": None}, "https://x/y?expires=soon", now) == (None, None)
          and D._link_windows({}, None, now) == (None, None) and D._minutes(None) == "unknown"
          and D._minutes(90) == "1.5 min")
finally:
    (D.webbrowser, D.register_nxm, D.unregister_nxm, D.resolve_browser_domain,
     D.CLICK_AHEAD, D.NXM_CLICK_TIMEOUT, D.ASSIST_WINDOW, D.QUEUE_DEPTH) = saved_assist
    D.download_one = real_one

# ---------------------------------------------------------------- 2.12
print("\n 2.12  a progress line that cannot be written must not fail the download")


class BrokenPipe:
    """stdout after the window that was reading it has gone."""
    def write(self, s):
        raise OSError(22, "Invalid argument")

    def flush(self):
        raise OSError(22, "Invalid argument")


real_stdout, D._PROGRESS_BROKEN = sys.stdout, False
sys.stdout = BrokenPipe()
try:
    p = dest("nostdout.bin")
    ok = D.stream_download(BASE + "/ok", p, expected_size=len(BODY))
finally:
    sys.stdout = real_stdout
check("the download completes although every progress write raises",
      ok is True and os.path.getsize(p) == len(BODY))
check("  and progress output is switched off for the rest of the run", D._PROGRESS_BROKEN is True)
D._PROGRESS_BROKEN = False

# ---------------------------------------------------------------- 2.13
print("\n 2.13  which program Windows is told to run for an nxm:// click")
fake_dir = os.path.join(TMP, "handler-home")
os.makedirs(fake_dir, exist_ok=True)
for leftover in os.listdir(fake_dir):                 # from an earlier run
    os.remove(os.path.join(fake_dir, leftover))
saved_paths = (D.wj_paths.frozen, D.wj_paths.app_dir)
D.wj_paths.frozen = lambda: False
D.wj_paths.app_dir = lambda f: fake_dir
try:
    cmd = D.nxm_handler_command()
    check("from source with no build beside the scripts, the interpreter runs the script",
          sys.executable in cmd and "wj_downloader" in cmd and '--nxm "%1"' in cmd, cmd)
    exe = os.path.join(fake_dir, D.wj_paths.CONSOLE_EXE)
    io.open(exe, "wb").write(b"MZ")
    cmd = D.nxm_handler_command()
    check("with a build beside the scripts, the console exe is the handler, as the build itself uses",
          cmd.startswith('"' + exe + '"') and D.wj_paths.STAGE_FLAG + "wj_downloader" in cmd
          and '--nxm "%1"' in cmd, cmd)
    check("  and never the interpreter: Firefox refuses a python.exe handler", "python" not in cmd.lower())
finally:
    D.wj_paths.frozen, D.wj_paths.app_dir = saved_paths

# ---------------------------------------------------------------- 2.14
print("\n 2.14  which game's page to open for a Nexus mod, and when there is none to click")


class ScriptedNexus:
    """A Nexus whose answers are written down:
    {(domain, mod_id): (files status, [file ids], mod status or None)}."""
    def __init__(self, table):
        self.table = table
        self.calls = []

    def mod_files(self, domain, mod_id):
        self.calls.append(("files", domain, mod_id))
        status, ids, _ = self.table.get((domain, mod_id), ("notfound", [], None))
        return ([{"file_id": i} for i in ids] if status == "ok" else None), status

    def mod_info(self, domain, mod_id):
        self.calls.append(("info", domain, mod_id))
        _, _, state = self.table.get((domain, mod_id), (None, None, None))
        return ({"status": state}, "ok") if state else (None, "notfound")


def se_item(mod_id, file_id, game="skyrimse"):
    return {"name": f"mod{mod_id}", "meta": {"gamename": game, "modid": str(mod_id),
                                            "fileid": str(file_id)}, "state": {}}


saved_pause = D.REQUEST_PAUSE_S
D.REQUEST_PAUSE_S = 0
try:
    # The case from the Alpyne run: a Special Edition mod removed from Nexus,
    # while old Skyrim serves an unrelated mod under the same id.
    nx = ScriptedNexus({("skyrimspecialedition", 120897): ("error:HTTP Error 403: Forbidden", [], "removed"),
                        ("skyrim", 120897): ("ok", [1000380757], "published")})
    learned = {}
    dom, note = D.resolve_browser_domain(nx, se_item(120897, 539400), learned)
    check("a mod removed from its own game: nothing to click, and old Skyrim's stranger is not taken for it",
          dom is None and "removed" in (note or "") and "120897" in note, str((dom, note)))
    check("  and no domain was learned from it", not learned, str(learned))
    check("  the verdict came from the mod's own game", ("info", "skyrimspecialedition", 120897) in nx.calls,
          str(nx.calls))

    nx = ScriptedNexus({("skyrimspecialedition", 5): ("ok", [50], "published"),
                        ("skyrim", 5): ("ok", [7], "published")})
    learned = {"skyrimse": "skyrim"}             # what the old code would have remembered
    dom, note = D.resolve_browser_domain(nx, se_item(5, 50), learned)
    check("a remembered other game is not believed on the mod id alone: the file must be listed there",
          dom == "skyrimspecialedition", str((dom, note, nx.calls)))
    check("  and the memory is corrected", learned.get("skyrimse") == "skyrimspecialedition", str(learned))

    en = se_item(9, 90, game="enderalspecialedition")
    nx = ScriptedNexus({("skyrimspecialedition", 9): ("ok", [90], "published")})
    learned = {}
    dom, note = D.resolve_browser_domain(nx, en, learned)
    check("a file really hosted on the parent game's page: that game, with a note, and remembered",
          dom == "skyrimspecialedition" and "the site uses" in (note or "")
          and learned.get("enderalspecialedition") == "skyrimspecialedition", str((dom, note, learned)))
    dom, note = D.resolve_browser_domain(nx, se_item(9, 90, game="enderalspecialedition"), learned)
    check("  and the second time without the note", dom == "skyrimspecialedition" and note is None, str(note))

    nx = ScriptedNexus({("skyrimspecialedition", 5): ("ok", [50], "published")})
    D.resolve_browser_domain(nx, se_item(5, 50), {})
    check("the usual mod costs one API call", nx.calls == [("files", "skyrimspecialedition", 5)], str(nx.calls))

    nx = ScriptedNexus({("skyrimspecialedition", 6): ("error:HTTP Error 403: Forbidden", [], "hidden")})
    dom, note = D.resolve_browser_domain(nx, se_item(6, 60), {})
    check("a hidden mod is nothing to click either", dom is None and "hidden" in (note or ""), str((dom, note)))
    nx = ScriptedNexus({("skyrimspecialedition", 6): ("notfound", [], "not_published")})
    dom, note = D.resolve_browser_domain(nx, se_item(6, 60), {})
    check("  so is an unpublished one, said in words", dom is None and "not published" in (note or ""),
          str((dom, note)))

    nx = ScriptedNexus({("skyrim", 8): ("ok", [3], "published")})
    dom, note = D.resolve_browser_domain(nx, se_item(8, 80), {})
    check("no verdict from its own game and another serving only the id: that page, with the doubt said",
          dom == "skyrim" and "does not list file 80" in (note or ""), str((dom, note)))
    nx = ScriptedNexus({})
    dom, note = D.resolve_browser_domain(nx, se_item(7, 70), {})
    check("a mod no game serves: the manifest's own page, with the 404 warning",
          dom == "skyrimspecialedition" and "404" in (note or ""), str((dom, note)))
    nx = ScriptedNexus({})
    dom, note = D.resolve_browser_domain(nx, {"name": "x", "meta": {"gamename": "skyrimse"}, "state": {}}, {})
    check("no mod id: the manifest's game, and no API call", dom == "skyrimspecialedition" and not nx.calls,
          str((dom, nx.calls)))
finally:
    D.REQUEST_PAUSE_S = saved_pause

# ---------------------------------------------------------------- 2.15
print("\n 2.15  an archive name with a path in it never becomes a path")
# Reproduced 2026-09-25 against the code as it stood: a manifest naming an
# archive '..\\..\\escaped.txt' had its download written two folders above
# Master, and it passed verification, since the manifest brings the hash too.
import json
import shutil
import zipfile
from harness import Sandbox

EVIL = ["..\\..\\escaped.bin", "C:\\x.bin", "sub/dir.bin", "trail.bin."]
wjp = os.path.join(TMP, "Evil.wabbajack")
with zipfile.ZipFile(wjp, "w") as z:
    z.writestr("modlist", json.dumps({"GameType": "Fallout4", "Archives": [
        {"Name": n, "Size": len(BODY), "Hash": "", "Meta": "",
         "State": {"$type": "HttpDownloader, Wabbajack.Lib", "Url": BASE + "/ok"}}
        for n in ["fine.bin"] + EVIL]}))
rec = D.parse_manifest(wjp)
check("parse_manifest keeps the plain name", [a["name"] for a in rec["archives"]] == ["fine.bin"],
      str(rec["archives"]))
check("  and names every other one as unsafe", sorted(rec["unsafe"]) == sorted(EVIL), str(rec["unsafe"]))

res = {"done": 0, "failed": 0, "failures": []}
inner = os.path.join(TMP, "pool", "Fallout4", "Master")
os.makedirs(inner, exist_ok=True)
ok = D.download_one(BASE + "/ok", dict(archive, name="..\\..\\escaped.bin"), inner, res)
check("download_one refuses such a name handed to it directly",
      ok is False and res["failed"] == 1
      and not os.path.exists(os.path.join(TMP, "pool", "escaped.bin")), str(res))

cache_path = os.path.join(TMP, "light_cache.json")
if os.path.exists(cache_path):
    os.remove(cache_path)
light = D.load_light_records(TMP, cache_path, files=["Evil.wabbajack"])
entry = light["Evil.wabbajack"]
check("the light cache leaves them out and counts them",
      entry["unsafe"] == 4 and [a["name"] for a in entry["archives"]] == ["fine.bin"], str(entry))
with io.open(cache_path, encoding="utf-8") as f:
    cached = json.load(f)
cached["Evil.wabbajack"]["archives"].append({"name": "..\\old.bin", "size": 1, "kind": "http"})
cached["Evil.wabbajack"]["unsafe"] = 0          # as a version before this one cached it
with io.open(cache_path, "w", encoding="utf-8") as f:
    json.dump(cached, f)
entry = D.load_light_records(TMP, cache_path, files=["Evil.wabbajack"])["Evil.wabbajack"]
check("  and an entry cached before is held to the same rule as it is read",
      entry["unsafe"] == 1 and [a["name"] for a in entry["archives"]] == ["fine.bin"], str(entry))

box = Sandbox(os.path.join(BOXROOT, "sb2"))
shutil.copy2(wjp, os.path.join(box.manifests, "Evil.wabbajack"))
rc, out = box.run("wj_downloader.py", ["--game", "Fallout4", "--list", "Evil", "--yes",
                                       "--skip-nexus"])
check("the downloader refuses the whole list", rc == 2 and "not plain file names" in out, out[-600:])
landed = [os.path.join(d, n) for d, _s, fs in os.walk(box.root) for n in fs
          if n.lower().endswith(".bin") or n.lower().startswith("escaped")]
check("  and writes nothing, inside the pool or out of it", landed == [], str(landed))
shutil.rmtree(box.root, ignore_errors=True)

# ---------------------------------------------------------------- 2.16
print("\n 2.16  time and clicks: what a list costs, and what is left of a run")
MB = 1 << 20
check("every source falls in one class",
      [D.source_class(k) for k in ("nexus", "http", "wabbajackcdn", "mediafire", "manual",
                                   "gamefile", "mega", "googledrive", "unknown")]
      == ["nexus", "direct", "direct", "direct", "manual", "gamefile", "other", "other", "other"])
arcs = [{"name": "n1", "size": 30 * MB, "kind": "nexus"}, {"name": "n2", "size": 10 * MB, "kind": "nexus"},
        {"name": "d1", "size": 100 * MB, "kind": "wabbajackcdn"}, {"name": "m1", "size": 5, "kind": "manual"},
        {"name": "g1", "size": 9, "kind": "gamefile"}, {"name": "x1", "size": 1, "kind": "mega"}]
sp = D.split_of(arcs)
check("split_of counts files and bytes by source, and game files nowhere",
      sp == {"nexus": (2, 40 * MB), "direct": (1, 100 * MB), "manual": 1, "other": 1}, str(sp))
speeds = {"nexus": 3 * MB, "direct": 50 * MB, "premium": False,
          "measured": {"nexus": False, "direct": True}}
e = D.estimate(sp, speeds)
check("Nexus bytes at Nexus's speed, direct ones at theirs",
      abs(e["nexus_seconds"] - 40 / 3) < 0.01 and abs(e["direct_seconds"] - 2) < 0.01
      and abs(e["seconds"] - (40 / 3 + 2)) < 0.01, str(e))
check("  a click per Nexus file on a free account, and what nothing here fetches counted",
      e["clicks"] == 2 and e["manual"] == 1 and e["other"] == 1, str(e))
check("  and no click on a Premium one", D.estimate(sp, dict(speeds, premium=True))["clicks"] == 0)
check("durations read the way people say them",
      [D.human_duration(s) for s in (5, 59, 60, 450, 3600, 7500)]
      == ["under a minute", "under a minute", "1 min", "7 min", "1 h 00 min", "2 h 05 min"])
check("  and short, for a table", [D.human_duration(s, short=True) for s in (5, 420, 7500)]
      == ["<1m", "7m", "2h 05m"])

sdir = os.path.join(TMP, "speeds")
shutil.rmtree(sdir, ignore_errors=True)
os.makedirs(sdir)
first = D.load_speeds(sdir)
check("with nothing measured, Nexus is its free-account cap and direct a guess",
      first["nexus"] == D.NEXUS_FREE_MBPS * MB and first["direct"] == D.DIRECT_MBPS * MB
      and first["measured"] == {"nexus": False, "direct": False}, str(first))
for cls in D._SPEED_SAMPLES:
    D._SPEED_SAMPLES[cls].clear()
D.note_speed("http", 1 * MB, 1.0)
check("a small transfer is not kept as a sample", D._SPEED_SAMPLES["direct"] == [])
for rate in (10, 40, 45, 50, 1000):                      # MB/s, one of them a freak
    D.note_speed("wabbajackcdn", 100 * MB, 100 / rate)
D.note_speed("nexus", 30 * MB, 10.0)                      # 3 MB/s
D.save_speeds(sdir, premium=False)
got = D.load_speeds(sdir)
check("the direct speed is the median of its samples, so a freak does not move it",
      abs(got["direct"] - 45 * MB) < 1 and got["measured"]["direct"], str(got))
check("  the Nexus one is what Nexus gave", abs(got["nexus"] - 3 * MB) < 1 and got["measured"]["nexus"])
check("  and the account type is kept", got["premium"] is False)
for cls in D._SPEED_SAMPLES:
    D._SPEED_SAMPLES[cls].clear()
for _ in range(D.SPEED_SAMPLES_KEPT + 5):
    D.note_speed("http", 100 * MB, 1.0)
D.save_speeds(sdir)
with io.open(os.path.join(sdir, D.SPEEDS_FILE), encoding="utf-8") as f:
    kept = json.load(f)
check("only the most recent samples are kept", len(kept["direct"]) == D.SPEED_SAMPLES_KEPT
      and len(kept["nexus"]) == 1, str({k: len(v) for k, v in kept.items() if isinstance(v, list)}))
for cls in D._SPEED_SAMPLES:
    D._SPEED_SAMPLES[cls].clear()

D.run_begin([{"name": "a", "size": 40 * MB, "kind": "http"},
             {"name": "b", "size": 40 * MB, "kind": "http"}], speeds)
note = D.run_note()
check("a run's progress line says which file of how many, and how long is left",
      "file 1 of 2" in note and "left" in note, note)
D.run_done_one(40 * MB)
D._tally({"done": 0, "failed": 0, "failures": []}, True)
check("  and moves on as files finish", "file 2 of 2" in D.run_note(), D.run_note())
D._RUN.update(total_files=0, started=None)
check("  and says nothing outside a run", D.run_note() == "")

# ---------------------------------------------------------------- 2.17
print("\n 2.17  the two quick answers leave the folder as they found it")
# Found 2026-09-25 checking a fresh build: --check-window left an empty
# runtime folder beside the binaries, since main() made it before either
# answer. Making it later must not cost a click nobody was listening for.
box = Sandbox(os.path.join(BOXROOT, "sb2"))
quiet = {"PYTHONDONTWRITEBYTECODE": "1"}        # no __pycache__ to tell apart from what it wrote
before = sorted(os.listdir(box.tools))
rc, out = box.run("wj_downloader.py", ["--check-window"], env_extra=quiet)
check("--check-window answers, and writes nothing beside the scripts",
      rc == 0 and re.search(r"^(yes|no): ", out, re.M) is not None
      and sorted(os.listdir(box.tools)) == before,
      out[-300:] + " new: " + str(sorted(set(os.listdir(box.tools)) - set(before))))

CLICK = "nxm://skyrimspecialedition/mods/1/files/2?key=k&expires=1&user_id=3"
heard = []
lsn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
lsn.bind(("127.0.0.1", 0))
lsn.listen(1)
lsn.settimeout(30)


def hear():
    try:
        conn, _ = lsn.accept()
    except OSError:
        return
    with conn:
        got = b""
        while True:
            part = conn.recv(4096)
            if not part:
                break
            got += part
    heard.append(got.decode("utf-8"))


listening = threading.Thread(target=hear, daemon=True)
listening.start()
os.makedirs(box.runtime)
io.open(os.path.join(box.runtime, D.NXM_PORT_FILE), "w", encoding="utf-8").write(
    str(lsn.getsockname()[1]))
rc, out = box.run("wj_downloader.py", ["--nxm", CLICK], env_extra=quiet)
listening.join(10)
lsn.close()
check("a click is handed to the session listening for it", rc == 0 and heard == [CLICK + "\n"],
      str(heard) + " " + out[-300:])
check("  and nothing else is written, beside the scripts or in the runtime folder",
      sorted(os.listdir(box.tools)) == sorted(before + [os.path.basename(box.runtime)])
      and box.runtime_files() == [D.NXM_PORT_FILE],
      str(sorted(os.listdir(box.tools))) + " " + str(box.runtime_files()))

shutil.rmtree(box.runtime)
rc, out = box.run("wj_downloader.py", ["--nxm", CLICK], env_extra=quiet)
kept = os.path.join(box.runtime, D.NXM_FALLBACK_FILE)
check("with nobody listening and no runtime folder yet, the click is kept for --nxm-file",
      rc == 0 and box.runtime_files() == [D.NXM_FALLBACK_FILE]
      and io.open(kept, encoding="utf-8").read() == CLICK + "\n",
      str(box.runtime_files()) + " " + out[-300:])
shutil.rmtree(box.root, ignore_errors=True)

srv.shutdown()
sys.exit(summary())
