"""Sandbox harness: run the real scripts against a synthetic archive tree.

ARCHIVE_ROOT is "../Mod Archive" resolved against the SCRIPT's folder, so
copying the scripts into <sandbox>/tools/ makes them operate on
<sandbox>/Mod Archive/ with no edits to the code under test.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile

REAL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The interpreter the suites run the scripts with: the one kept inside the
# project when there is one, else the environment setup_env.bat made.
PY = os.path.join(REAL, ".python", "python.exe")
if not os.path.isfile(PY):
    PY = os.path.join(REAL, ".venv-win", "Scripts", "python.exe")
SCRIPTS = ("wj_autoupdate.py", "wj_wizard.py", "wj_config.py",
           "wj_downloader.py", "wj_paths.py", "wj_assistwin.py",
           "wj_sorter.py", "wj_cdn.py",
           "wj_wabbajack.py", "wj_gallery.py", "wj_merge_pool.py")


class Sandbox:
    def __init__(self, root):
        self.root = root
        self.tools = os.path.join(root, "tools")
        self.manifests = os.path.join(self.tools, "modlists")
        self.archives = os.path.join(root, "Mod Archive")
        shutil.rmtree(root, ignore_errors=True)
        os.makedirs(self.manifests)
        os.makedirs(self.archives)
        for s in SCRIPTS:
            shutil.copy2(os.path.join(REAL, s), os.path.join(self.tools, s))

    # -- fixture building --------------------------------------------
    def manifest(self, name, game, archives, corrupt=None):
        """archives: [(filename, size, kind)] ; kind -> State $type"""
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
        for i, (fn, size, kind) in enumerate(archives):
            entries.append({
                "Name": fn, "Size": size, "Hash": "AAAAAAAAAAA=",
                "Meta": "[General]\ngameName=fallout4\nmodID=%d\nfileID=%d\n"
                        % (100 + i, 900 + i),
                "State": {"$type": kinds[kind], "GameName": "Fallout4",
                          "ModID": 100 + i, "FileID": 900 + i,
                          "Url": "https://example.invalid/%s" % fn},
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

    def pool_file(self, game, folder, name, size):
        d = os.path.join(self.archives, game, folder)
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, name)
        io.open(p, "wb").write(b"x" * size)
        return p

    # -- running the code under test ---------------------------------
    def run(self, script, args, expect=None, env_extra=None):
        env = dict(os.environ, PYTHONUNBUFFERED="1",
                   WJ_MANIFEST_DIR=self.manifests)
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
