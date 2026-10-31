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
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import urllib.request
import webbrowser
import zipfile

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
    """Wabbajack processes running right now, as (pid, image): empty when
    there are none, and None when that could not be told - tasklist missing,
    stuck, or failing. None is not "not running": it used to be, and then a
    check that could not run waved through the deletion of Wabbajack's own
    version folders. Each caller decides: the console and the window ask
    the person, an unattended run does not move archives, and pruning
    Wabbajack's folders does not happen."""
    try:
        proc = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                              capture_output=True, text=True, errors="replace",
                              timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
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
                # Only a folder named like a version is one. 'Copy of
                # 4.2.3.0', a 'backup_20261010' holding the CLI, a folder
                # Explorer named '4.2.3.0 - Copy (2)' - each would have been
                # ranked by every digit in its name, taken for the newest,
                # and had the real versions pruned as superseded by it.
                cli = os.path.join(root, name, "cli", "wabbajack-cli.exe")
                if _VERSION_DIR.match(name) and os.path.isfile(cli):
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


def prune_old_versions(wj, running=None, keep=()):
    """Remove the version folders an update has superseded. Returns their
    names.

    Wabbajack's launcher downloads each new version into its own folder and
    leaves the old ones where they were, at some 700 MB each. Only folders
    that are version-named and hold the program go, only ones older than a
    newest that is itself version-named, and never while a Wabbajack process
    is running - or while that cannot be told: the launcher itself is one,
    and while it runs the newest folder may still be arriving. The newest
    folder has to be whole - program and CLI both - before anything older is
    judged superseded by it. A folder holding any path in `keep` stays
    whatever its age: MANIFEST_DIR may follow Wabbajack's own versioned
    downloaded_mod_lists, and fall back to an older version's while the
    newest is still empty, and that folder is the user's manifests."""
    if not wj or not wj.get("version") or not wj.get("dir"):
        return []
    root, newest = wj["dir"], wj["version"]
    if not _VERSION_DIR.match(str(newest)):
        return []
    if not (os.path.isfile(os.path.join(root, newest, "Wabbajack.exe"))
            and os.path.isfile(os.path.join(root, newest, "cli", "wabbajack-cli.exe"))):
        return []
    if running is None:
        running = running_processes()
    if running is None or running:
        return []
    kept = [os.path.normcase(os.path.abspath(p)) for p in keep if p]
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
        folder = os.path.normcase(os.path.abspath(os.path.join(root, name)))
        if any(k == folder or k.startswith(folder + os.sep) for k in kept):
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
            "published": str(data.get("published_at") or ""),
            "asset": release_asset(data, tag)}


def release_asset(data, tag):
    """The Windows build among a release's assets, as {"name", "url", "size",
    "sha256"}, or None when the release has none.

    It is the asset named for the tag, "4.3.2.1.zip": the file Wabbajack's own
    launcher fetches and unpacks into a folder of that name. GitHub gives each
    asset's size and, on releases since mid-2025, its SHA-256; either may be
    missing, and is then None."""
    want = f"{tag}.zip"
    for a in (data.get("assets") or []) if isinstance(data, dict) else []:
        if not isinstance(a, dict) or a.get("name") != want:
            continue
        size = a.get("size")
        algo, _, value = str(a.get("digest") or "").partition(":")
        return {"name": want, "url": str(a.get("browser_download_url") or ""),
                "size": size if isinstance(size, int) and size > 0 else None,
                "sha256": value.lower() if algo.lower() == "sha256"
                and re.fullmatch(r"[0-9a-fA-F]{64}", value) else None}
    return None


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
            "text": text, "asset": (rel or {}).get("asset")}


# The newest release, brought in the way Wabbajack's own launcher brings it:
# the release's Windows build unpacked into a folder named for its version,
# beside the one in use. The download and the half-unpacked folder have names
# of their own, which no version folder has, until the whole program is
# there; then the folder is renamed to the version, and prune_old_versions()
# retires the old one the next time Wabbajack is looked at.
UPDATE_TIMEOUT = 120
UPDATE_CHUNK = 1 << 20
UPDATE_SPARE = 1 << 30          # free space left over once it is unpacked


def bring_up_to_date(wj, auto=True, on_line=None, keep=()):
    """update_status() for `wj` - and when it is out of date and `auto` is
    set (WABBAJACK_AUTO_UPDATE), update_wabbajack() first, then the new
    version found and the one it superseded pruned. The one way every
    install, new or an update, learns which Wabbajack to use.

    Returns (wj, update, not_updated): the Wabbajack to use, how it stands
    against the newest release, and why it was not updated when that was
    tried and failed - None otherwise."""
    say = on_line or (lambda _line: None)
    update = update_status(wj)
    if not (auto and wj and update.get("outdated")):
        return wj, update, None
    say(f"{update['text']}: updating it.")
    ok, said = update_wabbajack(wj, update, on_line=say)
    say(f"{said}.")
    if not ok:
        return wj, update, said
    wj = find_wabbajack() or wj
    for name in prune_old_versions(wj, keep=keep):
        say(f"Removed Wabbajack {name}, superseded by {wj['version']}.")
    return wj, update_status(wj), None


def update_wabbajack(wj, update, on_line=None, running=None):
    """Bring Wabbajack up to the newest release. Returns (ok, one sentence
    saying what happened). Never raises.

    `update` is update_status()'s answer. The download is held to the size
    and SHA-256 GitHub publishes for it before anything is unpacked, and
    nothing in use is touched: the version folder in use stays, and a folder
    already bearing the new version's name is never written into. Not while
    a Wabbajack process runs - its launcher may be fetching the same version -
    nor when that cannot be told."""
    say = on_line or (lambda _line: None)
    update = update or {}
    version, asset = update.get("latest"), update.get("asset")
    if not wj or not wj.get("dir"):
        return False, "Wabbajack was not found, so there is nothing to update"
    if not version or not _VERSION_DIR.match(str(version)):
        return False, "the newest Wabbajack release could not be looked up"
    if wj.get("version") and version_key(version) <= version_key(wj["version"]):
        return True, f"Wabbajack {wj['version']} is already the newest release"
    if not asset or wj_paths.web_url_problem(asset.get("url")):
        return False, f"Wabbajack {version} has no Windows build ({version}.zip) to download"
    if running is None:
        running = running_processes()
    if running is None:
        return False, "whether Wabbajack is running could not be checked, so it was not updated"
    if running:
        return False, "Wabbajack is running; close it, and it is updated at the next check"
    root = wj["dir"]
    dest = os.path.join(root, version)
    if os.path.lexists(dest):
        return False, (f"{dest} is already there without a whole Wabbajack in it; run "
                       f"Wabbajack.exe to finish its own update, or remove that folder")
    part = os.path.join(root, f"{version}.zip.part")
    staging = os.path.join(root, f"{version}.unpacking")
    try:
        _discard(part, staging)         # what an earlier attempt left
        free = _free_bytes(root)
        if asset.get("size") and free is not None and free < asset["size"] + UPDATE_SPARE:
            return False, f"there is not room in {root} for the {_mb(asset['size'])} download"
        _download(asset["url"], part, asset.get("size"), asset.get("sha256"), version, say)
        say(f"Unpacking Wabbajack {version}...")
        wj_paths.move_folder(_unpack(part, staging), dest)
    except Exception as e:          # the network, the disk, a damaged zip: said, never raised
        return False, f"Wabbajack {version} could not be installed: {e}"
    finally:
        _discard(part, staging)
    return True, f"Wabbajack {version} installed in {dest}"


def _download(url, part, size, sha256, version, say):
    """The release's zip to `part`, held to the size and hash it was published with."""
    agent = getattr(CFG, "USER_AGENT", None) or "WabbajackWizard"
    req = urllib.request.Request(url, headers={"User-Agent": agent})
    digest, got, shown = hashlib.sha256(), 0, -1
    with wj_paths.urlopen(req, timeout=UPDATE_TIMEOUT) as r, io.open(part, "xb") as f:
        total = size or int(r.headers.get("Content-Length") or 0) or None
        while True:
            chunk = r.read(UPDATE_CHUNK)
            if not chunk:
                break
            f.write(chunk)
            digest.update(chunk)
            got += len(chunk)
            if size and got > size:
                raise ValueError(f"the download is larger than the {size:,} bytes published")
            if total and got * 10 // total > shown:
                shown = got * 10 // total
                say(f"Downloading Wabbajack {version}: {got * 100 // total}% of {_mb(total)}")
    if size and got != size:
        raise ValueError(f"the download stopped at {got:,} of {size:,} bytes")
    if sha256 and digest.hexdigest() != sha256:
        raise ValueError("the download's SHA-256 is not the one published with the release")


def _unpack(zip_path, into):
    """Unpack the release into `into`, and return the folder in it holding a
    whole Wabbajack: `into` itself, or the one folder a zip wraps it in."""
    with zipfile.ZipFile(zip_path) as z:
        members = z.infolist()
        for m in members:
            parts = m.filename.replace("\\", "/").split("/")
            if m.filename.startswith(("/", "\\")) or ":" in m.filename or ".." in parts:
                raise ValueError(f"the zip names a file outside its own folder: {m.filename}")
        need = sum(m.file_size for m in members)
        free = _free_bytes(os.path.dirname(into))
        if free is not None and free < need + UPDATE_SPARE:
            raise ValueError(f"there is not room to unpack it ({_mb(need)})")
        z.extractall(into)              # every byte is checked against its CRC on the way
    if _whole(into):
        return into
    inner = [os.path.join(into, n) for n in os.listdir(into)]
    if len(inner) == 1 and _whole(inner[0]):
        return inner[0]
    raise ValueError("the zip does not hold Wabbajack.exe and cli\\wabbajack-cli.exe")


def _whole(folder):
    return (os.path.isfile(os.path.join(folder, "Wabbajack.exe"))
            and os.path.isfile(os.path.join(folder, "cli", "wabbajack-cli.exe")))


def _discard(part, staging):
    """An update's own download and half-unpacked folder: names no version
    folder has, and nothing but update_wabbajack() writes."""
    try:
        if os.path.isfile(part):
            os.remove(part)
    except OSError:
        pass
    shutil.rmtree(staging, ignore_errors=True)


def _free_bytes(path):
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _mb(n):
    return f"{n / 2 ** 20:.1f} MB"


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
    else:
        # Wabbajack owns the folder it installs into. Inside the archive root
        # it would be read as a game's pools, or a stray to park, or Master's
        # content to reclaim; holding the archive root, the tools or the
        # manifests, it would be installing over them. Only this list's own
        # downloads folder used to be checked.
        for what, place in _places_not_to_install():
            p = os.path.normcase(os.path.abspath(place))
            o = os.path.normcase(out_abs)
            if o == p or o.startswith(p.rstrip("\\/") + os.sep):
                problems.append("the install path is inside %s (%s); install somewhere "
                                "of its own" % (what, place))
                break
            if p.startswith(o.rstrip("\\/") + os.sep):
                problems.append("the install path holds %s (%s); install somewhere "
                                "of its own" % (what, place))
                break
        if not problems and manifest:
            m_dir = os.path.normcase(os.path.dirname(os.path.abspath(manifest)))
            if m_dir == os.path.normcase(out_abs) or m_dir.startswith(
                    os.path.normcase(out_abs).rstrip("\\/") + os.sep):
                problems.append("the install path holds the modlist file itself; "
                                "install somewhere of its own")

    drive = os.path.splitdrive(out_abs)[0]
    if drive and not os.path.isdir(drive + os.sep):
        problems.append("there is no drive %s on this machine" % drive)
    return problems


def _places_not_to_install():
    """(what, path) for the folders an install may neither be in nor hold."""
    places = []
    if CFG is not None:
        places.append(("the archive root", CFG.resolved_archive_root()))
    places.append(("this tool's folder", wj_paths.app_dir(__file__)))
    return places


# What a list asks of the PC before an install - Windows and Steam settings,
# programs to install first, how the game must be set up - is in its readme
# and nowhere else. A manifest carries the files, the steps that rebuild
# them, the game build it starts from and a link to that readme, but no field
# for prerequisites, and the readmes are prose in no common form, often
# behind pages a tool cannot read (docs/modlist-prerequisites.md). So an
# install opens the readme and asks, rather than guessing.
README_WHY = ("Lists ask for things there that no tool can check: Windows and Steam "
              "settings, programs to install first, how the game must be set up.")

# The manifest's top-level Readme field. It comes after the archive list and
# the install steps, which run to hundreds of megabytes in a large list, so
# it is looked for in one pass over the manifest instead of by parsing all of
# it. A quote inside a string is escaped, so a "Readme" in an archive's Meta
# text cannot match; the last match wins all the same.
_README_FIELD = re.compile(rb'(?<!\\)"Readme"\s*:\s*"((?:[^"\\]|\\.)*)"')
_README_CHUNK = 1 << 22
_README_OVERLAP = 1 << 16


def manifest_readme(manifest):
    """The address of a list's readme, from its manifest, or None when it
    names none or the file cannot be read."""
    found = None
    try:
        with zipfile.ZipFile(manifest) as z:
            member = next((m for m in z.namelist()
                           if os.path.basename(m).lower() == "modlist"), None)
            if member is None:
                return None
            tail = b""
            with z.open(member) as fh:
                while True:
                    chunk = fh.read(_README_CHUNK)
                    if not chunk:
                        break
                    buf = tail + chunk
                    for m in _README_FIELD.finditer(buf):
                        found = m.group(1)
                    tail = buf[-_README_OVERLAP:]
        if found is None:
            return None
        url = json.loads(b'"' + found + b'"')
    except Exception:           # a missing, damaged or foreign file: no readme to name
        return None
    url = url.strip() if isinstance(url, str) else ""
    return url or None


def open_readme(url):
    """Open a readme in the default browser. Returns None when it opened,
    otherwise why not.

    The address comes from the manifest, which anyone could have edited, and
    the shell starts whatever it is handed: a path to a program would run it.
    So only a web address is opened (wj_paths.web_url_problem)."""
    why = wj_paths.web_url_problem(url)
    if why:
        return why
    try:
        return None if webbrowser.open(url) else "no browser could be started"
    except Exception as e:      # whatever the shell raised
        return str(e) or e.__class__.__name__


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
