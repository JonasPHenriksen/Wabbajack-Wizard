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
import struct
import sys
import threading
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

srv.shutdown()
sys.exit(summary())
