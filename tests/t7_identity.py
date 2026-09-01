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
from harness import Sandbox, check, summary

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
rc, out = s.run("wabbajack_archive_sorter_v2.py",
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
rc, out = s.run("wabbajack_archive_sorter_v2.py",
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
rc, out = s.run("wabbajack_archive_sorter_v2.py",
                ["--game", G, "--list", "ListA", "--yes", "--dry-run"], expect=0)
check("the run says a name exists in more than one place",
      "more than one location" in out)
check("  and lists where", out.count("shared.7z") >= 2)

# ---------------------------------------------------------------- 7.4
print("\n 7.4  the downloader must not call a foreign build 'present'")
s = two_lists()
s.pool_file(G, "ListB", "shared.7z", 111)      # only ListB's build exists
rc, out = s.run("wabbajack_archive_downloader.py",
                ["--game", G, "--list", "ListA", "--dry-run", "--yes"], expect=0)
plan = (out.split("FILES THAT WOULD BE DOWNLOADED", 1)[1]
        .split("[DRY-RUN COMPLETE]", 1)[0]
        if "FILES THAT WOULD BE DOWNLOADED" in out else "")
check("ListA still needs its own build of shared.7z", "shared.7z" in plan,
      plan.strip()[:200])
rc, out = s.run("wabbajack_archive_downloader.py",
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
A.setup_logging(False)
A.LOG.setLevel(logging.CRITICAL)
A.WJ = A.load_downloader_module()
A.MANIFESTS = s.manifests
A.HERE = s.tools
A.WJ.ARCHIVE_ROOT = s.archives
import wj_wizard as W
recs = A.load_plan_records()
r = W.master_report(G, recs)
check("the 999-byte build in Master counts as reclaimable",
      r["free_files"] == 1 and r["free_bytes"] == 999, str(r))

s.pool_file(G, "Master", "only-a.7z", 10)      # this one ListA does want
A.WJ.invalidate_existing_cache()
r = W.master_report(G, recs)
check("a file a list wants is protected even sharing a folder with junk",
      r["kept_files"] == 1 and r["free_files"] == 1, str(r))

sys.exit(summary())
