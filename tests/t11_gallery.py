"""The published gallery: reading it, folding it onto our pools, judging it.

Nothing here touches the network. Every fetch is served from a cache this
suite writes itself, which is also the point of one of the checks: the browse
stage has to work when GitHub is unreachable, because a 254 MB report sweep
that half-fails must still leave a usable gallery.
"""
import gzip
import io
import json
import logging
import os
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOXROOT = os.path.join(os.environ.get("TEMP", "/tmp"), "wj_tests")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import check, summary

sys.path.insert(0, REPO)
os.chdir(REPO)
import wj_autoupdate as A
A.setup_logging(False)
A.LOG.setLevel(logging.CRITICAL)
WJ = A.load_downloader_module()
import wj_config as CFG
import wj_gallery as G

BOX = os.path.join(BOXROOT, "sb11")

print("=" * 70)
print("  T11  THE PUBLISHED GALLERY")
print("=" * 70)


# ------------------------------------------------------------------ setup
def fresh_box():
    """Point the module's cache and the pool root at a private sandbox."""
    shutil.rmtree(BOX, ignore_errors=True)
    os.makedirs(BOX)
    G.HERE = BOX
    CFG.ARCHIVE_ROOT = os.path.join(BOX, "Mod Archive")
    os.makedirs(CFG.ARCHIVE_ROOT)
    return BOX


def seed_cache(rel, obj):
    """Write a cache entry the way _write_cache does, so fetch_json finds it."""
    G._write_cache(rel, json.dumps(obj).encode("utf-8"))


def no_network():
    """Make every fetch fail, to prove the cache is doing the work."""
    def boom(rel):
        raise OSError("network disabled for this test")
    G.http_bytes = boom


REAL_HTTP = G.http_bytes


def rep(name, version, archives, status="Available"):
    """A status.json in the real published shape: Original-wrapped archives."""
    kinds = {"nexus": "NexusDownloader, Wabbajack.Lib",
             "http": "HttpDownloader, Wabbajack.Lib",
             "gamefile": "GameFileSourceDownloader, Wabbajack.Lib"}
    return {
        "MachineURL": name, "Name": name, "Version": version,
        "ModListHash": "AAAAAAAAAAA=", "Status": status,
        "Failures": sum(1 for a in archives if a[3] != "Valid"),
        "Archives": [
            {"Status": st,
             "Original": {"Name": n, "Size": sz, "Hash": "AAAAAAAAAAA=",
                          "Meta": "[General]\ngameName=fallout4\n",
                          "State": {"$type": kinds[k],
                                    "Url": "https://example.invalid/%s" % n}}}
            for (n, sz, k, st) in archives],
    }


def feed_entry(repo, machine, game, version="1.0", **kw):
    e = {"repo": repo, "key": "%s/%s" % (repo, machine), "machine": machine,
         "title": kw.get("title", machine), "game": game, "version": version,
         "updated": "", "download": kw.get("download", "https://x/%s" % machine),
         "hash": "AAAAAAAAAAA=", "size": kw.get("size", 1000),
         "archives": kw.get("archives", 10),
         "arch_size": kw.get("arch_size", 10000),
         "force_down": kw.get("force_down", False),
         "nsfw": False, "official": False, "tags": []}
    return e


def summ_row(key, passed=10, failed=0, missing=False):
    return {"machineURL": key, "name": key, "passed": passed, "failed": failed,
            "updating": 0, "mirrored": 0, "modlist_missing": missing,
            "has_failures": bool(failed or missing)}


# ---------------------------------------------------------------- 11.1
print("\n 11.1  disabled_mod_lists.json is hand-edited and not valid JSON")
check("a trailing comma before ] is tolerated",
      G.lenient_json('[{"a": 1},]') == [{"a": 1}])
check("a trailing comma before } is tolerated",
      G.lenient_json('{"a": 1,}') == {"a": 1})
check("valid JSON is still parsed unchanged",
      G.lenient_json('{"a": [1, 2]}') == {"a": [1, 2]})
bad = False
try:
    G.lenient_json("{this is not json at all")
except ValueError:
    bad = True
check("genuinely broken JSON still raises", bad)

# ---------------------------------------------------------------- 11.2
print("\n 11.2  the feeds disagree about case; grouping must not")
check("skyrimspecialedition folds onto our GameType",
      G.canonical_game("skyrimspecialedition") == "SkyrimSpecialEdition",
      G.canonical_game("skyrimspecialedition"))
check("SkyrimSpecialEdition is left alone",
      G.canonical_game("SkyrimSpecialEdition") == "SkyrimSpecialEdition")
check("'starfield' and 'Starfield' share one grouping key",
      G.game_key("starfield") == G.game_key("Starfield"),
      "%r vs %r" % (G.game_key("starfield"), G.game_key("Starfield")))
check("'fallout4' and 'Fallout4' share one grouping key",
      G.game_key("fallout4") == G.game_key("Fallout4"))
check("an empty token yields no game", G.canonical_game("") is None)

# ---------------------------------------------------------------- 11.3
print("\n 11.3  a game is in scope only if its pool folder exists")
fresh_box()
os.makedirs(os.path.join(CFG.ARCHIVE_ROOT, "Skyrim"))
os.makedirs(os.path.join(CFG.ARCHIVE_ROOT, "Fallout4"))
check("SkyrimSpecialEdition resolves to the shared Skyrim folder",
      G.pool_dir("skyrimspecialedition") ==
      os.path.join(CFG.ARCHIVE_ROOT, "Skyrim"), str(G.pool_dir("skyrimspecialedition")))
check("Skyrim LE resolves to the same folder, as configured",
      G.pool_dir("Skyrim") == G.pool_dir("SkyrimSpecialEdition"))
check("a differently-cased token still finds the folder",
      G.pool_dir("FALLOUT4") == os.path.join(CFG.ARCHIVE_ROOT, "Fallout4"),
      str(G.pool_dir("FALLOUT4")))
check("a game with no folder is out of scope",
      G.pool_dir("morrowind") is None)
pooled = G.pooled_games(["skyrimspecialedition", "fallout4", "morrowind",
                         "starfield", "Fallout4"])
check("pooled_games keeps only what is on disk",
      set(pooled) == {"skyrimspecialedition", "fallout4"}, str(sorted(pooled)))
check("  and does not list one game twice for two spellings",
      len(pooled) == 2, str(sorted(pooled)))

# ---------------------------------------------------------------- 11.4
print("\n 11.4  the cache answers when the network does not")
fresh_box()
seed_cache(G.SUMMARY_PATH, [summ_row("r/a")])
no_network()
try:
    got = G.load_summary()
    check("a fresh cache entry is served without any fetch",
          list(got) == ["r/a"], str(list(got)))
    # Force past the TTL: the fetch fails, so the stale copy must be used.
    os.utime(G.cache_file(G.SUMMARY_PATH), (0, 0))
    got = G.load_summary()
    check("a STALE entry is served rather than failing outright",
          list(got) == ["r/a"], str(list(got)))
    missing = False
    try:
        G.fetch_json("reports/nope/status.json")
    except G.GalleryError:
        missing = True
    check("with no cache and no network, the error is a GalleryError", missing)
finally:
    G.http_bytes = REAL_HTTP

check("a cache entry is gzipped on disk",
      G._read_cache(G.SUMMARY_PATH) is not None
      and gzip.decompress(io.open(G.cache_file(G.SUMMARY_PATH), "rb").read()))
check("a truncated cache entry reads as absent, not as short JSON",
      (io.open(G.cache_file(G.SUMMARY_PATH), "wb").write(b"not gzip") or True)
      and G._read_cache(G.SUMMARY_PATH) is None)

# ---------------------------------------------------------------- 11.5
print("\n 11.5  a report's archives arrive in the manifest's own shape")
r = rep("x", "1.0", [("a.7z", 100, "nexus", "Valid"),
                     ("b.7z", 200, "http", "InValid"),
                     ("c.esm", 300, "gamefile", "Valid")])
arch = G.report_archives(r, WJ)
check("every archive is unwrapped from Original", len(arch) == 3, str(len(arch)))
check("names survive", [a["name"] for a in arch] == ["a.7z", "b.7z", "c.esm"])
check("sizes survive", [a["size"] for a in arch] == [100, 200, 300])
check("the downloader's own state_type classifies the source",
      [a["kind"] for a in arch] == ["nexus", "http", "gamefile"],
      str([a["kind"] for a in arch]))
check("per-archive validation status is carried through",
      [a["status"] for a in arch] == ["Valid", "InValid", "Valid"])
check("the shape is what present_match() consumes",
      all("name" in a and "size" in a for a in arch))
check("an entry with no Original is skipped, not crashed on",
      len(G.report_archives({"Archives": [{"Status": "Valid"}]}, WJ)) == 0)

# ---------------------------------------------------------------- 11.6
print("\n 11.6  ForcedDown reports carry no archives; zero is not 'none work'")
check("Available is reported as such",
      G.report_status(rep("x", "1", [])) == "Available")
check("ForcedDown is reported as such",
      G.report_status(rep("x", "1", [], status="ForcedDown")) == "ForcedDown")
check("an empty Archives[] is detected",
      G.report_is_empty(rep("x", "1", [], status="ForcedDown")))
check("a populated Archives[] is not",
      not G.report_is_empty(rep("x", "1", [("a.7z", 1, "nexus", "Valid")])))
check("a missing report is 'Unknown', never 'Failed'",
      G.report_status(None) == "Unknown")

# ---------------------------------------------------------------- 11.7
print("\n 11.7  the gap: what you hold, what you need, what is unobtainable")
fresh_box()
base = os.path.join(CFG.ARCHIVE_ROOT, "Fallout4")
for folder, name, size in (("Master", "have-master.7z", 100),
                           ("Shared", "have-shared.7z", 200),
                           ("SomeList", "have-list.7z", 300),
                           ("Master", "dead-but-mine.7z", 400),
                           ("Master", "wrongsize.7z", 999)):
    d = os.path.join(base, folder)
    os.makedirs(d, exist_ok=True)
    io.open(os.path.join(d, name), "wb").write(b"x" * size)
WJ.invalidate_existing_cache()
existing = WJ.index_existing(base)

r = rep("x", "1.0", [
    ("have-master.7z", 100, "nexus", "Valid"),
    ("have-shared.7z", 200, "nexus", "Valid"),
    ("have-list.7z", 300, "nexus", "Valid"),
    ("dead-but-mine.7z", 400, "http", "InValid"),
    ("dead-and-gone.7z", 500, "http", "InValid"),
    ("just-missing.7z", 600, "nexus", "Valid"),
    ("wrongsize.7z", 700, "nexus", "Valid"),
    ("from-the-game.esm", 800, "gamefile", "Valid"),
])
st = G.evaluate(G.report_archives(r, WJ), existing, WJ)
check("game files are counted apart and not called missing",
      st["gamefile"] == 1 and st["total"] == 7, str(st["gamefile"]))
check("a copy in Master counts as held", st["present"] == 4, str(st["present"]))
check("  Shared and a sibling list folder count too, so present is 4 not 1",
      st["present"] == 4)
check("a wrong-size copy is NOT counted as held",
      st["missing"] == 3, str(st["missing"]))
check("held bytes exclude the game file",
      st["present_bytes"] == 100 + 200 + 300 + 400, str(st["present_bytes"]))
check("missing bytes are what a download would actually cost",
      st["missing_bytes"] == 500 + 600 + 700, str(st["missing_bytes"]))
check("percent is over the downloadable archives only",
      abs(st["percent"] - 100.0 * 4 / 7) < 0.01, str(st["percent"]))
check("a dead archive you HOLD is reported separately",
      [a["name"] for a in st["dead_have"]] == ["dead-but-mine.7z"],
      str([a["name"] for a in st["dead_have"]]))
check("a dead archive you LACK is the one that blocks an install",
      [a["name"] for a in st["dead_need"]] == ["dead-and-gone.7z"],
      str([a["name"] for a in st["dead_need"]]))
check("an empty list divides by zero nowhere",
      G.evaluate([], existing, WJ)["percent"] == 0.0)

# ---------------------------------------------------------------- 11.8
print("\n 11.8  'Unavailable' reproduces Wabbajack's own rule")
base_row = {"failed": 0, "modlist_missing": False, "force_down": False}


def row(**kw):
    d = dict(base_row)
    d.update(kw)
    return d


check("a clean list is available", not G.unavailable(row()))
check("a failing archive makes it Unavailable", G.unavailable(row(failed=1)))
check("a missing modlist makes it Unavailable",
      G.unavailable(row(modlist_missing=True)))
check("the author's force_down makes it Unavailable, with 0 failures",
      G.unavailable(row(force_down=True)))
check("force_down with no failures still explains itself",
      G.why_unavailable(row(force_down=True)) ==
      ["flagged force_down by its author"],
      str(G.why_unavailable(row(force_down=True))))
check("both causes are reported, not just the first",
      len(G.why_unavailable(row(failed=2, force_down=True))) == 2)
check("a clean list has nothing to explain",
      G.why_unavailable(row()) == [])

# ---------------------------------------------------------------- 11.9
print("\n 11.9  building the browsable rows")
fresh_box()
os.makedirs(os.path.join(CFG.ARCHIVE_ROOT, "Fallout4"))
feeds = {
    "r1/alpha": feed_entry("r1", "alpha", "fallout4", "2.0"),
    "r1/beta": feed_entry("r1", "beta", "Fallout4", "3.0", force_down=True),
    "r2/gamma": feed_entry("r2", "gamma", "morrowind", "1.0"),
    "r3/revived": feed_entry("r3", "revived", "fallout4", "9.0"),
}
summary_rows = {"r1/alpha": summ_row("r1/alpha", passed=10, failed=2),
                "r1/beta": summ_row("r1/beta"),
                "r3/revived": summ_row("r3/revived")}
featured = {"r1/alpha"}
disabled = {
    "retired/oldie": {"title": "Oldie", "game": "fallout4", "version": "0.1",
                      "links": {"machineURL": "oldie",
                                "download": "https://x/oldie"},
                      "download_metadata": {"NumberOfArchives": 5,
                                            "SizeOfArchives": 500,
                                            "Size": 50, "Hash": "A="}},
    # Same machineURL as a live feed entry: this one came back.
    "retired/revived": {"title": "Revived", "game": "fallout4",
                        "version": "0.2",
                        "links": {"machineURL": "revived",
                                  "download": "https://x/revived"},
                        "download_metadata": {}},
}
rows = G.build_rows(feeds, summary_rows, featured, disabled,
                    have_keys={"r1/beta"}, games={"fallout4"})
byk = {r["key"]: r for r in rows}
check("out-of-scope games are dropped",
      "r2/gamma" not in byk, str(sorted(byk)))
check("both case spellings of the game are kept in scope",
      {"r1/alpha", "r1/beta"} <= set(byk), str(sorted(byk)))
check("a retired list is included as its own row", "retired/oldie" in byk)
check("a retired entry whose list came back is NOT shown twice",
      "retired/revived" not in byk, str(sorted(byk)))
check("  while the live version of it stays", "r3/revived" in byk)
check("the have flag comes from what is tracked",
      byk["r1/beta"]["have"] and not byk["r1/alpha"]["have"])
check("featured membership is carried",
      byk["r1/alpha"]["featured"] and not byk["r1/beta"]["featured"])
check("force_down survives normalisation into the row",
      byk["r1/beta"]["force_down"] is True)
check("failures come from the summary, not the feed",
      byk["r1/alpha"]["failed"] == 2)
check("a retired row still carries a download link, so it can be fetched",
      byk["retired/oldie"]["entry"]["download"] == "https://x/oldie")
check("  but no repository, because there is no feed behind it",
      byk["retired/oldie"]["entry"]["repo"] == "")
check("a list with no summary row reads as 0 failures, not as broken",
      not G.unavailable(byk["retired/oldie"]))

# ---------------------------------------------------------------- 11.10
print("\n 11.10  syncing reports counts honestly and survives a bad one")
fresh_box()
keys = ["a/one", "a/two", "b/three"]
seed_cache(G.report_rel("a/one"), rep("one", "1", []))
calls = []


def half_broken(rel):
    calls.append(rel)
    if "three" in rel:
        raise OSError("404")
    return json.dumps(rep("two", "1", [])).encode()


G.http_bytes = half_broken
try:
    fetched, cached, failed = G.sync_reports(keys)
finally:
    G.http_bytes = REAL_HTTP
check("an already-cached report is not re-fetched",
      cached == 1, "cached=%s" % cached)
check("  and is not among the requests made",
      not any("one" in c for c in calls), str(calls))
check("a fetchable report is counted as fetched", fetched == 1, str(fetched))
check("a 404 is collected, not raised", failed == ["b/three"], str(failed))
check("the good report landed in the cache", G.have_report("a/two"))
check("the failed one did not", not G.have_report("b/three"))

fresh_box()
no_network()
try:
    f, c, bad = G.sync_reports([])
    check("syncing nothing does nothing and reports nothing",
          (f, c, bad) == (0, 0, []), str((f, c, bad)))
finally:
    G.http_bytes = REAL_HTTP

# ---------------------------------------------------------------- 11.11
print("\n 11.11  the feed snapshot is reused, and only when it is complete")
A.reset_feed_cache()
A._FEED_CACHE["all"] = {"r1/a": feed_entry("r1", "a", "fallout4"),
                        "r2/b": feed_entry("r2", "b", "fallout4")}
A._FEED_CACHE["complete"] = True
got, complete = A.load_feeds()
check("a cached snapshot serves the all-feeds request",
      set(got) == {"r1/a", "r2/b"} and complete, str(sorted(got)))
got, complete = A.load_feeds({"r2"})
check("  and serves a subset request from the same snapshot",
      set(got) == {"r2/b"}, str(sorted(got)))
A.reset_feed_cache()
check("reset_feed_cache clears it", A._FEED_CACHE["all"] is None)

shutil.rmtree(BOX, ignore_errors=True)
sys.exit(summary())
