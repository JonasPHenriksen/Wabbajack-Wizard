"""The rules that keep data safe, checked in the source itself.

The other suites check what the code does in the cases someone thought of.
This one checks where it CAN do harm, so a change that adds a new way to
lose a file fails here before any case for it is written:

  * every call that deletes or overwrites a file - os.remove, os.replace,
    shutil.rmtree and the rest - is in a function listed below, with what it
    is allowed to destroy and why that is safe. A new one fails the suite
    until it is reviewed against the never-overwrite rule (wj_paths) and
    listed; a listed one that is gone fails it too, so the list stays true;
  * nothing fetches except through wj_paths.urlopen, whose opener knows no
    scheme but http and https and leaves credentials behind on a redirect;
  * the primitives that put a file in place without losing the one there -
    unique_path, move_file, move_folder, set_aside, publish - have one
    implementation, in wj_paths;
  * the launchers stay what cmd.exe needs, and the version is the one the
    changelog says.

Nothing here runs the code under test; it is read, never imported.
"""
import ast
import io
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import check, summary

print("=" * 70)
print("  T19  WHERE THE CODE CAN DO HARM")
print("=" * 70)

MODULES = sorted(n[:-3] for n in os.listdir(REPO) if n.startswith("wj_") and n.endswith(".py"))

# The calls that delete, or replace what is at their destination.
DESTRUCTIVE = {"os.remove", "os.unlink", "os.rmdir", "os.removedirs", "os.replace", "os.rename",
               "os.truncate", "shutil.rmtree", "shutil.move", "shutil.copy", "shutil.copy2",
               "shutil.copyfile", "shutil.copytree"}

# (module, function) -> (the calls it may make, what they destroy and why that is safe)
ALLOWED = {
    # -- the primitives everything else uses -----------------------------------
    ("wj_paths", "move_file"): ({"os.rename", "shutil.copy2", "os.remove"},
        "refuses a taken destination; across volumes copies to its own temporary name, "
        "renames that into place, and removes the temporary copy or, once in place, the source"),
    ("wj_paths", "move_folder"): ({"os.rename"}, "a rename, refused onto a folder already there"),
    ("wj_paths", "set_aside"): ({"os.rename"}, "a file and its sidecar to a free '~N' name beside"),
    ("wj_paths", "publish"): ({"os.replace"},
        "a verified .part into place, after set_aside() moved any other file out of the way; "
        "replaces only a file that same() says holds these very bytes"),
    ("wj_paths", "adopt_runtime"): ({"os.replace"},
        "an older version's runtime file into runtime/, only where nothing has that name"),
    ("wj_cdn", "_publish"): ({"os.rename", "os.replace"},
        "wj_paths.publish, inline for wj_cdn used on its own: the same never-overwrite rule"),
    # -- a tool's own files: written whole, or spent ----------------------------
    ("wj_autoupdate", "save_state"): ({"os.replace"}, "the state file, from its own .tmp"),
    ("wj_autoupdate", "write_nexus_todo"): ({"os.remove"}, "its own to-do CSV, once nothing is left to do"),
    ("wj_autoupdate", "run_script"): ({"os.remove"}, "the stop file it made for the child"),
    ("wj_config", "_save_settings"): ({"os.replace", "os.remove"},
        "wj_settings.json from this writer's own temporary file, or that file when it failed"),
    ("wj_downloader", "prune_old_logs"): ({"os.remove"}, "its own older logs"),
    ("wj_downloader", "load_light_records"): ({"os.replace"}, "the manifest cache, from its .tmp"),
    ("wj_downloader", "save_speeds"): ({"os.replace"}, "the measured speeds, from their .tmp"),
    ("wj_downloader", "_forget_backup"): ({"os.remove"}, "the nxm:// handler backup, once restored"),
    ("wj_downloader", "_write_small"): ({"f.truncate"},
        "its own nxm:// backup, --register-nxm note or port file, cut to what was just written"),
    ("wj_downloader", "NxmListener.close"): ({"os.remove"}, "the listener's own port file"),
    ("wj_downloader", "watch_stop_file.watch"): ({"os.remove"}, "the stop file, once read"),
    ("wj_sorter", "watch_stop_file.watch"): ({"os.remove"}, "the stop file, once read"),
    ("wj_gallery", "_write_cache"): ({"os.replace"}, "a gallery cache file, from its .tmp"),
    ("wj_gui", "save_overview"): ({"os.replace"}, "the Start tab's overview, from its .tmp"),
    ("wj_wizard", "save_known_hashes"): ({"os.replace"},
        "the hashes staging has read, from its .tmp"),
    ("wj_sorter", "prune_old_logs"): ({"os.remove"}, "its own older logs"),
    ("wj_merge_pool", "Record.close"): ({"os.remove"}, "its own record, when nothing was recorded"),
    ("wj_wizard", "DeletionRecord.close"): ({"os.remove"}, "its own record, when nothing was recorded"),
    # -- partial downloads, which are a tool's own until they are verified -------
    ("wj_downloader", "_stream_download_once"): ({"os.remove"},
        "its own .part: past the end of the file, resumed from the wrong place, or larger "
        "than the manifest says the file is"),
    ("wj_downloader", "download_one"): ({"os.remove"},
        "a .part that failed verification; a CDN download that failed the manifest's check, "
        "which wj_cdn published only after setting aside anything with its name"),
    ("wj_cdn", "_drop_sidecar"): ({"os.remove"}, "its own .parts.json resume record"),
    ("wj_cdn", "download"): ({"os.remove", "fx.truncate"},
        "its own .part: trimmed to the definition's size, or dropped when its hash is wrong"),
    ("wj_wabbajack", "_discard"): ({"os.remove", "shutil.rmtree"},
        "a Wabbajack update's own '<version>.zip.part' and '<version>.unpacking' folder, "
        "names no version folder has; the new version is renamed out of it first"),
    # -- manifests, which the feeds can always supply again ----------------------
    ("wj_autoupdate", "fetch_manifest"): ({"os.remove", "os.replace"},
        "its own staging copy and quarantine; the verified new manifest into place only "
        "once backup_existing() has kept the old one"),
    ("wj_autoupdate", "backup_existing"): ({"os.replace"},
        "the previous .bak of a manifest, once the manifest superseding it is safely kept"),
    ("wj_autoupdate", "prune_backups"): ({"os.remove"},
        ".bak and .rejected manifests older than KEEP_BACKUP_DAYS"),
    ("wj_wizard", "delete_modlist"): ({"os.remove", "os.rmdir"},
        "the deleted list's manifest and its sidecars, and its folder once emptied into Master"),
    # -- what the person asked to be deleted ------------------------------------
    ("wj_autoupdate", "purge_trash"): ({"os.remove"},
        "files a deletion record puts in the trash, TRASH_KEEP_DAYS after the record says"),
    ("wj_wizard", "remove_leftovers"): ({"os.remove"},
        "OS metadata and orphaned sidecars, plain files only, after a confirmed yes"),
    ("wj_wizard", "prune_stale_csvs"): ({"os.remove"}, "to-do CSVs for lists complete or gone"),
    ("wj_wabbajack", "prune_old_versions"): ({"shutil.rmtree"},
        "Wabbajack version folders older than a whole newest one, never while it may run"),
    ("wj_merge_pool", "drop_duplicates"): ({"os.remove"},
        "a copy whose xxHash64 matches the one the destination pool keeps, and its sidecar"),
    ("wj_merge_pool", "remove_if_empty"): ({"os.remove", "os.rmdir"},
        "OS litter in a folder the merge emptied, and the folder"),
    ("wj_keystore", "move_file_in"): ({"os.remove"},
        "nexus_api_key.txt, once Credential Manager reads the same key back"),
}


def dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


class Calls(ast.NodeVisitor):
    """Every call in a module, with the qualified name of the function it is in."""

    def __init__(self):
        self.stack, self.calls, self.defs = [], [], []

    def _scope(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node):
        self.defs.append(".".join(self.stack + [node.name]))
        self._scope(node)

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = _scope

    def visit_Call(self, node):
        self.calls.append((".".join(self.stack) or "<module>", dotted(node.func), node.lineno))
        self.generic_visit(node)


SOURCE, PARSED = {}, {}
for mod in MODULES:
    with io.open(os.path.join(REPO, mod + ".py"), encoding="utf-8") as f:
        SOURCE[mod] = f.read()
    v = Calls()
    v.visit(ast.parse(SOURCE[mod]))
    PARSED[mod] = v

# ---------------------------------------------------------------- 19.1
print("\n 19.1  every call that deletes or overwrites is one that was reviewed")
found = {}
for mod, v in PARSED.items():
    for func, name, line in v.calls:
        if name in DESTRUCTIVE or (name or "").endswith((".unlink", ".rmdir", ".truncate")):
            found.setdefault((mod, func), {}).setdefault(name, line)
unreviewed = [f"{mod}.py:{line} {func}() calls {name}"
              for (mod, func), names in sorted(found.items())
              for name, line in sorted(names.items(), key=lambda x: x[1])
              if name not in ALLOWED.get((mod, func), (set(), ""))[0]]
check("no call deletes or overwrites a file anywhere it has not been reviewed",
      not unreviewed, "\n        ".join([""] + unreviewed))
stale = [f"{mod}.{func}: {sorted(set(allowed) - set(found.get((mod, func), {})))}"
         for (mod, func), (allowed, _why) in sorted(ALLOWED.items())
         if set(allowed) - set(found.get((mod, func), {}))]
check("  and every reviewed one is still there, so the list says what the code does",
      not stale, "\n        ".join([""] + stale))
check("  each with what it destroys and why that is safe",
      all(why.strip() for _calls, why in ALLOWED.values()))
check("no module moves anything with shutil.move, which overwrites on one volume and "
      "copies-then-deletes across two", not any(
          name == "shutil.move" for v in PARSED.values() for _f, name, _l in v.calls))

# ---------------------------------------------------------------- 19.2
print("\n 19.2  nothing fetches but through the web-only opener")
direct = [f"{mod}.py:{line} {func}()" for mod, v in PARSED.items()
          for func, name, line in v.calls
          if name in ("urllib.request.urlopen", "urlopen", "urllib.request.build_opener",
                      "build_opener", "urllib.request.OpenerDirector")
          and (mod, func) not in {("wj_cdn", "_urlopen"), ("wj_paths", "_web_opener")}]
check("urllib's own opener is used nowhere but wj_cdn's stand-alone fallback",
      not direct, "\n        ".join([""] + direct))
fetchers = [mod for mod in MODULES if "urlopen(" in SOURCE[mod] and mod != "wj_paths"]
check("  and every module that fetches calls wj_paths.urlopen, or wj_cdn's alias of it",
      all("wj_paths.urlopen(" in SOURCE[m] or "_urlopen(" in SOURCE[m] for m in fetchers),
      str(fetchers))
check("  whose opener has no handler for file:, ftp: or data:",
      not re.search(r"\b(FileHandler|FTPHandler|DataHandler|CacheFTPHandler)\b", SOURCE["wj_paths"]))

# ---------------------------------------------------------------- 19.3
print("\n 19.3  one implementation of putting a file in place")
for prim in ("unique_path", "move_file", "move_folder", "set_aside", "publish"):
    where = [mod for mod, v in PARSED.items() if prim in v.defs]
    check("%s is defined only in wj_paths" % prim, where == ["wj_paths"], str(where))
aliases = [mod for mod in MODULES if re.search(r"^unique_path = wj_paths\.unique_path", SOURCE[mod], re.M)]
check("  the modules that name unique_path take wj_paths' own",
      all(not re.search(r"^def unique_path", SOURCE[m], re.M) for m in MODULES if m != "wj_paths")
      and set(aliases) >= {"wj_sorter", "wj_wizard"}, str(aliases))

# ---------------------------------------------------------------- 19.4
print("\n 19.4  the launchers stay what cmd.exe needs")
for bat in ("run.bat", "setup_env.bat"):
    with io.open(os.path.join(REPO, bat), "rb") as f:
        raw = f.read()
    lone_lf = len(re.findall(rb"(?<!\r)\n", raw))
    check("%s has Windows line ends throughout" % bat, lone_lf == 0, "%d bare LF" % lone_lf)
    check("  and is plain ASCII", all(b < 128 for b in raw))
    text = raw.decode("ascii", "replace").lower()
    check("  keeps its variables to itself (setlocal) and hands back an exit code (exit /b)",
          "setlocal" in text and "exit /b" in text)
with io.open(os.path.join(REPO, "run.bat"), encoding="ascii", errors="replace") as f:
    run_bat = f.read().lower()
check("run.bat pauses only when someone could press a key, and WJ_NO_PAUSE is not set",
      "wj_no_pause" in run_bat and "pause" in run_bat)

# ---------------------------------------------------------------- 19.5
print("\n 19.5  the version is the one the changelog's top entry gives")
m = re.search(r'^__version__ = "([^"]+)"', SOURCE["wj_config"], re.M)
with io.open(os.path.join(REPO, "CHANGELOG.md"), encoding="utf-8") as f:
    top = next((line for line in f if line.startswith("## ")), "")
check("wj_config.__version__ and CHANGELOG.md agree, as release.py requires",
      bool(m) and top.startswith("## %s " % m.group(1)), "%s vs %r" % (m and m.group(1), top.strip()))

sys.exit(summary())
