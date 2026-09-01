"""Planning decisions, the manifest cache, and the backup-pruning deletion path."""
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
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Sandbox, check, summary

sys.path.insert(0, REPO)
os.chdir(REPO)
import wj_autoupdate as A
A.setup_logging(False)
A.LOG.setLevel(logging.CRITICAL)
A.WJ = A.load_downloader_module()

BOX = os.path.join(BOXROOT, "sb6")
G = "Fallout4"

print("=" * 70)
print("  T6  PLANNING, CACHE, PRUNING")
print("=" * 70)

# ---------------------------------------------------------------- 6.1
print("\n 6.1  what the downloader decides needs fetching")
box = Sandbox(BOX)
box.manifest("ListA", G, [
    ("present.7z", 100, "nexus"),      # on disk, right size  -> skip
    ("stale.7z", 200, "nexus"),        # on disk, WRONG size  -> refetch
    ("absent.7z", 300, "nexus"),       # not on disk          -> fetch
    ("elsewhere.7z", 400, "nexus"),    # right size, other folder -> skip
    ("fromgame.7z", 500, "gamefile"),  # game install         -> never fetched
    ("manual.7z", 600, "manual"),      # unsupported          -> reported
])
box.pool_file(G, "ListA", "present.7z", 100)
box.pool_file(G, "ListA", "stale.7z", 199)          # one byte short
box.pool_file(G, "Shared", "elsewhere.7z", 400)
box.pool_file(G, "Nolvus", "absent.7z", 300)        # must NOT count as present

rc, out = box.run("wabbajack_archive_downloader.py",
                  ["--game", G, "--list", "ListA", "--dry-run", "--yes"],
                  expect=0)
check("dry-run exits 0", rc == 0)
check("the right-size copy counts as present", "Already present:2" in out
      or "Already present: 2" in out, [l for l in out.split("\n") if "present" in l])
check("the wrong-size copy is flagged and refetched", "stale.7z" in out)
check("  and named as wrong size, not absent", "wrong size" in out)
def planned(text):
    """Just the list of files the dry-run says it would download."""
    if "FILES THAT WOULD BE DOWNLOADED" not in text:
        return ""
    body = text.split("FILES THAT WOULD BE DOWNLOADED", 1)[1]
    return body.split("[DRY-RUN COMPLETE]", 1)[0]


plan = planned(out)
check("a copy in ANOTHER pool folder counts as present",
      "elsewhere.7z" not in plan, plan.strip()[:160])
check("a copy inside Nolvus does NOT count as present",
      "absent.7z" in plan, plan.strip()[:160])
check("Nolvus is announced as skipped", "Ignoring foreign installer" in out)
check("game-file sources are not download candidates",
      "From game files:1" in out or "From game files: 1" in out)
check("an unsupported source is reported, not silently dropped",
      "manual.7z" in out and "manually" in out.lower())
check("  and is NOT offered as something we can fetch",
      "manual.7z" not in plan, plan.strip()[:160])

# ---------------------------------------------------------------- 6.2
print("\n 6.2  --force ignores what is already on disk")
rc, out = box.run("wabbajack_archive_downloader.py",
                  ["--game", G, "--list", "ListA", "--dry-run", "--yes",
                   "--force"], expect=0)
check("--force exits 0", rc == 0)
check("--force plans the present file too", "present.7z" in out)
check("--force reports nothing as already present",
      "Already present:0" in out or "Already present: 0" in out)

# ---------------------------------------------------------------- 6.3
print("\n 6.3  an unreadable manifest is reported but does not stop a download")
box.manifest("Broken", G, [], corrupt="notzip")
rc, out = box.run("wabbajack_archive_downloader.py",
                  ["--game", G, "--list", "ListA", "--dry-run", "--yes"],
                  expect=0)
check("the downloader still runs (unlike the sorter, which must abort)",
      rc == 0)
check("  and says which manifest it could not read",
      "Broken" in out and "could not be read" in out)

# ---------------------------------------------------------------- 6.4
print("\n 6.4  the manifest cache must notice a changed manifest")
A.MANIFESTS = box.manifests
A.HERE = box.tools
A.WJ.ARCHIVE_ROOT = box.archives
cache_file = os.path.join(box.tools, A.MANIFEST_CACHE_FILE)
try:
    os.remove(cache_file)
except OSError:
    pass
r1 = A.load_plan_records()
n1 = len(r1["ListA.wabbajack"]["archives"])
check("cold read parses the manifest (%d archives)" % n1, n1 == 6)
check("a cache file is written", os.path.exists(cache_file))

calls = {"n": 0}
real_parse = A.WJ.parse_manifest


def counting(path):
    calls["n"] += 1
    return real_parse(path)


A.WJ.parse_manifest = counting
try:
    r2 = A.load_plan_records()
    check("a warm read parses nothing again", calls["n"] == 0,
          "parses=%d" % calls["n"])
    check("  and returns the same data", len(r2["ListA.wabbajack"]["archives"]) == n1)

    # Rewrite the manifest with a different archive count.
    time.sleep(1.1)                     # make sure mtime actually moves
    box.manifest("ListA", G, [("only-one.7z", 10, "nexus")])
    calls["n"] = 0
    r3 = A.load_plan_records()
    check("a changed manifest is re-parsed", calls["n"] == 1,
          "parses=%d" % calls["n"])
    check("  and the new content is used",
          len(r3["ListA.wabbajack"]["archives"]) == 1,
          str(len(r3["ListA.wabbajack"]["archives"])))

    # Remove a manifest: it must vanish from the results.
    os.remove(os.path.join(box.manifests, "Broken.wabbajack"))
    r4 = A.load_plan_records()
    check("a deleted manifest disappears from the cache",
          "Broken.wabbajack" not in r4, str(list(r4)))
finally:
    A.WJ.parse_manifest = real_parse

# ---------------------------------------------------------------- 6.5
print("\n 6.5  prune_backups may only ever delete .bak and .rejected")
box2 = Sandbox(BOX + "b")
A.HERE = box2.tools
A.MANIFESTS = box2.manifests
A.BACKUPS = os.path.join(box2.tools, "manifest_backups")
os.makedirs(A.BACKUPS, exist_ok=True)
old = time.time() - (A.KEEP_BACKUP_DAYS + 5) * 86400
kinds = {
    "OldList.wabbajack.bak": (old, "should go"),
    "OldList.wabbajack.rejected": (old, "should go"),
    "NewList.wabbajack.bak": (time.time(), "too recent"),
    "SomeList.wabbajack": (old, "a real manifest"),
    "notes.txt": (old, "an unrelated file"),
    "important.json": (old, "an unrelated file"),
}
for name, (mtime, _why) in kinds.items():
    p = os.path.join(A.BACKUPS, name)
    io.open(p, "wb").write(b"x" * 10)
    os.utime(p, (mtime, mtime))
A.prune_backups()
left = set(os.listdir(A.BACKUPS))
check("an old .bak is pruned", "OldList.wabbajack.bak" not in left)
check("an old .rejected is pruned", "OldList.wabbajack.rejected" not in left)
check("a recent .bak is KEPT", "NewList.wabbajack.bak" in left)
check("an old real manifest is KEPT", "SomeList.wabbajack" in left)
check("an unrelated .txt is KEPT", "notes.txt" in left)
check("an unrelated .json is KEPT", "important.json" in left)

print("\n      and it must not touch the manifest folder itself")
A.BACKUPS = A.MANIFESTS          # the fallback when backups cannot be made
io.open(os.path.join(A.MANIFESTS, "Live.wabbajack"), "wb").write(b"x")
os.utime(os.path.join(A.MANIFESTS, "Live.wabbajack"), (old, old))
A.prune_backups()
check("with BACKUPS == MANIFESTS it prunes nothing at all",
      os.path.exists(os.path.join(A.MANIFESTS, "Live.wabbajack")))

sys.exit(summary())
