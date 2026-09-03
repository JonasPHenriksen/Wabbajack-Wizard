# Wabbajack modlist tooling

Tracks published Wabbajack modlists, fetches their manifests and archives, and
organises the archives into per-game pools so Wabbajack can install any one of
them without re-downloading what is already on disk.

Runs on Windows against the mapped share.

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

Skyrim, Skyrim Special Edition, Skyrim VR and Enderal Special Edition lists all
draw from the one `Skyrim` folder: VR lists are built almost entirely from
Special Edition archives, and Enderal is a total conversion on the same engine
whose lists are three quarters Skyrim SE Nexus files. `GAME_FOLDERS` in
`wj_config.py` is where that is said, and every list drawing from a shared
folder also protects it. Folding an existing folder into another is
`wj_merge_pool.py`'s job. It was run once, on 2026-09-03, to fold the Enderal
folder in: 332 of its 938 archives were the same name, size and xxHash64 as a
copy the Skyrim pool already held, and were dropped for 30.9 GB; none differed
in content; the two list folders moved across as renames, and the record is in
`merged_EnderalSpecialEdition_into_Skyrim_<stamp>.csv`.

A folder is only visible to the tooling if a manifest of the same name exists in
`modlists/`. `modlists/Foo.wabbajack` ↔ `Mod Archive/<Game>/Foo/`. Delete the
manifest and the folder goes dark, so deletion is handled explicitly (stage 5).

Manifest filenames follow `<repo>_@@_<machineURL>.wabbajack`, which *is* the
published feed key with `/` written as `_@@_`. That is how a local file is
matched to its published list — exactly, with no guessing from titles.

## Quick start

```
setup_env.bat        once, creates .venv-win and installs xxhash
run.bat              the interactive manager
```

## The interactive manager

`run.bat` with no arguments walks five stages. Each shows what it can currently
see, then offers **skip**, **all**, or **some** (`1,3,5-7`). Stages are
independent — skipping one does not affect the others.

1. **Browse** — every modlist published for a game you keep archives for, and
   how much of each one is already in your pools, sorted most-complete first.
   Picking one shows the gap in full and offers to fetch its manifest and start
   tracking it (see below).
2. **Updates** — which tracked modlists have a newer published version, and
   which are already current. Fetches the manifests you pick.
3. **Incomplete** — which modlists are missing archives, and how many of those
   need a Nexus click. Downloads one list at a time, finishing each before
   offering the next.
4. **Prepare** — gathers everything one modlist needs into its own folder for
   Wabbajack, then offers to hand it straight over (see below). One at a time by
   design: staging a second list pulls the shared archives back out of the first.
5. **Space** — what the manifests and the per-game Master pools are costing,
   with the option to delete modlists, park folders that are not ours, and then
   clear Master under a protection rule you choose. Every step confirms, and
   lets you change the selection at the confirmation.

## Stage 1: what you already own of what you do not have

Wabbajack's gallery is not an API — it is a set of JSON files in the
`wabbajack-tools/mod-lists` repository. Among them is one report per published
list, at `reports/<repo>/<machineURL>/status.json`, holding **every archive** of
that list with its `Name`, `Size`, `Hash`, `Meta` and `State`: the same shape as
the `Archives[]` inside the `.wabbajack` itself.

That is worth a lot when archives are pooled. A `.wabbajack` runs from 100 MB to
5 GB; its report is a median 728 KB, and about 140 KB gzipped. So the question
"how much of this list do I already have?" gets answered for the price of a few
hundred kilobytes, before committing to the big download rather than after.

The answer is rarely small. Measured against this share's Skyrim pool:

| a list not tracked here | archives | already owned | still to fetch |
| --- | --- | --- | --- |
| Tempus Maledictum | 1953 | **99.4%** | 4.3 GB of 143.7 GB |
| Keizaal | 876 | 98.3% | 2.5 GB of 20.0 GB |
| NGVO | 1573 | 87.7% | 25.8 GB of 122.1 GB |
| WakingDreams/TwistedSkyrim | 6031 | 39.4% | 367 GB of 539 GB |

Only games with a pool folder under the archive root are shown, since nothing
else can overlap what you own. The whole sweep is 224 reports, about 45 MB, and
takes roughly nine seconds; it is cached under `.gallery-cache/` and served from
there afterwards, including when GitHub is unreachable.

### "Unavailable" does not mean uninstallable

Wabbajack tags a gallery entry **Unavailable** when either of two unrelated
things is true, which its own view model computes as:

```csharp
IsBroken = (Summary?.HasFailures ?? false) || metadata.ForceDown;
```

- `HasFailures` — `Failed > 0 || ModListIsMissing`. The validator could not
  download some archives *from their source*.
- `ForceDown` — a flag the author sets by hand. Of the 33 lists carrying it, 25
  have zero failing archives.

That state disables the Install button on the gallery tile and does nothing
else. The `.wabbajack` stays on the CDN, and `Wabbajack.CLI`'s `install` verb —
the one this tooling drives — never reads either value. More to the point, a
failing archive only matters if you do not already have it, which for a pooled
collection is often not the case.

So stage 1 shows these lists, marks them `!`, and names the dead archives split
into the ones already in your pools and the ones that are not. Keizaal, for
instance, is Unavailable over a single dead `xLODGen.106.7z` — which is already
in Master, so nothing about it blocks the install.

Five lists carry `Status: ForcedDown` in their report, and those reports have an
**empty** `Archives[]` because the validator does not walk a list its author has
pulled. Their upstream `passed=0 failed=0` means *not checked*, not *none of it
works*, so they are shown with a `-` for overlap rather than as 0%.

### There is no hidden pool of modlists

The gallery aggregates every repository in `repositories.json`, not just the
official ones, so the 224 lists it shows are already everything. What sits off
to the side is small: `unlisted_modlists.json` has 1 entry,
`utility_modlists.json` has 0, `invalid_repositories.json` names 2 rejected
repos, and `disabled_mod_lists.json` holds 7 retired lists whose download URLs
still work. Two of those seven — `living_skyrim` and `elysium` — are published
again today, so they are filtered out rather than listed twice.

Retired lists appear marked `>`. They can be fetched and installed, but there is
no feed behind them, so their version can never be tracked.

### What protects an archive from deletion

Stage 5c clears Master, and offers two answers to "what counts as still
wanted?":

| rule | protected by | on this share |
| --- | --- | --- |
| `[y]` your modlists only | the 22 manifests held here | frees ~717 GB |
| `[a]` any known modlist | those **plus all ~220 published lists** | frees ~386 GB, keeps **330 GB** |

The second is the useful one for a pooled collection. Master is *defined* as
"needed by no list you have", but a lot of what lands there is still wanted by
lists in the gallery — 330 GB of it here, measured on 2026-09-03. Under `[y]` that gets deleted and
re-downloaded the day you install one of them; under `[a]` only genuine dead
weight goes: superseded builds from past list updates, and archives for lists
that no longer exist.

`[a]` **refuses rather than guessing**. If any author feed did not respond, or
any published list has no cached validation report, the protected set is short —
and a short protected set does not mean deleting a bit less, it means deleting
the wrong thing. So it declines and tells you to fetch the reports. The handful
of lists whose authors have pulled them publish an empty report permanently, or
drop out of the validator's summary altogether; those cannot block the feature,
so they are named in the confirmation instead.

Name **and** size must match, every file is re-checked at the moment it goes
rather than trusted from the table, and every file is recorded in
`deleted_<game>_<timestamp>.csv`, one row as it goes, with the reason, where it
went, the folder and the rule used.

**Nothing is deleted outright.** What stage 5c reclaims moves into that game's
`Trash` folder, stamped with the moment it arrived, and is purged
`TRASH_KEEP_DAYS` days later (30) at the start of the next run, with a
`purged_trash_<timestamp>.csv` record. Until then a file can be moved back into
Master by hand and the next rebuild sorts it home. Stage 5d shows what the
trash holds and when each game's oldest file falls due, and offers to empty it
at once. The trash is invisible to every tool: never indexed, never counted as
present, never moved, and a modlist may not be called `Trash`.

One rule reclaims *more*: a Master copy of a build that also exists in Shared,
in a modlist's folder, or under a better name inside Master itself is a
**duplicate**, and goes under either protection rule. The sorter creates these
on purpose, parking a redundant copy rather than overwriting, so a pool that
has been through a few rebuilds carries a fair number of them. Before a
duplicate goes, both files are read in full and their xxHash64 compared; a pair
with the same name and size but different bytes is kept, since that is exactly
the case a size-based identity cannot see. A copy in a stray, excluded or trash
folder never counts, and the record names which copy made it redundant. Six
further rules, each added after a sandbox run showed the deletion path
removing something it should not have:

- **A name is read through its `~N` suffix.** When two builds share a name and
  both must survive in one folder, the second is stored as `Name~1.ext`. No
  manifest asks for that name, so before this a wanted build parked that way was
  invisible to demand: never staged, never counted as present, and reclaimable.
  Now `Synthesis~1.zip` at a size some list wants *is* that list's
  `Synthesis.zip` — protected here, counted as present by the downloader, and
  given its real name back by the sorter when it is staged. And when a deleted
  list's build collides with a different build already in Master, the one a
  surviving list wants keeps the name; the other gets the suffix.
- **Demand is counted per pool folder.** Skyrim, Skyrim Special Edition and
  Skyrim VR share one folder here, so all three games' lists protect it, and the
  space table shows that Master once, under every name that draws from it. Counting per GameType would have cleared
  the shared Master "for Special Edition" and taken what the LE lists needed.
- **A manifest named after a pool folder is never deleted.** Its "folder" *is*
  the pool. Deleting `Master.wabbajack` emptied Master in the sandbox, every
  file read as a redundant copy of itself. The sorter always refused such
  names; the wizard and the downloader now do too.
- **A failed rebuild disarms the step.** If the sorter's cleanup pass after a
  deletion exits non-zero, that game's Master may still hold what other lists
  need, and it is not offered for clearing.
- **Untrustworthy demand disarms the step.** A manifest that cannot be read
  disarms every Master, since it does not even say which game it belongs to; one
  that reads fine but lists no archives disarms its own. The sorter refuses both
  as well.

### Folders that are not ours

Any subfolder of a game root gets read as a modlist's folder, which is wrong for
a hand-built install. Three real examples from this share: a `TTW` folder
holding Tale of Two Wastelands, a `ModEngine-2.1.0.0-win64` tool drop, and a
`SkyGround_Chronicles_@@_W3VO` folder whose modlist was delisted. Every file in
them reads as unwanted to a cleanup that judges by demand.

`UNMANAGED_FOLDER` (default `Unmanaged`) is the answer. Stage 5b lists any
folder that is neither a pool, nor excluded, nor named after a manifest you
hold, and offers to move it there **intact** — nothing is deleted. Both tools
then ignore it completely: never indexed, never moved, never deleted, and a
modlist may not claim the name. Stage 5 reports the store's size, because it is
usually not small: **79.7 GB** across four folders on this share.

Strays are sized with `tree_size()`, not `dir_size()`. Pool folders are flat, so
not descending is both right and faster there; a stray folder is whatever
someone put on the share. `Workbase Improved` reported **81.7 KB** from its
eight top-level files while actually holding **42.9 GB** in `PatchedBa2` and
`Repack7z`, and `TTW` reported 1.7 GB against a real 18.8 GB. Those numbers are
what the decision gets made on.

The search walks the archive root rather than the games named in the manifests.
A game holding nothing but orphaned content has no manifest by definition —
`DARK SOULS 3` and `Witcher3` here — and would otherwise never be looked at.

Until a stray is parked, it is excluded from the index and reported, rather
than counted as archives you own. That matters because the sorter only ever
scans manifest-named folders: a file counted as present in a stray folder would
be skipped by the download and then never staged, leaving Wabbajack short of a
file the tooling had just called downloaded.

### How stage 5 avoids deleting something you need

Deleting a modlist moves its archives into that game's `Master` rather than
removing them, because some are very likely needed by lists you are keeping.
The pools are then rebuilt, which pulls anything still wanted back out of
Master. Only after that is Master offered for clearing, and every candidate is
re-checked against the surviving lists' demands before it is removed. A file any
remaining modlist needs is never deleted, whatever you select. Every deletion is
recorded in `deleted_<game>_<timestamp>.csv`.

That rebuild re-sorts the whole game folder, which un-stages whatever stage 4
arranged there: the shared archives go back to Shared. The confirmation says so
by name, and stage 4 has to be run again before installing that list.

Nothing that moves archives starts while a Wabbajack process is running, in
case it is reading them: stages 4 and 5 check with `tasklist` and wait for you
to close it. A scheduled run skips its sort instead and says so.

### What a run reads, and what it does not

The share is where the time goes, so a run rereads only what can have changed.
The manifests are read once per change: `wj_manifest_cache.json` holds each
list's archive names, sizes and source kinds, keyed by the manifest's mtime and
size, and the wizard, the updater *and* the sorter all plan from it, so a sort
no longer opens 22 multi-gigabyte zips to learn what it already knew. The
gallery's index files are cached for six hours and its reports for fourteen
days. Every pool scan reads sizes from the directory listing itself rather than
statting each file, which over SMB is one round trip per folder instead of one
per archive.

The pool scan itself is rebuilt on every run and is not cached between runs,
on purpose. The share can change outside this tooling — a hand-copied file, a
Wabbajack install writing into a downloads folder — and a stale picture of what
is on disk is exactly what would make a stage say "present" about something
that is not there, or move a file it should have left alone. That scan is now
cheap enough that its freshness costs seconds, not minutes. The two reads that
cannot be avoided are a downloaded archive being hashed after it lands, and a
duplicate being hashed before it goes.

## Scripted use

Any action flag skips the wizard:

```
run.bat --gallery                     summarise the published gallery
run.bat --gallery --sync-reports      refresh the validation-report cache
run.bat --check                       report what has updates, change nothing
run.bat --only <list>                 do the archive work for one list only
run.bat --dry-run                     plan everything, change nothing
run.bat --rediscover                  re-match local manifests against the feeds
run.bat --nexus-mode skip             leave Nexus files to Wabbajack
run.bat --selftest                    check upstream formats still read as expected
run.bat --order largest               fetch the big files first for one run
```

Exit codes: `0` clean, `1` finished with problems, `2` could not start,
`3` another run holds the lock, `4` unhandled error.

## Configuration

At the top of `wj_autoupdate.py`:

| setting | meaning |
| --- | --- |
| `NEXUS_MODE` | `assist` opens each Nexus page for a click; `skip` leaves them to Wabbajack |
| `STAGE_LIST` | which list a non-interactive run stages; `--only` overrides it |
| `EXCLUDE` | lists to ignore, by feed key or filename stem |
| `REQUIRE_HASH` | refuse to run rather than silently degrade to size-only checks |
| `KEEP_BACKUP_DAYS` | how long superseded manifests are kept |
| `DOWNLOAD_ORDER` | `smallest` (default), `largest`, or `manifest` — see below |

The archive layout lives in **`wj_config.py`** — `ARCHIVE_ROOT`,
`GAME_FOLDERS`, `GAME_DISPLAY`, `POOL_FOLDERS`, `EXCLUDE_POOL_DIRS`,
`UNMANAGED_FOLDER`, `TRASH_FOLDER` and `TRASH_KEEP_DAYS` — along with
`WABBAJACK_DIR` (where Wabbajack is; `None` auto-detects) and `INSTALL_MODE`
(`cli`, `gui`, or `off`; see below). Both the downloader and the sorter import
it, so there is one place to edit. A start-up
check still compares the two, because the downloader decides where an archive is
written and the sorter decides where it is moved to: a local override re-added
to one of them would send every run across two directory trees while both
reported success.

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
every page on "mod could not be found". The Enderal lists are the extreme case:
their files are tagged `enderalse` and `skyrimse`, with a few on the Skyrim LE
and Enderal LE Nexus, and the API path tries each slug's real domain first
(`enderalspecialedition`, `skyrimspecialedition`), then the parent game's.

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

## Handing a list to Wabbajack

Once stage 4 has staged a list, everything Wabbajack needs is in one folder and
the only real decision left is where to install. Stage 4 therefore offers to
hand it over, with `INSTALL_MODE`:

- **`cli`** (default) — asks where to install, then runs
  `wabbajack-cli install --wabbajack … --downloads … --output …`. Both paths are
  ones this tooling already knows, so nothing is typed twice. The install
  location is remembered per list.
- **`gui`** — opens the Wabbajack window and prints the two paths to paste.
- **`off`** — never offers.

### The preflight is the useful part

Wabbajack cannot install a list for a game it cannot find: a modlist's
`GameFile` archives are *copied out of the base game install*, not downloaded.
Finding that out after committing to an install wastes an evening, so it is
checked first, using `wabbajack-cli list-games`:

```
    Game        Skyrim Special Edition  [BLOCKED]
                Wabbajack cannot find an installation of Skyrim Special
                Edition. A modlist copies files out of the base game, so
                installing without it will fail.

  Not offering to install: the check above has to pass first.
```

Three outcomes are kept distinct, because they need different responses: the
game is installed; the game is missing; or Wabbajack does not support that game
at all (Dark Souls 3, for one, is not in its list). A missing CLI reports
"unknown" rather than guessing either way.

It also warns when archives are still outstanding — Wabbajack will try to fetch
those itself, which on a free account means its own click-per-file flow rather
than this tooling's.

## Download order

Archives are fetched **smallest first** by default. On a free Nexus account,
where every file costs a click, that means the count of finished files climbs
quickly, an interruption costs the least, and the long tail of small mods is out
of the way before a multi-gigabyte download monopolises the session. In practice
that runs from a few hundred bytes up to a few gigabytes.

`DOWNLOAD_ORDER` in `wabbajack_archive_downloader.py` (or `--order` for one run)
takes `smallest`, `largest`, or `manifest`. Archives whose manifest states no
size always go last, whichever order is chosen: there is nowhere sensible to put
them. `nexus_todo_<list>.csv` follows the same order, so it can be worked through
top to bottom alongside a run.

## When results look wrong

```
run.bat --selftest
```

Everything this tooling reads is Wabbajack's own undocumented internals: the
shape of `repositories.json`, of a feed entry, of the JSON inside a
`.wabbajack`, of a CDN definition, and the set of downloader type names. None of
it is a published contract, so any of it can change without notice.

`--selftest` checks thirteen of those assumptions against live data and says
which, if any, now read differently. The point is not to prevent upstream
change but to make it announce itself: without it, a feed that stops carrying
`machineURL` shows up as "Following 0 list(s)", and a renamed downloader shows
up as files quietly reclassified as unsupported — both of which look like your
problem rather than an upstream one.

It found `MegaDownloader` on its first run, which is why `mega` is now a
recognised (if unfetchable) source rather than "unknown".

## Files

| file | role |
| --- | --- |
| `wj_config.py` | the archive layout, and the naming rules the rest share: reserved names, the `~N` suffix, which GameTypes share a folder |
| `wj_wabbajack.py` | finding Wabbajack, the preflight, and the hand-over |
| `wj_autoupdate.py` | orchestrator: feeds, tracking, state, manifests |
| `wj_gallery.py` | the published gallery and its validation reports: what exists, and how much of it you hold |
| `wj_wizard.py` | the interactive five-stage front end |
| `wabbajack_archive_downloader.py` | fetches archives; also the shared library for manifest parsing, hashing and the on-disk index |
| `wabbajack_archive_sorter_v2.py` | moves archives between pools |
| `wabbajack_cdn.py` | the Wabbajack CDN's multi-part protocol |
| `wj_merge_pool.py` | folds one game's pool folder into another's, once: renames, hash-proven drops, a record |

## Tests

```
.venv-win\Scripts\python.exe tests\run_all.py
```

654 checks across fourteen suites. None of them touch the live archive pools:
`ARCHIVE_ROOT` is `"../Mod Archive"` resolved against the *script* folder, so
each suite copies the scripts into a temporary directory where they operate on
a temporary pool, with no changes to the code under test. The download suites
serve their own localhost HTTP server, so they need no internet and can force
exactly the failure they want to check.

| suite | covers |
| --- | --- |
| `t1_sorter` | aborting on an unreadable manifest, reserved names, demand-based pooling, staging, size-checked collisions, dry-run purity, bad arguments, identical extras settling in one pass, the shared manifest cache |
| `t2_download` | hard vs transient HTTP failures, retry and resume, truncated transfers, verification, `.meta` sidecars, URL encoding |
| `t3_cdn` | broken definitions in four shapes, bad part bytes and lengths, whole-file hash mismatch, resume from a partial download, stale and impossible sidecars |
| `t4_updater` | filename-to-feed-key matching, negative-cache expiry, damaged state files, odd feed shapes, the start-up guards |
| `t5_wizard` | empty data in every stage, missing folders, deleting a modlist whose pieces are already gone, awkward filenames |
| `t6_planning` | present vs stale vs absent decisions, `--force`, foreign-installer exclusion, manifest cache invalidation, backup pruning |
| `t7_identity` | one filename, several builds: the wrong copy must never satisfy a list that wants a different one |
| `t8_order` | download order — smallest, largest, manifest — and where unknown sizes land |
| `t9_wabbajack` | finding the newest CLI, the game preflight, path overlap, the install command |
| `t10_lookup` | every location an archive may sit in and still count as already downloaded |
| `t11_gallery` | the gallery's hand-edited JSON, case-inconsistent game tokens, cache fallback when the network is down, the gap maths, and Wabbajack's own "Unavailable" rule |
| `t12_cleanup` | both protection rules measured from one scan, the merge of two bodies of demand, folders that are nobody's and how they are sized, and the refusal to delete on an incomplete picture |
| `t13_delete` | stage 5 driven end to end with scripted answers: a build another list needs surviving a deletion, `~N` copies protected and restored, reserved names refused, a failed rebuild or an untrustworthy manifest disarming the step, folder-level demand, the per-row record, the un-staging warning, hash-proven duplicates, the trash and its purge, stale CSVs, waiting for Wabbajack |
| `t14_merge` | folding one game folder into another: refusals on anything unrecognised, a dry run that changes nothing, hash-proven drops, same-size-different-content kept, suffixed collisions, the record |

Seven real bugs were found this way and are covered by regression checks: a
connection closing part-way through a download was reported as success (so the
resume logic never engaged); the sorter validated `--list` only after its
cleanup pass had already moved files; the downloader counted the game root as a
pool while the sorter did not, so a file loose there was called present and then
never staged; `normalise()` dropped `force_down`, which silently disarmed half
of the Unavailable check; deleting a modlist renamed a build another list still
needed to `Name~1.ext`, after which the rebuild handed that list the junk build
and stage 5c deleted the good one; deleting a manifest named `Master` emptied
the Master pool; and a list adopted and fetched in one run read as entirely
missing, because the set of legitimate list folders was snapshotted at start-up.
A CDN archive was also accepted on the CDN's own hash alone; it is now refused
before download when that differs from the manifest's.

## Requirements

Python 3.8+ and `xxhash`. Without `xxhash` only sizes can be compared, so the
tooling refuses to run rather than quietly weakening verification; pass
`--allow-size-only` to override.

## Notes for this share

The share is an SMB mount also reached from a Mac, so AppleDouble files (`._*`)
and `.DS_Store` are ignored everywhere. `truncate()` on it is a synchronous
zero-fill at roughly 17 MB/s, which is why the CDN downloader never pre-sizes
its `.part` file.
