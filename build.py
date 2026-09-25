#!/usr/bin/env python3
r"""
Build the Windows binaries: a zip for people with no Python
===========================================================

    python build.py            -> dist/WabbajackWizard/                (a folder to run from)
                                  dist/WabbajackWizard-v<version>-win64.zip

release.py packages the source for people who have Python. This packages
Python itself along with it, so the answer to "what do I need installed" is
nothing.

What comes out is a folder, not a single file, and WabbajackWizard.spec says
at length why. Two binaries sit in it beside an `_internal` folder they
share: the window, and its console twin. Everything the tool writes - your
settings and the manifests beside those two, the logs and the state file in
the runtime folder beside them - lands exactly where it lands beside the
scripts.

PyInstaller is a tool for building this, not a thing this needs to run, so it
is not in requirements.txt. With the interpreter kept inside the project it
simply goes into that, and nothing about the build then depends on where the
folder is:

    .python\python.exe -m pip install -r requirements-build.txt

With a machine-wide Python instead, a separate environment keeps it apart:

    python -m venv .venv-build
    .venv-build\Scripts\python -m pip install -r requirements-build.txt
"""

import io
import os
import re
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import wj_config as CFG
import wj_paths
import release

BUILD_VENV = os.path.join(HERE, ".venv-build")
SPEC = os.path.join(HERE, "WabbajackWizard.spec")
WORK = os.path.join(HERE, "build")
OUT = os.path.join(HERE, "dist", "WabbajackWizard")

# Plain files that belong beside the binaries rather than inside the bundle:
# the licence, the documentation, and the settings template to copy from.
COMPANIONS = ("README.md", "CHANGELOG.md", "LICENSE", "wj_settings.example.json")

# Written beside each binary as <name>.exe.config. The download window is
# Edge's WebView2 engine reached through .NET, and .NET Framework refuses to
# load assemblies from a network share - a mapped drive counts - unless the
# host program's config says otherwise. This tool lives on a share by design,
# so it says otherwise. The interpreter in .python\ carries the same file.
CLR_CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <runtime>
    <loadFromRemoteSources enabled="true"/>
  </runtime>
</configuration>
"""


# What a build must carry, by the module that proves it is there. PyInstaller
# only warns about a hidden import it cannot find and builds anyway, so a
# build environment without pywebview used to produce binaries that quietly
# fell back to the browser for every Nexus page.
BUNDLED = (("xxhash", "the real xxHash64 check on every download"),
           ("webview", "the download window assist mode shows Nexus pages in (pywebview)"),
           ("clr", "the .NET bridge the download window runs on (pythonnet)"))


def builder():
    """The interpreter that has PyInstaller, or None.

    The in-project interpreter first, when there is one, then the separate
    build environment. Whichever is found has to actually import PyInstaller:
    an interpreter that is there but lacks it is the same as none."""
    for py in (os.path.join(HERE, ".python", "python.exe"),
               os.path.join(BUILD_VENV, "Scripts", "python.exe"),
               os.path.join(BUILD_VENV, "bin", "python")):
        if not os.path.isfile(py):
            continue
        rc = subprocess.run([py, "-c", "import PyInstaller"],
                            capture_output=True).returncode
        if rc == 0:
            return py
    return None


def missing_from(py):
    """The BUNDLED modules `py` cannot import, as (module, what it is for)."""
    gone = []
    for mod, why in BUNDLED:
        if subprocess.run([py, "-c", f"import {mod}"], capture_output=True).returncode != 0:
            gone.append((mod, why))
    return gone


def main():
    version = CFG.__version__
    changelog = io.open(os.path.join(HERE, "CHANGELOG.md"), encoding="utf-8").read()
    entry = release.top_changelog_entry(changelog)
    if entry is None or not entry.startswith(f"## {version}"):
        # A warning, not a refusal: builds happen while you are working, and
        # the changelog is written when the work is done.
        print(f"[NOTE] wj_config.__version__ is {version} but CHANGELOG.md's top "
              f"entry is {(entry or '').splitlines()[0] if entry else 'missing'}. "
              f"The zip will be named for {version}.")

    py = builder()
    if py is None:
        print("[REFUSED] No interpreter with PyInstaller. It is needed to make the "
              "binaries, and is deliberately not in requirements.txt - nobody "
              "running this tool needs it. Install it:")
        print("    .python\\python.exe -m pip install -r requirements-build.txt")
        print("or, with a machine-wide Python, in an environment of its own:")
        print("    python -m venv .venv-build")
        print("    .venv-build\\Scripts\\python -m pip install -r requirements-build.txt")
        return 2
    gone = missing_from(py)
    if gone:
        print(f"[REFUSED] {py} has PyInstaller but not everything a build carries:")
        for mod, why in gone:
            print(f"    {mod:<8} {why}")
        print("Install them into the same interpreter:")
        print(f"    \"{py}\" -m pip install -r requirements-build.txt")
        return 2

    # A stale bundle is worse than no bundle: a renamed or deleted module
    # would otherwise survive in dist/ and ship.
    shutil.rmtree(WORK, ignore_errors=True)
    shutil.rmtree(OUT, ignore_errors=True)

    rc = subprocess.run([py, "-m", "PyInstaller", "--noconfirm", "--clean",
                         "--distpath", os.path.join(HERE, "dist"),
                         "--workpath", WORK, SPEC],
                        cwd=HERE).returncode
    if rc != 0:
        print(f"[REFUSED] PyInstaller exited {rc}.")
        return 2

    for exe in (wj_paths.GUI_EXE, wj_paths.CONSOLE_EXE):
        if not os.path.isfile(os.path.join(OUT, exe)):
            print(f"[REFUSED] {exe} is not in {OUT}.")
            return 2
    for name in COMPANIONS:
        shutil.copy2(os.path.join(HERE, name), os.path.join(OUT, name))
    for exe in (wj_paths.GUI_EXE, wj_paths.CONSOLE_EXE):
        with io.open(os.path.join(OUT, exe + ".config"), "w", encoding="utf-8",
                     newline="\n") as f:
            f.write(CLR_CONFIG)

    name = f"WabbajackWizard-v{version}-win64"
    zip_path = os.path.join(HERE, "dist", name + ".zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    members = []
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(OUT):
            for f in sorted(files):
                full = os.path.join(root, f)
                rel = os.path.relpath(full, OUT).replace(os.sep, "/")
                members.append(rel)
                z.write(full, f"{name}/{rel}")

    # Same check release.py makes, for the same reason: this is the one place
    # worth looking twice before something leaves the machine.
    bad = [m for m in members if release.forbidden(m)]
    if bad:
        os.remove(zip_path)
        print("[REFUSED] These must never ship, and were in the bundle:")
        for m in bad:
            print("   ", m)
        return 2

    print()
    print(f"{OUT}")
    w = max(len(wj_paths.GUI_EXE), len(wj_paths.CONSOLE_EXE))
    print(f"  {wj_paths.GUI_EXE:<{w}}  the window")
    print(f"  {wj_paths.CONSOLE_EXE:<{w}}  the console side, and every stage")
    print(f"{zip_path}")
    print(f"  {len(members)} file(s), {os.path.getsize(zip_path) / 2 ** 20:.1f} MB "
          f"zipped, {sum(os.path.getsize(os.path.join(OUT, m.replace('/', os.sep))) for m in members) / 2 ** 20:.1f} MB unpacked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
