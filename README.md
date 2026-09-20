# Wabbajack Wizard

Tracks published Wabbajack modlists, fetches their manifests and archives, and
organises the archives into per-game pools so Wabbajack can install any one of
them without re-downloading what is already on disk.

Runs on Windows against the mapped share. MIT licensed; the version history
is in `CHANGELOG.md`.

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

Two ways in, and neither needs the other.

**If you have no Python.** Download `WabbajackWizard-v<version>-win64.zip`,
unzip it anywhere, and run it. Python is inside; nothing is installed, and
nothing is written outside the folder you unzipped.

```
WabbajackWizard.exe                    opens the window
WabbajackWizard-console.exe --wizard   the same five stages in the console
```

The two are one program built twice, the way `python.exe` and `pythonw.exe`
are: the first has no console, the second does. Every flag below works on the
console one. Windows will warn about an unsigned download the first time -
**More info** then **Run anyway** - because the zip is not code-signed.

**If you have Python**, run the scripts directly:

```
setup_env.bat        once; installs xxhash into .python\ when that is there, else into a new .venv-win
run.bat              opens the window
run.bat --wizard     the same five stages in the console
```

No Python on the machine, and no wish to install one? Unpack a
[python-build-standalone](https://github.com/astral-sh/python-build-standalone/releases)
Windows release (the `install_only` one, which carries tkinter) into `.python\`
inside this folder, so that `.python\python.exe` exists. `setup_env.bat` then
installs xxhash straight into it and makes no virtual environment at all, since
a venv records where its Python is and breaks when the folder moves; `run.bat`,
the tests and `build.py` all prefer that interpreter. Nothing is installed
machine-wide, nothing in the folder names an absolute path, and the whole folder
can be moved or copied as it is. The one path that does reach outside it is
`ARCHIVE_ROOT`, `../Mod Archive` by default, so a move that separates the tools
from the archives needs that set in `wj_settings.json`. The same goes for git: a
[MinGit](https://github.com/git-for-windows/git/releases) zip unpacked into
`.mingit\`, the portable
[Git Credential Manager](https://github.com/git-ecosystem/git-credential-manager/releases)
zip unpacked into its `mingw64\libexec\git-core\` (where MinGit's default
`credential.helper = manager` finds it), and a `git.bat` in this folder in
front of it, is a complete git for this checkout, GitHub sign-in included. Its
settings are `.mingit\etc\gitconfig`, which also has to mark the checkout safe
because files on a share read as foreign-owned; its token goes to Windows
Credential Manager, so the folder itself holds no secret. Both folders and the
wrapper are gitignored.

Either way, everything the tool writes - your settings, `modlists/`, the logs,
the state file - lands in the folder you started it from, so a build can be
dropped straight into an existing setup and pick up what is already there.

Settings are optional: copy `wj_settings.example.json` to `wj_settings.json`
and change what differs, or use the window's Settings tab. Without it the
defaults in `wj_config.py` apply.

## The window, and the console wizard

`run.bat` opens a window with a tab per stage and a log pane below; `run.bat
--wizard` walks the same five stages in the console, each showing what it can
see and offering **skip**, **all**, or **some** (`1,3,5-7`). The two front
ends drive the same functions in `wj_wizard.py`, so they cannot drift apart;
every job in the window runs on a worker thread with its output streaming
into the log pane, and anything destructive asks first in the same words the
console uses. Stages are independent: skipping one does not affect the others.

The window opens on a **Start** tab: an overview of your modlists, pending
updates, incomplete lists, the pools and the trash, the gallery and the
settings, each card showing what the last session measured and when. Opening
the window reads the manifests and nothing else; a tab measures when you open
it, and what it finds is written to `wj_overview.json` beside the scripts so the
next start shows it again at once. **Refresh everything** runs the three
measurements in a row. **Dark mode**, beside the progress bar, switches the
window's colours, title bar included on Windows 10 and 11, and is remembered
as `THEME`.

1. **Browse** — every modlist published for a game you keep archives for, and
   how much of each one is already in your pools, sorted most-complete first.
   By default only lists you could finish are shown (see below). Picking one
   shows the gap in full and offers to fetch its manifest and start tracking it.
   Tick **every game** (`a` in the console) to include
   games you keep no archives for at all: their lists measure against nothing,
   so the "to fetch" column is the whole download, which is the number worth
   seeing before taking a new game on. **Set up a pool** (`p N` in the console)
   then lays one out — see below. A list you will never want can be **hidden**,
   after which it shows only under **Hidden lists** in the Game menu — see below.
2. **Updates** — which tracked modlists have a newer published version, and
   which are already current. Fetches the manifests you pick.
3. **Incomplete** — which modlists are missing archives, and how many of those
   need a Nexus click. Downloads one list at a time, finishing each before
   offering the next.
4. **Prepare** — gathers everything one modlist needs into its own folder for
   Wabbajack, then offers to hand it straight over (see below), after checking
   that Wabbajack is installed, can see the game, and is the newest release. One
   at a time by design: staging a second list pulls the shared archives back out
   of the first.
5. **Space** — what the manifests and the per-game Master pools are costing,
   with the option to delete modlists, park folders that are not ours, clear
   Master under a protection rule you choose, empty the trash, and sweep the
   leftovers no other step reaches. Every step confirms, and lets you change
   the selection at the confirmation.

### When a tab rebuilds itself

Each tab records what it was built from, and rebuilds when something it reads
has moved on. Three things are tracked separately: the **manifests** held and
which published list each one is, the **archives** on the share, and the
**settings**.

| a tab reads | manifests | archives | settings |
| --- | :---: | :---: | :---: |
| Start | ● | ● | ● |
| Browse | ● | ● | |
| Updates | ● | | |
| Incomplete, Prepare | ● | ● | |
| Space | ● | ● | |
| Settings | | | ● |

Start also rebuilds whenever another tab finishes a measurement, since that is
what it reports. Every operation says which of the three it moved, so a new tab
cannot be forgotten by an older handler. Tracking them apart is what keeps the refresh
cheap: fetching a manifest changes what Updates and Browse show, but not the
archives on disk, so Browse keeps its pool index rather than paying the
28-second Skyrim scan again.

While a job runs, the other tabs are locked. Leaving mid-way landed you on a
tab rebuilt from data the job had not finished writing.

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

Only games with a pool folder under the archive root are shown by default, since
nothing else can overlap what you own. The whole sweep is 224 reports, about
45 MB, and takes roughly nine seconds; it is cached under `.gallery-cache/` and
served from there afterwards, including when GitHub is unreachable.

### Taking on a game you keep nothing for

**Every game** in the window, `a` in the console, drops that filter and shows
the whole gallery. A game with no pool is measured against an empty index, so
every list reads as 0% held and the "to fetch" column is the entire download.
That is the honest number, and it is the one worth seeing before deciding.

**Set up a pool** (`p N` in the console) then creates one folder under the
archive root holding the same `Master`, `Shared`, `Unmanaged` and `Trash`
every other game has. It is safe to run twice: an existing folder is
completed rather than duplicated, and a folder already on the share is matched
case-insensitively, so `Fallout4` is never created beside `fallout4`.

The prompt asks for the folder name. Leaving it as the game's own name gives
that game its own pool. Typing the name of a pool you already keep folds the
game into it instead, the way Skyrim VR draws from Skyrim: one `Master` between
them, and each game's lists then protect the other's archives from stage 5. That
writes a `GAME_FOLDERS` entry to `wj_settings.json`, so every later run and
every other tool in the suite agrees about it. Two cases on the current gallery
are worth folding rather than separating:

| game | fold into | why |
| --- | --- | --- |
| `Fallout4VR` | `Fallout4` | VR lists are built almost entirely from Fallout 4 archives, exactly as Skyrim VR is from Skyrim SE |
| `Fallout3` | `FalloutNewVegas` | Tale of Two Wastelands lists are New Vegas lists that pull Fallout 3 files |

A folder name that would collide with a pool folder (`Master`, `Shared`,
`Unmanaged`, `Trash`, `Nolvus`) is refused, as is one carrying a path
separator, which would put a game's pools outside the archive root.

None of this is required before tracking a list. Adopting a list for a new game
works with no pool at all, and the first sort creates the folders it needs. The
button exists so the layout is there, and named the way you meant, before any
archive lands in it.

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

### Only lists you could finish

A game's table shows, by default, only the lists that could end up installed:
every archive is either already in your pools or one the validator could still
download, or the list is one you track. **Only lists that can be completed** in
the window, `c` in the console, flips that. The rule is about the outcome, not
the label: a list flagged Unavailable over a dead archive you already hold is
shown, since nothing blocks its install; a list with a dead archive you do not
hold is not, since nobody can get it any more. Lists that cannot be measured at
all — retired, pulled by their author, or with no report cached yet — are kept
out too, and counted separately under the table as unmeasurable, because
"cannot tell" is not "cannot install".

Deciding needs the pool index, so it happens when a game is shown, and the Game
menu keeps counting every list. Toggled off, a list that cannot be completed
carries an `x`. The toggle is display only: a list kept out of the table is still
a published list, and still protects its archives in Space.

### Hiding a list you will never want

**Hide this list**, under the Browse tab's detail pane (`[h]` on a list's detail
screen in the console), takes a list out of the gallery. It then appears
nowhere but the **Hidden lists** entry at the end of the Game menu (`[h]` in the
console's games menu), which spans every game and is where the flag comes off
again. Nothing is fetched for a hidden list: its validation report is left out
of every sweep, and it is never measured against your pools, so the Hidden view
shows the feeds' own figures. It also protects nothing: under the `[a]` rule in
Space its archives are as reclaimable as if the list did not exist, and a
missing report for it does not block that rule.

A list you track cannot be hidden, and a flag on a tracked list is ignored,
because its own manifest is what protects its archives and a flag that could
quietly cancel that would be worse than none. The flags are the `HIDDEN` list in
`wj_settings.json`, editable there and in the Settings tab as well.

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
so they are named in the confirmation instead. Hidden lists are not published
lists as far as `[a]` is concerned: they protect nothing, a missing report for
one blocks nothing, and the confirmation names them. An archive only a hidden
list wants therefore goes; one that another list also wants stays, because
protection is per archive, not per list.

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

Parking is not always the right answer, so each stray says where it came from.
Wabbajack writes a `.metadata` sidecar beside every modlist it downloads, so a
stray with a sidecar but no manifest is not a hand-built install: it is a list
whose paperwork went missing. If the gallery still publishes it, stage 5b and
the Space tab say **adopt it** rather than offering to park it, because parking
would hide archives you already own from the list that wants them. Adopting
fetches the manifest and the folder is legitimate again. Whether a list is
still published is only read from feeds already loaded in that run: stage 5
never reaches for the network mid-deletion, so a cold cache reports the
sidecar and leaves the rest unknown rather than guessing.

Sidecars whose modlist is gone entirely, with no folder left either, are listed
too, and stage 5e can now delete them.

### Stage 5e: leftovers

Every other step here judges archives. Stage 5 goes by demand, the trash runs
on a thirty-day timer, backups expire on `KEEP_BACKUP_DAYS`, and the stale
`nexus_todo` CSVs go when the list they describe is complete. Nothing reaches
the debris that lands *beside* the archives, so it accumulates for good.

**Remove leftovers** in the Space tab, `5e` in the console, sweeps two kinds:

| what | why it is there |
| --- | --- |
| OS metadata | `.DS_Store`, `Thumbs.db`, `desktop.ini`, and the `._name` AppleDouble files a Mac writes beside real files on a non-HFS share |
| orphaned sidecars | Wabbajack's `.metadata` for a modlist that is no longer here |

It lists what it found and what it will free, and asks twice before deleting.

Only debris that **stays** gone is swept. `__pycache__` and the captured output
of the last child run are both rewritten by the very next run, so offering them
would put a prompt in front of you every time and reclaim nothing lasting.

Never swept, whatever a sweep might think of them: `nexus_api_key.txt`, the
settings, the state and manifest caches, the manifests themselves, the lock, a
resumable `.part`, the `.nxm_*` files a running assist session owns, anything
under `.venv-win`, `.gallery-cache`, `manifest_backups` or `dist`, and the
`deleted_*.csv`, `purged_trash_*.csv` and `merged_*.csv` records, which are the
only account of what stage 5 ever took.

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
run.bat --gui                         the window, from a console
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

Every setting has a default in `wj_config.py` and can be overridden in
**`wj_settings.json`** beside it: your file, which the GUI's Settings tab writes
and a code update never touches. Copy `wj_settings.example.json` to start; only
the keys you change need to be in it. A dict setting (`GAME_FOLDERS`,
`GAME_DISPLAY`) is merged over its default; every other setting replaces it. A
bad value is reported at start-up and its default used, never fatal.

| setting | default | meaning |
| --- | --- | --- |
| `ARCHIVE_ROOT` | `../Mod Archive` | where the per-game pools live; a relative path is resolved against the tools folder |
| `MANIFEST_DIR` | `modlists` | where the `.wabbajack` manifests live |
| `GAME_FOLDERS` | the Skyrim family, all to `Skyrim` | GameType to pool folder, for games that share one or whose folder is not named after them |
| `GAME_DISPLAY` | | friendly names for menus; an unknown game shows its GameType |
| `EXCLUDE_POOL_DIRS` | `nolvus` | folders under a game root that belong to another installer: never indexed, moved or deleted |
| `UNMANAGED_FOLDER` | `Unmanaged` | where stage 5b parks folders that are not ours |
| `TRASH_FOLDER`, `TRASH_KEEP_DAYS` | `Trash`, 30 | where stage 5c puts what it reclaims, and how long before it is purged |
| `WABBAJACK_DIR` | `null` | where Wabbajack is installed; `null` auto-detects |
| `INSTALL_MODE` | `cli` | how a staged list is handed over: `cli`, `gui`, or `off` |
| `AUTO_TRACK` | `true` | match every manifest in the folder against the feeds automatically |
| `EXCLUDE`, `TRACK` | `[]` | lists to leave alone, and pins for lists auto-matching cannot resolve |
| `HIDDEN` | `[]` | published lists hidden from the gallery, by feed key; the Browse tab's Hide button writes it |
| `STAGE_LIST` | `null` | which list a scheduled run stages; `--only` overrides it |
| `NEXUS_MODE` | `skip` | `assist` opens each Nexus page for a click; `skip` leaves them to Wabbajack |
| `KEEP_BACKUP_DAYS` | 14 | how long superseded manifests are kept |
| `REQUIRE_HASH` | `true` | refuse to run rather than silently degrade to size-only checks |
| `DOWNLOAD_ORDER` | `smallest` | `smallest`, `largest`, or `manifest`; see below |
| `FREE_SPACE_MARGIN_GB` | 20 | refuse a download batch that would leave less than this free |
| `PARALLEL_SMALL_DOWNLOADS`, `SMALL_DOWNLOAD_MB` | 1, 50 | how many files under the threshold may download at once from direct sources; 1 is one at a time. See "Several small files at once" |
| `ASSIST_CLICK_AHEAD` | `false` | assist mode opens the next Nexus page while the current file downloads |
| `ASSIST_WINDOW` | `true` | show the Nexus pages in the wizard's own window rather than your browser; see "The download window" |
| `NOTIFY_URL` | `null` | an ntfy topic or Discord webhook for a scheduled run's report |
| `THEME` | `light` | the window's colours, `light` or `dark`; the Dark mode switch writes it |

The downloader decides where an archive is written and the sorter decides where
it is moved to, so both read the layout from the same module, and a start-up
check still confirms they agree.

## Nexus accounts

Nexus only mints automated download links for Premium accounts. On a free
account the website mints one per click, which assist mode captures: it
registers itself as the `nxm://` handler, shows each mod's download page in
turn, and waits for you to click *Slow download*. The handler is always handed
back on exit, including Ctrl+C — without that, Wabbajack's own downloads keep
landing here instead.

Before a page is opened, the API is asked which game's site serves the mod,
because manifests name a game several ways and not all of them are site names.
A game other than the manifest's own is believed only when it lists the wanted
file: mod ids are per game and dense, so a Special Edition mod's id is usually
some unrelated old-Skyrim mod's as well, and a page for a stranger is what that
used to open. A mod its own game reports as hidden, removed or unpublished has
no page to click; it is recorded as not downloaded, with Nexus's word for why,
and passed over at once instead of waiting out the three minutes a click is
given. A mod that is still up but no longer offers the pinned file - the author
has updated it since the list was built - gets the page of its current file of
the same name or size instead, and the hash check after the download decides
whether those were the same bytes; when nothing matches, the file is recorded
and passed over the same way.

### The download window

The pages are shown in a window of the wizard's own, the way Wabbajack's
installer does it: one window, one page at a time, replaced for each file, with
a banner across the top saying which file this is and how many are left. It is
Edge's WebView2 engine, which Windows 10 and 11 ship, reached through the
pywebview library. The window keeps its own browser profile under `.webview\`
beside the scripts — cookies, and so the Nexus login — so you sign in to Nexus
in it once, and everything stays inside the folder. Closing the window ends
the run, and everything downloaded so far is kept. A page with nothing to
click on it - a file Nexus no longer offers - has *Skip this file* at the
banner's right, which records the file as not downloaded and goes on to the
next; left alone, the run goes on by itself after three minutes.

The click is caught inside the window. The engine announces a navigation to an
external scheme before it happens and leaves the decision to the host; the
wizard cancels the `nxm://` one and hands the link straight to the waiting run,
so no protocol handler and no "open this program?" prompt is involved. That
matters more than it sounds: Nexus fires the link from a timer after your click,
and the engine's default for a launch not directly caused by a click is to drop
it without a word. The protocol handler stays registered as the fallback.

`ASSIST_WINDOW` set to `false`, or the engine or library missing, falls back to
your normal browser, one tab per file. `wj_downloader.py --check-window` says
which it would be on this machine, without opening anything.

Two things this needs on a network share. .NET Framework refuses to load
assemblies from a mapped drive unless the host program's config allows it, so
`python.exe.config` sits beside the interpreter in `.python\` and the build
writes `WabbajackWizard.exe.config` and `WabbajackWizard-console.exe.config`
beside its binaries, each one line of substance: `loadFromRemoteSources`. And
the browser side: Firefox will not launch a protocol handler whose program is
`python.exe`, so the handler registered is the console exe whenever a build
sits beside the scripts, from source and from the build alike.

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

### Is Wabbajack itself current?

The same step asks whether the Wabbajack it found is the newest release, since
a modlist built for a newer Wabbajack can fail to install on an older one.
Wabbajack tags its releases with the bare version, `4.2.3.0`, the same string
its install uses as the versioned folder name, so the two compare directly; the
release feed is read once and cached for six hours beside the gallery reports.
The panel says which version is installed and which is newest, with the download
page when they differ, and starting an install with an older one asks first. A
lookup that fails is "cannot tell", which neither blocks an install nor blesses
one. This is the only place the suite uses Wabbajack itself; everywhere else it
only checks whether a Wabbajack process is running before moving archives.

Wabbajack is looked for in `WABBAJACK_DIR` if set, then in a `Wabbajack\`
folder inside this one, then beside this one, then in the per-user install
location. Keeping it inside is the arrangement that moves as one piece, and the
folder is gitignored. Wabbajack updates itself: run `Wabbajack.exe` and its
launcher downloads the new version into a subfolder of its own, leaving the old
one where it was at some 700 MB. Whenever this tooling next looks at Wabbajack
it removes those superseded version folders, and says so in the log. Only
folders that are version-named and hold the program go, only ones older than
the newest, only once the newest is whole, and never while a Wabbajack process
is running, since the launcher is one and the new version may still be arriving.

It also warns when archives are still outstanding — Wabbajack will try to fetch
those itself, which on a free account means its own click-per-file flow rather
than this tooling's.

## Download order

Archives are fetched **smallest first** by default. On a free Nexus account,
where every file costs a click, that means the count of finished files climbs
quickly, an interruption costs the least, and the long tail of small mods is out
of the way before a multi-gigabyte download monopolises the session. In practice
that runs from a few hundred bytes up to a few gigabytes.

`DOWNLOAD_ORDER` in `wj_settings.json` (or `--order` for one run)
takes `smallest`, `largest`, or `manifest`. Archives whose manifest states no
size always go last, whichever order is chosen: there is nowhere sensible to put
them. `nexus_todo_<list>.csv` follows the same order, so it can be worked through
top to bottom alongside a run.

## Several small files at once

Downloads run one file at a time, and that is the default for a reason: a
stalled connection competes with nothing, costs at most its two-minute timeout,
and resumes from its partial file. What one-at-a-time wastes is the fixed cost
before each file's bytes flow — the connection, the request, the response —
which for a small file is most of the download, and a big list has hundreds of
those. Wabbajack's own eight-way parallelism is faster through that stretch,
and worse afterwards, when eight streams split a slow line and a stuck slot
starves the rest.

`PARALLEL_SMALL_DOWNLOADS` takes the good half. Set it to 3 and up to three
files under `SMALL_DOWNLOAD_MB` from direct sources — the Wabbajack CDN,
GitHub, plain http — run together, while anything larger, or of unknown size,
still runs alone: it starts as soon as a slot frees, and nothing new starts
beside it. Because archives are fetched smallest first, a run turns into a
parallel phase of small files followed by a sequential phase of large ones.
Every stream keeps its own timeout, resume and retries, so a stall in one slot
holds up nothing else. Concurrency cannot add bandwidth — on a 3 MB/s line one
stream already fills it — so the number is for hiding per-file latency, not
for speed, and more than three only splits the line.

Nexus files on a free account are not pooled: each is a click. What helps
there is `ASSIST_CLICK_AHEAD`, which opens the next file's page while the
current file is still downloading, so you are never waiting on a transfer and a
transfer is never waiting on you. The captured link is resolved to a download
URL the moment it arrives, and at most one file waits behind the one
downloading, so a link cannot sit long enough to expire. The downloads
themselves stay one at a time.

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
| `wj_config.py` | every setting's default, the archive layout, and the naming rules the rest share: reserved names, the `~N` suffix, which GameTypes share a folder |
| `wj_settings.json` | your settings; `wj_settings.example.json` documents every key |
| `wj_overview.json` | what the Start tab shows: each tab's last answer and when it was measured; rewritten as tabs are used |
| `wj_wabbajack.py` | finding Wabbajack, whether it is the newest release, the game preflight, and the hand-over |
| `wj_autoupdate.py` | orchestrator: feeds, tracking, state, manifests |
| `wj_gallery.py` | the published gallery and its validation reports: what exists, and how much of it you hold |
| `wj_gui.py` | the desktop window: a tab per stage, a log pane, a Settings tab |
| `wj_wizard.py` | the console wizard, and the per-stage helpers both front ends share |
| `wj_downloader.py` | fetches archives; also the shared library for manifest parsing, hashing and the on-disk index |
| `wj_assistwin.py` | the download window assist mode shows Nexus pages in: WebView2 through pywebview, one page at a time, its own login |
| `wj_sorter.py` | moves archives between pools |
| `wj_cdn.py` | the Wabbajack CDN's multi-part protocol |
| `wj_merge_pool.py` | folds one game's pool folder into another's, once: renames, hash-proven drops, a record |
| `wj_paths.py` | which folder is ours, and how a script is launched — the two answers that differ between running from source and running from a build |
| `wj_launcher.py` | a build's entry point: decides which of the programs above the binary is being asked to be |
| `build.py`, `WabbajackWizard.spec` | make the Windows binaries; `release.py` makes the source zip |

## Tests

```
.python\python.exe tests\run_all.py
```

(or `.venv-win\Scripts\python.exe` where a machine-wide Python made that
environment instead). 972 checks across sixteen suites. None of them touch the live archive pools:
`ARCHIVE_ROOT` is `"../Mod Archive"` resolved against the *script* folder, so
each suite copies the scripts into a temporary directory where they operate on
a temporary pool, with no changes to the code under test. The download suites
serve their own localhost HTTP server, so they need no internet and can force
exactly the failure they want to check.

| suite | covers |
| --- | --- |
| `t1_sorter` | aborting on an unreadable manifest, reserved names, demand-based pooling, staging, size-checked collisions, dry-run purity, bad arguments, identical extras settling in one pass, the shared manifest cache |
| `t2_download` | hard vs transient HTTP failures, retry and resume, truncated transfers, verification, `.meta` sidecars, URL encoding, several small files at once with a stalled slot holding up nothing else, assist mode's click-ahead, the pages shown by a window that ends the run when closed, a progress line that cannot be written, and which program is registered for `nxm://` |
| `t3_cdn` | broken definitions in four shapes, bad part bytes and lengths, whole-file hash mismatch, resume from a partial download, stale and impossible sidecars |
| `t4_updater` | filename-to-feed-key matching, negative-cache expiry, damaged state files, odd feed shapes, the start-up guards |
| `t5_wizard` | empty data in every stage, missing folders, deleting a modlist whose pieces are already gone, awkward filenames |
| `t6_planning` | present vs stale vs absent decisions, `--force`, foreign-installer exclusion, manifest cache invalidation, backup pruning |
| `t7_identity` | one filename, several builds: the wrong copy must never satisfy a list that wants a different one |
| `t8_order` | download order — smallest, largest, manifest — and where unknown sizes land |
| `t9_wabbajack` | finding the newest CLI, the game preflight, path overlap, the install command |
| `t10_lookup` | every location an archive may sit in and still count as already downloaded |
| `t11_gallery` | the gallery's hand-edited JSON, case-inconsistent game tokens, cache fallback when the network is down, the gap maths, Wabbajack's own "Unavailable" rule, and the hidden flag never reaching a tracked list |
| `t12_cleanup` | both protection rules measured from one scan, the merge of two bodies of demand, folders that are nobody's and how they are sized, the refusal to delete on an incomplete picture, and a hidden list protecting nothing and blocking nothing |
| `t13_delete` | stage 5 driven end to end with scripted answers: a build another list needs surviving a deletion, `~N` copies protected and restored, reserved names refused, a failed rebuild or an untrustworthy manifest disarming the step, folder-level demand, the per-row record, the un-staging warning, hash-proven duplicates, the trash and its purge, stale CSVs, waiting for Wabbajack |
| `t14_merge` | folding one game folder into another: refusals on anything unrecognised, a dry run that changes nothing, hash-proven drops, same-size-different-content kept, suffixed collisions, the record |
| `t15_gui` | the window built for real on a withdrawn root against a sandbox, every tab loaded, the destructive flows driven through the buttons with dialogs stubbed, the Settings tab round-tripped against a sandbox file, browsing a game with no pool and giving it one, hiding a list and bringing it back, dark mode, and the Start tab opening a second window from the first one's file without measuring anything |
| `t16_pool` | laying out a pool for a new game: the layout produced, running it twice, an existing folder reused whatever its case, names that would collide with a pool folder or escape the root, folding one game into another game's pool and the mapping that writes |

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

## Building the binaries

```
.python\python.exe -m pip install -r requirements-build.txt
.python\python.exe build.py
```

Out comes `dist/WabbajackWizard/` to run from and
`dist/WabbajackWizard-v<version>-win64.zip` to publish. PyInstaller is needed
to *make* a build and never to run one, so it stays out of `requirements.txt`;
with the in-project interpreter it simply goes into that. With a machine-wide
Python instead, keep it in an environment of its own, which `build.py` also
looks for:

```
python -m venv .venv-build
.venv-build\Scripts\python -m pip install -r requirements-build.txt
python build.py
```

It is a one-folder build, not a one-file build, for two reasons. A run
launches the downloader and then the sorter as child processes, and a
one-file build unpacks the whole of Python afresh for each of them. And a
one-file build puts `__file__` in a temporary folder that disappears, when
this entire tool is built around living next to your archives. `wj_paths.py`
is the one place that knows the difference; nothing else has to.

The two binaries find each other by name, so do not rename them.

## Requirements

Python 3.8+ and `xxhash`. Without `xxhash` only sizes can be compared, so the
tooling refuses to run rather than quietly weakening verification; pass
`--allow-size-only` to override.

A build needs none of that - it carries its own Python and its own `xxhash`.
It is 64-bit Windows only, and wants Windows 10 or newer.

## Notes for this share

The share is an SMB mount also reached from a Mac, so AppleDouble files (`._*`)
and `.DS_Store` are ignored everywhere. `truncate()` on it is a synchronous
zero-fill at roughly 17 MB/s, which is why the CDN downloader never pre-sizes
its `.part` file.
