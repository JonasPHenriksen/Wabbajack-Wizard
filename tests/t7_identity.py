"""A filename does not identify content.

Several modlists legitimately pin different builds of a file with the same
generic name - Synthesis.zip exists five times in the live pools at five
different sizes, one per list, each correct for its own list. Anything that
treats "same name" as "same file" will hand one list another list's build.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOXROOT = os.path.join(os.environ.get("TEMP", "/tmp"), "wj_tests")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Sandbox, check, summary, quiet_logging

BOX = os.path.join(BOXROOT, "sb7")
G = "Fallout4"

print("=" * 70)
print("  T7  ONE NAME, SEVERAL BUILDS")
print("=" * 70)


def two_lists():
    """ListA wants shared.7z at 200 bytes; ListB wants it at 111."""
    s = Sandbox(BOX)
    s.manifest("ListA", G, [("only-a.7z", 10, "nexus"),
                            ("shared.7z", 200, "nexus")])
    s.manifest("ListB", G, [("only-b.7z", 20, "nexus"),
                            ("shared.7z", 111, "nexus")])
    s.pool_file(G, "ListA", "only-a.7z", 10)
    s.pool_file(G, "ListB", "only-b.7z", 20)
    return s


# ---------------------------------------------------------------- 7.1
print("\n 7.1  staging must take the build the TARGET asked for")
# The wrong build sits in Shared, which the sorter scans FIRST; the right one
# is further down the search order. A first-wins index picks the wrong one.
s = two_lists()
s.pool_file(G, "Shared", "shared.7z", 111)     # ListB's build - wrong for ListA
s.pool_file(G, "ListB", "shared.7z", 200)      # ListA's build - right one
rc, out = s.run("wj_sorter.py",
                ["--game", G, "--list", "ListA", "--yes"], expect=0)
hits = dict(s.where("shared.7z"))
check("ListA receives the 200-byte build it asked for",
      hits.get("ListA") == 200, "found %s" % hits)
check("the 111-byte build is not destroyed", 111 in hits.values(),
      "found %s" % hits)

# ---------------------------------------------------------------- 7.2
print("\n 7.2  and the same in reverse, so it is not passing by luck")
s = two_lists()
s.pool_file(G, "Shared", "shared.7z", 200)     # ListA's build
s.pool_file(G, "ListA", "shared.7z", 111)      # ListB's build, misfiled
rc, out = s.run("wj_sorter.py",
                ["--game", G, "--list", "ListB", "--yes"], expect=0)
hits = dict(s.where("shared.7z"))
check("ListB receives the 111-byte build it asked for",
      hits.get("ListB") == 111, "found %s" % hits)
check("the 200-byte build is not destroyed", 200 in hits.values(),
      "found %s" % hits)

# ---------------------------------------------------------------- 7.3
print("\n 7.3  duplicates are reported, not passed over in silence")
s = two_lists()
s.pool_file(G, "Shared", "shared.7z", 111)
s.pool_file(G, "ListB", "shared.7z", 200)
rc, out = s.run("wj_sorter.py",
                ["--game", G, "--list", "ListA", "--yes", "--dry-run"], expect=0)
check("the run says a name exists in more than one place",
      "more than one location" in out)
check("  and lists where", out.count("shared.7z") >= 2)

# ---------------------------------------------------------------- 7.4
print("\n 7.4  the downloader must not call a foreign build 'present'")
s = two_lists()
s.pool_file(G, "ListB", "shared.7z", 111)      # only ListB's build exists
rc, out = s.run("wj_downloader.py",
                ["--game", G, "--list", "ListA", "--dry-run", "--yes"], expect=0)
plan = (out.split("FILES THAT WOULD BE DOWNLOADED", 1)[1]
        .split("[DRY-RUN COMPLETE]", 1)[0]
        if "FILES THAT WOULD BE DOWNLOADED" in out else "")
check("ListA still needs its own build of shared.7z", "shared.7z" in plan,
      plan.strip()[:200])
rc, out = s.run("wj_downloader.py",
                ["--game", G, "--list", "ListB", "--dry-run", "--yes"], expect=0)
plan = (out.split("FILES THAT WOULD BE DOWNLOADED", 1)[1]
        .split("[DRY-RUN COMPLETE]", 1)[0]
        if "FILES THAT WOULD BE DOWNLOADED" in out else "")
check("ListB is satisfied by the copy that IS its build",
      "shared.7z" not in plan, plan.strip()[:200])

# ---------------------------------------------------------------- 7.5
print("\n 7.5  a build no list wants is reclaimable; one a list wants is not")
s = two_lists()
s.pool_file(G, "Master", "shared.7z", 999)     # nobody's build
sys.path.insert(0, REPO)
os.chdir(REPO)
import logging
import wj_autoupdate as A
quiet_logging(A)
A.WJ = A.load_downloader_module()
A.MANIFESTS = s.manifests
A.HERE = s.tools
A.WJ.ARCHIVE_ROOT = s.archives
import wj_wizard as W
recs = A.load_plan_records()
r = W.master_report(G, recs)
check("the 999-byte build in Master counts as reclaimable",
      r["free_files"] == 1 and r["free_bytes"] == 999, str(r))

s.pool_file(G, "Master", "shared~1.7z", 200)   # ListA's build; the ONLY copy
A.WJ.invalidate_existing_cache()
r = W.master_report(G, recs)
check("a file a list wants is protected even sharing a folder with junk",
      r["kept_files"] == 1 and r["free_files"] == 1, str(r))

s.pool_file(G, "Master", "only-a.7z", 10)      # ListA wants it - and already holds it
A.WJ.invalidate_existing_cache()
r = W.master_report(G, recs)
check("a wanted build that a list folder already holds is a duplicate, reclaimable",
      r["dup_files"] == 1 and r["free_files"] == 2 and r["kept_files"] == 1, str(r))

# ---------------------------------------------------------------- 7.6
print("\n 7.6  a download never takes the place of a build another list needs")
# Reproduced 2026-10-10 against 1.4.1. Staging moves the build two lists share
# into the staged list's folder. When that list then moves on to a new build
# of the same name, its download deleted the old one in its way - the build
# the other list still pins - with no trash and no record of it.
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from harness import wj_hash

OLD = b"O" * 200            # ListB's build, and ListA's before its update
NEW = b"N" * 100            # the build ListA moves on to
SERVE = {"/Synthesis.zip": NEW}


class Serve(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = SERVE.get(self.path.split("?")[0])
        if body is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


srv = ThreadingHTTPServer(("127.0.0.1", 0), Serve)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = "http://127.0.0.1:%d/Synthesis.zip" % srv.server_port


def held(s, folder, name):
    p = os.path.join(s.archives, G, folder, name)
    return open(p, "rb").read() if os.path.isfile(p) else None


def synthesis(s):
    return sorted(s.where("Synthesis.zip") + s.where("Synthesis~1.zip"))


s = Sandbox(BOX)
s.manifest("ListA", G, [("Synthesis.zip", 200, "http", URL, wj_hash(OLD)), ("only-a.7z", 10, "nexus")])
s.manifest("ListB", G, [("Synthesis.zip", 200, "http", URL, wj_hash(OLD)), ("only-b.7z", 20, "nexus")])
s.pool_file(G, "Shared", "Synthesis.zip", 200, data=OLD)
s.pool_file(G, "ListA", "only-a.7z", 10)
s.pool_file(G, "ListB", "only-b.7z", 20)
rc, out = s.run("wj_sorter.py", ["--game", G, "--list", "ListA", "--yes"], expect=0)
check("staging ListA takes the build both lists share into its folder",
      s.where("Synthesis.zip") == [("ListA", 200)], str(s.where("Synthesis.zip")))
s.manifest("ListA", G, [("Synthesis.zip", 100, "http", URL, wj_hash(NEW)), ("only-a.7z", 10, "nexus")])
rc, out = s.run("wj_downloader.py", ["--game", G, "--list", "ListA", "--yes", "--skip-nexus"],
                expect=0)
check("ListA's new build is downloaded into its folder",
      rc == 0 and held(s, "ListA", "Synthesis.zip") == NEW, str(synthesis(s)))
check("  and the build ListB pins is kept beside it, not deleted",
      held(s, "ListA", "Synthesis~1.zip") == OLD, str(synthesis(s)))
check("  which the run says", "[KEPT]" in out and "Synthesis~1.zip" in out, out[-600:])
rc, out = s.run("wj_sorter.py", ["--game", G, "--list", "ListB", "--yes"], expect=0)
check("staging ListB afterwards finds its build, under the name its manifest uses",
      held(s, "ListB", "Synthesis.zip") == OLD, str(synthesis(s)))
check("  and ListA's new one is still in the pools, wherever the rebuild put it",
      any(sz == 100 for _f, sz in synthesis(s)), str(synthesis(s)))

# ---------------------------------------------------------------- 7.7
print("\n 7.7  staging never hands a list another list's build as its own")
# The same loss by another road: ListA's build was not on disk at all, so
# staging moved ListB's build out of Shared into ListA's folder under the same
# name, reported nothing missing - and the download for ListA deleted it.
s = Sandbox(BOX)
s.manifest("ListA", G, [("Synthesis.zip", 100, "http", URL, wj_hash(NEW)), ("only-a.7z", 10, "nexus")])
s.manifest("ListB", G, [("Synthesis.zip", 200, "http", URL, wj_hash(OLD)), ("only-b.7z", 20, "nexus")])
s.pool_file(G, "Shared", "Synthesis.zip", 200, data=OLD)
s.pool_file(G, "ListA", "only-a.7z", 10)
s.pool_file(G, "ListB", "only-b.7z", 20)
rc, out = s.run("wj_sorter.py", ["--game", G, "--list", "ListA", "--yes"], expect=0)
check("staging ListA leaves ListB's build in Shared",
      s.where("Synthesis.zip") == [("Shared", 200)], str(s.where("Synthesis.zip")))
check("  and reports ListA's own build as still to fetch, with both sizes",
      "only a DIFFERENT build" in out and "wants 100; on disk at 200" in out
      and "1 missing" in out, out[-900:])
rc, out = s.run("wj_downloader.py", ["--game", G, "--list", "ListA", "--yes", "--skip-nexus"],
                expect=0)
check("the download then fetches ListA's build into its folder",
      rc == 0 and held(s, "ListA", "Synthesis.zip") == NEW, str(synthesis(s)))
check("  and ListB's build is untouched in Shared", held(s, "Shared", "Synthesis.zip") == OLD,
      str(synthesis(s)))
srv.shutdown()

sys.exit(summary())
