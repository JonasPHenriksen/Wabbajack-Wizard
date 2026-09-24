#!/usr/bin/env python3
r"""Run every sandbox suite and report a combined total.

    .python\python.exe tests\run_all.py

(or .venv-win\Scripts\python.exe where a machine-wide Python made that
environment). Each suite runs the scripts with the same interpreter that runs
it - see harness.PY.

Nothing here touches the live archive pools. ARCHIVE_ROOT is "../Mod Archive"
resolved against the SCRIPT folder, so each suite copies the scripts into a
temporary directory and they operate on a temporary pool instead. The two HTTP
suites serve their own localhost server, so no suite needs the internet except
where it is explicitly checking a real feed.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SUITES = [
    ("t1_sorter.py", "sorter: aborts, collisions, reserved names, dry-run"),
    ("t2_download.py", "downloads: hard vs transient failures, resume, verify"),
    ("t3_cdn.py", "CDN multi-part: broken definitions, bad parts, resume"),
    ("t4_updater.py", "updater: state recovery, matching, start-up guards"),
    ("t5_wizard.py", "wizard: empty data, missing folders, odd names"),
    ("t6_planning.py", "planning, manifest cache, backup pruning"),
    ("t7_identity.py", "one filename, several builds"),
    ("t8_order.py", "download order: smallest, largest, manifest"),
    ("t9_wabbajack.py", "handing a staged list to Wabbajack"),
    ("t10_lookup.py", "where a file may live and count as present"),
    ("t11_gallery.py", "the published gallery, and what you already own"),
    ("t12_cleanup.py", "deleting what nothing wants; folders that are not ours"),
    ("t13_delete.py", "stage 5 end to end: what a deletion must never take"),
    ("t14_merge.py", "folding one game folder into another"),
    ("t15_gui.py", "the desktop window, driven through its own buttons"),
    ("t16_pool.py", "laying out a pool for a game you keep nothing for"),
    ("t17_collection.py", "Nexus collections as lists, and the collections2mo2 hand-over"),
    ("t18_mo2.py", "the Wizard's own MO2 build: patches, Stardew layout, SMAPI, order"),
]

total_pass = total_fail = 0
failed_suites = []
for script, blurb in SUITES:
    print()
    print("#" * 72)
    print("#  %s  -  %s" % (script, blurb))
    print("#" * 72)
    # stdin closed: a suite that reaches an input() prompt by mistake must
    # fail at once, not wait forever on a stdin the runner inherited.
    proc = subprocess.run([sys.executable, os.path.join(HERE, script)],
                          capture_output=True, text=True, errors="replace",
                          stdin=subprocess.DEVNULL)
    out = proc.stdout + proc.stderr
    print(out.rstrip())
    for line in out.split("\n"):
        if "passed," in line and "failed" in line:
            try:
                bits = line.split()
                total_pass += int(bits[0])
                total_fail += int(bits[2])
            except (ValueError, IndexError):
                pass
    if proc.returncode != 0:
        failed_suites.append(script)

print()
print("=" * 72)
print("  TOTAL: %d passed, %d failed, across %d suites"
      % (total_pass, total_fail, len(SUITES)))
if failed_suites:
    print("  suites with failures: " + ", ".join(failed_suites))
print("=" * 72)
sys.exit(1 if failed_suites else 0)
