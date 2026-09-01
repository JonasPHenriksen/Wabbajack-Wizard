"""Sorter edge cases. Everything here moves real files, so it is the part
most worth testing before it ever runs on the live pools."""
import os
import sys

# Derive the repo root from this file so the suite runs from anywhere, rather
# than being pinned to whatever drive letter the share happens to have.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOXROOT = os.path.join(os.environ.get("TEMP", "/tmp"), "wj_tests")
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Sandbox, check, summary

BOX = os.path.join(BOXROOT, "sb1")
G = "Fallout4"


def fresh():
    s = Sandbox(BOX)
    s.manifest("ListA", G, [("a1.7z", 100, "nexus"),
                            ("shared.7z", 200, "nexus"),
                            ("gone.7z", 300, "nexus"),
                            ("fromgame.7z", 400, "gamefile")])
    s.manifest("ListB", G, [("b1.7z", 150, "nexus"),
                            ("shared.7z", 200, "nexus")])
    s.pool_file(G, "ListA", "a1.7z", 100)
    s.pool_file(G, "ListA", "shared.7z", 200)
    s.pool_file(G, "ListB", "b1.7z", 150)
    s.pool_file(G, "Master", "orphan.7z", 999)
    s.pool_file(G, "Nolvus", "nolvus-mod.7z", 500)
    return s


print("=" * 70)
print("  T1  SORTER")
print("=" * 70)

# ---------------------------------------------------------------- 1.1
print("\n 1.1  an unreadable manifest must abort, not silently reshape demand")
for kind, label in (("notzip", "not a zip"),
                    ("badjson", "truncated JSON"),
                    ("nomodlist", "no 'modlist' member")):
    s = fresh()
    s.manifest("Broken", G, [], corrupt=kind)
    before = s.files()
    rc, out = s.run("wabbajack_archive_sorter_v2.py",
                    ["--game", G, "--list", "ListA", "--yes"], expect=2)
    check("aborts on %-20s (exit 2)" % label, rc == 2)
    check("  and moved no file at all", s.files() == before)
    check("  and said which file", "Broken" in out)

# ---------------------------------------------------------------- 1.2
print("\n 1.2  a modlist named after a reserved pool folder must abort")
s = fresh()
s.manifest("Master", G, [("m1.7z", 10, "nexus")])
before = s.files()
rc, out = s.run("wabbajack_archive_sorter_v2.py",
                ["--game", G, "--list", "ListA", "--yes"], expect=1)
check("aborts when a list is called 'Master'", rc == 1)
check("  and moved nothing", s.files() == before)
check("  and explained why", "reserved" in out.lower())

# ---------------------------------------------------------------- 1.3
print("\n 1.3  cleanup pass: demand decides where each archive lives")
s = fresh()
rc, out = s.run("wabbajack_archive_sorter_v2.py",
                ["--game", G, "--cleanup-only", "--yes"], expect=0)
check("cleanup exits 0", rc == 0)
check("shared.7z (2 lists want it) -> Shared",
      s.where("shared.7z") == [("Shared", 200)], str(s.where("shared.7z")))
check("a1.7z (1 list wants it) stays in ListA",
      s.where("a1.7z") == [("ListA", 100)], str(s.where("a1.7z")))
check("b1.7z (1 list wants it) stays in ListB",
      s.where("b1.7z") == [("ListB", 150)], str(s.where("b1.7z")))
check("orphan.7z (nobody wants it) stays in Master",
      s.where("orphan.7z") == [("Master", 999)], str(s.where("orphan.7z")))
check("Nolvus is never touched",
      s.where("nolvus-mod.7z") == [("Nolvus", 500)],
      str(s.where("nolvus-mod.7z")))
check("Nolvus not even scanned", "Nolvus" not in out.split("DISK SCAN")[-1]
      .split("BUILDING PLAN")[0] or "Ignoring" in out)

# ---------------------------------------------------------------- 1.4
print("\n 1.4  staging gathers what the target needs into its folder")
rc, out = s.run("wabbajack_archive_sorter_v2.py",
                ["--game", G, "--list", "ListA", "--yes"], expect=0)
check("staging exits 0", rc == 0)
check("shared.7z pulled out of Shared into ListA",
      s.where("shared.7z") == [("ListA", 200)], str(s.where("shared.7z")))
check("a1.7z still in ListA", s.where("a1.7z") == [("ListA", 100)])
check("gone.7z reported missing (never on disk)", "gone.7z" in out)
check("fromgame.7z NOT called missing (game-file source)",
      "come from your game install" in out)

# ---------------------------------------------------------------- 1.5
print("\n 1.5  collision: two files with one name, only one the right size")
s = fresh()
# ListA holds a WRONG-size shared.7z; the correct one sits in Shared.
os.remove(os.path.join(s.archives, G, "ListA", "shared.7z"))
s.pool_file(G, "ListA", "shared.7z", 111)      # wrong: manifest says 200
s.pool_file(G, "Shared", "shared.7z", 200)     # right
rc, out = s.run("wabbajack_archive_sorter_v2.py",
                ["--game", G, "--list", "ListA", "--yes"], expect=0)
hits = dict(s.where("shared.7z"))
check("the 200-byte copy ends up in ListA", hits.get("ListA") == 200, str(hits))
check("the 111-byte copy is preserved, not deleted",
      111 in hits.values(), str(hits))
check("the wrong copy was parked in Master",
      hits.get("Master") == 111 or any(
          v == 111 for k, v in hits.items() if k == "Master"), str(hits))
check("conflict was reported", "CONFLICT" in out.upper())

# ---------------------------------------------------------------- 1.6
print("\n 1.6  dry-run must move nothing whatsoever")
s = fresh()
before = s.files()
rc, out = s.run("wabbajack_archive_sorter_v2.py",
                ["--game", G, "--list", "ListA", "--yes", "--dry-run"],
                expect=0)
check("dry-run exits 0", rc == 0)
check("dry-run changed no file", s.files() == before)
check("dry-run still printed a plan", "move(s)" in out)

# ---------------------------------------------------------------- 1.7
print("\n 1.7  bad --list / --game are refused, not guessed at")
s = fresh()
before = s.files()
rc, _o = s.run("wabbajack_archive_sorter_v2.py",
               ["--game", G, "--list", "NoSuchList", "--yes"], expect=2)
check("unknown --list exits 2", rc == 2)
rc, _o = s.run("wabbajack_archive_sorter_v2.py",
               ["--game", "NoSuchGame", "--list", "ListA", "--yes"], expect=2)
check("unknown --game exits 2", rc == 2)
check("nothing moved by either", s.files() == before)

# ---------------------------------------------------------------- 1.8
print("\n 1.8  a list can be selected by name OR by menu number")
s = fresh()
rc, out = s.run("wabbajack_archive_sorter_v2.py",
                ["--game", G, "--list", "1", "--yes", "--dry-run"], expect=0)
check("numeric --list still works (back-compat)", rc == 0)
rc, out = s.run("wabbajack_archive_sorter_v2.py",
                ["--game", G, "--list", "listb", "--yes", "--dry-run"],
                expect=0)
check("--list is case-insensitive by name", rc == 0 and "ListB" in out)

sys.exit(summary())
