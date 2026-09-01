"""Wizard: the degenerate cases. Empty data, missing folders, nothing to do."""
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
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Sandbox, check, summary

sys.path.insert(0, REPO)
os.chdir(REPO)
import wj_autoupdate as A
A.setup_logging(False)
A.LOG.setLevel(logging.CRITICAL)
A.WJ = A.load_downloader_module()
import wj_wizard as W

BOX = os.path.join(BOXROOT, "sb5")
G = "Fallout4"
box = Sandbox(BOX)

# Point the wizard at the sandbox instead of the live share.
A.MANIFESTS = box.manifests
A.HERE = box.tools
A.WJ.ARCHIVE_ROOT = box.archives

print("=" * 70)
print("  T5  WIZARD: DEGENERATE CASES")
print("=" * 70)


def quiet(fn, *a, **k):
    """Run a stage with its printing suppressed; return (value, output)."""
    buf = io.StringIO()
    real = W.out
    W.out = lambda msg="": buf.write(str(msg) + "\n")
    try:
        return fn(*a, **k), buf.getvalue()
    finally:
        W.out = real


# ---------------------------------------------------------------- 5.1
print("\n 5.1  column sizing with nothing to size")
check("fit([]) falls back to the minimum", W.fit([], 7) == 7)
check("fit picks the longest", W.fit(["a", "abc", "ab"], 1) == 3)
check("fit respects the minimum over short values", W.fit(["a"], 9) == 9)
check("fit copes with non-strings", W.fit([1, 22, 333], 1) == 3)

# ---------------------------------------------------------------- 5.2
print("\n 5.2  sizing a directory that is not there")
sz, n = W.dir_size(os.path.join(box.archives, "nope", "gone"))
check("dir_size on a missing path is (0, 0)", (sz, n) == (0, 0), str((sz, n)))
box.pool_file(G, "Master", "one.7z", 42)
box.pool_file(G, "Master", "one.7z.meta", 10)
sz, n = W.dir_size(os.path.join(box.archives, G, "Master"))
check("dir_size ignores .meta in the count", n == 1, "count=%d" % n)
check("dir_size sums only real archives", sz == 42, "bytes=%d" % sz)

# ---------------------------------------------------------------- 5.3
print("\n 5.3  master_report for a game with no Master folder at all")
r = W.master_report("NoSuchGame", {})
check("returns a zeroed report rather than raising",
      r["bytes"] == 0 and r["files"] == 0 and r["free_files"] == 0, str(r))
recs = {"L.wabbajack": {"game": G, "archives": [
    {"name": "one.7z", "size": 42, "kind": "nexus"}]}}
r = W.master_report(G, recs)
check("a file a list still needs counts as protected, not reclaimable",
      r["kept_files"] == 1 and r["free_files"] == 0, str(r))
r = W.master_report(G, {})
check("with no list wanting it, the same file is reclaimable",
      r["free_files"] == 1 and r["free_bytes"] == 42, str(r))

# ---------------------------------------------------------------- 5.4
print("\n 5.4  list_status on a manifest with no GameType")
check("returns None rather than guessing a game",
      W.list_status({"game": None, "archives": []}) is None)
st = W.list_status({"game": G, "archives": []})
check("an empty archive list is complete, not broken",
      st is not None and st["missing"] == 0, str(st))

# ---------------------------------------------------------------- 5.5
print("\n 5.5  every stage must cope with nothing to do")
(res, out) = quiet(W.stage_incomplete, {}, False)
check("stage_incomplete with no manifests returns False", res is False)
check("  and says there is nothing to do", "Nothing to do" in out)

(res, out) = quiet(W.stage_prepare, {})
check("stage_prepare with no manifests returns False", res is False)
check("  and explains rather than crashing",
      "No modlist" in out or "Nothing" in out, out[-120:])

gameless = {"X.wabbajack": {"game": None, "archives": []}}
(res, out) = quiet(W.stage_prepare, gameless)
check("stage_prepare skips a gameless manifest", res is False)

(res, out) = quiet(W.stage_space, {"lists": {}, "map": {}}, {})
check("stage_space with no manifests and no pools survives", res in (False, True),
      str(res))
check("  and reports empty rather than dividing by zero",
      "MODLIST MANIFESTS" in out, out[:120])

# ---------------------------------------------------------------- 5.6
print("\n 5.6  deleting a modlist whose pieces are already missing")
state = {"lists": {"r/x": {"file": "Ghost.wabbajack"}},
         "map": {"ghost.wabbajack": "r/x"}}
moved, redundant, freed = W.delete_modlist(
    state, {"file": "Ghost.wabbajack", "game": G})
check("no crash when the manifest is not on disk", True)
check("nothing claimed as freed", freed == 0, "freed=%d" % freed)
check("the state entry is still cleaned up", "r/x" not in state["lists"])
check("the map entry is cleaned up too", "ghost.wabbajack" not in state["map"])

moved, redundant, freed = W.delete_modlist(
    state, {"file": "AlsoGhost.wabbajack", "game": None})
check("a gameless entry does not raise", True)

# ---------------------------------------------------------------- 5.7
print("\n 5.7  clearing a Master that does not exist")
deleted, kept, freed = W.clear_master("NoSuchGame", set())
check("returns empty results rather than raising",
      deleted == [] and kept == 0 and freed == 0, str((deleted, kept, freed)))

# ---------------------------------------------------------------- 5.8
print("\n 5.8  a manifest name with characters that would break a table")
box2 = Sandbox(BOX + "b")
A.MANIFESTS = box2.manifests
A.WJ.ARCHIVE_ROOT = box2.archives
A.HERE = box2.tools
odd = "weird name (2) [v1] & more"
box2.manifest(odd, G, [("a.7z", 5, "nexus")])
box2.pool_file(G, odd, "a.7z", 5)
recs = A.load_plan_records()
check("an awkward filename parses", odd + ".wabbajack" in recs, str(list(recs)))
(res, out) = quiet(W.stage_incomplete, recs, False)
check("and renders in the table without blowing up", odd in out or res is False)
lists, masters = quiet(W.gather_space, recs)[0]
check("gather_space handles it", any(x["file"].startswith("weird") for x in lists),
      str([x["file"] for x in lists]))

sys.exit(summary())
