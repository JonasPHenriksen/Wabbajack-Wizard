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

import os
import re
import subprocess

try:
    import wj_config as CFG
except ImportError:                     # pragma: no cover - see wj_config.py
    CFG = None


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


def find_wabbajack(hint=None):
    """Locate a Wabbajack install. Returns a dict, or None.

    Looks where this tooling is most likely to find one: an explicit setting,
    then a sibling of the tools folder (the usual arrangement on this share),
    then the ordinary per-user install location. The CLI lives in a
    version-numbered subfolder, so the newest one is taken."""
    roots = []
    if hint:
        roots.append(hint)
    if CFG is not None and getattr(CFG, "WABBAJACK_DIR", None):
        roots.append(CFG.WABBAJACK_DIR)
    here = os.path.dirname(os.path.abspath(__file__))
    roots.append(os.path.normpath(os.path.join(here, "..", "Wabbajack")))
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
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                errors="replace", bufsize=1,
                                stdin=subprocess.DEVNULL)
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
