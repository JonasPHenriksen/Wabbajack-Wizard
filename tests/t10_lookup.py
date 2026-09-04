"""Where an archive may live and still count as already downloaded.

The question this answers: before downloading, does the planner look at
everything on disk - the list's own folder AND the shared pools - or only at
the list's own folder?
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOXROOT = os.path.join(os.environ.get("TEMP", "/tmp"), "wj_tests")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Sandbox, check, summary

BOX = os.path.join(BOXROOT, "sb10")
G = "Fallout4"

print("=" * 70)
print("  T10  WHERE A FILE MAY LIVE AND STILL COUNT AS PRESENT")
print("=" * 70)


def planned(out):
    """The archives a dry-run says it would download."""
    if "FILES THAT WOULD BE DOWNLOADED" not in out:
        return ""
    return (out.split("FILES THAT WOULD BE DOWNLOADED", 1)[1]
            .split("[DRY-RUN COMPLETE]", 1)[0])


# ---------------------------------------------------------------- 10.1
print("\n 10.1  one archive per location; which are seen?")
s = Sandbox(BOX)
# Target list wants one archive from each candidate location.
s.manifest("Target", G, [
    ("in-own.7z", 100, "nexus"),
    ("in-shared.7z", 200, "nexus"),
    ("in-master.7z", 300, "nexus"),
    ("in-otherlist.7z", 400, "nexus"),
    ("in-gameroot.7z", 500, "nexus"),
    ("in-nolvus.7z", 600, "nexus"),
    ("nowhere.7z", 700, "nexus"),
])
# A second list, so its folder is a legitimate pool location.
s.manifest("Other", G, [("in-otherlist.7z", 400, "nexus")])

s.pool_file(G, "Target", "in-own.7z", 100)
s.pool_file(G, "Shared", "in-shared.7z", 200)
s.pool_file(G, "Master", "in-master.7z", 300)
s.pool_file(G, "Other", "in-otherlist.7z", 400)
s.pool_file(G, "Nolvus", "in-nolvus.7z", 600)
# Loose in the game root, which neither tool manages.
root_file = os.path.join(s.archives, G, "in-gameroot.7z")
open(root_file, "wb").write(b"x" * 500)

rc, out = s.run("wj_downloader.py",
                ["--game", G, "--list", "Target", "--dry-run", "--yes"],
                expect=0)
plan = planned(out)
for name, where, expect_present in (
        ("in-own.7z", "the list's OWN folder", True),
        ("in-shared.7z", "the Shared pool", True),
        ("in-master.7z", "the Master pool", True),
        ("in-otherlist.7z", "ANOTHER list's folder", True),
        ("in-gameroot.7z", "loose in the game root", False),
        ("in-nolvus.7z", "inside Nolvus", False),
        ("nowhere.7z", "nowhere at all", False)):
    seen = name not in plan
    check("%-24s %-24s -> %s"
          % (name, where, "present" if seen else "will download"),
          seen == expect_present,
          "plan: %s" % plan.strip()[:120])

# ---------------------------------------------------------------- 10.2
print("\n 10.2  the two tools agree on what the pools ARE")
rc, sout = s.run("wj_sorter.py",
                 ["--game", G, "--list", "Target", "--yes", "--dry-run"],
                 expect=0)
scanned = set()
for line in sout.split("\n"):
    if "Indexed" in line and " in '" in line:
        scanned.add(line.split(" in '")[1].split("'")[0])
check("the sorter scans Shared", "Shared" in scanned, str(sorted(scanned)))
check("the sorter scans Master", "Master" in scanned, str(sorted(scanned)))
check("the sorter scans each list folder",
      {"Target", "Other"} <= scanned, str(sorted(scanned)))
check("the sorter does NOT scan Nolvus", "Nolvus" not in scanned,
      str(sorted(scanned)))
check("the sorter reports the game-root file as missing, as the "
      "downloader does", "in-gameroot.7z" in sout)

# ---------------------------------------------------------------- 10.3
print("\n 10.3  a wrong-size copy elsewhere does not mask a right-size one")
s = Sandbox(BOX)
s.manifest("Target", G, [("both.7z", 500, "nexus")])
s.manifest("Other", G, [("both.7z", 111, "nexus")])
s.pool_file(G, "Shared", "both.7z", 111)      # the OTHER list's build
s.pool_file(G, "Target", "both.7z", 500)      # the build Target wants
rc, out = s.run("wj_downloader.py",
                ["--game", G, "--list", "Target", "--dry-run", "--yes"],
                expect=0)
check("a right-size copy anywhere satisfies the requirement",
      "both.7z" not in planned(out), planned(out).strip()[:120])

print("\n      a copy parked under a '~N' suffix is still that file")
s = Sandbox(BOX)
s.manifest("Target", G, [("parked.7z", 500, "nexus"), ("wrong.7z", 5, "nexus")])
s.pool_file(G, "Master", "parked~1.7z", 500)   # right build, second name
s.pool_file(G, "Master", "wrong~1.7z", 999)    # wrong build, second name
rc, out = s.run("wj_downloader.py",
                ["--game", G, "--list", "Target", "--dry-run", "--yes"],
                expect=0)
check("a suffixed copy at the right size counts as present",
      "parked.7z" not in planned(out), planned(out).strip()[:120])
check("  a suffixed copy at the wrong size does not",
      "wrong.7z" in planned(out), planned(out).strip()[:120])

print("\n      and when only the wrong build exists it is not accepted")
s = Sandbox(BOX)
s.manifest("Target", G, [("only.7z", 500, "nexus")])
s.pool_file(G, "Shared", "only.7z", 111)
rc, out = s.run("wj_downloader.py",
                ["--game", G, "--list", "Target", "--dry-run", "--yes"],
                expect=0)
check("a wrong-size copy is re-downloaded",
      "only.7z" in planned(out), planned(out).strip()[:120])
check("  and reported as wrong size, not absent", "wrong size" in out)

sys.exit(summary())
