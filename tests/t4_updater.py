"""Updater: state handling, matching helpers, and the guards that stop a run."""
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
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Sandbox, check, summary, quiet_logging

sys.path.insert(0, REPO)
os.chdir(REPO)
import wj_autoupdate as A
quiet_logging(A)
A.WJ = A.load_downloader_module()

BOX = os.path.join(BOXROOT, "sb4")
G = "Fallout4"

print("=" * 70)
print("  T4  UPDATER: STATE, MATCHING, GUARDS")
print("=" * 70)

# ---------------------------------------------------------------- 4.1
print("\n 4.1  filename -> published feed key")
cases = [
    ("wj-featured_@@_living_skyrim.wabbajack", "wj-featured/living_skyrim"),
    ("A_@@_B.wabbajack", "A/B"),
    ("repo_@@_a_@@_b.wabbajack", "repo/a_@@_b"),   # only the FIRST separator
    ("NoSeparator.wabbajack", None),
    ("_@@_orphan.wabbajack", None),                # empty repo
    ("orphan_@@_.wabbajack", None),                # empty machine
    ("", None),
]
for name, want in cases:
    got = A.feed_key_from_filename(name) if name else None
    check("%-38s -> %s" % (name or "(empty)", got), got == want,
          "expected %s" % want)

# ---------------------------------------------------------------- 4.2
print("\n 4.2  a remembered 'matches nothing' must expire, and legacy ones retry")
future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
past = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
check("legacy '' is always retried", A.negative_expired("") is True)
check("a future retry_after is respected",
      A.negative_expired({"retry_after": future}) is False)
check("a past retry_after has expired",
      A.negative_expired({"retry_after": past}) is True)
check("garbage retry_after retries rather than sticking",
      A.negative_expired({"retry_after": "not a date"}) is True)
check("missing retry_after retries", A.negative_expired({}) is True)
check("an unexpected type retries", A.negative_expired(12345) is True)

print("\n      and a no-match is only recorded when every feed was readable")
cache = {}
A.remember_no_match(cache, "X.wabbajack", feeds_complete=False)
check("incomplete feeds -> nothing cached", cache == {}, str(cache))
A.remember_no_match(cache, "X.wabbajack", feeds_complete=True)
check("complete feeds -> cached with an expiry",
      "x.wabbajack" in cache and "retry_after" in cache["x.wabbajack"],
      str(cache))

# ---------------------------------------------------------------- 4.3
print("\n 4.3  naming a manifest we do not yet have")
ent = {"title": "Some List: With/Bad*Chars?"}
check("follows the _@@_ convention from the feed key",
      A.manifest_filename_for("LoreRim/LoreRim", ent)
      == "LoreRim_@@_LoreRim.wabbajack",
      A.manifest_filename_for("LoreRim/LoreRim", ent))
n = A.manifest_filename_for("Repo/With:Bad*Chars", ent)
check("falls back to the title when the key is not a legal filename",
      n.endswith(".wabbajack") and ":" not in n and "*" not in n, n)

# ---------------------------------------------------------------- 4.4
print("\n 4.4  a damaged state file must not stop a run")
box = Sandbox(BOX)
os.makedirs(box.runtime, exist_ok=True)
for bad, label in (("{ not json at all", "invalid JSON"),
                   ("[1,2,3]", "a list instead of an object"),
                   ("null", "null"),
                   ("", "empty file")):
    sf = os.path.join(box.runtime, A.STATE_FILE)
    io.open(sf, "w", encoding="utf-8").write(bad)
    saved_here, A.HERE = A.HERE, box.tools
    try:
        read_from = A.runtime_path(A.STATE_FILE)
        st = A.load_state()
    finally:
        A.HERE = saved_here
    check("recovers from %-28s" % label,
          os.path.normcase(read_from) == os.path.normcase(sf)
          and isinstance(st, dict) and isinstance(st.get("lists"), dict), str(st)[:60])

# ---------------------------------------------------------------- 4.5
print("\n 4.5  verify_manifest reuses a hash instead of re-reading the file")
p = os.path.join(box.tools, "probe.bin")
io.open(p, "wb").write(b"Q" * 1234)
real = A.WJ.wabbajack_hash(p)
ok, why = A.verify_manifest(p, 1234, real, known_hash=real)
check("matching known_hash passes without re-hashing", ok and "download" in why, why)
ok, why = A.verify_manifest(p, 1234, real, known_hash="AAAAAAAAAAA=")
check("mismatching known_hash fails", not ok and "mismatch" in why, why)
ok, why = A.verify_manifest(p, 9999, real, known_hash=real)
check("a wrong size fails before any hashing", not ok and "size" in why, why)
ok, why = A.verify_manifest(p, 1234, real)
check("with no known_hash it hashes for itself", ok, why)

# ---------------------------------------------------------------- 4.6
print("\n 4.6  odd feed shapes must not crash the parser")
shapes = [
    ([], "empty list"),
    ([{"links": {"machineURL": "m"}, "title": "T"}], "entry with links only"),
    ({"lists": [{"machineURL": "m", "title": "T"}]}, "dict wrapping a list"),
    ({"machineURL": "m", "title": "T"}, "a single bare entry"),
    ([None, 5, "x", {"machineURL": "ok"}], "list with junk in it"),
    ({"junk": "nothing useful"}, "dict with nothing usable"),
]
for data, label in shapes:
    try:
        got = list(A.iter_entries(data))
        norm = [A.normalise(e, "repo") for e in got]
        check("survives %-26s (%d usable)" % (label, len([n for n in norm if n])),
              True)
    except Exception as e:
        check("survives %-26s" % label, False, repr(e))

check("normalise drops an entry with no machineURL",
      A.normalise({"title": "T"}, "repo") is None)
n = A.normalise({"machineURL": "m", "title": "T",
                 "download_metadata": {"Size": "not an int"}}, "repo")
check("a non-integer Size becomes None rather than propagating",
      n is not None and n["size"] is None, str(n))

# ---------------------------------------------------------------- 4.7
print("\n 4.7  guards that must stop a run before it touches anything")
box = Sandbox(BOX)
box.manifest("ListA", G, [("a1.7z", 100, "nexus")])

# wj_autoupdate SETS WJ_MANIFEST_DIR for its children; the guard against a
# bad value lives in the two scripts that READ it.
bogus = {"WJ_MANIFEST_DIR": os.path.join(box.tools, "does-not-exist")}
for script in ("wj_downloader.py",
               "wj_sorter.py"):
    rc, out = box.run(script, ["--game", G, "--list", "ListA", "--dry-run",
                               "--yes"], env_extra=bogus)
    check("%-34s refuses a bad WJ_MANIFEST_DIR" % script.split("_")[-1], rc == 2,
          "exit %d" % rc)
    check("  and names the variable rather than guessing",
          "WJ_MANIFEST_DIR" in out)

# GAME_FOLDERS disagreeing between the two scripts. Both now take it from
# wj_config.py, so the way it can still go wrong is a local override added
# back into one of them - which is exactly what the check exists to catch.
sp = os.path.join(box.tools, "wj_sorter.py")
s = io.open(sp, encoding="utf-8").read()
anchor = "MANIFEST_SUBDIR = CFG.MANIFEST_SUBDIR"
assert anchor in s, "config import block not found in the sorter"
override = ("GAME_FOLDERS = dict(GAME_FOLDERS, "
            "SkyrimSpecialEdition=" + chr(34) + "SkyrimSE" + chr(34) + ")")
io.open(sp, "w", encoding="utf-8", newline="").write(
    s.replace(anchor, anchor + chr(10) + override, 1))
rc, out = box.run("wj_autoupdate.py", ["--check", "--nexus-mode", "skip"])
check("mismatched GAME_FOLDERS stops the run", rc == 2, "exit %d" % rc)
check("  and names the setting", "GAME_FOLDERS" in out)
shutil.copy2(os.path.join(REPO, "wj_sorter.py"), sp)

# xxhash missing -> refuse rather than silently weaken verification.
# An interpreter without xxhash: another one on the machine if there is one,
# else this one started with -S, which leaves site-packages - and so xxhash -
# off the path. The scripts need nothing else from there, so that is the same
# machine-without-xxhash the guard exists for, and the check no longer
# depends on what happens to be installed.
nohash = None
for cand in ("py", "python"):
    try:
        r = subprocess.run([cand, "-c", "import xxhash"], capture_output=True,
                           timeout=60)
        if r.returncode != 0:
            nohash = [cand]
            break
    except Exception:
        continue
if nohash is None:
    nohash = [sys.executable, "-S"]
proc = subprocess.run(nohash + [os.path.join(box.tools, "wj_autoupdate.py"),
                                "--check", "--nexus-mode", "skip"],
                      cwd=box.tools, capture_output=True, text=True,
                      errors="replace", timeout=300,
                      env=dict(os.environ, WJ_MANIFEST_DIR=box.manifests),
                      stdin=subprocess.DEVNULL)
o = proc.stdout + proc.stderr
check("without xxhash the run refuses to start (exit 2)",
      proc.returncode == 2, "exit %d" % proc.returncode)
check("  and explains why", "xxhash" in o)
proc = subprocess.run(nohash + [os.path.join(box.tools, "wj_autoupdate.py"),
                                "--check", "--nexus-mode", "skip",
                                "--allow-size-only"],
                      cwd=box.tools, capture_output=True, text=True,
                      errors="replace", timeout=300,
                      env=dict(os.environ, WJ_MANIFEST_DIR=box.manifests),
                      stdin=subprocess.DEVNULL)
check("  --allow-size-only is a deliberate way past it",
      proc.returncode != 2, "exit %d" % proc.returncode)

# ---------------------------------------------------------------- 4.8
print("\n 4.8  what an older version left beside the scripts moves into runtime")
import wj_paths as WP
box = Sandbox(BOX)
old = {"wj_autoupdate.log": b"old log\n", "wj_autoupdate_state.json": b'{"lists": {}}',
       "deleted_Skyrim_20260924_210617.csv": b"archive,size_bytes\n",
       "wj_sorter_20260924_210445.log": b"sorter\n", "wj_autoupdate_sorter.out": b"out\n",
       ".nxm_prev_handler": b"", ".wj_autoupdate.lock": b"1234 then\n"}
for name, data in old.items():
    io.open(os.path.join(box.tools, name), "wb").write(data)
os.makedirs(box.runtime)
io.open(os.path.join(box.runtime, "wj_overview.json"), "w", encoding="utf-8").write("{}")
io.open(os.path.join(box.tools, "wj_overview.json"), "w", encoding="utf-8").write('{"old": 1}')
io.open(os.path.join(box.tools, "notes.csv"), "w", encoding="utf-8").write("mine")
moved = WP.adopt_runtime(box.tools)
check("every runtime file is moved", sorted(moved) == sorted(old), str(sorted(moved)))
check("  and arrives intact", all(
    io.open(os.path.join(box.runtime, n), "rb").read() == d for n, d in old.items()))
check("  none of them is left beside the scripts",
      not any(os.path.exists(os.path.join(box.tools, n)) for n in old))
check("one already in the runtime folder is not overwritten, and the old copy is kept",
      io.open(os.path.join(box.runtime, "wj_overview.json"), encoding="utf-8").read() == "{}"
      and os.path.isfile(os.path.join(box.tools, "wj_overview.json")))
check("anything else beside the scripts is left alone",
      os.path.isfile(os.path.join(box.tools, "notes.csv"))
      and os.path.isfile(os.path.join(box.tools, "wj_config.py")))
check("a second pass moves nothing", WP.adopt_runtime(box.tools) == [])
io.open(os.path.join(box.tools, "wj_downloader_20260101_000000.log"), "w").write("x")
rc, out = box.run("wj_sorter.py", ["--game", G, "--yes", "--dry-run"])
check("a tool moves them itself as it starts",
      "wj_downloader_20260101_000000.log" in box.runtime_files()
      and not os.path.exists(os.path.join(box.tools, "wj_downloader_20260101_000000.log")),
      str(box.runtime_files()))
check("  and writes its own log in the runtime folder",
      len([n for n in box.runtime_files() if n.startswith("wj_sorter_")]) == 2,
      str(box.runtime_files()))

sys.exit(summary())
