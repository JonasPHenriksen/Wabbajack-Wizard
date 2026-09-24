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

What the file names are, and why they come from the v1 API: a collection
records each file's display name, not the name it downloads under, and the
pools and c2mo2 both know an archive by that download name. The GraphQL API
has a `uri` field that looks like it, but for files uploaded since 2026 it is a
storage path, so each file's own v1 record is asked instead, the way c2mo2
asks it. Its answers are kept in the list file, and a later revision asks only
about the files that changed.

Commands:
    wj_collection.py fetch <collection URL or slug> [--revision N] [--force]
    wj_collection.py inspect <list name, URL or slug> [--md5] [--no-game]
    wj_collection.py setup-c2mo2
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
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone

import wj_paths
import wj_config as CFG
import wj_downloader as WJ
import wj_wabbajack as WB

LOG = logging.getLogger("wj_collection")

API_BASE = "https://api.nexusmods.com"
GRAPHQL_URL = API_BASE + "/v2/graphql"
SITE_URL = "https://www.nexusmods.com"
SEP = "_@@_"
FORMAT = 1                       # of wizard.json inside a .collection

# A collection names its game by Nexus domain; the pools know it by the
# GameType Wabbajack uses. These are the games c2mo2 can build an instance
# for; a collection for any other game is refused at fetch rather than pooled
# for an install that cannot happen.
DOMAIN_GAMETYPE = {
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

# The executable whose version a collection's gameVersions is compared with.
GAME_EXES = {
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


def seven_zip():
    """A 7-Zip console this tooling may run without installing one: the copy
    Wabbajack ships inside its own install, else the one c2mo2 keeps."""
    cands = []
    wj = WB.find_wabbajack()
    if wj and wj.get("version"):
        base = os.path.join(wj["dir"], wj["version"])
        cands += [os.path.join(base, "Extractors", "windows-x64", "7z.exe"),
                  os.path.join(base, "cli", "Extractors", "windows-x64", "7z.exe")]
    cands.append(os.path.join(c2mo2_dir(), "tools", "7za.exe"))
    return next((c for c in cands if os.path.isfile(c)), None)


def read_archive(path):
    """(collection.json as a dict, member names) from a collection archive.

    Only collection.json is extracted, and only into memory: bundled files,
    patches and INI tweaks are c2mo2's to unpack, and it fetches the archive
    itself. The member names are kept to say what the archive carries."""
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
        members = [line[7:].strip() for line in text.splitlines() if line.startswith("Path = ")]
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
                             "why": "packed inside the collection itself; collections2mo2 "
                                    "unpacks it, nothing to download"})
        else:
            where = src.get("url") or src.get("instructions") or mod.get("instructions") or ""
            left_out.append({"mod": name, "source": kind or "?",
                             "why": "downloaded by hand from another site, which "
                                    "collections2mo2 does not install yet"
                                    + (f" ({where})" if where else "")})

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


def write_list_file(path, info, manifest, entries, left_out, members):
    """Write the .collection: 'modlist' for every stage that reads a list,
    collection.json as fetched, and wizard.json for this module."""
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
    }
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("modlist", json.dumps(modlist, indent=1, ensure_ascii=False))
        z.writestr("collection.json", json.dumps(manifest, indent=1, ensure_ascii=False))
        z.writestr("wizard.json", json.dumps(wizard, indent=1, ensure_ascii=False))
    os.replace(tmp, path)
    return path


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
                              f"collections2mo2 cannot build an instance for")
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
    finally:
        for p in (scratch, scratch + ".part"):
            try:
                os.remove(p)
            except OSError:
                pass

    entries, left_out = archive_entries(nexus, manifest, known=known, say=say)
    filename = existing or (list_stem(info["name"], info["slug"]) + CFG.COLLECTION_EXT)
    write_list_file(os.path.join(manifest_dir, filename), info, manifest, entries, left_out,
                    members)
    _say(say, f"  Wrote {filename}: {len(entries)} archive(s) to pool"
              + (f", {len(left_out)} mod(s) not downloadable here" if left_out else "") + ".")
    return filename, True


# ----------------------------------------------------------------------
# What a collection asks of this machine
# ----------------------------------------------------------------------
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
    return {
        "mods": len(mods),
        "sources": kinds,
        "fomod": sum(1 for m in mods if (m.get("choices") or {}).get("type") == "fomod"),
        "replicate": sum(1 for m in mods if m.get("hashes")),
        "patches": sum(1 for m in mods if m.get("patches")),
        "overrides": sum(1 for m in mods if m.get("fileOverrides")),
        "optional": sum(1 for m in mods if m.get("optional")),
        "types": typed,
        "rules": len(manifest.get("modRules") or []),
        "plugins": len(manifest.get("plugins") or []),
        "plugin_rules": len(((manifest.get("pluginRules") or {}).get("plugins")) or []),
        "tools": len(manifest.get("tools") or []),
        "versions": list((manifest.get("info") or {}).get("gameVersions") or []),
    }


def exe_version(path):
    """An executable's file version as 'a.b.c.d', or None."""
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
        ptr, n = ctypes.c_void_p(), wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(n)) or not ptr:
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


def report_lines(report):
    """The report as the lines the console, the window and the CLI all show."""
    wiz, feat, pools = report["wizard"], report["features"], report["pools"]
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
                    + " (through Root Builder)")
    if feat["rules"]:
        bits.append(f"{feat['rules']} mod rule(s) deciding conflicts")
    if feat["plugins"]:
        bits.append(f"{feat['plugins']} plugin(s)" + (f", {feat['plugin_rules']} plugin rule(s)"
                                                     if feat["plugin_rules"] else ""))
    lines.append("  Install    " + ("; ".join(bits) if bits else "plain archives, nothing to replay"))
    members = wiz.get("members") or {}
    if members.get("ini_tweaks"):
        lines.append(f"             {members['ini_tweaks']} INI tweak file(s), merged by collections2mo2")
    gaps = []
    if feat["patches"]:
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
                lines.append("             SKSE plugins are built for one game version; on a "
                             "different one they fail to load.")
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


def c2mo2_args(c2, url, revision, out, game_path, downloads, stock_game=True):
    """The command that builds the instance. --skip-survey leaves out c2mo2's
    optional preview of every mod page, which costs API allowance and changes
    nothing it installs."""
    args = [c2["python"], "-X", "utf8", "-s", "-m", "collections2mo2.cli", "create", url,
            "--revision", str(revision), "--out", out, "--game-path", game_path,
            "--downloads-dir", downloads, "--skip-survey"]
    if stock_game:
        args.append("--stock-game")
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


def install_plan(list_path, out=None, game_path=None):
    """Everything the hand-over needs, and what stands in its way.

    Returns a dict: list, info (wizard.json), downloads, game (path, how,
    version, matches), c2 (find_c2mo2()), staged (ok, problems), blockers -
    the reasons it cannot start - and, once an instance folder is chosen
    (here or with set_instance()), out and path_problems. Reading every staged
    archive for its MD5 is the slow part, so a plan is made once and pointed at
    a folder afterwards."""
    data = read_list_file(list_path)
    wiz = data["wizard"]
    gametype = DOMAIN_GAMETYPE.get(wiz["domain"], wiz["domain"])
    downloads = os.path.join(WJ.resolve_game_dirs(gametype)[0],
                             os.path.splitext(os.path.basename(list_path))[0])
    blockers = []
    c2 = find_c2mo2()
    if not c2:
        blockers.append("collections2mo2 is not set up here yet (wj_collection.py setup-c2mo2)")
    elif not c2["python"]:
        blockers.append("collections2mo2 needs Python; run this from the scripts, not a build")
    elif not c2["current"]:
        blockers.append("the collections2mo2 in .c2mo2 is not the pinned commit; set it up again")
    how = "given on the command line"
    if not game_path:
        game_path, how = game_install(gametype)
    exe = os.path.join(game_path, GAME_EXES.get(gametype, "")) if game_path else None
    have = exe_version(exe) if exe else None
    game = {"path": game_path, "how": how, "version": have,
            "matches": version_matches(have, (data["collection"].get("info") or {})
                                       .get("gameVersions"))}
    if not game_path or not os.path.isdir(game_path):
        blockers.append(f"the game install was not found ({how}); give it with --game-path")
    if not os.path.isdir(downloads):
        staged = (0, [("(folder)", "the list has not been staged yet")])
    else:
        staged = check_staged(list_path, downloads)
    if staged[1]:
        blockers.append(f"{len(staged[1])} archive(s) are missing or wrong in the staged folder")
    plan = {"list": list_path, "info": wiz, "gametype": gametype, "downloads": downloads,
            "game": game, "c2": c2, "staged": staged, "blockers": blockers,
            "out": None, "path_problems": []}
    if out:
        set_instance(plan, out)
    return plan


def set_instance(plan, out):
    """Point a plan at an instance folder, and say what is wrong with it."""
    plan["out"] = os.path.abspath(out) if out else None
    plan["path_problems"] = (check_instance_path(plan["list"], plan["downloads"], plan["out"],
                                                 plan["game"]["path"]) if out else [])
    return plan


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
    if game["path"]:
        verdict = {True: "matches", False: "DIFFERS from", None: "cannot be compared with"}[
            game["matches"]]
        lines.append(f"Game        {game['path']}  (version {game['version'] or '?'}, "
                     f"{verdict} what the collection wants)")
    else:
        lines.append(f"Game        not found: {game['how']}")
    c2 = plan["c2"]
    lines.append("Installer   " + (f"collections2mo2 {(c2['commit'] or '?')[:7]} in {c2['dir']}"
                                   if c2 else "collections2mo2 is not set up"))
    if plan.get("out"):
        lines.append(f"Instance    {plan['out']}  (portable MO2, the game copied into Stock Game)")
    for b in plan["blockers"] + plan.get("path_problems", []):
        lines.append("[NO] " + b)
    return lines


def install(plan, api_key, on_line=None):
    """Run c2mo2 for a plan with nothing in its way. Returns its exit code."""
    stops = plan["blockers"] + plan.get("path_problems", [])
    if not plan.get("out"):
        stops = stops + ["no instance folder was chosen"]
    if stops:
        raise CollectionError("; ".join(stops))
    wiz, c2 = plan["info"], plan["c2"]
    os.makedirs(plan["out"], exist_ok=True)
    args = c2mo2_args(c2, wiz["url"], wiz["revision"], plan["out"], plan["game"]["path"],
                      plan["downloads"])
    return run_c2mo2(args, c2mo2_env(c2, api_key), cwd=c2["dir"], on_line=on_line)


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


def cmd_install(args):
    mdir = _manifest_dir()
    fn = resolve_list(mdir, args.ref)
    if not fn:
        raise CollectionError(f"no collection list matching '{args.ref}' in {mdir}")
    out = os.path.abspath(os.path.expandvars(os.path.expanduser(args.out)))
    plan = install_plan(os.path.join(mdir, fn), out, game_path=args.game_path)
    for line in plan_lines(plan):
        print(line)
    if plan["blockers"] or plan["path_problems"]:
        print("\nNothing started.")
        return 2
    if not args.yes:
        try:
            if input("\nBuild the instance?  [y] yes  [n] no  ").strip().lower() != "y":
                print("Nothing started.")
                return 0
        except EOFError:
            print("Nothing started (no answer; pass --yes to run unattended).")
            return 0
    _nexus_client, key = _nexus()
    rc = install(plan, key, on_line=lambda l: print("  | " + l))
    print(f"\ncollections2mo2 exited {rc}." + (f" The instance is in {out}." if rc == 0 else ""))
    return 0 if rc == 0 else 1


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Nexus collections as lists: fetch, inspect, and build a portable MO2 "
                    "instance with collections2mo2.")
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
