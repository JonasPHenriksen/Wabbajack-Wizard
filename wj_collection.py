#!/usr/bin/env python3
"""
Nexus collections, as lists the rest of the suite already understands
=====================================================================
A Nexus collection is a small archive holding collection.json: every mod the
curator used, pinned as a Nexus mod id, file id, size and MD5, and how the
curator installed each one - the FOMOD answers, which mod wins a file
conflict, the plugin order. Vortex reads it and redoes the whole install on
your machine, a dialog at a time. This module reads it instead, and does two
things with it.

The first is to make the collection a list like any other. `fetch` turns
collection.json into a .collection file in the manifest folder: a zip whose
'modlist' member has the shape of the one inside a .wabbajack - a GameType,
and one Archives[] entry per file with its name, size, MD5 and a Nexus State.
From there nothing else in the suite needs to know what a collection is. The
downloader fetches its archives, through the assist window on a free account;
the sorter pools them beside every Wabbajack list's; Space protects them;
Prepare stages them into one folder. The pools pay off at once, because
'SkyUI-12604-6-11-1778020881.zip' is the same bytes whichever installer asked
for it: of the first collection measured here, 3 of its 11 files were already
held, each confirmed by MD5.

The second is the install, which is where a collection differs. Wabbajack
cannot install one, so the staged folder is handed to collections2mo2
(c2mo2), a GPL-3 Python tool that builds a portable Mod Organizer 2 instance
from a collection, with the game copied into a 'Stock Game' folder so nothing
touches the real install. It replays the curator's FOMOD answers, orders the
mods by the collection's rules, and routes SKSE and other game-root files
through Root Builder. What it lacks is any way to download on a free account.
But it skips an archive already in its downloads folder whose MD5 matches, so
the Wizard fills that folder first, checks every MD5 itself, and c2mo2 never
has to ask Nexus for a link.

c2mo2 is kept in .c2mo2 beside the scripts: a pinned commit, checked against
a checksum, with the few libraries its command line needs installed into
.c2mo2/site. Nothing is installed machine-wide and the folder moves with the
rest. It runs as a program of its own, which also keeps its licence apart from
this MIT code. The Nexus API key reaches it through its environment for that
one run, so it needs no sign-in of its own and stores nothing in Windows'
credential store.

c2mo2 builds the Bethesda games only. A collection for another game MO2 runs -
Stardew Valley, so far - is built by the Wizard itself (wj_mo2), in the same
shape: a portable MO2 with the game in Stock Game, each mod laid out as that
game's Vortex extension lays it, the rules deciding the order, and the
curator's binary patches applied. Its bundled mods and patches come from the
collection archive, which is why a list file keeps what that archive carried
besides collection.json.

What the file names are, and why they come from the v1 API: a collection
records each file's display name, not the name it downloads under, and the
pools and c2mo2 both know an archive by that download name. The GraphQL API
has a `uri` field that looks like it, but for files uploaded since 2026 it is a
storage path, so each file's own v1 record is asked instead, the way c2mo2
asks it. Its answers are kept in the list file, and a later revision asks only
about the files that changed.

A collection is built for one game version, and SKSE plugins load on no other.
When the store's install is newer, `assemble-game` lays the older depots that
Steam's console downloads into a game folder of their own, and the build copies
that into Stock Game instead; the store's install is never touched.

Commands:
    wj_collection.py fetch <collection URL or slug> [--revision N] [--force]
    wj_collection.py inspect <list name, URL or slug> [--md5] [--no-game]
    wj_collection.py setup-c2mo2
    wj_collection.py assemble-game --out <new game folder> [--game GAMETYPE] [--depots A,B,C]
    wj_collection.py install <list name> --out <instance folder> [--game-path DIR] [--yes]
"""

import argparse
import concurrent.futures
import ctypes
import hashlib
import json
import logging
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone

import wj_paths
import wj_config as CFG
import wj_downloader as WJ
import wj_mo2 as MO2
import wj_wabbajack as WB

LOG = logging.getLogger("wj_collection")

API_BASE = "https://api.nexusmods.com"
GRAPHQL_URL = API_BASE + "/v2/graphql"
SITE_URL = "https://www.nexusmods.com"
SEP = "_@@_"
FORMAT = 2                       # of wizard.json inside a .collection; 2 keeps the extras

# A collection names its game by Nexus domain; the pools know it by the
# GameType Wabbajack uses. These are the games c2mo2 can build an instance for;
# wj_mo2.GAMES adds the ones the Wizard builds itself, and a collection for any
# other game is refused at fetch rather than pooled for an install that cannot
# happen.
C2MO2_DOMAINS = {
    "skyrimspecialedition":  "SkyrimSpecialEdition",
    "skyrim":                "Skyrim",
    "skyrimvr":              "SkyrimVR",
    "fallout4":              "Fallout4",
    "fallout4vr":            "Fallout4VR",
    "newvegas":              "FalloutNewVegas",
    "falloutnv":             "FalloutNewVegas",
    "fallout3":              "Fallout3",
    "oblivion":              "Oblivion",
    "morrowind":             "Morrowind",
    "starfield":             "Starfield",
    "enderal":               "Enderal",
    "enderalspecialedition": "EnderalSpecialEdition",
}
DOMAIN_GAMETYPE = dict(C2MO2_DOMAINS, **{d: g["gametype"] for d, g in MO2.GAMES.items()})

# The executable whose version a collection's gameVersions is compared with.
GAME_EXES = {
    "StardewValley":         "Stardew Valley.exe",
    "SkyrimSpecialEdition":  "SkyrimSE.exe",
    "Skyrim":                "TESV.exe",
    "SkyrimVR":              "SkyrimVR.exe",
    "Fallout4":              "Fallout4.exe",
    "Fallout4VR":            "Fallout4VR.exe",
    "FalloutNewVegas":       "FalloutNV.exe",
    "Fallout3":              "Fallout3.exe",
    "Oblivion":              "Oblivion.exe",
    "Morrowind":             "Morrowind.exe",
    "Starfield":             "Starfield.exe",
    "Enderal":               "TESV.exe",
    "EnderalSpecialEdition": "SkyrimSE.exe",
}

# ---- collections2mo2 -----------------------------------------------------
# The commit read before it was trusted with an install, and the SHA-256 of
# GitHub's archive of it. A different archive is refused rather than run.
C2MO2_DIRNAME = ".c2mo2"
C2MO2_REPO = "https://github.com/spooknik/collections2mo2"
C2MO2_COMMIT = "ca460c705d7e097a2a3cf1b66bbd65d00740ee52"      # 2026-09-19, after 0.2.0
C2MO2_ZIP_URL = "https://codeload.github.com/spooknik/collections2mo2/zip/" + C2MO2_COMMIT
C2MO2_ZIP_SHA256 = "1056d1b1242431946722f4bddc7477ae4f33fd24c013f26ea9585aa106805aae"
# What its command line imports, at the versions its uv.lock pins. PySide6,
# its window, is left out: the Wizard is the window.
C2MO2_REQUIREMENTS = ("requests==2.34.2", "python-dotenv==1.2.3", "py7zr==1.1.3",
                      "keyring==25.7.0")
C2MO2_CONSTRAINTS = (
    "certifi==2026.7.22", "charset-normalizer==3.5.1", "idna==3.19", "urllib3==2.7.0",
    "texttable==1.7.0", "pycryptodomex==3.23.0", "pyppmd==1.3.1", "pybcj==1.0.8",
    "multivolumefile==0.2.3", "inflate64==1.0.4", "brotli==1.2.0", "psutil==7.2.2",
    "backports-zstd==1.7.0", "jaraco-classes==3.4.0", "jaraco-context==6.1.2",
    "jaraco-functools==4.6.0", "more-itertools==11.1.0", "pywin32-ctypes==0.2.3",
)
C2MO2_STAMP = "installed.json"

# Parallel requests for the per-file records at fetch. Nexus meters by the hour
# and the day, not the second, so this only hides latency; c2mo2 uses four too.
FILE_INFO_WORKERS = 4


class CollectionError(Exception):
    """Something that stops a collection job, said in words."""


def here():
    return wj_paths.app_dir(__file__)


def _say(say, text=""):
    (say or print)(text)


# ----------------------------------------------------------------------
# Naming a collection
# ----------------------------------------------------------------------
_URL_RE = re.compile(r"^(?:https?://)?(?:[a-z]+\.)?nexusmods\.com/(?:games/)?(?P<game>[a-z0-9_-]+)"
                     r"/collections/(?P<slug>[a-z0-9]+)(?:/revisions/(?P<rev>\d+))?", re.I)
_NXM_RE = re.compile(r"^nxm://(?P<game>[a-z0-9_-]+)/collections/(?P<slug>[a-z0-9]+)"
                     r"(?:/revisions/(?P<rev>\d+|latest))?", re.I)
_SLUG_RE = re.compile(r"^[a-z0-9]{4,16}$", re.I)


def parse_ref(text):
    """{"game", "slug", "revision"} from a collection's web address, its
    nxm:// link, or a bare slug; None when the text is none of those. `game`
    and `revision` are None when the text does not say."""
    text = (text or "").strip().strip('"')
    for rx in (_URL_RE, _NXM_RE):
        m = rx.match(text)
        if m:
            rev = m.group("rev")
            return {"game": m.group("game").lower(), "slug": m.group("slug").lower(),
                    "revision": int(rev) if rev and rev.isdigit() else None}
    if _SLUG_RE.match(text):
        return {"game": None, "slug": text.lower(), "revision": None}
    return None


def collection_url(domain, slug):
    """The collection's page, in the form c2mo2 takes on its command line."""
    return f"{SITE_URL}/games/{domain}/collections/{slug}"


def list_stem(name, slug):
    """The list file's stem, and so the name of its pool folder: the
    collection's name made safe for a folder, then its slug.

    `<name>_@@_<slug>` follows the Wabbajack lists' `<repo>_@@_<machineURL>`.
    The slug is the collection's identity - the name is the curator's to
    change - so a later revision keeps whatever file the first one made, and
    the pool folder is never renamed under a list."""
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name or ""))
    clean = re.sub(r"\s+", " ", clean).strip(" ._")[:60].strip(" ._")
    return f"{clean or 'collection'}{SEP}{slug.lower()}"


def find_list_file(manifest_dir, slug):
    """The .collection held for this slug, or None."""
    tail = (SEP + slug + CFG.COLLECTION_EXT).lower()
    for fn in WJ.list_manifest_files(manifest_dir):
        if fn.lower().endswith(tail):
            return fn
    return None


def resolve_list(manifest_dir, text):
    """The .collection a name, URL or slug refers to, or None: a list name
    (with or without its extension), or anything parse_ref() reads."""
    text = (text or "").strip().strip('"')
    names = [f for f in WJ.list_manifest_files(manifest_dir) if CFG.is_collection(f)]
    for fn in names:
        if text.lower() in (fn.lower(), os.path.splitext(fn)[0].lower()):
            return fn
    ref = parse_ref(text)
    if ref:
        return find_list_file(manifest_dir, ref["slug"])
    return None


# ----------------------------------------------------------------------
# Nexus: GraphQL for the revision, v1 for the files
# ----------------------------------------------------------------------
def _headers():
    return {"Accept": "application/json", "Content-Type": "application/json",
            "Application-Name": CFG.APP_NAME, "Application-Version": CFG.__version__,
            "User-Agent": CFG.USER_AGENT}


def graphql(query, variables=None, timeout=60, attempts=3):
    """One GraphQL request, anonymous: collection metadata needs no key."""
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    last = None
    for attempt in range(attempts):
        req = urllib.request.Request(GRAPHQL_URL, data=body, headers=_headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                raise CollectionError(f"Nexus refused the collection query (HTTP {e.code})")
            last = e
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            last = e
        else:
            errors = data.get("errors") if isinstance(data, dict) else None
            if errors:
                raise CollectionError("Nexus answered the collection query with: "
                                      + "; ".join(str(e.get("message")) for e in errors))
            return (data or {}).get("data") or {}
        time.sleep(2 * (attempt + 1))
    raise CollectionError(f"Nexus could not be reached for the collection ({last})")


REVISION_QUERY = """
query($slug: String!, $revision: Int) {
  collection(slug: $slug, viewAdultContent: true) {
    name slug game { domainName } user { name }
    latestPublishedRevision { revisionNumber }
  }
  collectionRevision(slug: $slug, revision: $revision, viewAdultContent: true) {
    revisionNumber revisionStatus modCount totalSize downloadLink adultContent
  }
}"""


def revision_info(slug, revision=None):
    """What Nexus says about one revision of a collection; the latest
    published one when `revision` is None."""
    data = graphql(REVISION_QUERY, {"slug": slug, "revision": revision})
    coll, rev = data.get("collection"), data.get("collectionRevision")
    if not coll:
        raise CollectionError(f"Nexus has no collection '{slug}'")
    if not rev:
        raise CollectionError(f"collection '{slug}' has no revision {revision}" if revision
                              else f"collection '{slug}' has no published revision")
    if not rev.get("downloadLink"):
        raise CollectionError(f"revision {rev.get('revisionNumber')} of '{slug}' offers no "
                              f"download (unpublished, or retracted by its curator)")
    latest = (coll.get("latestPublishedRevision") or {}).get("revisionNumber")
    return {
        "slug": str(coll.get("slug") or slug).lower(),
        "name": coll.get("name") or slug,
        "domain": ((coll.get("game") or {}).get("domainName") or "").lower() or None,
        "author": (coll.get("user") or {}).get("name") or "",
        "revision": int(rev["revisionNumber"]),
        "latest": int(latest) if latest is not None else None,
        "status": rev.get("revisionStatus"),
        "mod_count": int(rev.get("modCount") or 0),
        "total_size": int(rev.get("totalSize") or 0),
        "download_link": rev["downloadLink"],
        "adult": bool(rev.get("adultContent")),
    }


def latest_revision(slug):
    """The newest published revision number, or None when Nexus cannot say."""
    try:
        data = graphql("query($slug: String!) { collection(slug: $slug, viewAdultContent: true)"
                       " { latestPublishedRevision { revisionNumber } } }", {"slug": slug})
    except CollectionError:
        return None
    rev = ((data.get("collection") or {}).get("latestPublishedRevision") or {})
    return int(rev["revisionNumber"]) if rev.get("revisionNumber") is not None else None


# ----------------------------------------------------------------------
# The collection archive
# ----------------------------------------------------------------------
SEVENZIP_MAGIC = b"7z\xbc\xaf\x27\x1c"
seven_zip = MO2.seven_zip

# What a collection archive carries besides collection.json, by top folder:
# mods packed into the collection itself, binary patches, INI tweaks. A list
# file keeps them under EXTRAS_PREFIX, unless together they are more than a
# list file should carry; a build then fetches the archive again.
EXTRA_TOPS = ("bundled", "patches", "ini tweaks")
EXTRAS_PREFIX = "archive/"
EXTRAS_CAP = 256 * 1024 * 1024


def _slt_files(text):
    """The files - not folders - of a `7z l -slt` listing. A folder says so in
    its Attributes in a 7z, and with Folder = + in some other formats."""
    out, cur = [], {}
    for line in text.splitlines() + [""]:
        if not line.strip():
            if cur.get("Path") and cur.get("Folder") != "+" \
                    and not cur.get("Attributes", "").startswith("D"):
                out.append(cur["Path"])
            cur = {}
            continue
        key, _sep, value = line.partition(" = ")
        cur[key.strip()] = value.strip()
    return out


def read_archive(path):
    """(collection.json as a dict, member names) from a collection archive.

    Only collection.json is read here, and only into memory; read_extras()
    takes the rest. The member names - files, not folders - say what the
    archive carries."""
    with open(path, "rb") as f:
        head = f.read(6)
    if head.startswith(b"PK"):
        with zipfile.ZipFile(path) as z:
            members = z.namelist()
            target = next((m for m in members if m.replace("\\", "/").lower()
                           == "collection.json"), None)
            if target is None:
                raise CollectionError("the collection archive holds no collection.json")
            raw = z.read(target)
    elif head == SEVENZIP_MAGIC:
        exe = seven_zip()
        if not exe:
            raise CollectionError("no 7-Zip was found to open the collection archive (the "
                                  "one inside Wabbajack's install is used when present)")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        listing = subprocess.run([exe, "l", "-ba", "-slt", path], capture_output=True,
                                 stdin=subprocess.DEVNULL, creationflags=flags)
        text = listing.stdout.decode("utf-8", "replace")
        members = _slt_files(text)
        if listing.returncode != 0 or not members:
            raise CollectionError("7-Zip could not list the collection archive")
        target = next((m for m in members if m.replace("\\", "/").lower() == "collection.json"),
                      None)
        if target is None:
            raise CollectionError("the collection archive holds no collection.json")
        got = subprocess.run([exe, "e", "-so", path, target], capture_output=True,
                             stdin=subprocess.DEVNULL, creationflags=flags)
        if got.returncode != 0 or not got.stdout:
            raise CollectionError("7-Zip could not extract collection.json")
        raw = got.stdout
    else:
        raise CollectionError("the collection archive is neither a 7z nor a zip")
    try:
        manifest = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        raise CollectionError(f"collection.json does not parse ({e})")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("mods"), list):
        raise CollectionError("collection.json has no mods list")
    return manifest, [m.replace("\\", "/") for m in members]


def _is_extra(rel):
    parts = rel.split("/")
    return len(parts) > 1 and parts[0].lower() in EXTRA_TOPS and bool(parts[-1])


def read_extras(path):
    """{member: bytes} of what a collection archive carries besides
    collection.json, or None when that is more than EXTRAS_CAP."""
    with open(path, "rb") as f:
        head = f.read(6)
    if head.startswith(b"PK"):
        with zipfile.ZipFile(path) as z:
            infos = [i for i in z.infolist()
                     if not i.is_dir() and _is_extra(i.filename.replace("\\", "/"))]
            if sum(i.file_size for i in infos) > EXTRAS_CAP:
                return None
            return {i.filename.replace("\\", "/"): z.read(i) for i in infos}
    exe = seven_zip()
    if not exe:
        raise CollectionError("no 7-Zip was found to open the collection archive")
    scratch = tempfile.mkdtemp(prefix="wj-collection-")
    try:
        got = subprocess.run([exe, "x", "-y", "-o" + scratch, path], capture_output=True,
                             stdin=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if got.returncode not in (0, 1):
            raise CollectionError("7-Zip could not unpack the collection archive")
        found = [f for f in MO2.list_files(scratch) if _is_extra(f)]
        if sum(os.path.getsize(os.path.join(scratch, *f.split("/"))) for f in found) > EXTRAS_CAP:
            return None
        out = {}
        for f in found:
            with open(os.path.join(scratch, *f.split("/")), "rb") as fh:
                out[f] = fh.read()
        return out
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def member_counts(members):
    """How many files the archive carries besides collection.json, by kind."""
    counts = {"bundled": 0, "patches": 0, "ini_tweaks": 0}
    tops = {"bundled": "bundled", "patches": "patches", "ini tweaks": "ini_tweaks"}
    for m in members:
        parts = m.split("/")
        key = tops.get(parts[0].lower()) if len(parts) > 1 and parts[-1] else None
        if key:
            counts[key] += 1
    return counts


# ----------------------------------------------------------------------
# collection.json -> a list file
# ----------------------------------------------------------------------
def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def direct_file_name(url, src, mod):
    """The name a 'direct' source's archive is kept under.

    It has to be the name c2mo2 looks for, since c2mo2 finds an archive in the
    downloads folder by name before it checks the MD5, so this follows its
    rule: the last part of the URL's path, URL-decoded; else the collection's
    logical file name; else the mod's name when it carries an extension."""
    path = urllib.parse.urlsplit(url or "").path
    base = urllib.parse.unquote(posixpath.basename(path)) if path else ""
    if base:
        return base
    if src.get("logicalFilename"):
        return str(src["logicalFilename"])
    name = str(mod.get("name") or "")
    return name if posixpath.splitext(name)[1] else None


def _nexus_entry(domain, mod_id, file_id, file_name, size, src, mod):
    gametype = DOMAIN_GAMETYPE.get(domain, domain)
    return {
        "Name": file_name,
        "Size": size,
        "Hash": None,
        "MD5": (src.get("md5") or "").lower() or None,
        # The .meta the downloader writes beside the archive, in the form MO2
        # and Wabbajack both read.
        "Meta": "[General]\ngameName=%s\nmodID=%d\nfileID=%d\n" % (domain, mod_id, file_id),
        "State": {"$type": "NexusDownloader, Wabbajack.Lib", "GameName": gametype,
                  "ModID": mod_id, "FileID": file_id, "Name": mod.get("name"),
                  "Version": mod.get("version"), "Author": mod.get("author")},
        "Collection": {"mod": mod.get("name"), "tag": src.get("tag"),
                       "optional": bool(mod.get("optional")), "source": "nexus"},
    }


def _http_entry(url, file_name, src, mod):
    return {
        "Name": file_name,
        "Size": _int(src.get("fileSize")),
        "Hash": None,
        "MD5": (src.get("md5") or "").lower() or None,
        "Meta": "[General]\ndirectURL=%s\n" % url,
        "State": {"$type": "HttpDownloader, Wabbajack.Lib", "Url": url, "Headers": []},
        "Collection": {"mod": mod.get("name"), "tag": src.get("tag"),
                       "optional": bool(mod.get("optional")), "source": "direct"},
    }


def archive_entries(nexus, manifest, known=None, say=None):
    """One Wabbajack-shaped Archives[] entry per file the collection downloads,
    and a note for every mod that cannot be one.

    `known` is {(mod id, file id): (file name, size)} from a list file already
    held, so a new revision only asks Nexus about the files that changed.
    Returns (entries, left_out), left_out being [{"mod", "source", "why"}]."""
    info = manifest.get("info") or {}
    default_domain = (info.get("domainName") or "").lower()
    known = known or {}
    mods = manifest.get("mods") or []
    nexus_mods, entries, left_out = [], [], []
    for mod in mods:
        src = mod.get("source") or {}
        kind = (src.get("type") or "").lower()
        name = mod.get("name") or "?"
        if kind == "nexus":
            mod_id, file_id = _int(src.get("modId")), _int(src.get("fileId"))
            if not (mod_id and file_id):
                left_out.append({"mod": name, "source": kind, "why": "no Nexus mod or file id"})
                continue
            nexus_mods.append(((mod.get("domainName") or default_domain).lower(),
                               mod_id, file_id, src, mod))
        elif kind == "direct":
            url = src.get("url")
            fname = direct_file_name(url, src, mod) if url else None
            if not fname:
                left_out.append({"mod": name, "source": kind, "why": "no URL to download from"})
                continue
            entries.append(_http_entry(url, fname, src, mod))
        elif kind == "bundle":
            left_out.append({"mod": name, "source": kind,
                             "why": "packed inside the collection itself and unpacked by the "
                                    "build; nothing to download"})
        else:
            where = src.get("url") or src.get("instructions") or mod.get("instructions") or ""
            left_out.append({"mod": name, "source": kind or "?",
                             "why": "downloaded by hand from another site, which no build "
                                    "here does yet" + (f" ({where})" if where else "")})

    def lookup(item):
        domain, mod_id, file_id, src, mod = item
        if (mod_id, file_id) in known:
            return item, known[(mod_id, file_id)], "ok"
        rec, status = nexus.file_info(domain, mod_id, file_id)
        if status != "ok" or not (rec or {}).get("file_name"):
            return item, None, status
        return item, (rec["file_name"], _int(rec.get("size_in_bytes"))), "ok"

    asked = sum(1 for d, m, f, s, mo in nexus_mods if (m, f) not in known)
    if asked:
        _say(say, f"  Asking Nexus for the file names of {asked} file(s)...")
    results = [None] * len(nexus_mods)
    with concurrent.futures.ThreadPoolExecutor(max_workers=FILE_INFO_WORKERS) as pool:
        futures = {pool.submit(lookup, item): i for i, item in enumerate(nexus_mods)}
        for fut in concurrent.futures.as_completed(futures):
            results[futures[fut]] = fut.result()
    for (domain, mod_id, file_id, src, mod), named, status in results:
        if named is None:
            why = ("Nexus no longer serves this file (the author removed it)"
                   if status == "notfound" else f"its file record could not be read ({status})")
            left_out.append({"mod": mod.get("name") or "?", "source": "nexus", "why": why})
            continue
        file_name, size = named
        size = _int(src.get("fileSize")) or size
        entries.append(_nexus_entry(domain, mod_id, file_id, file_name, size, src, mod))
    return entries, left_out


def write_list_file(path, info, manifest, entries, left_out, members, extras=None):
    """Write the .collection: 'modlist' for every stage that reads a list,
    collection.json as fetched, wizard.json for this module, and under
    archive/ what the collection archive carried besides collection.json
    (`extras`, from read_extras(); None when it was not kept)."""
    domain = info["domain"]
    modlist = {
        "Name": info["name"],
        "Author": info["author"],
        "Version": str(info["revision"]),
        "GameType": DOMAIN_GAMETYPE[domain],
        "Description": (manifest.get("info") or {}).get("description") or "",
        "Archives": entries,
        # A .wabbajack's directives say how to install it. A collection is
        # installed by c2mo2 from collection.json, so there are none.
        "Directives": [],
    }
    wizard = {
        "kind": "nexus-collection",
        "format": FORMAT,
        "slug": info["slug"],
        "revision": info["revision"],
        "latest": info.get("latest"),
        "domain": domain,
        "url": collection_url(domain, info["slug"]),
        "name": info["name"],
        "author": info["author"],
        "mod_count": info.get("mod_count"),
        "total_size": info.get("total_size"),
        "fetched": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "members": member_counts(members),
        "left_out": left_out,
        "extras": extras is not None,
    }
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("modlist", json.dumps(modlist, indent=1, ensure_ascii=False))
        z.writestr("collection.json", json.dumps(manifest, indent=1, ensure_ascii=False))
        z.writestr("wizard.json", json.dumps(wizard, indent=1, ensure_ascii=False))
        for member, data in sorted((extras or {}).items()):
            z.writestr(EXTRAS_PREFIX + member, data)
    os.replace(tmp, path)
    return path


def list_extras(path):
    """What the collection archive carried besides collection.json, as the
    list file keeps it: {member: bytes}. None when the list does not keep them
    - fetched before lists did, or too big to keep."""
    try:
        with zipfile.ZipFile(path) as z:
            names = {n.lower(): n for n in z.namelist()}
            wiz = json.loads(z.read(names["wizard.json"]).decode("utf-8-sig"))
            if not wiz.get("extras"):
                return None
            return {n[len(EXTRAS_PREFIX):]: z.read(n) for n in z.namelist()
                    if n.startswith(EXTRAS_PREFIX) and not n.endswith("/")}
    except (OSError, KeyError, zipfile.BadZipFile, ValueError) as e:
        raise CollectionError(f"{os.path.basename(path)} could not be read ({e})")


def read_list_file(path):
    """{"modlist", "collection", "wizard"} from a .collection."""
    try:
        with zipfile.ZipFile(path) as z:
            names = {n.lower(): n for n in z.namelist()}
            got = {}
            for key, member in (("modlist", "modlist"), ("collection", "collection.json"),
                                ("wizard", "wizard.json")):
                if member not in names:
                    raise CollectionError(f"{os.path.basename(path)} has no {member}")
                got[key] = json.loads(z.read(names[member]).decode("utf-8-sig"))
    except (OSError, zipfile.BadZipFile, ValueError) as e:
        raise CollectionError(f"{os.path.basename(path)} could not be read ({e})")
    if got["wizard"].get("kind") != "nexus-collection":
        raise CollectionError(f"{os.path.basename(path)} is not a Nexus collection list")
    return got


def fetch(text, manifest_dir, nexus, revision=None, force=False, say=None):
    """Fetch a collection and write or refresh its list file. Returns
    (filename, changed): changed is False when that revision was already held.

    Nothing is written until everything has been read, so a failure half way
    leaves whatever list file was there before exactly as it was."""
    ref = parse_ref(text)
    if not ref:
        raise CollectionError(f"'{text}' is not a collection's address, nxm:// link or slug")
    info = revision_info(ref["slug"], revision or ref["revision"])
    domain = info["domain"] or ref["game"]
    if domain not in DOMAIN_GAMETYPE:
        raise CollectionError(f"'{info['name']}' is a collection for '{domain}', which "
                              f"neither collections2mo2 nor the Wizard's own build can make "
                              f"an instance for yet")
    info["domain"] = domain

    existing = find_list_file(manifest_dir, info["slug"])
    known = {}
    if existing:
        try:
            held = read_list_file(os.path.join(manifest_dir, existing))
        except CollectionError:
            held = None
        if held and held["wizard"].get("revision") == info["revision"] and not force:
            _say(say, f"  {existing} already holds revision {info['revision']}; nothing to fetch.")
            return existing, False
        for a in (held or {}).get("modlist", {}).get("Archives") or []:
            st = a.get("State") or {}
            if st.get("ModID") and st.get("FileID") and a.get("Name"):
                known[(int(st["ModID"]), int(st["FileID"]))] = (a["Name"], a.get("Size"))

    _say(say, f"  {info['name']} by {info['author'] or '?'}, revision {info['revision']}: "
              f"{info['mod_count']} mods, {WJ.human_bytes(info['total_size'])}.")
    link, status = nexus.collection_archive_url(info["download_link"])
    if not link:
        raise CollectionError("Nexus would not hand out the collection archive: " + status)
    scratch = os.path.join(manifest_dir, f".{info['slug']}-{info['revision']}.download")
    try:
        if not WJ.stream_download(link, scratch, quiet=True):
            raise CollectionError("the collection archive did not download")
        manifest, members = read_archive(scratch)
        extras = read_extras(scratch)
    finally:
        for p in (scratch, scratch + ".part"):
            try:
                os.remove(p)
            except OSError:
                pass

    entries, left_out = archive_entries(nexus, manifest, known=known, say=say)
    filename = existing or (list_stem(info["name"], info["slug"]) + CFG.COLLECTION_EXT)
    write_list_file(os.path.join(manifest_dir, filename), info, manifest, entries, left_out,
                    members, extras=extras)
    _say(say, f"  Wrote {filename}: {len(entries)} archive(s) to pool"
              + (f", {len(left_out)} mod(s) not downloadable here" if left_out else "") + ".")
    return filename, True


# ----------------------------------------------------------------------
# What a collection asks of this machine
# ----------------------------------------------------------------------
def internal_rules(manifest):
    """How many before/after rules name two mods of this collection.

    A curator's rules are copied from their whole Vortex setup, so many name a
    mod the collection does not include; only a rule whose two ends are both in
    it can decide a conflict here, and those are the ones c2mo2 applies. Both
    ends are matched by MD5, the one identity they carry reliably."""
    md5s = {((m.get("source") or {}).get("md5") or "").lower()
            for m in manifest.get("mods") or []} - {""}
    n = 0
    for r in manifest.get("modRules") or []:
        if r.get("type") not in ("before", "after"):
            continue
        a = (((r.get("source") or {}).get("fileMD5")) or "").lower()
        b = (((r.get("reference") or {}).get("fileMD5")) or "").lower()
        n += a in md5s and b in md5s
    return n


def features(manifest):
    """What the collection's install uses, counted: the parts c2mo2 replays
    and the parts it cannot."""
    mods = manifest.get("mods") or []
    kinds = {}
    for m in mods:
        k = ((m.get("source") or {}).get("type") or "?").lower()
        kinds[k] = kinds.get(k, 0) + 1
    typed = {}
    for m in mods:
        t = (m.get("details") or {}).get("type") or ""
        if t:
            typed[t] = typed.get(t, 0) + 1
    rules = manifest.get("modRules") or []
    return {
        "mods": len(mods),
        "sources": kinds,
        "fomod": sum(1 for m in mods if (m.get("choices") or {}).get("type") == "fomod"),
        "replicate": sum(1 for m in mods if m.get("hashes")),
        "patches": sum(1 for m in mods if m.get("patches")),
        "overrides": sum(1 for m in mods if m.get("fileOverrides")),
        "optional": sum(1 for m in mods if m.get("optional")),
        "types": typed,
        "rules": len(rules),
        "rules_internal": internal_rules(manifest),
        "plugins": len(manifest.get("plugins") or []),
        "plugin_rules": len(((manifest.get("pluginRules") or {}).get("plugins")) or []),
        "tools": len(manifest.get("tools") or []),
        "versions": list((manifest.get("info") or {}).get("gameVersions") or []),
    }


_DOTTED = re.compile(r"^\s*(\d+)[.,]\s*(\d+)[.,]\s*(\d+)(?:[.,]\s*(\d+))?")


def dotted_version(text):
    """'1.5.97.0' from a version string as resource tables write it - '1.5.97.0',
    '1, 5, 97, 0', with trailing words or not - or None."""
    m = _DOTTED.match(text or "")
    if not m:
        return None
    return ".".join(str(int(p)) for p in m.groups(default="0"))


def exe_version(path):
    """An executable's file version as 'a.b.c.d', or None.

    The version as the file's own string table writes it, the one Explorer and
    PowerShell show, and only failing that the fixed binary field. Old
    Bethesda executables leave the fixed field at 1.0.0.0: Skyrim 1.5.97's
    SkyrimSE.exe says 1.5.97.0 only in its strings, where 1.7.104's says it in
    both, so reading the fixed field alone mistook 1.5.97 for 1.0."""
    if os.name != "nt" or not os.path.isfile(path):
        return None
    try:
        from ctypes import wintypes
        ver = ctypes.WinDLL("version")
        ver.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        ver.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                            ctypes.c_void_p]
        ver.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                       ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.POINTER(wintypes.UINT)]
        size = ver.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(path, 0, size, buf):
            return None

        def query(key):
            ptr, n = ctypes.c_void_p(), wintypes.UINT()
            if not ver.VerQueryValueW(buf, key, ctypes.byref(ptr), ctypes.byref(n)) or not ptr:
                return None, 0
            return ptr, n.value

        # The string table, under each language and code page the file lists,
        # then the ones resource compilers write when they list none.
        tables = []
        ptr, n = query("\\VarFileInfo\\Translation")
        if ptr and n >= 4:
            pairs = ctypes.cast(ptr, ctypes.POINTER(wintypes.WORD * (n // 2))).contents
            tables += ["%04x%04x" % (pairs[i], pairs[i + 1]) for i in range(0, n // 2 - 1, 2)]
        tables += ["040904b0", "040904e4", "000004b0"]
        for table in tables:
            for field in ("FileVersion", "ProductVersion"):
                ptr, n = query("\\StringFileInfo\\%s\\%s" % (table, field))
                if ptr and n:
                    found = dotted_version(ctypes.wstring_at(ptr, n).rstrip("\x00"))
                    if found:
                        return found
        ptr, n = query("\\")
        if not ptr:
            return None
        words = ctypes.cast(ptr, ctypes.POINTER(wintypes.DWORD * 13)).contents
        ms, ls = words[2], words[3]          # dwFileVersionMS, dwFileVersionLS
        return "%d.%d.%d.%d" % (ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF)
    except (OSError, AttributeError, ValueError):
        return None


def version_matches(have, wanted):
    """Does the installed version satisfy any the collection names? The fourth
    part is a build counter nobody pins by, so three parts decide, as in c2mo2.
    None when there is nothing to compare."""
    if not have or not wanted:
        return None
    mine = have.split(".")[:3]
    return any(str(w).split(".")[:3] == mine for w in wanted)


def game_install(gametype, cli=None):
    """(path, how) for the game this collection builds on, as Wabbajack's CLI
    finds it: the same store scan the Wabbajack preflight uses. (None, why)
    when it cannot be found."""
    wanted = WB.WABBAJACK_GAME_NAMES.get(gametype)
    if not wanted:
        return None, f"no store scan knows {gametype}"
    if cli is None:
        wj = WB.find_wabbajack()
        cli = (wj or {}).get("cli")
    if not cli:
        return None, "wabbajack-cli.exe was not found, so the game install could not be looked up"
    path = WB.detected_games(cli).get(wanted)
    if not path:
        return None, f"Wabbajack's store scan found no installation of {wanted}"
    return path, f"{wanted} found by Wabbajack's store scan"


def pool_status(list_path, md5=False, say=None):
    """Which of the list's archives the pools hold, and which need fetching.

    The same test the downloader plans with - name and size, anywhere in the
    game's pools - and with `md5` every hit is also read in full and checked
    against the collection's MD5, which is the only identity it pins."""
    manifest_dir = os.path.dirname(list_path)
    rec = WJ.parse_manifest(list_path)
    if not rec["readable"] or not rec["game"]:
        raise CollectionError(f"{os.path.basename(list_path)} could not be read as a list")
    # Scanned afresh every time, as every stage scans the pools. The window
    # imports the downloader twice over (wj_autoupdate loads its own copy), and
    # a cached scan in this copy would never see what that one's downloads added.
    WJ.invalidate_existing_cache()
    WJ.set_known_list_dirs(WJ.known_list_dirs_from(manifest_dir))
    game_base, _m, _s = WJ.resolve_game_dirs(rec["game"])
    index = WJ.index_existing(game_base)
    have, need, wrong = [], [], []
    for a in rec["archives"]:
        hit, _why = WJ.present_match(index, a)
        if hit and md5 and a.get("md5"):
            if WJ.file_md5(hit) != a["md5"]:
                wrong.append((a, hit))
                continue
        (have if hit else need).append(a)
    return {"game": rec["game"], "have": have, "need": need, "md5_wrong": wrong,
            "md5_checked": bool(md5)}


def inspect(list_path, md5=False, look_up_game=True, look_up_latest=True, say=None):
    """Everything worth knowing before committing to a collection."""
    data = read_list_file(list_path)
    wiz, manifest = data["wizard"], data["collection"]
    report = {"file": os.path.basename(list_path), "wizard": wiz, "features": features(manifest),
              "pools": pool_status(list_path, md5=md5, say=say), "game": None, "latest": None}
    gametype = report["pools"]["game"]
    if look_up_game:
        path, how = game_install(gametype)
        exe = os.path.join(path, GAME_EXES.get(gametype, "")) if path else None
        have = exe_version(exe) if exe else None
        report["game"] = {"path": path, "how": how, "version": have,
                          "matches": version_matches(have, report["features"]["versions"])}
    if look_up_latest:
        report["latest"] = latest_revision(wiz["slug"])
    return report


def engine_for(domain):
    """Who builds an instance for a game: "c2mo2" for the Bethesda games,
    "wizard" for the ones wj_mo2 knows, None for any other."""
    if domain in C2MO2_DOMAINS:
        return "c2mo2"
    if domain in MO2.GAMES:
        return "wizard"
    return None


def installer_name(plan_or_domain):
    """The installer, named for a message."""
    engine = plan_or_domain.get("engine") if isinstance(plan_or_domain, dict) \
        else engine_for(plan_or_domain)
    return "collections2mo2" if engine == "c2mo2" else "the Wizard's own build"


def version_note(plan_or_domain):
    """Why a game version other than the collection's matters, for this game."""
    domain = plan_or_domain["info"]["domain"] if isinstance(plan_or_domain, dict) \
        else plan_or_domain
    if engine_for(domain) == "wizard":
        loader = MO2.GAMES[domain]["loader"]
        return (f"{loader} and its mods are built against one game version; on another, "
                f"some of them fail to load")
    return "SKSE plugins are built for one game version; on a different one they fail to load"


def needs_setup(plan):
    """Is the installer itself still to be set up (only c2mo2 ever is)?"""
    return plan.get("engine") == "c2mo2" and not plan.get("c2")


def done_lines(plan):
    """What to know about an instance the Wizard built, once it is built."""
    if plan.get("engine") != "wizard":
        return []
    return list(MO2.GAMES[plan["info"]["domain"]].get("after_build") or ())


def report_lines(report):
    """The report as the lines the console, the window and the CLI all show."""
    wiz, feat, pools = report["wizard"], report["features"], report["pools"]
    wizard = engine_for(wiz.get("domain")) == "wizard"
    lines = [f"{wiz['name']} by {wiz.get('author') or '?'}  (revision {wiz['revision']}, "
             f"{wiz['url']})"]
    latest = report.get("latest")
    if latest and latest > wiz["revision"]:
        lines.append(f"  Revision {latest} is published; this list holds {wiz['revision']}. "
                     f"Fetch it again to move to it.")
    have = sum(a["size"] or 0 for a in pools["have"])
    need = sum(a["size"] or 0 for a in pools["need"])
    nexus_need = sum(1 for a in pools["need"] if WJ.state_type(a["state"]) == "nexus")
    lines.append(f"  Archives   {len(pools['have']) + len(pools['need']) + len(pools['md5_wrong'])}"
                 f" to pool: {len(pools['have'])} already held ({WJ.human_bytes(have)})"
                 + (", MD5-checked" if pools["md5_checked"] else "")
                 + f", {len(pools['need'])} to fetch ({WJ.human_bytes(need)})"
                 + (f", {nexus_need} of them a Nexus click each" if nexus_need else ""))
    for a, hit in pools["md5_wrong"]:
        lines.append(f"    ! {a['name']}: the pooled copy's MD5 is not the collection's ({hit})")
    src = ", ".join(f"{n} {k}" for k, n in sorted(feat["sources"].items()))
    lines.append(f"  Mods       {feat['mods']} ({src})"
                 + (f", {feat['optional']} optional" if feat["optional"] else ""))
    bits = []
    if feat["fomod"]:
        bits.append(f"{feat['fomod']} FOMOD install(s) replayed from the curator's answers")
    if feat["replicate"]:
        bits.append(f"{feat['replicate']} installed file-for-file from recorded hashes")
    if feat["types"]:
        bits.append("game-root mods: " + ", ".join(f"{n} {t}" for t, n in sorted(feat["types"].items()))
                    + (" (into Stock Game)" if wizard else " (through Root Builder)"))
    if wizard and feat["patches"]:
        bits.append(f"{feat['patches']} mod(s) patched with the curator's binary patches")
    if feat["rules_internal"]:
        bits.append(f"{feat['rules_internal']} mod rule(s) between its own mods deciding "
                    f"conflicts")
    elif feat["rules"]:
        bits.append("no mod rule between its own mods (its " + str(feat["rules"])
                    + " name mods it does not include)")
    if feat["plugins"]:
        bits.append(f"{feat['plugins']} plugin(s)" + (f", {feat['plugin_rules']} plugin rule(s)"
                                                     if feat["plugin_rules"] else ""))
    lines.append("  Install    " + ("; ".join(bits) if bits else "plain archives, nothing to replay"))
    members = wiz.get("members") or {}
    if members.get("ini_tweaks"):
        lines.append(f"             {members['ini_tweaks']} INI tweak file(s), merged by collections2mo2")
    gaps = []
    if feat["patches"] and not wizard:
        gaps.append(f"{feat['patches']} mod(s) carry binary patches, which collections2mo2 does "
                    f"not apply yet: those files install unpatched")
    if feat["overrides"]:
        gaps.append(f"{feat['overrides']} mod(s) hide files from others (fileOverrides)")
    for item in wiz.get("left_out") or []:
        gaps.append(f"{item['mod']} [{item['source']}]: {item['why']}")
    for g in gaps:
        lines.append("  Note       " + g)
    game = report.get("game")
    if game is not None:
        want = ", ".join(feat["versions"]) or "no particular version"
        if not game["path"]:
            lines.append(f"  Game       not found: {game['how']}")
        else:
            verdict = {True: "matches", False: "DIFFERS", None: "cannot compare"}[game["matches"]]
            lines.append(f"  Game       {game['path']}")
            lines.append(f"             version {game['version'] or '?'}; the collection wants "
                         f"{want} [{verdict}]")
            if game["matches"] is False:
                lines.append("             " + version_note(wiz.get("domain")) + ".")
    return lines


# ----------------------------------------------------------------------
# Before the hand-over
# ----------------------------------------------------------------------
def check_staged(list_path, downloads, say=None):
    """Every archive the collection needs, checked in the staged folder by
    name, size and MD5. Returns (ok_count, problems), problems being
    [(name, why)].

    c2mo2 does check MD5s itself, but a file it finds wrong or missing is one
    it then asks Nexus for, and on a free account that request fails with a
    Premium error that says nothing about which file was the problem. Checked
    here, the answer is a list of names."""
    rec = WJ.parse_manifest(list_path)
    ok, problems = 0, []
    total = len(rec["archives"])
    for i, a in enumerate(rec["archives"], 1):
        p = os.path.join(downloads, a["name"])
        if not os.path.isfile(p):
            problems.append((a["name"], "not in the staged folder"))
            continue
        size = os.path.getsize(p)
        if a["size"] is not None and size != a["size"]:
            problems.append((a["name"], f"{size} bytes, the collection pins {a['size']}"))
            continue
        if a.get("md5"):
            if say and total > 20 and i % 20 == 0:
                _say(say, f"  checked {i} of {total}...")
            if WJ.file_md5(p) != a["md5"]:
                problems.append((a["name"], "its MD5 is not the one the collection pins"))
                continue
        ok += 1
    return ok, problems


def unobtainable(nexus, archives):
    """Of these archives, the ones Nexus will not hand out any more, and why:
    {name: why}.

    A curator pins exact files, and authors replace theirs: the pinned file is
    then archived out of the mod's listing and its page offers only the new
    one - no click can fetch the old one, on any account, and Vortex stops at
    the same place. Asked of the mod's own file listing, the one the download
    page shows. Only a Nexus file can be judged; a missing direct download is
    simply missing."""
    out = {}
    for a in archives:
        if WJ.state_type(a["state"]) != "nexus":
            continue
        mod_id, file_id = WJ.nexus_ids(a)
        domain = WJ.nexus_url_domain(WJ.nexus_domain(a))
        if not (mod_id and file_id and domain):
            continue
        files, status = nexus.mod_files(domain, mod_id)
        if status == "notfound":
            out[a["name"]] = "the mod is gone from Nexus"
        elif status == "ok" and file_id not in WJ.NexusClient._file_ids(files):
            out[a["name"]] = ("Nexus no longer offers the file the collection pins; its author "
                              "has replaced it")
    return out


def drive_is_remote(path):
    """True when `path` is on a network drive. The game runs from the
    instance, and MO2's virtual file system is not built for a share."""
    if os.name != "nt":
        return False
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if drive.startswith("\\\\"):
        return True
    try:
        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4    # DRIVE_REMOTE
    except (AttributeError, OSError):
        return False


def check_instance_path(list_path, downloads, out, game_path):
    """Problems with an instance location, worded for the confirmation."""
    problems = WB.check_paths(list_path, downloads, out)
    if not out or not str(out).strip():
        return problems
    out_abs = os.path.normcase(os.path.abspath(out))
    if game_path:
        game_abs = os.path.normcase(os.path.abspath(game_path))
        if out_abs == game_abs or out_abs.startswith(game_abs + os.sep):
            problems.append("the instance cannot go inside the game's own folder")
        elif game_abs.startswith(out_abs + os.sep):
            problems.append("the game's folder is inside the instance path")
    if drive_is_remote(out):
        problems.append("the instance is on a network drive; the game runs from it, so put "
                        "it on a local disk (the archives can stay on the share)")
    return problems


# ----------------------------------------------------------------------
# A game folder at another version, from Steam's depots
# ----------------------------------------------------------------------
# Steam's console downloads one depot at one manifest into
# steamapps\content\app_<app>\depot_<depot>, beside the install rather than
# into it. A game at an older version is those depots laid over each other in
# order, and a folder of its own keeps it: the store's install stays current,
# and every instance built for that version copies from the one folder.
STEAM_APPS = {
    "SkyrimSpecialEdition": ("489830", ("489831", "489832", "489833")),
}
ASSEMBLED_MARK = "wj_assembled.json"


def steam_path():
    """Where Steam is installed, from the registry; None when it cannot say."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            return os.path.normpath(winreg.QueryValueEx(k, "SteamPath")[0])
    except (OSError, ImportError):
        return None


def depot_folders(app, depots, steam=None):
    base = os.path.join(steam or steam_path() or "", "steamapps", "content", f"app_{app}")
    return [os.path.join(base, f"depot_{d}") for d in depots]


def assemble_game(gametype, out, depots=None, steam=None, say=None):
    """Lay Steam's downloaded depots over each other, in order, into a game
    folder of their own. Returns the version of the executable that results.

    Never into a Steam library - that is the store's install, and overwriting
    it is exactly what this exists to avoid - and never over a folder holding
    anything but an earlier assembly of this kind."""
    if gametype not in STEAM_APPS:
        raise CollectionError(f"no Steam depots are known for {gametype}")
    app, default = STEAM_APPS[gametype]
    folders = depot_folders(app, depots or default, steam)
    missing = [f for f in folders if not os.path.isdir(f) or not os.listdir(f)]
    if missing:
        raise CollectionError("not downloaded yet: " + ", ".join(missing))
    out_abs = os.path.abspath(out)
    if "steamapps" in [p.lower() for p in out_abs.replace("/", "\\").split("\\")]:
        raise CollectionError(f"{out_abs} is inside a Steam library; give a folder of its own")
    if os.path.isdir(out_abs) and os.listdir(out_abs) \
            and not os.path.isfile(os.path.join(out_abs, ASSEMBLED_MARK)):
        raise CollectionError(f"{out_abs} already holds something; give an empty or new folder")
    os.makedirs(out_abs, exist_ok=True)
    total = 0
    for folder in folders:
        count = size = 0
        for root, _dirs, files in os.walk(folder):
            rel = os.path.relpath(root, folder)
            target = out_abs if rel == "." else os.path.join(out_abs, rel)
            os.makedirs(target, exist_ok=True)
            for name in files:
                src = os.path.join(root, name)
                shutil.copy2(src, os.path.join(target, name))
                count += 1
                size += os.path.getsize(src)
        _say(say, f"  {os.path.basename(folder)}: {count} file(s), {WJ.human_bytes(size)}")
        total += size
    exe = os.path.join(out_abs, GAME_EXES.get(gametype, ""))
    version = exe_version(exe)
    if not version:
        raise CollectionError(f"the depots laid down no readable {GAME_EXES.get(gametype)}; "
                              f"check that each depot download finished")
    with open(os.path.join(out_abs, ASSEMBLED_MARK), "w", encoding="utf-8") as f:
        json.dump({"game": gametype, "app": app, "depots": list(depots or default),
                   "version": version, "from": folders,
                   "made": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, f, indent=1)
    _say(say, f"  {out_abs}: {WJ.human_bytes(total)}, {GAME_EXES[gametype]} {version}")
    return version


# ----------------------------------------------------------------------
# collections2mo2
# ----------------------------------------------------------------------
def c2mo2_dir():
    return os.path.join(here(), C2MO2_DIRNAME)


def _console_python():
    """The interpreter to run c2mo2 with: this one, but its console twin
    when this is pythonw - the window's - since c2mo2 prints its progress."""
    exe = sys.executable or "python"
    head, tail = os.path.split(exe)
    if tail.lower() == "pythonw.exe" and os.path.isfile(os.path.join(head, "python.exe")):
        return os.path.join(head, "python.exe")
    return exe


def find_c2mo2():
    """The c2mo2 kept in .c2mo2, or None. A dict: dir, src, site, python,
    commit (what installed.json says), current (is it the pinned commit)."""
    d = c2mo2_dir()
    src = os.path.join(d, "src")
    if not os.path.isfile(os.path.join(src, "collections2mo2", "cli.py")):
        return None
    stamp = {}
    try:
        with open(os.path.join(d, C2MO2_STAMP), encoding="utf-8") as f:
            stamp = json.load(f)
    except (OSError, ValueError):
        pass
    return {"dir": d, "src": src, "site": os.path.join(d, "site"),
            # A build carries no interpreter that could run c2mo2's libraries.
            "python": None if wj_paths.frozen() else _console_python(),
            "commit": stamp.get("commit"), "current": stamp.get("commit") == C2MO2_COMMIT}


def _extract_zip_root(zip_path, dest):
    """Unpack a GitHub archive into dest, dropping its single top folder, and
    refusing any member that would land outside dest."""
    dest_abs = os.path.abspath(dest)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        top = names[0].split("/")[0] + "/" if names else ""
        for n in names:
            if not n.startswith(top) or n == top:
                continue
            rel = n[len(top):]
            target = os.path.abspath(os.path.join(dest_abs, *rel.split("/")))
            if not target.startswith(dest_abs + os.sep):
                raise CollectionError(f"the archive tried to write outside {dest}: {n}")
            if n.endswith("/"):
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(n) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def setup_c2mo2(say=None):
    """Put the pinned c2mo2 into .c2mo2, with its libraries in .c2mo2/site.

    Built beside the old copy and swapped in only when complete, so a failure
    leaves whatever worked before. The tools folder c2mo2 fills itself - its
    7-Zip, and the MO2 and Root Builder releases it caches - is carried over,
    so an upgrade does not download those again. pip runs with --no-cache-dir,
    so nothing is left in the user profile either."""
    if wj_paths.frozen():
        raise CollectionError("collections2mo2 needs Python to run, so set it up from the "
                              "scripts (run.bat), not from a build")
    found = find_c2mo2()
    if found and found["current"] and os.path.isdir(found["site"]):
        _say(say, f"  collections2mo2 {C2MO2_COMMIT[:7]} is already in {found['dir']}.")
        return found
    d = c2mo2_dir()
    new = d + ".new"
    shutil.rmtree(new, ignore_errors=True)
    os.makedirs(new)
    zip_path = new + ".zip"
    try:
        _say(say, f"  Downloading collections2mo2 {C2MO2_COMMIT[:7]} from {C2MO2_REPO}...")
        if not WJ.stream_download(C2MO2_ZIP_URL, zip_path, quiet=True):
            raise CollectionError("the collections2mo2 source did not download")
        with open(zip_path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        if digest != C2MO2_ZIP_SHA256:
            raise CollectionError(f"the collections2mo2 archive's SHA-256 is {digest}, not the "
                                  f"pinned {C2MO2_ZIP_SHA256}; refusing to run it")
        _extract_zip_root(zip_path, new)
        constraints = os.path.join(new, "constraints.txt")
        with open(constraints, "w", encoding="utf-8") as f:
            f.write("\n".join(C2MO2_CONSTRAINTS) + "\n")
        _say(say, "  Installing its libraries into .c2mo2\\site (no cache, nothing machine-wide)...")
        cmd = [_console_python(), "-m", "pip", "install", "--target", os.path.join(new, "site"),
               "--only-binary=:all:", "--no-cache-dir", "--disable-pip-version-check",
               "--no-warn-script-location", "--no-input", "-c", constraints,
               *C2MO2_REQUIREMENTS]
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                              stdin=subprocess.DEVNULL)
        for line in (proc.stdout + proc.stderr).splitlines():
            if line.strip() and ("error" in line.lower() or line.startswith("Successfully")):
                _say(say, "    " + line.strip())
        if proc.returncode != 0:
            raise CollectionError(f"pip could not install collections2mo2's libraries "
                                  f"(exit {proc.returncode}); its output is above")
        with open(os.path.join(new, C2MO2_STAMP), "w", encoding="utf-8") as f:
            json.dump({"commit": C2MO2_COMMIT, "sha256": digest, "source": C2MO2_ZIP_URL,
                       "python": sys.version.split()[0], "requirements": list(C2MO2_REQUIREMENTS),
                       "installed": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                      f, indent=1)
        old_tools = os.path.join(d, "tools")
        if os.path.isdir(old_tools):
            shutil.rmtree(os.path.join(new, "tools"), ignore_errors=True)
            shutil.move(old_tools, os.path.join(new, "tools"))
        shutil.rmtree(d, ignore_errors=True)
        os.replace(new, d)
    finally:
        for p in (zip_path, zip_path + ".part"):
            try:
                os.remove(p)
            except OSError:
                pass
        shutil.rmtree(new, ignore_errors=True)
    found = find_c2mo2()
    _say(say, f"  collections2mo2 is ready in {d}.")
    return found


def c2mo2_args(c2, url, revision, out, game_path, downloads, stock_game=True,
               allow_missing=False):
    """The command that builds the instance. --skip-survey leaves out c2mo2's
    optional preview of every mod page, which costs API allowance and changes
    nothing it installs. --allow-missing, only when the plan found mods Nexus
    no longer serves, has it build without them and name them; an MD5 that
    does not match still stops it."""
    args = [c2["python"], "-X", "utf8", "-s", "-m", "collections2mo2.cli", "create", url,
            "--revision", str(revision), "--out", out, "--game-path", game_path,
            "--downloads-dir", downloads, "--skip-survey"]
    if stock_game:
        args.append("--stock-game")
    if allow_missing:
        args.append("--allow-missing")
    return args


def c2mo2_env(c2, api_key):
    """c2mo2's environment for one run: its source and libraries first on the
    path, and the key where it looks for one (NEXUS_API_KEY), so it needs no
    sign-in of its own."""
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env["PYTHONPATH"] = os.pathsep.join([c2["src"], c2["site"]])
    env["PYTHONIOENCODING"] = "utf-8"
    env["NEXUS_API_KEY"] = api_key
    return env


def run_c2mo2(args, env, cwd, on_line=None):
    """Run c2mo2, streaming its output. Returns the exit code."""
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, cwd=cwd, env=env, text=True,
                                encoding="utf-8", errors="replace", bufsize=1,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as e:
        if on_line:
            on_line(f"could not start collections2mo2: {e}")
        return 127
    with proc:
        for line in proc.stdout:
            if on_line:
                on_line(line.rstrip())
    return proc.returncode


def install_plan(list_path, out=None, game_path=None, nexus=None):
    """Everything the hand-over needs, and what stands in its way.

    Returns a dict: list, info (wizard.json), downloads, game (path, how,
    version, wanted, matches), c2 (find_c2mo2()), staged (ok, problems),
    unavailable, blockers - the reasons it cannot start - game_problems, and,
    once an instance folder is chosen (here or with set_instance()), out and
    path_problems. Reading every staged archive for its MD5 is the slow part,
    so a plan is made once, then pointed at a game folder (set_game()) and an
    instance folder (set_instance()) as those are chosen.

    With `nexus`, a missing archive Nexus no longer hands out is moved from
    the problems to `unavailable`: no download could ever supply it, so it is
    not a reason to wait. The build then goes ahead without those mods, which
    are named, and c2mo2 is told to leave them out (--allow-missing)."""
    data = read_list_file(list_path)
    wiz = data["wizard"]
    gametype = DOMAIN_GAMETYPE.get(wiz["domain"], wiz["domain"])
    downloads = os.path.join(WJ.resolve_game_dirs(gametype)[0],
                             os.path.splitext(os.path.basename(list_path))[0])
    blockers = []
    engine = engine_for(wiz["domain"])
    c2 = find_c2mo2() if engine == "c2mo2" else None
    if engine == "wizard":
        if not seven_zip():
            blockers.append("no 7-Zip was found to unpack the mods with (the one inside "
                            "Wabbajack's install is used)")
    elif engine is None:
        blockers.append(f"no build here makes an instance for '{wiz['domain']}'")
    elif not c2:
        blockers.append("collections2mo2 is not set up here yet (wj_collection.py setup-c2mo2)")
    elif not c2["python"]:
        blockers.append("collections2mo2 needs Python; run this from the scripts, not a build")
    elif not c2["current"]:
        blockers.append("the collections2mo2 in .c2mo2 is not the pinned commit; set it up again")
    if not os.path.isdir(downloads):
        staged = (0, [("(folder)", "the list has not been staged yet")])
    else:
        staged = check_staged(list_path, downloads)
    unavailable = []
    missing = {name for name, why in staged[1] if why == "not in the staged folder"}
    if nexus is not None and missing:
        gone = unobtainable(nexus, [a for a in WJ.parse_manifest(list_path)["archives"]
                                    if a["name"] in missing])
        unavailable = sorted(gone.items())
        staged = (staged[0], [(n, w) for n, w in staged[1] if n not in gone])
    if staged[1]:
        blockers.append(f"{len(staged[1])} archive(s) are missing or wrong in the staged folder")
    plan = {"list": list_path, "info": wiz, "gametype": gametype, "downloads": downloads,
            "wanted": list((data["collection"].get("info") or {}).get("gameVersions") or []),
            "engine": engine, "c2": c2, "staged": staged, "unavailable": unavailable,
            "blockers": blockers, "game": None, "game_problems": [], "out": None,
            "path_problems": []}
    set_game(plan, game_path)
    if out:
        set_instance(plan, out)
    return plan


def set_game(plan, game_path=None):
    """Point a plan at the game folder the instance copies into Stock Game.

    None means the install Wabbajack's store scan finds - the one Steam keeps
    current. Anything else is a copy kept at another version: a collection
    built for 1.5.97 wants a 1.5.97 game, which is exactly what a Stock Game
    copy makes possible without touching the store's install."""
    gametype = plan["gametype"]
    how = "chosen for this collection"
    if not game_path:
        game_path, how = game_install(gametype)
    exe = os.path.join(game_path, GAME_EXES.get(gametype, "")) if game_path else None
    have = exe_version(exe) if exe else None
    plan["game"] = {"path": game_path, "how": how, "version": have,
                    "matches": version_matches(have, plan["wanted"])}
    problems = []
    if not game_path or not os.path.isdir(game_path):
        problems.append(f"the game folder was not found ({how})")
    elif exe and not os.path.isfile(exe):
        problems.append(f"{game_path} holds no {GAME_EXES.get(gametype)}, so it is not a game "
                        f"folder")
    plan["game_problems"] = problems
    if plan.get("out"):
        set_instance(plan, plan["out"])
    return plan


def set_instance(plan, out):
    """Point a plan at an instance folder, and say what is wrong with it.

    c2mo2 adds to an instance it built before; the Wizard's own build only
    ever makes a new one, so a folder with anything in it is refused."""
    plan["out"] = os.path.abspath(out) if out else None
    plan["path_problems"] = (check_instance_path(plan["list"], plan["downloads"], plan["out"],
                                                 plan["game"]["path"]) if out else [])
    if out and plan.get("engine") == "wizard":
        why = MO2.check_out(plan["out"])
        if why:
            plan["path_problems"].append(why)
    return plan


def stops(plan):
    """Every reason the plan cannot start, in the order they are worth fixing."""
    return plan["blockers"] + plan.get("game_problems", []) + plan.get("path_problems", [])


def plan_lines(plan):
    """The plan as lines for a confirmation."""
    wiz, game, (ok, problems) = plan["info"], plan["game"], plan["staged"]
    lines = [f"Collection  {wiz['name']}, revision {wiz['revision']}   {wiz['url']}",
             f"Downloads   {plan['downloads']}",
             f"Checked     {ok} archive(s) match the collection's MD5"
             + (f"; {len(problems)} do not:" if problems else "")]
    for name, why in problems[:15]:
        lines.append(f"              {name}: {why}")
    if len(problems) > 15:
        lines.append(f"              ... and {len(problems) - 15} more")
    for name, why in plan.get("unavailable") or []:
        lines.append(f"Left out    {name}: {why}. The instance is built without it.")
    want = ", ".join(plan.get("wanted") or []) or "no particular version"
    if game["path"]:
        verdict = {True: "matches", False: "DIFFERS from", None: "cannot be compared with"}[
            game["matches"]]
        lines.append(f"Game        {game['path']}  (version {game['version'] or '?'}, "
                     f"{verdict} the {want} the collection wants)")
    else:
        lines.append(f"Game        not found: {game['how']}")
    c2 = plan["c2"]
    if plan.get("engine") == "wizard":
        game_name = MO2.GAMES[wiz["domain"]]["mo2_name"]
        lines.append(f"Installer   the Wizard's own build for {game_name}: Mod Organizer "
                     f"{MO2.MO2_VERSION} portable"
                     + ("" if MO2.mo2_cached() else
                        f", downloaded first ({WJ.human_bytes(MO2.MO2_SIZE)}, pinned by SHA-256)"))
    else:
        lines.append("Installer   " + (f"collections2mo2 {(c2['commit'] or '?')[:7]} in {c2['dir']}"
                                       if c2 else "collections2mo2 is not set up"))
    if plan.get("out"):
        lines.append(f"Instance    {plan['out']}  (portable MO2, the game copied into Stock Game)")
    for b in stops(plan):
        lines.append("[NO] " + b)
    return lines


def tagless(manifest):
    """The mods of a collection that carry no source.tag."""
    return [m for m in manifest.get("mods") or [] if not (m.get("source") or {}).get("tag")]


def give_tags(manifest):
    """Give every mod without a tag one of its own; returns how many were given.

    Vortex added per-mod tags in 2023, and a collection published before that
    has none. c2mo2 names its folders by tag-or-name, but keys its install by
    the tag alone, so every mod of such a collection falls onto the one empty
    key and installs into one folder - the last mod's - which is what the first
    build of 'Basic Essential Mods For SSE v1.5.97' did with its 26. The tags
    given here are names for that bookkeeping only: unique, the same for the
    same manifest, and prefixed so they read as ours."""
    used = {(m.get("source") or {}).get("tag") for m in manifest.get("mods") or []} - {None, ""}
    given = 0
    for i, mod in enumerate(manifest.get("mods") or []):
        src = mod.setdefault("source", {})
        if src.get("tag"):
            continue
        tag = "wj%03d%s" % (i, (src.get("md5") or "")[:5])
        while tag in used:
            tag += "x"
        src["tag"] = tag
        used.add(tag)
        given += 1
    return given


def seed_c2mo2_manifest(plan, nexus, say=None):
    """Lay the collection's archive where c2mo2 reads it, with tags given to
    the mods that have none. Returns how many mods were given one (0 when the
    collection needs none, and nothing is written then).

    c2mo2's fetch reuses an unpacked archive it finds in the instance, so the
    archive is unpacked there in full - bundled mods and patches are read from
    it - with collection.json rewritten and the curator's own kept beside it
    as collection.original.json. c2mo2 itself is left exactly as pinned."""
    manifest = read_list_file(plan["list"])["collection"]
    if not tagless(manifest):
        return 0
    wiz = plan["info"]
    rev_dir = os.path.join(plan["out"], "c2mo2", "collections", wiz["slug"], str(wiz["revision"]))
    archive_dir = os.path.join(rev_dir, "archive")
    target = os.path.join(archive_dir, "collection.json")
    if os.path.isfile(target):
        try:
            with open(target, encoding="utf-8-sig") as f:
                if not tagless(json.load(f)):
                    return 0                         # seeded by an earlier run
        except (OSError, ValueError):
            pass
    os.makedirs(rev_dir, exist_ok=True)
    blob = os.path.join(rev_dir, "collection_archive.bin")     # the name c2mo2 gives it
    if not os.path.isfile(blob):
        info = revision_info(wiz["slug"], wiz["revision"])
        link, status = nexus.collection_archive_url(info["download_link"])
        if not link:
            raise CollectionError("Nexus would not hand out the collection archive: " + status)
        if not WJ.stream_download(link, blob, quiet=True):
            raise CollectionError("the collection archive did not download")
    shutil.rmtree(archive_dir, ignore_errors=True)
    os.makedirs(archive_dir)
    with open(blob, "rb") as f:
        head = f.read(6)
    if head.startswith(b"PK"):
        with zipfile.ZipFile(blob) as z:
            z.extractall(archive_dir)
    else:
        exe = seven_zip()
        if not exe:
            raise CollectionError("no 7-Zip was found to unpack the collection archive")
        got = subprocess.run([exe, "x", "-y", "-o" + archive_dir, blob], capture_output=True,
                             stdin=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if got.returncode != 0:
            raise CollectionError("7-Zip could not unpack the collection archive")
    original = os.path.join(archive_dir, "collection.json")
    with open(original, encoding="utf-8-sig") as f:
        unpacked = json.load(f)
    os.replace(original, os.path.join(archive_dir, "collection.original.json"))
    given = give_tags(unpacked)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(unpacked, f, indent=1, ensure_ascii=False)
    _say(say, f"gave {given} mod(s) with no tag one of their own, for collections2mo2")
    return given


PLUGIN_EXTS = (".esp", ".esm", ".esl")
MISSING_MARK = "replicate: file listed in manifest is missing"


def c2mo2_layer(out, slug, revision):
    """c2mo2's own records for one collection layer of an instance: the ledger
    and its path, and the paths of the layer's manifest, downloads.json and
    install.json. None when the instance has no such layer."""
    ledger_path = os.path.join(out, "c2mo2-instance.json")
    try:
        with open(ledger_path, encoding="utf-8") as f:
            ledger = json.load(f)
    except (OSError, ValueError):
        return None
    for layer in ledger.get("layers") or []:
        if layer.get("slug") == slug and layer.get("revision") == revision:
            files = layer.get("files") or {}
            return {"ledger": ledger, "ledger_path": ledger_path,
                    "manifest": os.path.join(out, layer.get("manifest") or ""),
                    "downloads": os.path.join(out, files.get("downloads") or ""),
                    "install": os.path.join(out, files.get("install") or "")}
    return None


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _unpack(archive, dest):
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
        return
    exe = seven_zip()
    if not exe:
        raise CollectionError("no 7-Zip was found to unpack " + os.path.basename(archive))
    got = subprocess.run([exe, "x", "-y", "-o" + dest, archive], capture_output=True,
                         stdin=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if got.returncode not in (0, 1):                  # 1 is 7-Zip's "warning", files written
        raise CollectionError("7-Zip could not unpack " + os.path.basename(archive))


def replicate_by_md5(plan, say=None):
    """Install, file for file, the mods c2mo2's replicate step could not.

    A curator can export a mod as its exact installed file list: a path and
    an MD5 for every file (collection.json's `hashes`). Vortex installs such
    a mod by hashing everything in the archive and copying each listed MD5 to
    its listed path. c2mo2 looks the listed path up in the archive instead,
    which only works when the archive is laid out the way the mod installs;
    a FOMOD-packed archive keeps its files under option folders ('00 Core\\
    meshes\\...'), so SMIM's 1,427 files all read as missing and it installed
    none. This does it Vortex's way for exactly those mods, then records the
    files and plugins in c2mo2's install.json and ledger, so its profile
    re-render (profile-instance) orders the plugins they brought.

    Returns [(mod name, files placed, files no archive member matched)]."""
    wiz = plan["info"]
    layer = c2mo2_layer(plan["out"], wiz["slug"], wiz["revision"])
    if layer is None:
        return []
    with open(layer["install"], encoding="utf-8") as f:
        install = json.load(f)
    with open(layer["downloads"], encoding="utf-8") as f:
        downloads = json.load(f)
    with open(layer["manifest"], encoding="utf-8-sig") as f:
        manifest = json.load(f)
    mods = {(m.get("source") or {}).get("tag"): m for m in manifest.get("mods") or []}
    archives = {e.get("tag"): e.get("path") for e in downloads.get("entries") or []}
    fixed = []
    for entry in install.get("entries") or []:
        if entry.get("strategy") != "replicate" or not any(
                str(w).startswith(MISSING_MARK) for w in entry.get("warnings") or []):
            continue
        mod, archive = mods.get(entry.get("tag")), archives.get(entry.get("tag"))
        if not mod or not archive or not os.path.isfile(archive):
            continue
        _say(say, f"placing {entry['name']} file for file, by MD5...")
        scratch = os.path.join(plan["out"], "c2mo2", "wj-replicate", entry["tag"])
        shutil.rmtree(scratch, ignore_errors=True)
        os.makedirs(scratch)
        try:
            _unpack(archive, scratch)
            by_md5 = {}
            for root, _dirs, names in os.walk(scratch):
                for name in names:
                    p = os.path.join(root, name)
                    by_md5.setdefault(WJ.file_md5(p), []).append(p)
            dest = os.path.join(install.get("mods_dir") or os.path.join(plan["out"], "mods"),
                                entry["folder"])
            placed, unmatched, plugins = 0, [], []
            for item in mod.get("hashes") or []:
                rel = str(item.get("path") or "").replace("\\", "/").strip("/")
                found = by_md5.get((item.get("md5") or "").lower()) or []
                if not rel or not found:
                    unmatched.append(rel)
                    continue
                # Several copies of one file: the one under the same name.
                src = next((c for c in found if os.path.basename(c).lower()
                            == posixpath.basename(rel).lower()), found[0])
                target = os.path.join(dest, *rel.split("/"))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(src, target)
                placed += 1
                if "/" not in rel and rel.lower().endswith(PLUGIN_EXTS):
                    plugins.append(rel)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        entry["file_count"] = placed
        entry["plugins"] = sorted(set(entry.get("plugins") or []) | set(plugins))
        entry["warnings"] = ([f"replicate: {placed} file(s) placed by MD5 by the Wabbajack Wizard"]
                             + [f"replicate: no file in the archive has the MD5 listed for {r}"
                                for r in unmatched])
        record = (layer["ledger"].get("mods") or {}).get(entry["folder"])
        if isinstance(record, dict):
            record["plugins"] = entry["plugins"]
        fixed.append((entry["name"], placed, len(unmatched)))
        _say(say, f"  {entry['name']}: {placed} file(s) placed"
                  + (f", {len(unmatched)} matched nothing in the archive" if unmatched else ""))
    if fixed:
        _write_json(layer["install"], install)
        _write_json(layer["ledger_path"], layer["ledger"])
        shutil.rmtree(os.path.join(plan["out"], "c2mo2", "wj-replicate"), ignore_errors=True)
    return fixed


# Mods whose own instructions ask for a step no installer performs: a file of
# the game's, under another name, beside the mod's own. Keyed by Nexus domain
# and mod id; each step copies <game>/<file> to <mod folder>/Root/<as>, so Root
# Builder lays both into Stock Game at launch and takes both away at exit, and
# the game folder itself stays as the store shipped it.
ROOT_FIXUPS = {
    # DLL Plugin Loader (meh321), what NetScriptFramework loads through: its
    # binkw64.dll stands in for the game's and then loads the original as
    # binkw64_.dll. Its readme: 'Rename "binkw64.dll" to "binkw64_.dll"'.
    # Without it SKSE's launch stops at "binkw64_.dll failed to load".
    ("skyrimspecialedition", 10546): [("binkw64.dll", "binkw64_.dll")],
}


def apply_root_fixups(plan, say=None):
    """Carry out ROOT_FIXUPS for the mods this instance holds. Returns
    [(mod name, file written)]; nothing is overwritten."""
    wiz = plan["info"]
    layer = c2mo2_layer(plan["out"], wiz["slug"], wiz["revision"])
    game = (plan.get("game") or {}).get("path")
    if layer is None or not game:
        return []
    with open(layer["install"], encoding="utf-8") as f:
        install = json.load(f)
    mods_dir = install.get("mods_dir") or os.path.join(plan["out"], "mods")
    done = []
    for entry in install.get("entries") or []:
        steps = ROOT_FIXUPS.get((wiz.get("domain"), entry.get("mod_id")))
        if not steps or entry.get("strategy") == "failed":
            continue
        for source, target_name in steps:
            src = os.path.join(game, source)
            target = os.path.join(mods_dir, entry["folder"], "Root", target_name)
            if os.path.exists(target) or not os.path.isfile(src):
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(src, target)
            done.append((entry["name"], target_name))
            _say(say, f"{entry['name']}: put the game's {source} beside it as {target_name}, "
                      f"as its own instructions ask")
    return done


def keep_saves_local(out, say=None):
    """Give every profile of an instance its own saves. Returns the profiles
    changed.

    c2mo2 writes LocalSaves=false with every profile render, which sends the
    instance's saves to Documents\\My Games beside those of every other setup
    on the machine: a 1.5.97 game then lists 1.7.104 saves it cannot load,
    and loading another list's save can wreck it. MO2's profile-specific saves
    keep them in profiles/<name>/saves, inside the instance, like the rest of
    it. Run after every render, since each render turns it off again."""
    profiles = os.path.join(out, "profiles")
    changed = []
    try:
        names = sorted(os.listdir(profiles))
    except OSError:
        return changed
    for name in names:
        ini = os.path.join(profiles, name, "settings.ini")
        if not os.path.isfile(ini):
            continue
        with open(ini, encoding="utf-8", newline="") as f:
            text = f.read()
        if re.search(r"(?im)^LocalSaves[ \t]*=[ \t]*true[ \t]*\r?$", text):
            continue
        nl = "\r\n" if "\r\n" in text else "\n"
        if re.search(r"(?im)^LocalSaves[ \t]*=", text):
            text = re.sub(r"(?im)^LocalSaves[ \t]*=[^\r\n]*", "LocalSaves=true", text)
        elif re.search(r"(?im)^\[General\][ \t]*(?=\r?$)", text):
            text = re.sub(r"(?im)^\[General\][ \t]*(?=\r?$)", "[General]" + nl + "LocalSaves=true",
                          text, count=1)
        else:
            text = "[General]" + nl + "LocalSaves=true" + nl + text
        with open(ini, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.makedirs(os.path.join(profiles, name, "saves"), exist_ok=True)
        changed.append(name)
        _say(say, f"profile '{name}' keeps its own saves (profiles\\{name}\\saves)")
    return changed


def rerender_profile(plan, api_key, on_line=None):
    """c2mo2's own re-render of the instance's profile, after the files under
    it have changed. Returns its exit code."""
    c2 = plan["c2"]
    args = [c2["python"], "-X", "utf8", "-s", "-m", "collections2mo2.cli", "profile-instance",
            "--instance", plan["out"]]
    return run_c2mo2(args, c2mo2_env(c2, api_key), cwd=c2["dir"], on_line=on_line)


# ----------------------------------------------------------------------
# The Wizard's own build, for the games c2mo2 does not know
# ----------------------------------------------------------------------
def download_extras(wiz, nexus, say=None):
    """What a collection archive carries besides collection.json, fetched
    afresh: for a list file that does not keep it."""
    _say(say, "fetching the collection archive for its bundled mods and patches...")
    info = revision_info(wiz["slug"], wiz["revision"])
    link, status = nexus.collection_archive_url(info["download_link"])
    if not link:
        raise CollectionError("Nexus would not hand out the collection archive: " + status)
    scratch = tempfile.mkdtemp(prefix="wj-collection-")
    blob = os.path.join(scratch, "collection.bin")
    try:
        if not WJ.stream_download(link, blob, quiet=True):
            raise CollectionError("the collection archive did not download")
        extras = read_extras(blob)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if extras is None:
        raise CollectionError(f"the collection archive carries more than "
                              f"{WJ.human_bytes(EXTRAS_CAP)} besides its collection.json")
    return extras


def bundled_source(extras, src, dest):
    """Lay a bundled mod's files out of the collection archive into dest.
    Returns what to install from - the folder, or the archive it was packed
    as - or None when the archive does not carry it.

    Vortex packs a bundled mod as the folder it had in the curator's staging
    area, under bundled/<file expression>/, or as the archive it came in,
    under bundled/<file name>."""
    names = [n for n in (src.get("fileExpression"), src.get("logicalFilename"),
                         posixpath.splitext(str(src.get("logicalFilename") or ""))[0]) if n]
    lowered = {m.lower(): m for m in extras}
    for name in names:
        prefix = f"bundled/{name}/".lower()
        files = {m[len(prefix):]: extras[m] for low, m in lowered.items() if low.startswith(prefix)}
        if files:
            for rel, data in files.items():
                target = os.path.join(dest, *rel.split("/"))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as f:
                    f.write(data)
            return dest
        member = lowered.get(f"bundled/{name}".lower())
        if member is not None:
            os.makedirs(dest, exist_ok=True)
            target = os.path.join(dest, MO2.safe_name(posixpath.basename(member)))
            with open(target, "wb") as f:
                f.write(extras[member])
            return target
    return None


def mod_patches(extras, mod):
    """[(path in the mod, CRC32, BSDIFF40 bytes or None)] for a mod's binary
    patches, which the collection archive keeps at
    patches/<mod name>/<path>.diff."""
    patches = mod.get("patches") or {}
    if not patches:
        return []
    name = str(mod.get("name") or "")
    folders = {m.split("/")[1] for m in extras if m.lower().startswith("patches/") and m.count("/") >= 2}
    folder = name if name in folders else next(
        (f for f in folders if f.strip().lower() == name.strip().lower()), name)
    lowered = {m.lower(): m for m in extras}
    out = []
    for rel, crc in sorted(patches.items()):
        rel = str(rel).replace("\\", "/").strip("/")
        member = lowered.get(f"patches/{folder}/{rel}.diff".lower())
        out.append((rel, str(crc or ""), extras[member] if member else None))
    return out


def rule_edges(manifest, jobs):
    """The collection's before/after rules between two of the mods being
    built, as (a, b) index pairs into jobs: a loads before b, so b wins.

    Both ends are matched by MD5, as internal_rules() counts them; a bundled
    mod, which has none, by its file expression or file name. A rule naming
    an MD5 no mod has names a file the collection does not include."""
    mods = manifest.get("mods") or []
    by_md5, by_name = {}, {}
    for i, job in enumerate(jobs):
        src = mods[job["index"]].get("source") or {}
        md5 = str(src.get("md5") or "").lower()
        if md5:
            by_md5.setdefault(md5, i)
            continue
        for key in ("fileExpression", "logicalFilename"):
            if src.get(key):
                by_name.setdefault(str(src[key]).lower(), i)

    def find(ref):
        ref = ref or {}
        md5 = str(ref.get("fileMD5") or "").lower()
        if md5:
            return by_md5.get(md5)
        for key in ("fileExpression", "logicalFileName"):
            if ref.get(key) and str(ref[key]).lower() in by_name:
                return by_name[str(ref[key]).lower()]
        return None

    edges = []
    for rule in manifest.get("modRules") or []:
        kind = rule.get("type")
        if kind not in ("before", "after"):
            continue
        a, b = find(rule.get("source")), find(rule.get("reference"))
        if a is None or b is None or a == b:
            continue
        edges.append((a, b) if kind == "before" else (b, a))
    return edges


def wizard_mods(plan, manifest, extras, scratch):
    """The collection as wj_mo2's build takes it. Returns (mods, left_out,
    set_aside): the mods lowest priority first, [(mod, why)] for those that
    cannot be built, and the rules dropped to break a cycle, as name pairs."""
    wiz = plan["info"]
    game = MO2.GAMES[wiz["domain"]]
    by_ids, by_url = {}, {}
    for a in read_list_file(plan["list"])["modlist"].get("Archives") or []:
        st = a.get("State") or {}
        if st.get("ModID") and st.get("FileID"):
            by_ids[(int(st["ModID"]), int(st["FileID"]))] = a
        if st.get("Url"):
            by_url[st["Url"]] = a
    unavailable = {n for n, _w in plan.get("unavailable") or []}
    jobs, left_out = [], []
    for idx, mod in enumerate(manifest.get("mods") or []):
        src = mod.get("source") or {}
        kind = str(src.get("type") or "").lower()
        name = str(mod.get("name") or "").strip() or f"mod {idx + 1}"
        mtype = (mod.get("details") or {}).get("type") or ""
        if mtype and mtype != game["loader"]:
            left_out.append((name, f"a '{mtype}' mod, which goes somewhere this build does not "
                                   f"put mods yet"))
            continue
        job = {"index": idx, "name": name, "enabled": not mod.get("optional"),
               "kind": "loader" if mtype == game["loader"] else "mod",
               "hashes": mod.get("hashes"),
               "meta": {"version": mod.get("version"),
                        "comments": f"collection:{wiz['slug']}@{wiz['revision']}"}}
        entry = None
        if kind == "nexus":
            entry = by_ids.get((_int(src.get("modId")), _int(src.get("fileId"))))
            if entry:
                job["meta"].update(mod_id=_int(src.get("modId")), file_id=_int(src.get("fileId")))
        elif kind == "direct":
            entry = by_url.get(src.get("url"))
        elif kind == "bundle":
            job["source"] = bundled_source(extras, src, os.path.join(scratch, str(idx)))
            if not job["source"]:
                left_out.append((name, "the collection archive does not carry its files"))
                continue
            job["meta"]["archive"] = src.get("logicalFilename") or ""
        else:
            left_out.append((name, "downloaded by hand from another site, which this build "
                                   "does not do yet"))
            continue
        if kind in ("nexus", "direct"):
            if not entry:
                left_out.append((name, "the list file names no archive for it"))
                continue
            if entry["Name"] in unavailable:
                left_out.append((name, "Nexus no longer serves the file the collection pins"))
                continue
            job["source"] = os.path.join(plan["downloads"], entry["Name"])
            job["meta"]["archive"] = entry["Name"]
        job["patches"] = mod_patches(extras, mod)
        jobs.append(job)
    for job, folder in zip(jobs, MO2.unique_names([MO2.safe_name(j["name"]) for j in jobs])):
        job["folder"] = folder
    order, set_aside = MO2.order_by_rules(len(jobs), rule_edges(manifest, jobs))
    return ([jobs[i] for i in order], left_out,
            [(jobs[a]["name"], jobs[b]["name"]) for a, b in set_aside])


def build_with_wizard(plan, api_key, on_line=None):
    """Build the instance with wj_mo2. Returns what it recorded."""
    wiz = plan["info"]
    manifest = read_list_file(plan["list"])["collection"]
    extras = list_extras(plan["list"])
    if extras is None:
        extras = download_extras(wiz, WJ.NexusClient(api_key), say=on_line)
    scratch = tempfile.mkdtemp(prefix="wj-bundled-")
    try:
        mods, left_out, set_aside = wizard_mods(plan, manifest, extras, scratch)
        for name, why in left_out:
            _say(on_line, f"left out: {name} ({why})")
        for a, b in set_aside:
            _say(on_line, f"a rule set aside, to break a cycle of rules: {a} before {b}")
        record = {"collection": {"slug": wiz["slug"], "revision": wiz["revision"],
                                 "name": wiz["name"], "author": wiz.get("author"),
                                 "url": wiz["url"]},
                  "list": os.path.basename(plan["list"]),
                  "left_out": [{"mod": n, "why": w} for n, w in left_out],
                  "rules_set_aside": [{"before": a, "after": b} for a, b in set_aside]}
        info = MO2.build(wiz["domain"], plan["out"], plan["game"]["path"],
                         MO2.safe_profile(wiz["name"]), mods, downloads=plan["downloads"],
                         record=record, say=on_line)
    except (MO2.BuildError, OSError) as e:
        raise CollectionError(str(e))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    patched = [p for m in info["mods"] for p in m["patches"]]
    applied = sum(1 for p in patched if p["result"] == "applied")
    _say(on_line, f"built: {len(info['mods'])} mod(s) in profile '{info['profile']}'"
                  + (f", {applied} of {len(patched)} patch(es) applied" if patched else "")
                  + (f", {len(left_out)} left out" if left_out else ""))
    return info


def install(plan, api_key, on_line=None):
    """Build the instance for a plan with nothing in its way: c2mo2 for a
    Bethesda game, wj_mo2 for the rest. Returns the exit code, 0 when built."""
    problems = stops(plan)
    if not plan.get("out"):
        problems = problems + ["no instance folder was chosen"]
    if problems:
        raise CollectionError("; ".join(problems))
    if plan.get("engine") == "wizard":
        try:
            build_with_wizard(plan, api_key, on_line=on_line)
        except CollectionError as e:
            _say(on_line, f"[ERROR] {e}")
            return 1
        return 0
    wiz, c2 = plan["info"], plan["c2"]
    os.makedirs(plan["out"], exist_ok=True)
    seed_c2mo2_manifest(plan, WJ.NexusClient(api_key), say=on_line)
    args = c2mo2_args(c2, wiz["url"], wiz["revision"], plan["out"], plan["game"]["path"],
                      plan["downloads"], allow_missing=bool(plan.get("unavailable")))
    rc = run_c2mo2(args, c2mo2_env(c2, api_key), cwd=c2["dir"], on_line=on_line)
    if rc != 0:
        return rc
    apply_root_fixups(plan, say=on_line)
    if replicate_by_md5(plan, say=on_line):
        rc = rerender_profile(plan, api_key, on_line=on_line)
    keep_saves_local(plan["out"], say=on_line)
    return rc


# ----------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------
def _manifest_dir():
    """The manifest folder, as the rest of the suite resolves it: the one the
    orchestrator hands its children, else MANIFEST_DIR."""
    env = os.environ.get("WJ_MANIFEST_DIR")
    if env and os.path.isdir(env):
        return env
    import wj_autoupdate as A                 # the one resolver MANIFEST_DIR has
    d = A.resolve_manifest_dir(CFG.MANIFEST_DIR)
    if not d:
        raise CollectionError("MANIFEST_DIR does not name a folder")
    return os.path.abspath(d)


def _nexus(prompt=True):
    key = WJ.resolve_api_key(None, here(), prompt=prompt)
    if not key:
        raise CollectionError("a Nexus API key is needed (nexus_api_key.txt, or NEXUS_API_KEY)")
    return WJ.NexusClient(key), key


def cmd_fetch(args):
    mdir = _manifest_dir()
    os.makedirs(mdir, exist_ok=True)
    nexus, _key = _nexus()
    fn, _changed = fetch(args.ref, mdir, nexus, revision=args.revision, force=args.force)
    print()
    for line in report_lines(inspect(os.path.join(mdir, fn), look_up_latest=False)):
        print(line)
    print()
    print(f"Next: it is a list like any other now. The Incomplete stage downloads what is "
          f"missing, a Nexus click each in assist mode, and Prepare stages it and offers to "
          f"build its MO2 instance (run.bat --wizard, or the window's Incomplete and Prepare "
          f"tabs).")
    return 0


def cmd_inspect(args):
    mdir = _manifest_dir()
    fn = resolve_list(mdir, args.ref)
    if not fn:
        raise CollectionError(f"no collection list matching '{args.ref}' in {mdir}; fetch it first")
    for line in report_lines(inspect(os.path.join(mdir, fn), md5=args.md5,
                                     look_up_game=not args.no_game)):
        print(line)
    return 0


def cmd_setup(_args):
    setup_c2mo2()
    return 0


def cmd_assemble(args):
    depots = [d.strip() for d in args.depots.split(",")] if args.depots else None
    print(f"Laying Steam's depots into {args.out}...")
    version = assemble_game(args.game, args.out, depots=depots, steam=args.steam)
    print(f"A {args.game} {version} game folder is ready. Give it to a build with "
          f"--game-path, or as the game folder in the window's Prepare tab.")
    return 0


def cmd_install(args):
    mdir = _manifest_dir()
    fn = resolve_list(mdir, args.ref)
    if not fn:
        raise CollectionError(f"no collection list matching '{args.ref}' in {mdir}")
    out = os.path.abspath(os.path.expandvars(os.path.expanduser(args.out)))
    nexus, key = _nexus()
    plan = install_plan(os.path.join(mdir, fn), out, game_path=args.game_path, nexus=nexus)
    for line in plan_lines(plan):
        print(line)
    if stops(plan):
        print("\nNothing started.")
        return 2
    if plan["game"]["matches"] is False and not args.yes:
        print(f"\nThe game is not the version this collection was built for: "
              f"{version_note(plan)}. Give a copy at the right version with --game-path "
              f"(assemble-game makes one from Steam's depots).")
    if not args.yes:
        try:
            if input("\nBuild the instance?  [y] yes  [n] no  ").strip().lower() != "y":
                print("Nothing started.")
                return 0
        except EOFError:
            print("Nothing started (no answer; pass --yes to run unattended).")
            return 0
    rc = install(plan, key, on_line=lambda l: print("  | " + l))
    print(f"\n{installer_name(plan)[0].upper() + installer_name(plan)[1:]} exited {rc}."
          + (f" The instance is in {out}." if rc == 0 else ""))
    return 0 if rc == 0 else 1


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Nexus collections as lists: fetch, inspect, and build a portable MO2 "
                    "instance (collections2mo2 for the Bethesda games, the Wizard's own "
                    "build for the rest).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="fetch a collection and write its list file")
    f.add_argument("ref", help="the collection's address, nxm:// link, or slug")
    f.add_argument("--revision", type=int, default=None, help="a revision (default: latest)")
    f.add_argument("--force", action="store_true", help="fetch again even if held")
    f.set_defaults(func=cmd_fetch)
    i = sub.add_parser("inspect", help="what a held collection needs, and from where")
    i.add_argument("ref", help="the list name, the collection's address, or its slug")
    i.add_argument("--md5", action="store_true", help="read every pooled hit and check its MD5")
    i.add_argument("--no-game", action="store_true", help="skip looking up the game install")
    i.set_defaults(func=cmd_inspect)
    s = sub.add_parser("setup-c2mo2", help="put the pinned collections2mo2 into .c2mo2")
    s.set_defaults(func=cmd_setup)
    g = sub.add_parser("assemble-game",
                       help="lay Steam's downloaded depots into a game folder of their own")
    g.add_argument("--out", required=True, help="the new game folder (not in a Steam library)")
    g.add_argument("--game", default="SkyrimSpecialEdition", help="the GameType (default: %(default)s)")
    g.add_argument("--depots", default=None, help="comma-separated depot ids (default: the game's)")
    g.add_argument("--steam", default=None, help="Steam's folder (default: from the registry)")
    g.set_defaults(func=cmd_assemble)
    n = sub.add_parser("install", help="build the MO2 instance from a staged collection")
    n.add_argument("ref", help="the list name, the collection's address, or its slug")
    n.add_argument("--out", required=True, help="where the instance goes (a local disk)")
    n.add_argument("--game-path", default=None, help="the game install (default: looked up)")
    n.add_argument("--yes", action="store_true", help="do not ask before starting")
    n.set_defaults(func=cmd_install)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        return args.func(args)
    except CollectionError as e:
        print(f"[ERROR] {e}")
        return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[ABORT] Interrupted.")
        sys.exit(130)
