#!/usr/bin/env python3
"""
Handing a staged modlist to Wabbajack
=====================================
Once a list is staged, everything Wabbajack needs is in one folder and the only
outstanding decisions are where to install and whether to start. This module
finds Wabbajack, checks it can actually do the job, and hands it the paths.

Wabbajack 4.x ships a CLI next to the GUI:

    <install>/<version>/cli/wabbajack-cli.exe install
        -w <the .wabbajack file>
        -d <the folder holding its archives>
        -o <where to install>

which is a better fit than driving the GUI: the two paths this tooling already
knows are passed directly instead of being typed in. The GUI remains an option
for anyone who wants its progress display, in which case the paths are printed
to paste.

The preflight matters more than the launch. Wabbajack cannot install a list for
a game it cannot find - a modlist's GameFile archives are copied out of the base
game install, not downloaded - so a list whose game is missing fails after you
have committed to it. Checking first turns that into a sentence beforehand.
"""

import csv
import io
import os
import re
import shutil
import subprocess

import wj_paths

try:
    import wj_config as CFG
except ImportError:                     # pragma: no cover - see wj_config.py
    CFG = None
try:
    import wj_gallery as G
except ImportError:                     # pragma: no cover
    G = None

# The processes that may be reading a downloads folder or an install: the
# GUI, and the CLI this tooling itself drives. Moving archives under a running
# install breaks it, so the wizard waits for these to be gone first.
WABBAJACK_PROCESSES = ("wabbajack.exe", "wabbajack-cli.exe")

# Where Wabbajack publishes itself. The release tag is the bare version,
# "4.2.3.0", the same string its install uses as the versioned folder name,
# so the two compare directly.
RELEASES_API = "https://api.github.com/repos/wabbajack-tools/wabbajack/releases/latest"
RELEASES_URL = "https://github.com/wabbajack-tools/wabbajack/releases"
RELEASE_TTL = 6 * 3600
RELEASE_CACHE = "wabbajack/latest_release.json"


def parse_tasklist(text):
    """[(pid, image)] for Wabbajack processes in `tasklist /FO CSV` output.

    Kept apart from the subprocess call so it can be checked without a
    running Wabbajack. tasklist prints one CSV row per process: image name,
    PID, session name, session number, memory."""
    found = []
    for row in csv.reader(io.StringIO(text or "")):
        if len(row) < 2:
            continue
        image = row[0].strip().strip('"')
        if image.lower() in WABBAJACK_PROCESSES:
            try:
                found.append((int(row[1].strip().strip('"')), image))
            except ValueError:
                continue
    return found


def running_processes():
    """Wabbajack processes running right now, as (pid, image). Empty when
    there are none, and also when tasklist itself is unavailable - which is
    reported as 'not running' because a check that cannot run must not stop
    every operation forever."""
    try:
        proc = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                              capture_output=True, text=True, errors="replace",
                              timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_tasklist(proc.stdout)


# Wabbajack's own game names, as `wabbajack-cli list-games` prints them. These
# are NOT the friendly labels in GAME_DISPLAY: Wabbajack writes "Fallout New
# Vegas" without the colon, and "Skyrim Legendary Edition" rather than
# "Skyrim (Legendary Edition)". Anything absent here is a game Wabbajack does
# not support - Dark Souls 3, for instance, is not in its list at all.
WABBAJACK_GAME_NAMES = {
    "SkyrimSpecialEdition":  "Skyrim Special Edition",
    "Skyrim":                "Skyrim Legendary Edition",
    "SkyrimVR":              "Skyrim VR",
    "Fallout4":              "Fallout 4",
    "Fallout4VR":            "Fallout 4 VR",
    "Fallout3":              "Fallout 3",
    "FalloutNewVegas":       "Fallout New Vegas",
    "Oblivion":              "Oblivion",
    "Morrowind":             "Morrowind",
    "Starfield":             "Starfield",
    "Enderal":               "Enderal",
    "EnderalSpecialEdition": "Enderal Special Edition",
    "BaldursGate3":          "Baldur's Gate 3",
    "Cyberpunk2077":         "Cyberpunk 2077",
    "StardewValley":         "Stardew Valley",
    "Witcher3":              "Witcher 3",
}


# Where a Wabbajack install is kept when it lives with this tooling: a folder
# of this name inside the tools folder. Gitignored; Wabbajack's launcher puts
# each version it downloads in a subfolder of it.
WABBAJACK_SUBDIR = "Wabbajack"


def find_wabbajack(hint=None):
    """Locate a Wabbajack install. Returns a dict, or None.

    Looks where this tooling is most likely to find one: an explicit setting,
    then inside the tools folder itself (so the whole arrangement moves as
    one), then a sibling of it, then the ordinary per-user install location.
    The CLI lives in a version-numbered subfolder, so the newest one is
    taken."""
    roots = []
    if hint:
        roots.append(hint)
    if CFG is not None and getattr(CFG, "WABBAJACK_DIR", None):
        roots.append(CFG.WABBAJACK_DIR)
    here = wj_paths.app_dir(__file__)
    roots.append(os.path.join(here, WABBAJACK_SUBDIR))
    roots.append(os.path.normpath(os.path.join(here, "..", WABBAJACK_SUBDIR)))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(os.path.join(local, "Wabbajack"))

    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        exe = os.path.join(root, "Wabbajack.exe")
        versions = []
        try:
            for name in os.listdir(root):
                cli = os.path.join(root, name, "cli", "wabbajack-cli.exe")
                if os.path.isfile(cli):
                    versions.append((version_key(name), name, cli))
        except OSError:
            pass
        if not versions and not os.path.isfile(exe):
            continue
        versions.sort()
        newest = versions[-1] if versions else None
        return {
            "dir": root,
            "exe": exe if os.path.isfile(exe) else None,
            "cli": newest[2] if newest else None,
            "version": newest[1] if newest else None,
        }
    return None


def version_key(name):
    """Sort 4.10 after 4.9 rather than before it."""
    return [int(n) for n in re.findall(r"\d+", name)] or [-1]


_VERSION_DIR = re.compile(r"^\d+(\.\d+)+$")


def is_version_dir(root, name):
    """Is `name` under `root` a Wabbajack version folder: named like one, and
    holding the program or its CLI? Wabbajack's launcher keeps every version
    it has ever downloaded in such a folder and runs the newest."""
    path = os.path.join(root, name)
    return bool(_VERSION_DIR.match(name)) and os.path.isdir(path) and (
        os.path.isfile(os.path.join(path, "Wabbajack.exe"))
        or os.path.isfile(os.path.join(path, "cli", "wabbajack-cli.exe")))


def prune_old_versions(wj, running=None):
    """Remove the version folders an update has superseded. Returns their
    names.

    Wabbajack's launcher downloads each new version into its own folder and
    leaves the old ones where they were, at some 700 MB each. Only folders
    that are version-named and hold the program go, only ones older than the
    newest, and never while a Wabbajack process is running: the launcher
    itself is one, and while it runs the newest folder may still be arriving.
    The newest folder has to be whole - program and CLI both - before anything
    older is judged superseded by it."""
    if not wj or not wj.get("version") or not wj.get("dir"):
        return []
    root, newest = wj["dir"], wj["version"]
    if not (os.path.isfile(os.path.join(root, newest, "Wabbajack.exe"))
            and os.path.isfile(os.path.join(root, newest, "cli", "wabbajack-cli.exe"))):
        return []
    if running is None:
        running = running_processes()
    if running:
        return []
    removed = []
    try:
        names = os.listdir(root)
    except OSError:
        return []
    for name in sorted(names):
        if name == newest or not is_version_dir(root, name):
            continue
        if version_key(name) >= version_key(newest):
            continue
        try:
            shutil.rmtree(os.path.join(root, name))
        except OSError:
            continue
        removed.append(name)
    return removed


def latest_release(force=False):
    """The newest Wabbajack release as {"version", "url", "published"}, or
    None when it cannot be learnt.

    Read from GitHub's release API and kept in the gallery cache beside the
    validation reports for six hours, so a session asks once and the answer
    survives the network being down. Unauthenticated, which allows sixty
    requests an hour; this makes at most one."""
    if G is None:
        return None
    try:
        data = G.fetch_json(RELEASE_CACHE, ttl=RELEASE_TTL, force=force, url=RELEASES_API)
    except Exception:              # the network, or JSON that is not a release
        return None
    if not isinstance(data, dict):
        return None
    tag = str(data.get("tag_name") or "").strip().lstrip("vV")
    if not tag:
        return None
    return {"version": tag, "url": str(data.get("html_url") or RELEASES_URL),
            "published": str(data.get("published_at") or "")}


def update_status(wj, latest=None):
    """How the installed Wabbajack stands against the newest release.

    `wj` is find_wabbajack()'s answer. Returns installed (the versioned
    folder's name, or None), latest (or None when it could not be learnt),
    outdated (True, False, or None for cannot tell), url, and text - one line
    that says it. Never raises. A lookup that fails is 'cannot tell', which
    neither stops an install nor blesses one: the check exists so that a
    modlist built for a newer Wabbajack is not installed on an older one by
    surprise, and a check that cannot run must say so rather than guess."""
    installed = (wj or {}).get("version")
    # No point asking GitHub about a version that is not there to compare.
    rel = latest if latest is not None else (latest_release() if installed else None)
    newest = rel.get("version") if rel else None
    url = (rel or {}).get("url") or RELEASES_URL
    outdated = (version_key(installed) < version_key(newest)) if installed and newest else None
    if not wj:
        text = "Wabbajack was not found"
    elif not installed:
        text = "Wabbajack is installed, but no versioned folder with a CLI was found in it"
    elif outdated is None:
        text = f"Wabbajack {installed} installed; the newest release could not be looked up"
    elif outdated:
        text = (f"Wabbajack {installed} installed; {newest} is the newest release, "
                f"so an update is available")
    else:
        text = f"Wabbajack {installed} installed, the newest release"
    return {"installed": installed, "latest": newest, "outdated": outdated, "url": url,
            "text": text}


_GAMES_CACHE = {}


def detected_games(cli, refresh=False):
    """{game name: install path} for every game Wabbajack reports as present.

    `list-games` prints one line per known game, marked [X] when it found an
    installation. It also prints a good deal of store-scanning noise to the
    same stream, hence the narrow match."""
    if cli in _GAMES_CACHE and not refresh:
        return _GAMES_CACHE[cli]
    try:
        proc = subprocess.run([cli, "list-games"], capture_output=True,
                              text=True, errors="replace", timeout=300,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return {}
    found = parse_games(proc.stdout + proc.stderr)
    _GAMES_CACHE[cli] = found
    return found


def parse_games(text):
    """{name: path} for the [X] lines in `list-games` output.

    Kept separate from the subprocess call so the parsing can be exercised
    without a Wabbajack install. The marked lines carry version and build
    fields after the name, and the same stream also carries store-scanning
    noise, hence the narrow match."""
    found = {}
    for line in text.split("\n"):
        m = re.search(r"\[X\]\s+(.+?)(?:\s+->\s+Path:\s*(.*))?$", line)
        if not m:
            continue
        name = m.group(1).split(",")[0].strip()
        name = re.sub(r"\s+\d[\d.]*$", "", name).strip()
        found[name] = (m.group(2) or "").strip()
    return found


def game_ready(cli, gametype):
    """Can Wabbajack install a list for this GameType?

    Returns (ok, detail). Not being able to tell is reported as such rather
    than assumed either way: a missing CLI is a different problem from a
    missing game."""
    wanted = WABBAJACK_GAME_NAMES.get(gametype)
    if wanted is None:
        return False, ("Wabbajack has no game called %r, so it cannot install "
                       "this list at all" % gametype)
    if not cli:
        return None, ("no wabbajack-cli.exe found, so whether %s is installed "
                      "could not be checked" % wanted)
    games = detected_games(cli)
    if wanted in games:
        return True, "%s found at %s" % (wanted, games[wanted] or "an unstated path")
    return False, ("Wabbajack cannot find an installation of %s. A modlist "
                   "copies files out of the base game, so installing without "
                   "it will fail." % wanted)


def check_paths(manifest, downloads, output):
    """Problems worth catching before Wabbajack is started.

    The CLI does validate its arguments, but it does so by throwing a .NET
    stack trace, which is a poor way to find out you mistyped a drive letter
    after committing to an install.

    The overlap checks are the ones that matter most: Wabbajack extracts the
    downloads into the install folder, so if one contains the other it ends up
    reading what it is writing."""
    problems = []
    if not os.path.isfile(manifest):
        problems.append("the modlist file is not there: %s" % manifest)
    if not os.path.isdir(downloads):
        problems.append("the downloads folder is not there: %s" % downloads)
    if not output or not str(output).strip():
        problems.append("no install path was given")
        return problems

    out_abs = os.path.abspath(output)
    dl_abs = os.path.abspath(downloads)
    if os.path.normcase(out_abs) == os.path.normcase(dl_abs):
        problems.append("the install path IS the downloads folder; "
                        "they have to be separate")
    elif os.path.normcase(out_abs).startswith(os.path.normcase(dl_abs) + os.sep):
        problems.append("the install path is inside the downloads folder; "
                        "they have to be separate")
    elif os.path.normcase(dl_abs).startswith(os.path.normcase(out_abs) + os.sep):
        problems.append("the downloads folder is inside the install path; "
                        "they have to be separate")

    drive = os.path.splitdrive(out_abs)[0]
    if drive and not os.path.isdir(drive + os.sep):
        problems.append("there is no drive %s on this machine" % drive)
    return problems


def install_args(cli, manifest, downloads, output):
    """The command that installs a staged list."""
    return [cli, "install",
            "--wabbajack", manifest,
            "--downloads", downloads,
            "--output", output]


def run_install(cli, manifest, downloads, output, on_line=None):
    """Run the install, streaming its output. Returns the exit code.

    Streamed rather than captured because an install of a large list runs for
    a long time and silence is indistinguishable from a hang."""
    cmd = install_args(cli, manifest, downloads, output)
    try:
        # stdin is deliberately NOT closed off. If Wabbajack needs to ask
        # something mid-install - its own Nexus sign-in, for a file this tooling
        # did not fetch - a closed stdin turns that into a confusing failure
        # instead of a question you can answer.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                errors="replace", bufsize=1)
    except OSError as e:
        if on_line:
            on_line("could not start wabbajack-cli: %s" % e)
        return 127
    with proc:
        for line in proc.stdout:
            if on_line:
                on_line(line.rstrip())
    return proc.returncode


def launch_gui(exe):
    """Open the Wabbajack GUI. Returns True if it started."""
    try:
        subprocess.Popen([exe], cwd=os.path.dirname(exe))
        return True
    except OSError:
        return False
