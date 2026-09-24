#!/usr/bin/env python3
"""
A Mod Organizer 2 instance of the Wizard's own making
=====================================================
collections2mo2 builds instances for the Bethesda games and no others. A
collection for any other game MO2 runs is built here instead, in the shape a
Wabbajack list for that game gives its install: Mod Organizer 2 portable, the
game copied into 'Stock Game' so the store's install is never touched, each mod
in mods/<name>/ laid out exactly as the game's Vortex extension lays it into
its staging folder, the collection's rules deciding which mod wins a conflict,
and the curator's binary patches applied on top.

Stardew Valley is the first game here. Vortex's Stardew extension installs a
mod by its SMAPI manifests: every folder holding a manifest.json is kept at its
path in the archive, and whatever lies outside one is dropped. MO2 maps
mods/<name>/ onto the game's Mods folder, so the same layout serves both, and a
collection's patches - paths inside that staging folder - land on the files
they were made from.

SMAPI is the one mod that is not a Mods-folder mod. Its installer package
carries the loader as internal/windows/install.dat, which belongs in the game
folder; here that is the instance's own Stock Game, so it goes in directly, the
way Wabbajack lists put a script extender into theirs. Root Builder, which The
Bustling Valley uses for it, copies a mod's Root folder into the game at launch,
but MO2 still maps the folder itself into Mods, where SMAPI would take it for a
mod with no manifest. SMAPI's bundled mods (Console Commands, Save Backup) stay
in its MO2 mod, to be switched off like any other.

What no MO2 instance can keep apart for Stardew is the saves. The game writes
them to %APPDATA%\\StardewValley\\Saves wherever it runs from, and MO2's
Stardew plugin offers no profile saves to redirect them with, so a modded farm
sits beside the vanilla ones - and Steam Cloud syncs that folder.

What a build needs, kept beside the scripts: MO2's release archive, pinned by
SHA-256 and cached in .mo2cache, and the 7-Zip console Wabbajack ships.
"""

import binascii
import bz2
import fnmatch
import hashlib
import heapq
import json
import os
import posixpath
import re
import shutil
import subprocess
import time
import zipfile
from datetime import datetime, timezone

import wj_paths
import wj_config as CFG
import wj_downloader as WJ
import wj_wabbajack as WB

MO2_VERSION = "2.5.2"
MO2_ARCHIVE = "Mod.Organizer-%s.7z" % MO2_VERSION
MO2_URL = ("https://github.com/ModOrganizer2/modorganizer/releases/download/v%s/%s"
           % (MO2_VERSION, MO2_ARCHIVE))
# GitHub lists no digest for release assets this old, so the pin is of the
# copy read before it was trusted; its size is the one GitHub serves.
MO2_SIZE = 149660212
MO2_SHA256 = "e6376efd87fd5ddd95aee959405e8f067afa526ea6c2c0c5aa03c5108bf4a815"
CACHE_DIRNAME = ".mo2cache"

STOCK_GAME = "Stock Game"
MARK = "wj-instance.json"             # what an instance built here says about itself
BUILDING_SUFFIX = ".wjbuild"          # the folder a build happens in, beside the instance
BUILDING_MARK = ".wj-building"        # says a leftover build folder is this tool's to clear
FORMAT = 1                            # of wj-instance.json

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# The games built here, keyed by Nexus domain. `mo2_name` is the game plugin's
# name, which ModOrganizer.ini records; `short` is its short name, which each
# mod's meta.ini records; `data` is where MO2 maps mods/<name>/ to. `loader` is
# the collection's mod type for the game's loader, and `not_copied` what a
# store install may hold that is not the game - its mods folder, an older
# loader - matched against the top level of the game folder.
GAMES = {
    "stardewvalley": {
        "gametype": "StardewValley",
        "mo2_name": "Stardew Valley",
        "short": "stardewvalley",
        "exe": "Stardew Valley.exe",
        "steam_app": "413150",
        "data": "Mods",
        "layout": "stardew",
        "loader": "SMAPI",
        "not_copied": ("mods", "smapi-internal", "stardewmoddingapi*"),
        "executables": (("SMAPI", "StardewModdingAPI.exe"),
                        ("Stardew Valley", "Stardew Valley.exe")),
        "after_build": (
            "Play it by running SMAPI, the executable MO2 has chosen; 'Stardew Valley' there "
            "starts the game without mods.",
            "Its saves go where Stardew always keeps them, %APPDATA%\\StardewValley\\Saves, "
            "beside your other farms and synced by Steam Cloud: start a new farm for it, and "
            "leave the vanilla farms to the vanilla game."),
    },
}


class BuildError(Exception):
    """Something that stops a build, said in words."""


class PatchError(Exception):
    """A binary patch that cannot be read or applied."""


def here():
    return wj_paths.app_dir(__file__)


def _say(say, text=""):
    (say or print)(text)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------------
# Tools: MO2's release, and a 7-Zip to unpack it and the mods with
# ----------------------------------------------------------------------
def cache_dir():
    return os.path.join(here(), CACHE_DIRNAME)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_pinned(path):
    try:
        return os.path.getsize(path) == MO2_SIZE and _sha256(path) == MO2_SHA256
    except OSError:
        return False


def _mo2_places():
    return (os.path.join(cache_dir(), MO2_ARCHIVE),
            os.path.join(here(), ".c2mo2", "tools", "cache", MO2_ARCHIVE))


def mo2_cached():
    """Is MO2's release archive here already? By size; the hash is checked
    when it is used."""
    for p in _mo2_places():
        try:
            if os.path.getsize(p) == MO2_SIZE:
                return True
        except OSError:
            pass
    return False


def mo2_archive(say=None):
    """MO2's release archive, checked against its pinned SHA-256: the copy in
    .mo2cache, else the one collections2mo2 cached, else GitHub's."""
    dest, borrowed = _mo2_places()
    if _is_pinned(dest):
        return dest
    os.makedirs(cache_dir(), exist_ok=True)
    if _is_pinned(borrowed):
        shutil.copyfile(borrowed, dest + ".copy")
        os.replace(dest + ".copy", dest)
        return dest
    _say(say, f"Downloading Mod Organizer {MO2_VERSION} from GitHub "
              f"({WJ.human_bytes(MO2_SIZE)})...")
    fetched = dest + ".download"
    try:
        if not WJ.stream_download(MO2_URL, fetched, expected_size=MO2_SIZE, quiet=True):
            raise BuildError(f"Mod Organizer {MO2_VERSION} did not download from {MO2_URL}")
        if not _is_pinned(fetched):
            raise BuildError(f"the {MO2_ARCHIVE} GitHub served is not the one pinned here "
                             f"(SHA-256 {MO2_SHA256[:12]}...); refusing to use it")
        os.replace(fetched, dest)
    finally:
        for p in (fetched, fetched + ".part"):
            try:
                os.remove(p)
            except OSError:
                pass
    return dest


def seven_zip():
    """A 7-Zip console this tooling may run without installing one: the copy
    Wabbajack ships inside its own install, else the one collections2mo2 keeps."""
    cands = []
    wj = WB.find_wabbajack()
    if wj and wj.get("version"):
        base = os.path.join(wj["dir"], wj["version"])
        cands += [os.path.join(base, "Extractors", "windows-x64", "7z.exe"),
                  os.path.join(base, "cli", "Extractors", "windows-x64", "7z.exe")]
    cands.append(os.path.join(here(), ".c2mo2", "tools", "7za.exe"))
    return next((c for c in cands if os.path.isfile(c)), None)


def extract_zip(path, dest):
    """Unpack a zip into dest, refusing any member that would land outside it."""
    dest_abs = os.path.abspath(dest)
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            rel = info.filename.replace("\\", "/")
            target = os.path.abspath(os.path.join(dest_abs, *[p for p in rel.split("/") if p]))
            if target != dest_abs and not target.startswith(dest_abs + os.sep):
                raise BuildError(f"{os.path.basename(path)} tried to write outside its folder: "
                                 f"{info.filename}")
            if rel.endswith("/"):
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def unpack(archive, dest):
    """Unpack an archive - 7z, zip or rar, whatever a mod ships in - into dest.
    Returns True when 7-Zip warned but still wrote the files."""
    os.makedirs(dest, exist_ok=True)
    exe = seven_zip()
    if exe:
        got = subprocess.run([exe, "x", "-y", "-bd", "-o" + dest, archive],
                             capture_output=True, stdin=subprocess.DEVNULL,
                             creationflags=NO_WINDOW)
        if got.returncode in (0, 1):                 # 1 is 7-Zip's warning: files written
            return got.returncode == 1
        tail = (got.stderr or got.stdout or b"").decode("utf-8", "replace").strip().splitlines()
        raise BuildError(f"7-Zip could not unpack {os.path.basename(archive)}"
                         + (f": {tail[-1].strip()}" if tail else f" (exit {got.returncode})"))
    if zipfile.is_zipfile(archive):
        extract_zip(archive, dest)
        return False
    raise BuildError(f"no 7-Zip was found to unpack {os.path.basename(archive)} (the one "
                     f"inside Wabbajack's install is used when present)")


def list_files(root):
    """Every file under root, relative and '/'-separated, sorted."""
    out = []
    for d, _dirs, files in os.walk(root):
        for name in files:
            out.append(os.path.relpath(os.path.join(d, name), root).replace(os.sep, "/"))
    return sorted(out)


# ----------------------------------------------------------------------
# Where a mod's files go
# ----------------------------------------------------------------------
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(text, fallback="mod"):
    """A name usable as one Windows folder name."""
    clean = _BAD_CHARS.sub("_", str(text or "")).strip().rstrip(". ").strip()
    return clean or fallback


def unique_names(names):
    """The names made unique the way Windows compares them, a ' (2)' and on
    added to a repeat."""
    seen, out = set(), []
    for name in names:
        cand, n = name, 1
        while cand.lower() in seen:
            n += 1
            cand = f"{name} ({n})"
        seen.add(cand.lower())
        out.append(cand)
    return out


_JSON_JUNK = re.compile(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', re.S)
_JSON_COMMAS = re.compile(r'("(?:\\.|[^"\\])*")|,(\s*[}\]])')


def loose_json(text):
    """JSON as SMAPI reads it: comments and trailing commas allowed."""
    text = text.lstrip("\ufeff")
    text = _JSON_JUNK.sub(lambda m: m.group(1) or "", text)
    text = _JSON_COMMAS.sub(lambda m: m.group(1) or m.group(2), text)
    return json.loads(text)


def manifest_name(path):
    """The Name a SMAPI manifest.json gives its mod, or None."""
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            data = loose_json(f.read())
    except (OSError, ValueError):
        return None
    name = data.get("Name") if isinstance(data, dict) else None
    return str(name).strip() if name else None


def is_manifest(rel):
    parts = rel.lower().split("/")
    return parts[-1] == "manifest.json" and "locale" not in parts[:-1]


def stardew_layout(root, files, fallback="mod"):
    """([(source, destination)], how) for an unpacked Stardew mod, as Vortex's
    Stardew extension lays it into its staging folder; both paths relative
    and '/'-separated.

    Its installer keeps every folder holding a manifest.json - SMAPI's mark of
    a mod - at the path it has in the archive, and drops whatever lies outside
    one: the readme beside the mod folder, the screenshots. A manifest.json at
    the top of the archive gets a folder named after the mod's own Name. One
    under a 'locale' folder belongs to a translation, not a mod. An archive
    with no manifest at all - config files for other mods, say - goes in as
    it is, which is what Vortex's fallback installer does with it."""
    manifests = [f for f in files if is_manifest(f)]
    if not manifests:
        return [(f, f) for f in files], "as is (no SMAPI manifest)"
    placed = {}
    for mf in sorted(manifests):
        folder = posixpath.dirname(mf)
        if folder:
            for f in files:
                if f.startswith(folder + "/"):
                    placed[f.lower()] = (f, f)
        else:
            name = safe_name(manifest_name(os.path.join(root, mf)), fallback)
            for f in files:
                dest = name + "/" + f
                placed[dest.lower()] = (f, dest)
    return sorted(placed.values(), key=lambda p: p[1].lower()), \
        f"{len(manifests)} SMAPI manifest(s)"


LAYOUTS = {"stardew": stardew_layout}


def replicate_layout(root, files, hashes):
    """([(source, destination)], unmatched) for a mod the curator exported
    file for file: each listed path gets the unpacked file with its MD5, the
    way Vortex installs such a mod whatever the archive's own layout."""
    by_md5 = {}
    for f in files:
        by_md5.setdefault(WJ.file_md5(os.path.join(root, f)), []).append(f)
    pairs, unmatched = [], []
    for item in hashes or []:
        rel = str(item.get("path") or "").replace("\\", "/").strip("/")
        found = by_md5.get(str(item.get("md5") or "").lower()) or []
        if not rel or not found:
            unmatched.append(rel)
            continue
        src = next((c for c in found if posixpath.basename(c).lower()
                    == posixpath.basename(rel).lower()), found[0])
        pairs.append((src, rel))
    return pairs, unmatched


def smapi_payload(files):
    """SMAPI's Windows payload inside its installer package: the path of
    internal/windows/install.dat, or None."""
    for f in files:
        parts = f.lower().split("/")
        if parts[-1] in ("install.dat", "windows-install.dat") and "windows" in parts[:-1]:
            return f
    return None


def install_smapi(unpacked, files, stock, mod_dir, scratch):
    """Lay SMAPI in: its loader into the game folder, its bundled mods into
    its MO2 mod. Returns (files put in the game folder, files put in the mod).

    install.dat is what SMAPI's own installer copies into the game folder, and
    Vortex does the same with it. Both then copy the game's own
    'Stardew Valley.deps.json' to 'StardewModdingAPI.deps.json', which the
    loader needs to find the game's native libraries."""
    dat = smapi_payload(files)
    if not dat:
        raise BuildError("SMAPI's archive holds no internal/windows/install.dat; it should be "
                         "SMAPI's installer package as Nexus serves it")
    unpack(os.path.join(unpacked, *dat.split("/")), scratch)
    inner = list_files(scratch)
    exe = next((f for f in inner if posixpath.basename(f).lower() == "stardewmoddingapi.exe"),
               None)
    if not exe:
        raise BuildError("SMAPI's install.dat holds no StardewModdingAPI.exe")
    base = posixpath.dirname(exe)
    to_game, to_mod = [], []
    for f in inner:
        if base and not f.startswith(base + "/"):
            continue
        rel = f[len(base) + 1:] if base else f
        parts = rel.split("/")
        if len(parts) > 1 and parts[0].lower() == "mods":
            to_mod.append((f, "/".join(parts[1:])))
        else:
            to_game.append((f, rel))
    for src, rel in to_game:
        _place(os.path.join(scratch, *src.split("/")), os.path.join(stock, *rel.split("/")))
    for src, rel in to_mod:
        _place(os.path.join(scratch, *src.split("/")), os.path.join(mod_dir, *rel.split("/")))
    game_files = [rel for _s, rel in to_game]
    deps = os.path.join(stock, "Stardew Valley.deps.json")
    if os.path.isfile(deps):
        shutil.copy2(deps, os.path.join(stock, "StardewModdingAPI.deps.json"))
        game_files.append("StardewModdingAPI.deps.json")
    shutil.rmtree(scratch, ignore_errors=True)
    return game_files, [rel for _s, rel in to_mod]


def _place(src, dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(src, dest)


# ----------------------------------------------------------------------
# Binary patches: bsdiff 4's BSDIFF40, as Vortex writes them
# ----------------------------------------------------------------------
_NONZERO = re.compile(rb"[^\x00]")


def _offtin(b):
    """bsdiff's signed 64-bit integer: little-endian magnitude, sign in the
    top bit of the last byte."""
    y = int.from_bytes(b[:7] + bytes([b[7] & 0x7F]), "little")
    return -y if b[7] & 0x80 else y


def bspatch(old, patch):
    """The new file a BSDIFF40 patch makes from `old`.

    The patch is three bzip2 streams: control triples (bytes to add to old,
    bytes to insert, how far to seek in old), the difference bytes, and the
    inserted bytes. Most difference bytes are zero where a file changed
    little, so only the nonzero ones are added one at a time."""
    if len(patch) < 32 or patch[:8] != b"BSDIFF40":
        raise PatchError("not a BSDIFF40 patch")
    ctrl_len, diff_len, new_size = _offtin(patch[8:16]), _offtin(patch[16:24]), _offtin(patch[24:32])
    if ctrl_len < 0 or diff_len < 0 or new_size < 0 or 32 + ctrl_len + diff_len > len(patch):
        raise PatchError("the patch's header is corrupt")
    try:
        ctrl = bz2.decompress(patch[32:32 + ctrl_len])
        diff = bz2.decompress(patch[32 + ctrl_len:32 + ctrl_len + diff_len])
        extra = bz2.decompress(patch[32 + ctrl_len + diff_len:])
    except (OSError, ValueError, EOFError) as e:
        raise PatchError(f"the patch's data does not decompress ({e})")
    new = bytearray(new_size)
    old_size = len(old)
    old_pos = new_pos = ci = di = ei = 0
    while new_pos < new_size:
        if ci + 24 > len(ctrl):
            raise PatchError("the patch ends early")
        x, y, z = _offtin(ctrl[ci:ci + 8]), _offtin(ctrl[ci + 8:ci + 16]), _offtin(ctrl[ci + 16:ci + 24])
        ci += 24
        if x < 0 or y < 0 or new_pos + x + y > new_size or di + x > len(diff) \
                or ei + y > len(extra):
            raise PatchError("the patch is corrupt")
        chunk = diff[di:di + x]
        # The part of this stretch that has old bytes under it gets them
        # added; any part beyond the old file's ends is the diff bytes alone.
        lo, hi = max(0, -old_pos), max(0, min(x, old_size - old_pos))
        new[new_pos:new_pos + x] = chunk
        if hi > lo:
            new[new_pos + lo:new_pos + hi] = old[old_pos + lo:old_pos + hi]
            for m in _NONZERO.finditer(chunk, lo, hi):
                i = m.start()
                new[new_pos + i] = (chunk[i] + old[old_pos + i]) & 0xFF
        new_pos += x
        old_pos += x
        di += x
        new[new_pos:new_pos + y] = extra[ei:ei + y]
        new_pos += y
        ei += y
        old_pos += z
    return bytes(new)


def crc32_hex(data):
    return "%08X" % (binascii.crc32(data) & 0xFFFFFFFF)


def apply_patch(target, crc, diff):
    """Patch one installed file in place, when it is the file the patch was
    made from. Returns "applied", or why it was not.

    A collection records the CRC32 of the file each patch was made against;
    a different file under that name - another version of the mod - is left
    as it is, as Vortex leaves it, since the patch would turn it into
    something neither version is."""
    if not os.path.isfile(target):
        return "the file is not in the mod"
    with open(target, "rb") as f:
        old = f.read()
    have = crc32_hex(old)
    if crc and have != str(crc).upper().rjust(8, "0"):
        return f"not the file the patch was made from (CRC32 {have}, the patch's is {crc})"
    new = bspatch(old, diff)
    tmp = target + ".wjpatch"
    with open(tmp, "wb") as f:
        f.write(new)
    os.replace(tmp, target)
    return "applied"


# ----------------------------------------------------------------------
# The order: which mod wins a conflict
# ----------------------------------------------------------------------
def order_by_rules(count, edges):
    """Mods 0..count-1, given in the collection's order, sorted so that every
    edge (a, b) - a loads before b, so b wins where both have a file - holds;
    otherwise they keep the collection's order. Returns (order, set_aside):
    the order lowest priority first, and the edges that closed a cycle and
    had to be dropped for it to exist at all."""
    succ = {i: set() for i in range(count)}
    indeg = [0] * count
    for a, b in edges:
        if a != b and 0 <= a < count and 0 <= b < count and b not in succ[a]:
            succ[a].add(b)
            indeg[b] += 1
    heap = [i for i in range(count) if indeg[i] == 0]
    heapq.heapify(heap)
    order, done, set_aside = [], set(), []
    while len(order) < count:
        if not heap:
            # Every mod left waits on another: a cycle. The earliest of them
            # goes next, and the rules that held it back are set aside.
            i = min(j for j in range(count) if j not in done)
            for a in sorted(range(count)):
                if a not in done and i in succ[a]:
                    succ[a].discard(i)
                    set_aside.append((a, i))
            indeg[i] = 0
            heapq.heappush(heap, i)
        i = heapq.heappop(heap)
        if i in done:
            continue
        done.add(i)
        order.append(i)
        for b in sorted(succ[i]):
            indeg[b] -= 1
            if indeg[b] == 0:
                heapq.heappush(heap, b)
    return order, set_aside


# ----------------------------------------------------------------------
# MO2's own files
# ----------------------------------------------------------------------
def _native(path):
    return str(path).replace("/", "\\")


def _forward(path):
    return str(path).replace("\\", "/")


def _qt_path(path):
    """A path as MO2 stores gamePath: a QByteArray, backslashes doubled."""
    return "@ByteArray(%s)" % _native(path).replace("\\", "\\\\")


def _qt_quoted(path):
    """A quoted path inside an executable's arguments, as Qt's ini writer
    escapes it."""
    return '\\"%s\\"' % _native(path).replace("\\", "\\\\")


def _qt_value(text):
    """A plain ini value; quoted when Qt would read it as a list."""
    return '"%s"' % text if "," in text else text


def safe_profile(name):
    """A profile name MO2 keeps as a folder and in an @ByteArray."""
    clean = re.sub(r"[^A-Za-z0-9 _.,()'&+-]", "_", str(name or "")).strip(" ._")
    return clean[:60].strip(" ._") or "Default"


def mo2_ini(game, out, profile, downloads=None):
    """ModOrganizer.ini for an instance at `out`: the game in its Stock Game,
    the loader first among the executables and chosen, the downloads folder
    the archives are pooled in."""
    stock = os.path.join(out, STOCK_GAME)
    exes = [{"title": title, "binary": _forward(os.path.join(stock, binary)), "arguments": "",
             "workingDirectory": _forward(stock), "steamAppID": game["steam_app"],
             "toolbar": "true"}
            for title, binary in game["executables"]]
    exes.append({"title": "Explore Virtual Folder",
                 "binary": _forward(os.path.join(out, "explorer++", "Explorer++.exe")),
                 "arguments": _qt_quoted(os.path.join(stock, game["data"])),
                 "workingDirectory": _forward(os.path.join(out, "explorer++")),
                 "steamAppID": "", "toolbar": "false"})
    lines = ["[General]",
             "gameName=" + game["mo2_name"],
             "gamePath=" + _qt_path(stock),
             "selected_profile=@ByteArray(%s)" % profile,
             "version=" + MO2_VERSION,
             "first_start=false",
             "",
             "[Settings]",
             "style="]
    if downloads:
        lines.append("download_directory=" + _qt_value(_forward(downloads)))
    lines += ["", "[customExecutables]", "size=%d" % len(exes)]
    for i, e in enumerate(exes, 1):
        for key in ("title", "binary", "arguments", "workingDirectory"):
            lines.append("%d\\%s=%s" % (i, key, e[key]))
        lines += ["%d\\hide=false" % i, "%d\\ownicon=true" % i,
                  "%d\\steamAppID=%s" % (i, e["steamAppID"]), "%d\\toolbar=%s" % (i, e["toolbar"])]
    lines += ["", "[PluginPersistance]", "Python%20Proxy\\tryInit=false",
              "", "[Widgets]", "MainWindow_executablesListBox_index=1"]
    return "\r\n".join(lines) + "\r\n"


def modlist_txt(folders_low_first, disabled=()):
    """A profile's modlist.txt: highest priority first, as MO2 keeps it."""
    off = {f.lower() for f in disabled}
    lines = ["# This file was automatically generated by Mod Organizer."]
    for folder in reversed(folders_low_first):
        lines.append(("-" if folder.lower() in off else "+") + folder)
    return "\r\n".join(lines) + "\r\n"


def profile_settings():
    """A profile's settings.ini. Saves stay where the game writes them: a
    game MO2 has no profile saves for would only get an empty folder."""
    return "\r\n".join(["[General]", "LocalSaves=false", "LocalSettings=false",
                        "AutomaticArchiveInvalidation=false"]) + "\r\n"


def meta_ini(game, meta):
    """A mod's meta.ini, in the form MO2 writes for a Nexus download."""
    mod_id, file_id = meta.get("mod_id"), meta.get("file_id")
    lines = ["[General]",
             "gameName=" + game["short"],
             "modid=%d" % (mod_id or 0),
             "version=" + str(meta.get("version") or ""),
             "newestVersion=",
             "category=",
             "installationFile=" + str(meta.get("archive") or ""),
             "repository=" + ("Nexus" if mod_id else ""),
             "comments=" + str(meta.get("comments") or ""),
             "notes=",
             "nexusFileStatus=1",
             "hasCustomURL=false",
             "validated=true",
             "converted=false",
             "",
             "[installedFiles]"]
    if mod_id and file_id:
        lines += ["1\\modid=%d" % mod_id, "1\\fileid=%d" % file_id, "size=1"]
    else:
        lines.append("size=0")
    return "\r\n".join(lines) + "\r\n"


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


# ----------------------------------------------------------------------
# The build
# ----------------------------------------------------------------------
def copy_game(src, dest, not_copied=(), say=None):
    """Copy the game into dest, leaving out what `not_copied` names at its
    top level. Returns (files, bytes)."""
    count = size = 0
    for name in sorted(os.listdir(src)):
        if any(fnmatch.fnmatch(name.lower(), pat) for pat in not_copied):
            _say(say, f"  left out of the copy: {name} (the build lays its own in)")
            continue
        s = os.path.join(src, name)
        if os.path.isdir(s):
            for root, _dirs, files in os.walk(s):
                target = os.path.normpath(os.path.join(dest, name, os.path.relpath(root, s)))
                os.makedirs(target, exist_ok=True)
                for f in files:
                    shutil.copy2(os.path.join(root, f), os.path.join(target, f))
                    count += 1
                    size += os.path.getsize(os.path.join(root, f))
        else:
            os.makedirs(dest, exist_ok=True)
            shutil.copy2(s, os.path.join(dest, name))
            count += 1
            size += os.path.getsize(s)
    return count, size


def check_out(out):
    """Why a build cannot go to `out`, or None. An instance is only built
    into a new or empty folder, so nothing that was there is overwritten."""
    if os.path.exists(out) and (not os.path.isdir(out) or os.listdir(out)):
        return f"{out} already holds something; give an empty or new folder"
    building = out + BUILDING_SUFFIX
    if os.path.exists(building) and not os.path.isfile(os.path.join(building, BUILDING_MARK)):
        return f"{building} is in the way, and is not an unfinished build of this tool's"
    return None


def install_mod(game, m, index, building, stock, say=None):
    """Lay one mod into mods/<folder>/ and patch it. Returns its record."""
    folder = os.path.join(building, "mods", m["folder"])
    os.makedirs(folder, exist_ok=True)
    scratch = os.path.join(building, ".wj-unpack", str(index))
    rec = {"name": m["name"], "folder": m["folder"], "enabled": m.get("enabled", True),
           "archive": (m.get("meta") or {}).get("archive") or os.path.basename(m["source"]),
           "notes": []}
    if os.path.isdir(m["source"]):
        unpacked = m["source"]
    else:
        unpacked = scratch
        if unpack(m["source"], unpacked):
            rec["notes"].append("7-Zip warned while unpacking it")
    try:
        files = list_files(unpacked)
        if m.get("kind") == "loader":
            game_files, mod_files = install_smapi(unpacked, files, stock, folder, scratch + "-dat")
            rec.update(layout="loader: into Stock Game, its bundled mods here",
                       files=len(mod_files), game_files=game_files)
        else:
            if m.get("hashes"):
                pairs, unmatched = replicate_layout(unpacked, files, m["hashes"])
                how = "file for file, by MD5"
                if unmatched:
                    rec["notes"].append(f"{len(unmatched)} listed file(s) matched nothing in the "
                                        f"archive")
            else:
                pairs, how = LAYOUTS[game["layout"]](unpacked, files, fallback=m["folder"])
            for src, dest in pairs:
                _place(os.path.join(unpacked, *src.split("/")), os.path.join(folder, *dest.split("/")))
            rec.update(layout=how, files=len(pairs))
    finally:
        if unpacked == scratch:
            shutil.rmtree(scratch, ignore_errors=True)
    _write(os.path.join(folder, "meta.ini"), meta_ini(game, m.get("meta") or {}))
    rec["patches"] = []
    for rel, crc, diff in m.get("patches") or []:
        target = os.path.join(folder, *rel.split("/"))
        try:
            result = apply_patch(target, crc, diff) if diff is not None \
                else "the patch file is not in the collection"
        except PatchError as e:
            result = f"the patch could not be applied ({e})"
        rec["patches"].append({"file": rel, "result": result})
    return rec


def build(game_key, out, game_path, profile, mods, downloads=None, record=None, say=None):
    """Build a portable MO2 instance at `out`. Returns what it wrote into the
    instance's wj-instance.json.

    `mods` is the install, lowest priority first: [{"name", "folder", "source"
    (an archive, or a folder already unpacked), "kind" ("mod" or "loader"),
    "enabled", "meta" (mod_id, file_id, version, archive), "patches"
    [(path in the mod, CRC32, BSDIFF40 bytes)], "hashes"}].

    The build happens in a folder of its own beside `out` and is moved into
    place only when complete, so a failure leaves no half instance behind -
    and nothing at all, since the unfinished folder is cleared."""
    game = GAMES[game_key]
    out = os.path.abspath(out)
    why = check_out(out)
    if why:
        raise BuildError(why)
    if not os.path.isfile(os.path.join(game_path, game["exe"])):
        raise BuildError(f"{game_path} holds no {game['exe']}, so it is not a game folder")
    building = out + BUILDING_SUFFIX
    if os.path.isdir(building):
        _say(say, f"clearing an unfinished build at {building}")
        shutil.rmtree(building)
    os.makedirs(building)
    open(os.path.join(building, BUILDING_MARK), "w").close()
    try:
        _say(say, f"Mod Organizer {MO2_VERSION}, portable...")
        unpack(mo2_archive(say=say), building)
        if not os.path.isfile(os.path.join(building, "ModOrganizer.exe")):
            raise BuildError(f"{MO2_ARCHIVE} held no ModOrganizer.exe")
        open(os.path.join(building, "portable.txt"), "w").close()

        stock = os.path.join(building, STOCK_GAME)
        _say(say, f"Copying the game from {game_path} into {STOCK_GAME}...")
        count, size = copy_game(game_path, stock, game["not_copied"], say=say)
        os.makedirs(os.path.join(stock, game["data"]), exist_ok=True)
        _say(say, f"  {count} file(s), {WJ.human_bytes(size)}")

        records = []
        for i, m in enumerate(mods, 1):
            rec = install_mod(game, m, i, building, stock, say=say)
            records.append(rec)
            applied = sum(1 for p in rec["patches"] if p["result"] == "applied")
            line = f"[{i}/{len(mods)}] {m['name']}: {rec['files']} file(s)"
            if rec.get("game_files"):
                line += f", {len(rec['game_files'])} into {STOCK_GAME}"
            if rec["patches"]:
                line += f", {applied} of {len(rec['patches'])} patch(es) applied"
            _say(say, line)
            for p in rec["patches"]:
                if p["result"] != "applied":
                    _say(say, f"    patch for {p['file']} not applied: {p['result']}")
            for note in rec["notes"]:
                _say(say, f"    {note}")
        shutil.rmtree(os.path.join(building, ".wj-unpack"), ignore_errors=True)

        folders = [m["folder"] for m in mods]
        disabled = [m["folder"] for m in mods if not m.get("enabled", True)]
        prof_dir = os.path.join(building, "profiles", profile)
        _write(os.path.join(prof_dir, "modlist.txt"), modlist_txt(folders, disabled))
        _write(os.path.join(prof_dir, "settings.ini"), profile_settings())
        _write(os.path.join(building, "ModOrganizer.ini"), mo2_ini(game, out, profile, downloads))

        info = dict(record or {})
        info.update({"kind": "wj-mo2-instance", "format": FORMAT, "game": game_key,
                     "mo2": MO2_VERSION, "built": _now(),
                     "by": f"{CFG.APP_NAME} {CFG.__version__}", "profile": profile,
                     "stock_game": {"from": game_path, "files": count, "bytes": size},
                     "downloads": downloads, "mods": records})
        with open(os.path.join(building, MARK), "w", encoding="utf-8") as f:
            json.dump(info, f, indent=1, ensure_ascii=False)
        os.remove(os.path.join(building, BUILDING_MARK))
        if os.path.isdir(out):
            os.rmdir(out)                            # empty: check_out said so
        _move_into_place(building, out)
    except BaseException:
        shutil.rmtree(building, ignore_errors=True)
        raise
    return info


def _move_into_place(building, out, attempts=20, pause=0.5):
    """Rename the finished build to its own name. A virus scanner reading an
    executable the build has just written holds it open for a moment, and
    Windows refuses to rename a folder with a file open inside, so a refusal
    is waited out before it counts."""
    for attempt in range(attempts):
        try:
            os.replace(building, out)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(pause)
