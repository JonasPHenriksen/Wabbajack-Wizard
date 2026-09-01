# Wabbajack modlist tooling

Tracks published Wabbajack modlists, fetches their manifests and archives, and
organises the archives into per-game pools so Wabbajack can install any one of
them without re-downloading what is already on disk.

Runs on Windows against the mapped share, or headless on Linux against a local
path.

## The layout everything serves

One tree per game, under `ARCHIVE_ROOT`:

```
Mod Archive/Skyrim/
├── Master/                       archives no present modlist needs
├── Shared/                       archives two or more present modlists need
├── LoreRim_@@_LoreRim/           archives exactly one modlist needs
├── ...one folder per manifest...
└── Nolvus/                       excluded: different installer, left alone
```

A folder is only visible to the tooling if a manifest of the same name exists in
`modlists/`. `modlists/Foo.wabbajack` ↔ `Mod Archive/<Game>/Foo/`. Delete the
manifest and the folder goes dark, so deletion is handled explicitly (stage 4).

Manifest filenames follow `<repo>_@@_<machineURL>.wabbajack`, which *is* the
published feed key with `/` written as `_@@_`. That is how a local file is
matched to its published list — exactly, with no guessing from titles.

## Quick start

```
setup_env.bat        once, creates .venv-win and installs xxhash
run.bat              the interactive manager
```

## The interactive manager

`run.bat` with no arguments walks four stages. Each shows what it can currently
see, then offers **skip**, **all**, or **some** (`1,3,5-7`). Stages are
independent — skipping one does not affect the others.

1. **Updates** — which tracked modlists have a newer published version, and
   which are already current. Fetches the manifests you pick.
2. **Incomplete** — which modlists are missing archives, and how many of those
   need a Nexus click. Downloads one list at a time, finishing each before
   offering the next.
3. **Prepare** — gathers everything one modlist needs into its own folder for
   Wabbajack. One at a time by design: staging a second list pulls the shared
   archives back out of the first.
4. **Space** — what the manifests and per-game Master pools cost, with the
   option to delete modlists and then clear Master. Both steps confirm, and let
   you change the selection at the confirmation.

### How stage 4 avoids deleting something you need

Deleting a modlist moves its archives into that game's `Master` rather than
removing them, because some are very likely needed by lists you are keeping.
The pools are then rebuilt, which pulls anything still wanted back out of
Master. Only after that is Master offered for clearing, and every candidate is
re-checked against the surviving lists' demands before it is removed. A file any
remaining modlist needs is never deleted, whatever you select. Every deletion is
recorded in `deleted_<game>_<timestamp>.csv`.

## Scripted use

Any action flag skips the wizard:

```
run.bat --check                       report what has updates, change nothing
run.bat --only <list>                 do the archive work for one list only
run.bat --dry-run                     plan everything, change nothing
run.bat --rediscover                  re-match local manifests against the feeds
run.bat --nexus-mode skip             leave Nexus files to Wabbajack
```

Exit codes: `0` clean, `1` finished with problems, `2` could not start,
`3` another run holds the lock, `4` unhandled error.

## Configuration

At the top of `wj_autoupdate.py`:

| setting | meaning |
| --- | --- |
| `NEXUS_MODE` | `assist` opens each Nexus page for a click; `skip` leaves them to Wabbajack (the only option off Windows) |
| `STAGE_LIST` | which list a non-interactive run stages; `--only` overrides it |
| `EXCLUDE` | lists to ignore, by feed key or filename stem |
| `REQUIRE_HASH` | refuse to run rather than silently degrade to size-only checks |
| `KEEP_BACKUP_DAYS` | how long superseded manifests are kept |

`ARCHIVE_ROOT`, `GAME_FOLDERS` and `EXCLUDE_POOL_DIRS` live in
`wabbajack_archive_downloader.py` **and** `wabbajack_archive_sorter_v2.py` and
must match. A run refuses to start if they differ — the downloader decides where
an archive is written and the sorter decides where it is moved to, so a mismatch
means the two work on different trees while both report success.

## Nexus accounts

Nexus only mints automated download links for Premium accounts. On a free
account the website mints one per click, which assist mode captures: it
registers itself as the `nxm://` handler, opens each mod's download page in
turn, and waits for you to click *Mod manager download*. One tab per file, in
your normal browser. The handler is always handed back on exit, including
Ctrl+C — without that, Wabbajack's own downloads keep landing here instead.

The page URL is confirmed against the API rather than taken from the manifest.
Manifests disagree with themselves: this collection records `skyrimse` in a
file's `Meta gameName` and `SkyrimSpecialEdition` in its `State.GameName`, and
only the second is a slug the website serves. Trusting the manifest opened
every page on "mod could not be found".

### Do not try to automate the browser

Two attempts were made to keep these pages out of your way, and both are worth
recording so they are not repeated:

- Closing each tab with a Ctrl+W keystroke once its click was captured. This
  cannot work reliably: at the moment the click lands, Firefox has raised its
  own "launch application?" dialog for the `nxm://` handoff, so the focused
  window is that dialog rather than the page.
- Driving a dedicated Firefox over Marionette. This cannot work at all. An
  automated Firefox reports `navigator.webdriver`, Nexus's bot protection
  challenges it, and the challenge then fails even when solved by hand.
  Working around bot detection is not something this tool should do.

Stacked tabs are the accepted cost of using your own browser and your own
session, which is the only thing that actually works.

An API key is required either way — it is what redeems the key from each click.
Put it in `nexus_api_key.txt` (gitignored) or `NEXUS_API_KEY`.

## Files

| file | role |
| --- | --- |
| `wj_autoupdate.py` | orchestrator: feeds, tracking, state, manifests |
| `wj_wizard.py` | the interactive four-stage front end |
| `wabbajack_archive_downloader.py` | fetches archives; also the shared library for manifest parsing, hashing and the on-disk index |
| `wabbajack_archive_sorter_v2.py` | moves archives between pools |
| `wabbajack_cdn.py` | the Wabbajack CDN's multi-part protocol |

## Tests

```
.venv-win\Scripts\python.exe tests\run_all.py
```

203 checks across six suites. None of them touch the live archive pools:
`ARCHIVE_ROOT` is `"../Mod Archive"` resolved against the *script* folder, so
each suite copies the scripts into a temporary directory where they operate on
a temporary pool, with no changes to the code under test. The download suites
serve their own localhost HTTP server, so they need no internet and can force
exactly the failure they want to check.

| suite | covers |
| --- | --- |
| `t1_sorter` | aborting on an unreadable manifest, reserved names, demand-based pooling, staging, size-checked collisions, dry-run purity, bad arguments |
| `t2_download` | hard vs transient HTTP failures, retry and resume, truncated transfers, verification, `.meta` sidecars, URL encoding |
| `t3_cdn` | broken definitions in four shapes, bad part bytes and lengths, whole-file hash mismatch, resume from a partial download, stale and impossible sidecars |
| `t4_updater` | filename-to-feed-key matching, negative-cache expiry, damaged state files, odd feed shapes, the start-up guards |
| `t5_wizard` | empty data in every stage, missing folders, deleting a modlist whose pieces are already gone, awkward filenames |
| `t6_planning` | present vs stale vs absent decisions, `--force`, foreign-installer exclusion, manifest cache invalidation, backup pruning |

Two real bugs were found this way and are covered by regression checks: a
connection closing part-way through a download was reported as success (so the
resume logic never engaged), and the sorter validated `--list` only after its
cleanup pass had already moved files.

## Requirements

Python 3.8+ and `xxhash`. Without `xxhash` only sizes can be compared, so the
tooling refuses to run rather than quietly weakening verification; pass
`--allow-size-only` to override.

## Notes for this share

The share is an SMB mount also reached from a Mac, so AppleDouble files (`._*`)
and `.DS_Store` are ignored everywhere. `truncate()` on it is a synchronous
zero-fill at roughly 17 MB/s, which is why the CDN downloader never pre-sizes
its `.part` file.
