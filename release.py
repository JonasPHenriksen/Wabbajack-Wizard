#!/usr/bin/env python3
"""
Build the release zip: exactly what git tracks, and nothing that is yours
=========================================================================

    python release.py          -> dist/WabbajackWizard-v<version>.zip
                                  dist/WabbajackWizard-v<version>.md   (release notes)

`git archive` is the packager on purpose. It takes the tracked files at HEAD
and nothing else, so the state file, the logs, the caches, the manifests, the
API key and your wj_settings.json can never ride along - they are all
untracked, and the zip is checked for them afterwards regardless. The version
comes from wj_config.py and must match the top entry of CHANGELOG.md, or this
refuses; the release notes are that entry, with the install steps appended.
"""

import io
import os
import re
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wj_config as CFG

# Anything matching these must never be in a release. Belt and braces: they
# are all untracked, but a release is the one place to check twice.
FORBIDDEN = ("wj_settings.json", "nexus_api_key", ".log", "wj_autoupdate_state",
             "wj_manifest_cache", ".gallery-cache/", "modlists/", "manifest_backups/",
             "deleted_", "purged_trash_", "merged_", ".venv", "__pycache__")
# Folders of ours that must never ship either - what the tools write as they
# run, and the download window's browser profile with its Nexus login - but
# matched only at the top of the tree: a bundled package may have a folder of
# the same name, and pythonnet has a runtime/.
FORBIDDEN_DIRS = ("runtime/", ".webview/")


def forbidden(member, prefix=""):
    """Must this archive member never ship? `prefix` is the folder every
    member of the archive sits in, if any."""
    rel = member[len(prefix):] if prefix and member.startswith(prefix) else member
    return any(f in member for f in FORBIDDEN) or rel.startswith(FORBIDDEN_DIRS)


def top_changelog_entry(text):
    """The first '## ...' section of CHANGELOG.md, header included."""
    starts = [m.start() for m in re.finditer(r"^## ", text, re.M)]
    if not starts:
        return None
    end = starts[1] if len(starts) > 1 else len(text)
    return text[starts[0]:end].rstrip() + "\n"


def main():
    version = CFG.__version__
    changelog = io.open(os.path.join(HERE, "CHANGELOG.md"), encoding="utf-8").read()
    entry = top_changelog_entry(changelog)
    if entry is None or not entry.startswith(f"## {version}"):
        print(f"[REFUSED] wj_config.__version__ is {version} but CHANGELOG.md's top entry "
              f"is {(entry or '').splitlines()[0] if entry else 'missing'}. Make them agree.")
        return 2
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=HERE,
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        print("[REFUSED] The working tree has uncommitted changes; a release is built "
              "from a commit:")
        print(dirty)
        return 2

    dist = os.path.join(HERE, "dist")
    os.makedirs(dist, exist_ok=True)
    name = f"WabbajackWizard-v{version}"
    zip_path = os.path.join(dist, name + ".zip")
    subprocess.check_call(["git", "archive", "--format=zip", f"--prefix={name}/",
                           "-o", zip_path, "HEAD"], cwd=HERE)

    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
    bad = [n for n in names if forbidden(n, f"{name}/")]
    if bad:
        os.remove(zip_path)
        print("[REFUSED] These must never ship, and were in the archive:")
        for n in bad:
            print("   ", n)
        return 2

    notes = entry + "\n### Install\n\n" + "\n".join([
        f"**WabbajackWizard-v{version}-win64.zip** - no Python needed. Unzip it",
        "anywhere and run `WabbajackWizard.exe` for the window, or",
        "`WabbajackWizard-console.exe --wizard` for the console. Windows warns about",
        "an unsigned download the first time: More info, then Run anyway.",
        "",
        f"**WabbajackWizard-v{version}.zip** - the source, for Python 3.8 or newer:",
        "",
        "1. Unzip anywhere; a mapped network share is fine.",
        "2. Run `setup_env.bat` once. It creates `.venv-win` and installs xxhash.",
        "3. Optional: copy `wj_settings.example.json` to `wj_settings.json` and change",
        "   what differs, or use the window's Settings tab.",
        "4. Run `run.bat` for the window, or `run.bat --wizard` for the console.",
        "",
        "Either way, everything the tool writes lands in the folder you started it",
        "from, so one can replace the other in place.",
    ]) + "\n"
    notes_path = os.path.join(dist, name + ".md")
    io.open(notes_path, "w", encoding="utf-8", newline="").write(notes)

    print(f"{zip_path}")
    print(f"  {len(names)} file(s), {os.path.getsize(zip_path) / 2 ** 20:.2f} MB")
    for n in sorted(names):
        print("   ", n)
    print(f"{notes_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
