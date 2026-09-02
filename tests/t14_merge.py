"""Folding one game folder into another.

The one-time move that saying "these two games share a pool" needs once the
folders already exist. Moves are renames; the only deletions are duplicates
proven by hash; and anything under the source that the tool does not
recognise stops it before it touches a file.
"""
import glob
import io
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOXROOT = os.path.join(os.environ.get("TEMP", "/tmp"), "wj_tests")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Sandbox, check, summary

BOX = os.path.join(BOXROOT, "sb14")
TOOL = "wj_merge_pool.py"

print("=" * 70)
print("  T14  MERGING ONE GAME FOLDER INTO ANOTHER")
print("=" * 70)


def build():
    s = Sandbox(BOX)
    s.manifest("LA", "GameA", [("a.7z", 10, "nexus"), ("dup.7z", 20, "nexus"),
                               ("differs.7z", 30, "nexus")])
    s.manifest("LB", "GameB", [("b.7z", 5, "nexus"), ("dup.7z", 20, "nexus")])
    # The source game, GameA
    s.pool_file("GameA", "LA", "a.7z", 10)
    s.pool_file("GameA", "LA", "a.7z.meta", 2)
    s.pool_file("GameA", "LA", "dup.7z", 20)          # GameB/Shared has the same
    s.pool_file("GameA", "LA", "dup.7z.meta", 3)
    s.pool_file("GameA", "LA", "differs.7z", 30)      # GameB/LB has same size, other bytes
    s.pool_file("GameA", "Master", "m.7z", 40)        # identical copy in GameB/Master
    s.pool_file("GameA", "Master", "n.7z", 50)        # GameB/Master has n.7z at 51
    s.pool_file("GameA", "Master", "o.7z", 60)        # nothing like it on the other side
    s.pool_file("GameA", "Master", "left.7z.part", 7)  # a partial download: moved, never judged
    s.pool_file("GameA", "Shared", "s.7z", 70)
    io.open(os.path.join(s.archives, "GameA", ".DS_Store"), "wb").write(b"z")
    # The destination game, GameB
    s.pool_file("GameB", "LB", "b.7z", 5)
    s.pool_file("GameB", "Shared", "dup.7z", 20)
    io.open(os.path.join(s.archives, "GameB", "LB", "differs.7z"), "wb").write(b"y" * 30)
    s.pool_file("GameB", "Master", "m.7z", 40)
    s.pool_file("GameB", "Master", "n.7z", 51)
    return s


def records(s):
    return sorted(glob.glob(os.path.join(s.tools, "merged_GameA_into_GameB_*.csv")))


# ---------------------------------------------------------------- 14.1
print("\n 14.1  anything unexpected under the source stops the run")
s = build()
s.pool_file("GameA", "TTW", "hand-built.7z", 99)
before = s.files()
rc, out = s.run(TOOL, ["GameA", "GameB", "--yes"], expect=2)
check("a stray folder is refused (exit 2)", rc == 2)
check("  by name", "TTW" in out, out.strip()[-300:])
check("  and nothing moved", s.files() == before)
check("  and no record was written", records(s) == [])

s = build()
s.pool_file("GameA", "Nolvus", "nolvus.7z", 1)
before = s.files()
rc, out = s.run(TOOL, ["GameA", "GameB", "--yes"], expect=2)
check("an excluded folder is refused too", rc == 2 and s.files() == before,
      out.strip()[-200:])

s = build()
s.pool_file("GameB", "LA", "clash.7z", 1)          # LA already exists across
before = s.files()
rc, out = s.run(TOOL, ["GameA", "GameB", "--yes"], expect=2)
check("a list folder present on both sides is refused", rc == 2
      and "already exists" in out and s.files() == before, out.strip()[-200:])

s = build()
before = s.files()
rc, out = s.run(TOOL, ["GameA", "NoSuchGame", "--yes"], expect=2)
check("a destination that does not exist is refused, not created",
      rc == 2 and not os.path.isdir(os.path.join(s.archives, "NoSuchGame"))
      and s.files() == before)
rc, out = s.run(TOOL, ["GameA", "GameA", "--yes"], expect=2)
check("source == destination is refused", rc == 2 and s.files() == before)

# ---------------------------------------------------------------- 14.2
print("\n 14.2  a dry run says what would happen and does nothing")
s = build()
before = s.files()
rc, out = s.run(TOOL, ["GameA", "GameB", "--dry-run"], expect=0)
check("dry run exits 0", rc == 0)
check("  and changes no file", s.files() == before)
check("  and writes no record", records(s) == [])
check("  but names the same-size candidates it found (3 of 7 archives)",
      "3 of 7 archive(s)" in out,
      str([l for l in out.split(chr(10)) if "archive(s)" in l]))
check("  and says nothing was moved", "Nothing was moved or deleted" in out)

# ---------------------------------------------------------------- 14.3
print("\n 14.3  the real run")
s = build()
rc, out = s.run(TOOL, ["GameA", "GameB", "--yes"], expect=0)
check("exits 0", rc == 0, out.strip()[-400:])
t = s.tree()
check("the source game folder is gone", "GameA" not in t, str(sorted(t)))
la = t.get("GameB", {}).get("LA", {})
check("the list folder moved across as a whole",
      la.get("a.7z") == 10 and la.get("a.7z.meta") == 2, str(la))
check("the duplicate the other side already held is gone from it",
      "dup.7z" not in la and "dup.7z.meta" not in la, str(la))
check("  while the copy it duplicated is untouched",
      t["GameB"]["Shared"].get("dup.7z") == 20, str(t["GameB"]["Shared"]))
check("same size but different content is KEPT",
      la.get("differs.7z") == 30, str(la))
master = t["GameB"]["Master"]
check("an identical Master copy was dropped, leaving one",
      master.get("m.7z") == 40 and "m~1.7z" not in master, str(master))
check("a different build under a taken name got a suffix",
      master.get("n.7z") == 51 and master.get("n~1.7z") == 50, str(master))
check("a new file simply moved", master.get("o.7z") == 60, str(master))
check("a partial download moved along, unjudged",
      master.get("left.7z.part") == 7, str(master))
check("Shared merged", t["GameB"]["Shared"].get("s.7z") == 70,
      str(t["GameB"]["Shared"]))
check("the other side's own list is untouched",
      t["GameB"]["LB"] == {"b.7z": 5, "differs.7z": 30}, str(t["GameB"]["LB"]))

recs = records(s)
check("one record was written", len(recs) == 1, str(recs))
rows = [r for r in io.open(recs[0], encoding="utf-8").read().split("\n") if r.strip()]
acts = {}
for r in rows[1:]:
    action, archive = r.split(",")[0], r.split(",")[1]
    acts.setdefault(action, set()).add(archive)
check("it records the two hash-proven drops",
      acts.get("dropped-duplicate") == {"dup.7z", "m.7z"}, str(acts))
check("  the copy kept because content differed",
      acts.get("kept-content-differs") == {"differs.7z"}, str(acts))
check("  and the suffixed move",
      acts.get("moved-suffixed") == {"n.7z"}, str(acts))
check("  with a hash on every drop",
      all(len(r.split(",")[5]) > 8 for r in rows[1:]
          if r.startswith("dropped-duplicate")), str(rows))

print("\n      and the merged folder is sound for the tools that use it")
rc, out = s.run("wabbajack_archive_downloader.py",
                ["--game", "GameA", "--list", "LA", "--dry-run", "--yes"])
# GameA maps to its own folder in the test config, so this only proves the
# manifests still parse; the shared-folder behaviour is covered elsewhere.
check("the manifests still parse after the move", rc in (0, 1), out.strip()[-200:])

sys.exit(summary())
