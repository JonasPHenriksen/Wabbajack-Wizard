#!/usr/bin/env python3
"""
The published gallery, and what you already own of it
====================================================
Wabbajack's gallery is not served by an API. It is a set of JSON files in the
`wabbajack-tools/mod-lists` GitHub repository, and this module reads them:

  repositories.json          149 author feeds (read by wj_autoupdate)
  featured_lists.json         59 "<repo>/<machineURL>" keys shown as Featured
  disabled_mod_lists.json      7 retired lists, still carrying live download
                               URLs. Hand-edited, so it has trailing commas
                               and is not strictly valid JSON.
  reports/modListSummary.json  one row per published list: how many of its
                               archives the validator could still download
  reports/<repo>/<machine>/status.json
                               EVERY archive of one list, with Name, Size,
                               Hash, Meta and State - the same shape as the
                               Archives[] inside the .wabbajack itself

That last file is the reason this module exists. A .wabbajack is 100 MB to
5 GB; its report is a median 728 KB, and gzipped over the wire it is nearer
140 KB. So the question "how much of this list do I already have?" can be
answered before committing to the big download - which for the lists measured
here was between 36% and 58% already on disk, or 150 GB not fetched twice.

Verified against a real manifest rather than assumed: for wj-featured/tbv the
report's archive set is identical to the local .wabbajack's, 273 for 273, same
names and same sizes.

Nothing here judges a list unfit to install. A "failed" archive means the
VALIDATOR could not download it from its source, which says nothing about
whether the file is already sitting in your Master pool - see evaluate(),
which reports the dead archives you have separately from the ones you do not.
"""

import concurrent.futures
import gzip
import json
import os
import re
import time
import urllib.error
import urllib.request

import wj_paths

try:
    import wj_config as CFG
except ImportError:
    CFG = None

HERE = wj_paths.app_dir(__file__)

# ============================ CONFIGURATION ============================
RAW_BASE = ("https://raw.githubusercontent.com/wabbajack-tools/"
            "mod-lists/master/")

SUMMARY_PATH = "reports/modListSummary.json"
FEATURED_PATH = "featured_lists.json"
DISABLED_PATH = "disabled_mod_lists.json"

# Where fetched JSON is kept. Reports are stored gzipped: 254 MB of reports
# is 48 MB on disk, and they are only ever read back through this module.
CACHE_DIRNAME = ".gallery-cache"

# The small index files change several times a day, so they are cheap to
# re-check. A per-list report only changes when its author recompiles.
INDEX_TTL = 6 * 3600
REPORT_TTL = 14 * 86400

HTTP_TIMEOUT = 60
USER_AGENT = CFG.USER_AGENT if CFG is not None else "WabbajackWizard"
WORKERS = 8
# =======================================================================


class GalleryError(Exception):
    """A gallery file could not be read. Never means 'the list is bad'."""


# ----------------------------------------------------------------------
# Cache
# ----------------------------------------------------------------------
def cache_root():
    return os.path.join(HERE, CACHE_DIRNAME)


def cache_file(rel):
    """Local path for a remote file, mirroring its remote layout.

    Kept gzipped, with the '.gz' on the end, so a half-written cache entry is
    obvious rather than being served back as truncated JSON.

    A report's `rel` is built from a list's key, and the keys come from the
    authors' own feeds. Those are refused on the way in when they are not
    plain names (wj_autoupdate.normalise), and this is the last word: a path
    that would leave the cache is a GalleryError, never a file written
    wherever it pointed."""
    root = os.path.abspath(cache_root())
    path = os.path.abspath(os.path.join(root, *rel.split("/")) + ".gz")
    try:
        inside = os.path.commonpath([root, path]) == root
    except ValueError:                      # another drive altogether
        inside = False
    if not inside:
        raise GalleryError("refusing a cache path outside the cache: %s" % rel)
    return path


def cache_age(rel):
    """Seconds since the cached copy was written, or None if there isn't one."""
    try:
        return max(0.0, time.time() - os.path.getmtime(cache_file(rel)))
    except OSError:
        return None


def _write_cache(rel, raw):
    path = cache_file(rel)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".part"
        with gzip.open(tmp, "wb") as fh:
            fh.write(raw)
        os.replace(tmp, path)
    except OSError:
        # A read-only or full share must not stop the caller getting an answer;
        # it only means the next run pays for the fetch again.
        pass


def _read_cache(rel):
    try:
        with gzip.open(cache_file(rel), "rb") as fh:
            return fh.read()
    except (OSError, EOFError, gzip.BadGzipFile):
        return None


def cache_size():
    """(bytes, files) held in the gallery cache."""
    total = files = 0
    for base, _dirs, names in os.walk(cache_root()):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(base, n))
                files += 1
            except OSError:
                pass
    return total, files


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------
def http_bytes(rel, url=None):
    """GET one gallery file, or any URL named outright. Asks for gzip: the
    big reports compress 5x."""
    req = urllib.request.Request(
        url or (RAW_BASE + rel),
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return raw


def lenient_json(text):
    """Parse JSON that a human has edited.

    disabled_mod_lists.json carries a trailing comma before its closing
    bracket, so json.loads refuses it outright. Dropping trailing commas is
    the smallest change that reads the file the way its author meant it, and
    is applied to that file alone rather than to everything."""
    try:
        return json.loads(text)
    except ValueError:
        return json.loads(re.sub(r",(\s*[}\]])", r"\1", text))


def fetch_json(rel, ttl=INDEX_TTL, force=False, lenient=False, url=None):
    """A gallery file as Python, from the cache when it is fresh enough.

    Falls back to a stale cached copy if the network is down, because a
    slightly old gallery is far more useful than no gallery. `url` names a
    file outside the gallery repository - Wabbajack's own release feed - which
    is then kept in this cache under `rel` like everything else."""
    age = cache_age(rel)
    if not force and age is not None and age < ttl:
        raw = _read_cache(rel)
        if raw is not None:
            return (lenient_json(raw.decode("utf-8-sig")) if lenient
                    else json.loads(raw.decode("utf-8-sig")))
    try:
        raw = http_bytes(rel) if url is None else http_bytes(rel, url)
    except Exception as e:
        stale = _read_cache(rel)
        if stale is not None:
            return (lenient_json(stale.decode("utf-8-sig")) if lenient
                    else json.loads(stale.decode("utf-8-sig")))
        raise GalleryError("%s: %s" % (rel, e))
    _write_cache(rel, raw)
    return (lenient_json(raw.decode("utf-8-sig")) if lenient
            else json.loads(raw.decode("utf-8-sig")))


# ----------------------------------------------------------------------
# The index files
# ----------------------------------------------------------------------
def load_summary(force=False):
    """{"<repo>/<machine>": {failed, passed, updating, mirrored, ...}}"""
    rows = fetch_json(SUMMARY_PATH, force=force)
    out = {}
    if isinstance(rows, list):
        for r in rows:
            if isinstance(r, dict) and r.get("machineURL"):
                out[str(r["machineURL"])] = r
    return out


def load_featured(force=False):
    """The keys Wabbajack presents as Featured.

    Not the same thing as the 'wj-featured' repository: that holds 18 lists,
    while this file names 59 across many authors' repositories."""
    keys = fetch_json(FEATURED_PATH, force=force)
    return {str(k) for k in keys} if isinstance(keys, list) else set()


def load_disabled(force=False):
    """Retired lists, keyed the same way as everything else.

    They are gone from the gallery but their download URLs still resolve, so
    they are worth showing as a separate group rather than hidden."""
    rows = fetch_json(DISABLED_PATH, force=force, lenient=True)
    out = {}
    if isinstance(rows, list):
        for r in rows:
            if not isinstance(r, dict):
                continue
            mu = (r.get("links") or {}).get("machineURL") or r.get("machineURL")
            # The key becomes a path in the cache, like a feed's; see
            # cache_file().
            if mu and (CFG is None or CFG.plain_name_problem(str(mu)) is None):
                out["retired/%s" % mu] = r
    return out


# ----------------------------------------------------------------------
# Games
# ----------------------------------------------------------------------
def _known_games():
    """Lowercased GameType -> the spelling this tooling uses."""
    known = {}
    if CFG is not None:
        for g in list(getattr(CFG, "GAME_DISPLAY", {})) + \
                 list(getattr(CFG, "GAME_FOLDERS", {})):
            known.setdefault(str(g).lower(), str(g))
    return known


def canonical_game(token):
    """A feed's `game` token as a GameType this tooling recognises.

    The feeds are not consistent about case - 'starfield' and 'Starfield' both
    appear, as do 'fallout4' and 'Fallout4' - so anything that groups or
    counts by game has to fold case or it reports one game twice."""
    if not token:
        return None
    return _known_games().get(str(token).lower(), str(token))


def game_key(token):
    """A case-stable key for grouping lists by game."""
    return str(canonical_game(token) or "").lower()


def pool_key(token):
    """A case-stable key for the POOL FOLDER a feed's game token maps onto.

    Distinct from game_key(): Skyrim and Skyrim Special Edition are two games
    but one folder here, and anything deciding what that folder's Master may
    hold has to consult the lists of both. Without wj_config this collapses to
    game_key(), which is right for every game that has its own folder."""
    g = canonical_game(token)
    if not g:
        return ""
    if CFG is not None and hasattr(CFG, "folder_key"):
        return CFG.folder_key(g)
    return g.lower()


def game_label(token):
    g = canonical_game(token)
    if CFG is not None and hasattr(CFG, "game_label"):
        return CFG.game_label(g)
    return g or "?"


# ----------------------------------------------------------------------
# Lists the user has hidden
# ----------------------------------------------------------------------
def hidden_keys():
    """Lowercased feed keys named in HIDDEN: lists kept out of the gallery.

    Compared case-insensitively like EXCLUDE, because the keys are typed by
    hand in the settings file as often as written by the Hide button."""
    if CFG is None:
        return set()
    return {str(k).strip().lower() for k in (getattr(CFG, "HIDDEN", None) or ())
            if str(k).strip()}


def is_hidden(key):
    """Is this feed key one the user has hidden?"""
    return str(key or "").strip().lower() in hidden_keys()


def pool_dir(token):
    """The existing pool folder for a game, or None.

    Matched case-insensitively against what is actually on the share: the
    folder is the authority on its own spelling, and 'Fallout4' from one feed
    must not create a second pool beside the 'fallout4' of another."""
    if CFG is None:
        return None
    g = canonical_game(token)
    if not g:
        return None
    want = CFG.game_folder(g)
    root = CFG.resolved_archive_root()
    direct = os.path.join(root, want)
    if os.path.isdir(direct):
        return direct
    try:
        names = os.listdir(root)
    except OSError:
        return None
    low = want.lower()
    for n in names:
        if n.lower() == low and os.path.isdir(os.path.join(root, n)):
            return os.path.join(root, n)
    return None


def pooled_games(tokens):
    """Of the game tokens given, those with a pool folder on the share.

    Returns {game_key: (label, pool_dir)}. This is what limits the browse
    stage to lists that could actually overlap what you own."""
    out = {}
    for t in tokens:
        k = game_key(t)
        if not k or k in out:
            continue
        d = pool_dir(t)
        if d:
            out[k] = (game_label(t), d)
    return out


# ----------------------------------------------------------------------
# Per-list reports
# ----------------------------------------------------------------------
def report_rel(key):
    """The report path for a '<repo>/<machineURL>' key."""
    return "reports/%s/status.json" % key


def have_report(key):
    return os.path.isfile(cache_file(report_rel(key)))


def load_report(key, force=False):
    """One list's full archive report, or None if it isn't published."""
    try:
        rep = fetch_json(report_rel(key), ttl=REPORT_TTL, force=force)
    except GalleryError:
        return None
    return rep if isinstance(rep, dict) else None


def report_status(rep):
    """The report's own verdict on the list.

    Observed values, across all 224 published lists: "Available" (202),
    "Failed" (17) and "ForcedDown" (5). The last one matters more than it
    looks: those five reports carry an EMPTY Archives[], because the validator
    does not walk a list its author has pulled. Their summary rows read
    passed=0 failed=0, which is "not checked" and not "nothing works" - so
    they must not be presented as a list you own none of."""
    return str((rep or {}).get("Status") or "Unknown")


def report_is_empty(rep):
    """True when a report exists but carries no archives to compare against."""
    return not (rep or {}).get("Archives")


def report_archives(rep, wj):
    """The report's Archives[] in the same shape parse_manifest() produces.

    Each entry wraps the real archive in `Original`, whose contents are the
    identical JSON object found inside the .wabbajack. Reusing the
    downloader's own state_type()/parse_meta() means a source this tooling
    cannot fetch is classified here exactly as it would be there."""
    out = []
    for item in (rep.get("Archives") or []):
        if not isinstance(item, dict):
            continue
        a = item.get("Original")
        if not isinstance(a, dict):
            continue
        name = a.get("Name")
        if not name:
            continue
        # Held to the rule a manifest is (wj_downloader.parse_manifest): a
        # name that is not a plain file name matches no file on disk, and
        # is no archive anyone could hold.
        if CFG is not None and CFG.plain_name_problem(str(name)):
            continue
        meta_text = a.get("Meta") or ""
        state = a.get("State") or {}
        out.append({
            "name": str(name),
            "size": a.get("Size") if isinstance(a.get("Size"), int) else None,
            "hash": a.get("Hash"),
            "meta_text": meta_text,
            "meta": wj.parse_meta(meta_text),
            "state": state,
            "kind": wj.state_type(state),
            "status": str(item.get("Status") or "Unknown"),
        })
    return out


def evaluate(archives, existing, wj):
    """How much of a list is already on disk, and what can no longer be got.

    `existing` is the downloader's index of the pools, so "present" means the
    same thing here as it does when the downloader decides to skip a file:
    a copy of the right size anywhere in Master, Shared, or a list folder.

    Dead archives are split deliberately. An archive the validator can no
    longer download is only a problem if you do not already have it, and the
    whole point of a pooled collection is that you often do."""
    st = {"total": 0, "present": 0, "missing": 0, "gamefile": 0,
          "total_bytes": 0, "present_bytes": 0, "missing_bytes": 0,
          "dead_have": [], "dead_need": []}
    for a in archives:
        size = a.get("size") or 0
        if a.get("kind") == "gamefile":
            st["gamefile"] += 1
            continue
        st["total"] += 1
        st["total_bytes"] += size
        hit, _why = wj.present_match(existing, a)
        if hit:
            st["present"] += 1
            st["present_bytes"] += size
        else:
            st["missing"] += 1
            st["missing_bytes"] += size
        if a.get("status") not in ("Valid", "Unknown"):
            (st["dead_have"] if hit else st["dead_need"]).append(a)
    st["percent"] = (100.0 * st["present"] / st["total"]) if st["total"] else 0.0
    return st


def sync_reports(keys, progress=None, force=False, workers=WORKERS):
    """Fetch every named list's report into the cache.

    Concurrent because the sweep is 233 files, and gzip-encoded because that
    turns 254 MB into roughly 48 MB. Returns (fetched, cached, failed_keys).
    A failure is collected rather than raised: one author's missing report is
    not a reason to abandon the other 232."""
    keys = list(keys)
    todo = []
    cached = 0
    for k in keys:
        age = cache_age(report_rel(k))
        if not force and age is not None and age < REPORT_TTL:
            cached += 1
        else:
            todo.append(k)

    fetched, failed = 0, []
    if not todo:
        if progress:
            progress(0, 0, cached, None)
        return fetched, cached, failed

    def grab(k):
        try:
            raw = http_bytes(report_rel(k))
        except Exception as e:
            return k, None, e
        return k, raw, None

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for k, raw, err in pool.map(grab, todo):
            done += 1
            if err is not None:
                failed.append(k)
            else:
                _write_cache(report_rel(k), raw)
                fetched += 1
            if progress:
                progress(done, len(todo), cached, k)
    return fetched, cached, failed



# ----------------------------------------------------------------------
# What the whole published gallery wants
# ----------------------------------------------------------------------
# Why a published list could not contribute to the protected set. Only the
# first is permanent: the validator does not walk a list its author has
# pulled, so its report carries an empty Archives[] for as long as it stays
# pulled. Every other reason is a gap that a refresh, or the validator's next
# run, closes - and a caller deciding what to DELETE must treat those as
# "do not know yet", not as "wants nothing".
PULLED = "pulled by its author; its report lists no archives"
UNVALIDATED = "not yet validated (no summary row)"
UNREADABLE = "report unreadable or not cached"


def published_builds(pools, feeds, summary, wj, progress=None):
    """Every archive build any published list for these pool folders asks for.

    `pools` is a set of pool_key() values. Returns (want, checked, unchecked)
    where `want` is { name_lower: set(sizes) } in the same shape the wizard's
    wanted_builds() produces, `checked` is how many lists contributed, and
    `unchecked` maps each list that could not to the reason - PULLED,
    UNVALIDATED, UNREADABLE, or a description of an empty report that is not
    ForcedDown.

    `unchecked` is the important return value. This set is used to decide what
    is safe to DELETE, so an incomplete answer is not a smaller answer - it is
    a wrong one. A caller must refuse to delete on the strength of a set it
    knows to be short, rather than quietly reclaiming a few more gigabytes.
    Every published list for the pools is named here, including those with no
    summary row: a list the validator has not seen yet wants things too, and
    silently leaving it out is exactly the short answer this guards against."""
    keys = [k for k, e in feeds.items() if pool_key(e.get("game")) in pools]
    want = {}
    checked, unchecked = 0, {}
    for i, key in enumerate(keys, 1):
        if key not in summary:
            # A pulled list can drop out of the summary altogether - LegacyRim
            # did, between one day and the next - and that is as permanent as
            # an empty ForcedDown report. The feed's own force_down flag, or a
            # cached report that says ForcedDown, tells the two apart from a
            # list the validator simply has not reached yet. Only the cache is
            # consulted here, so a brand-new list costs no request.
            rep = load_report(key) if have_report(key) else None
            if (feeds[key].get("force_down")
                    or (rep is not None and report_is_empty(rep)
                        and report_status(rep) == "ForcedDown")):
                unchecked[key] = PULLED
            else:
                unchecked[key] = UNVALIDATED
            continue
        rep = load_report(key)
        if rep is None:
            unchecked[key] = UNREADABLE
            continue
        if report_is_empty(rep):
            st = report_status(rep)
            unchecked[key] = (PULLED if st == "ForcedDown"
                              else "report lists no archives (status %s)" % st)
            continue
        checked += 1
        for a in report_archives(rep, wj):
            low = a["name"].lower()
            size = a.get("size")
            if size is None:
                want[low] = None
            elif low in want:
                if want[low] is not None:
                    want[low].add(size)
            else:
                want[low] = {size}
        if progress:
            progress(i, len(keys), key)
    return want, checked, unchecked


def merge_builds(*maps):
    """Union of several { name: set(sizes) | None } maps.

    None means "wanted, but at an unstated size", which has to win over any
    specific set: if one list pins a size and another does not, every copy of
    that name must be protected."""
    out = {}
    for m in maps:
        for low, sizes in (m or {}).items():
            if low not in out:
                out[low] = None if sizes is None else set(sizes)
            elif out[low] is None or sizes is None:
                out[low] = None
            else:
                out[low].update(sizes)
    return out


# ----------------------------------------------------------------------
# Assembling a browsable gallery
# ----------------------------------------------------------------------
def build_rows(feeds, summary, featured, disabled, have_keys, games=None):
    """One row per published list, with everything known before any report.

    `feeds` is wj_autoupdate.load_feeds()'s dict, so version/size/archive
    count come from the same normalised entries the updater uses. `games`, if
    given, restricts the result to those game keys.

    Every row carries `hidden`: named in HIDDEN and not tracked. The rows are
    all returned, hidden ones included, so a caller decides what to do with
    them - the browse stage shows them in a view of their own, the protection
    rule leaves them out. A tracked list is never hidden whatever HIDDEN
    says, because its own manifest is what protects its archives and a flag
    that could quietly cancel that would be worse than none."""
    hid = hidden_keys()
    rows = []
    for key, ent in feeds.items():
        gk = game_key(ent.get("game"))
        if games is not None and gk not in games:
            continue
        s = summary.get(key) or {}
        rows.append({
            "key": key,
            "title": ent.get("title") or key,
            "game": canonical_game(ent.get("game")),
            "game_key": gk,
            "version": ent.get("version") or "",
            "archives": ent.get("archives"),
            "arch_size": ent.get("arch_size"),
            "download": ent.get("download"),
            "have": key in have_keys,
            "hidden": key.lower() in hid and key not in have_keys,
            "featured": key in featured,
            "retired": False,
            "force_down": bool(ent.get("force_down")),
            "failed": s.get("failed") or 0,
            "passed": s.get("passed") or 0,
            "modlist_missing": bool(s.get("modlist_missing")),
            "nsfw": bool(ent.get("nsfw")),
            "tags": [str(t) for t in (ent.get("tags") or [])],
            "updated": updated_date(ent.get("updated")),
            "entry": ent,
        })

    # disabled_mod_lists.json is a graveyard, and some of its occupants got
    # up again: 'living_skyrim' and 'elysium' are both in there AND published
    # today under wj-featured and Elysium. Listing those as Retired beside
    # their own live entries would be worse than not listing them at all.
    live_machines = {str(e.get("machine") or "").lower()
                     for e in feeds.values()}
    for key, raw in (disabled or {}).items():
        gk = game_key(raw.get("game"))
        if games is not None and gk not in games:
            continue
        machine = str((raw.get("links") or {}).get("machineURL") or "").lower()
        if machine in live_machines:
            continue
        dm = raw.get("download_metadata") or {}
        links = raw.get("links") or {}
        rows.append({
            "key": key,
            "title": raw.get("title") or key,
            "game": canonical_game(raw.get("game")),
            "game_key": gk,
            "version": str(raw.get("version") or ""),
            "archives": dm.get("NumberOfArchives"),
            "arch_size": dm.get("SizeOfArchives"),
            "download": links.get("download"),
            "have": key in have_keys,
            "hidden": key.lower() in hid and key not in have_keys,
            "featured": False,
            "retired": True,
            "force_down": False,
            "failed": 0,
            "passed": 0,
            "modlist_missing": False,
            "nsfw": bool(raw.get("nsfw")),
            "tags": [str(t) for t in (raw.get("tags") or []) if isinstance(t, (str, int))],
            "updated": updated_date(raw.get("dateUpdated")),
            # Enough of a feed entry to download from, but deliberately no
            # repository: there is no feed behind a retired list, so it can be
            # fetched and installed and can never be version-tracked.
            "entry": {
                "repo": "", "key": key, "machine": links.get("machineURL"),
                "title": raw.get("title") or key,
                "game": raw.get("game") or "",
                "version": str(raw.get("version") or ""),
                "updated": "", "download": links.get("download"),
                "hash": dm.get("Hash"),
                "size": dm.get("Size") if isinstance(dm.get("Size"), int) else None,
                "archives": dm.get("NumberOfArchives"),
                "arch_size": dm.get("SizeOfArchives"),
            } if links.get("download") else None,
        })
    return rows


def updated_date(text):
    """A feed's dateUpdated as YYYY-MM-DD, or '' when it carries none. The
    feeds write it several ways - a date, a date and time, with or without a
    zone - and the date is the part a table needs."""
    m = re.search(r"\d{4}-\d{2}-\d{2}", str(text or ""))
    return m.group(0) if m else ""


def search_text(row):
    """What a Browse search looks through for one row, lower-cased: the
    title, the key, the game, and the tags."""
    return " ".join([str(row.get("title") or ""), str(row.get("key") or ""),
                     game_label(row.get("game")) or ""]
                    + [str(t) for t in row.get("tags") or []]).lower()


def browse_filter(rows, search="", hide_nsfw=False):
    """(shown, nsfw_hidden): the rows a search and the NSFW setting leave in
    a Browse table, and how many NSFW rows the setting kept out.

    Every word of `search` has to appear somewhere in search_text(), in any
    order, so "survival skyrim" finds a list tagged Survival. Display only,
    like split_completable(): nothing here changes what a list protects."""
    words = str(search or "").lower().split()
    shown, nsfw = [], 0
    for r in rows:
        if hide_nsfw and r.get("nsfw"):
            nsfw += 1
            continue
        if words:
            text = search_text(r)
            if not all(w in text for w in words):
                continue
        shown.append(r)
    return shown, nsfw


def completable(row):
    """Could this list end up installed, from what is held plus what is
    still obtainable? True, False, or None when that cannot be told.

    Decided from the measured gap, so only after evaluate(): every archive is
    either already in the pools or one the validator could still download.
    A dead archive already held costs nothing; a dead archive not held is
    what stops an install, whoever does the downloading. None for a list
    with no usable report - retired, pulled, or simply not fetched yet -
    because "cannot tell" must never read as "cannot install"."""
    g = row.get("gap")
    if g is None:
        return None
    return not g["dead_need"]


def split_completable(rows):
    """(shown, blocked, unknown) under the 'only lists that can be
    completed' rule.

    A tracked list is always shown: its archives are already this tooling's
    business, whatever the validator says about them. `blocked` is what a
    dead archive not in the pools rules out; `unknown` is what could not be
    measured, which is a different thing and is reported as such."""
    shown, blocked, unknown = [], [], []
    for r in rows:
        c = completable(r)
        if r.get("have") or c is True:
            shown.append(r)
        elif c is False:
            blocked.append(r)
        else:
            unknown.append(r)
    return shown, blocked, unknown


def unavailable(row):
    """Wabbajack's own 'Unavailable' test, reproduced.

    Wabbajack.App.Wpf/ViewModels/Gallery/BaseModListMetadataVM.cs:

        IsBroken = (Summary?.HasFailures ?? false) || metadata.ForceDown;

    with HasFailures being `Failed > 0 || ModListIsMissing`. It disables the
    Install button on the gallery tile and nothing else: the .wabbajack stays
    on the CDN, and Wabbajack.CLI's install verb - the one this tooling
    drives - never consults it. So this is shown as a label and a warning,
    not as a veto."""
    return bool(row["failed"] or row["modlist_missing"] or row["force_down"])


def why_unavailable(row):
    bits = []
    if row["failed"]:
        bits.append("%d archive(s) no longer downloadable at source"
                    % row["failed"])
    if row["modlist_missing"]:
        bits.append("the .wabbajack itself is missing upstream")
    if row["force_down"]:
        bits.append("flagged force_down by its author")
    return bits
