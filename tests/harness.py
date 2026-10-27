"""Sandbox harness: run the real scripts against a synthetic archive tree.

ARCHIVE_ROOT is "../Mod Archive" resolved against the SCRIPT's folder, so
copying the scripts into <sandbox>/tools/ makes them operate on
<sandbox>/Mod Archive/ with no edits to the code under test.
"""
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone

REAL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Neither a suite nor anything it runs reaches the Nexus key this Windows
# account keeps in Credential Manager: a suite that wants a store puts a
# stand-in in wj_keystore.STORE. Nor the nxm:// handler this account has
# registered: the start-up that hands back a handler a killed run left
# behind (wj_downloader.recover_nxm_handler) stays out of the registry.
os.environ["WJ_NO_CREDENTIAL_STORE"] = "1"
os.environ["WJ_NO_REGISTRY"] = "1"
# Nor do they read the published feeds: the feed registry is one nobody
# serves, unless a suite serves its own (WJ_FEED_REGISTRY, see wj_autoupdate).
# A run that reads the feeds then finds none, at once, rather than asking GitHub.
NO_FEEDS = "http://127.0.0.1:9/no-feeds-in-the-test-suites/repositories.json"
os.environ["WJ_FEED_REGISTRY"] = NO_FEEDS
# The interpreter the suites run the scripts with: the one kept inside the
# project when there is one, else the environment setup_env.bat made.
PY = os.path.join(REAL, ".python", "python.exe")
if not os.path.isfile(PY):
    PY = os.path.join(REAL, ".venv-win", "Scripts", "python.exe")
SCRIPTS = ("wj_autoupdate.py", "wj_wizard.py", "wj_config.py",
           "wj_downloader.py", "wj_paths.py", "wj_assistwin.py",
           "wj_sorter.py", "wj_cdn.py", "wj_keystore.py",
           "wj_wabbajack.py", "wj_gallery.py", "wj_merge_pool.py")


class Sandbox:
    def __init__(self, root):
        self.root = root
        self.tools = os.path.join(root, "tools")
        self.manifests = os.path.join(self.tools, "modlists")
        # Where the scripts write their logs, state and records as they run.
        self.runtime = os.path.join(self.tools, "runtime")
        self.archives = os.path.join(root, "Mod Archive")
        shutil.rmtree(root, ignore_errors=True)
        os.makedirs(self.manifests)
        os.makedirs(self.archives)
        for s in SCRIPTS:
            shutil.copy2(os.path.join(REAL, s), os.path.join(self.tools, s))

    def runtime_files(self):
        """Sorted names in the runtime folder; [] before anything wrote one."""
        try:
            return sorted(os.listdir(self.runtime))
        except OSError:
            return []

    # -- fixture building --------------------------------------------
    def manifest(self, name, game, archives, corrupt=None):
        """archives: [(filename, size, kind)] ; kind -> State $type. An entry
        may carry two more, (filename, size, kind, url, hash): where an http
        archive is fetched from - a suite's own localhost server - and the
        hash it must have. Without one, the hash is that of what pool_file()
        writes by default, so a pool built of those holds the bytes its
        manifests pin, as a healthy one does, and staging reads it as such."""
        path = os.path.join(self.manifests, name + ".wabbajack")
        if corrupt == "notzip":
            io.open(path, "wb").write(b"this is not a zip file at all")
            return path
        kinds = {
            "nexus": "NexusDownloader, Wabbajack.Lib",
            "http": "HttpDownloader, Wabbajack.Lib",
            "gamefile": "GameFileSourceDownloader, Wabbajack.Lib",
            "manual": "ManualDownloader, Wabbajack.Lib",
        }
        entries = []
        for i, entry in enumerate(archives):
            fn, size, kind = entry[:3]
            url, hsh = (tuple(entry[3:5]) + (None, None))[:2]
            entries.append({
                "Name": fn, "Size": size,
                "Hash": hsh or (wj_hash(b"x" * size) if isinstance(size, int) and size >= 0
                                else "AAAAAAAAAAA="),
                "Meta": "[General]\ngameName=fallout4\nmodID=%d\nfileID=%d\n"
                        % (100 + i, 900 + i),
                "State": {"$type": kinds[kind], "GameName": "Fallout4",
                          "ModID": 100 + i, "FileID": 900 + i,
                          "Url": url or "https://example.invalid/%s" % fn},
            })
        doc = {"Archives": entries}
        if game:
            doc["GameType"] = game
        body = json.dumps(doc)
        if corrupt == "badjson":
            body = body[:len(body) // 2]
        with zipfile.ZipFile(path, "w") as z:
            if corrupt == "nomodlist":
                z.writestr("something-else", body)
            else:
                z.writestr("modlist", body)
        return path

    def pool_file(self, game, folder, name, size, data=None):
        """A file of `size` bytes in a pool folder - or holding `data`, for a
        suite that needs particular bytes, a build with a known hash."""
        d = os.path.join(self.archives, game, folder)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        io.open(p, "wb").write(b"x" * size if data is None else data)
        return p

    def trash_record(self, game_folder, name, size, days_ago=0.0, trash="Trash"):
        """Record a file as sent to <game_folder>/<trash> by a stage 5c
        cleanup, `days_ago` days ago: the deletion record the purge and the
        undo go by. A file in a trash folder with no record is one no tool
        deletes, so a suite that puts one there by hand, standing in for an
        earlier cleanup, writes its record too. Returns the record's path."""
        os.makedirs(self.runtime, exist_ok=True)
        when = datetime.now(timezone.utc) - timedelta(days=days_ago)
        stamp = when.astimezone().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.runtime, f"deleted_{game_folder}_{stamp}.csv")
        new = not os.path.exists(path)
        with io.open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["archive", "size_bytes", "reason", "trashed_to", "master",
                            "rule", "trashed_at"])
            w.writerow([name, size, "recorded by a test",
                        os.path.join(self.archives, game_folder, trash, name),
                        os.path.join(self.archives, game_folder, "Master"),
                        "your modlists only", when.isoformat(timespec="seconds")])
        return path

    # -- running the code under test ---------------------------------
    def run(self, script, args, expect=None, env_extra=None):
        # No Nexus key reaches a sandboxed script from this machine: not the
        # environment's, and not the one this account keeps in Windows
        # Credential Manager (see wj_keystore).
        env = dict(os.environ, PYTHONUNBUFFERED="1",
                   WJ_MANIFEST_DIR=self.manifests, WJ_NO_CREDENTIAL_STORE="1",
                   WJ_NO_REGISTRY="1", WJ_FEED_REGISTRY=NO_FEEDS)
        env.pop("NEXUS_API_KEY", None)
        if env_extra:
            env.update(env_extra)
        proc = subprocess.run([PY, os.path.join(self.tools, script)] + args,
                              cwd=self.tools, env=env, capture_output=True,
                              text=True, errors="replace", timeout=600,
                              stdin=subprocess.DEVNULL)
        out = proc.stdout + proc.stderr
        if expect is not None and proc.returncode != expect:
            print("      exit %d (expected %d)" % (proc.returncode, expect))
            print("      --- output tail ---")
            for line in out.strip().split("\n")[-15:]:
                print("      " + line)
        return proc.returncode, out

    def tree(self):
        """{game: {folder: {name: size}}} of the whole synthetic pool."""
        found = {}
        for game in sorted(os.listdir(self.archives)):
            gp = os.path.join(self.archives, game)
            if not os.path.isdir(gp):
                continue
            found[game] = {}
            for folder in sorted(os.listdir(gp)):
                fp = os.path.join(gp, folder)
                if not os.path.isdir(fp):
                    continue
                found[game][folder] = {
                    n: os.path.getsize(os.path.join(fp, n))
                    for n in sorted(os.listdir(fp))
                    if os.path.isfile(os.path.join(fp, n))}
        return found

    def files(self):
        """Flat {(game, folder, name): size}. Ignores empty directories, so a
        run that merely ensures its pool folders exist does not read as a
        change to the data."""
        out = {}
        for game, folders in self.tree().items():
            for folder, names in folders.items():
                for n, sz in names.items():
                    out[(game, folder, n)] = sz
        return out

    def where(self, name):
        """Every folder holding a file called `name`, with its size."""
        hits = []
        for game, folders in self.tree().items():
            for folder, files in folders.items():
                if name in files:
                    hits.append((folder, files[name]))
        return sorted(hits)


def wj_hash(data):
    """The hash Wabbajack records for `data`: base64 of its xxHash64, little
    endian - what a manifest's Hash holds and the tools check."""
    import base64
    import struct
    import xxhash
    h = xxhash.xxh64()
    h.update(data)
    return base64.b64encode(struct.pack("<Q", h.intdigest())).decode("ascii")


def quiet_logging(A):
    """wj_autoupdate's logging, for a suite that imports it from the checkout.

    setup_logging() opens runtime/wj_autoupdate.log under A.HERE, and for an
    import from the checkout that is the real project: the suites used to
    append their warnings - an unreadable test manifest, a stray test folder
    - to the real log, the downloader's included. This opens it in a scratch
    folder that run_all removes instead, and puts HERE back."""
    import logging
    saved = A.HERE
    A.HERE = os.path.join(os.environ.get("TEMP", "/tmp"), "wj_tests", "logs")
    try:
        A.setup_logging(False)
    finally:
        A.HERE = saved
    A.LOG.setLevel(logging.CRITICAL)


PASS = []
FAIL = []


def check(label, ok, detail=""):
    (PASS if ok else FAIL).append(label)
    print("   %s %s%s" % ("PASS" if ok else "FAIL", label,
                          ("   " + detail) if detail and not ok else ""))


def summary():
    print()
    print("=" * 70)
    print("  %d passed, %d failed" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("    FAILED: " + f)
    print("=" * 70)
    return 1 if FAIL else 0
