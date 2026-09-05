# Changelog

## 1.1.0 — 2026-09-04

### Added

- Browse the whole gallery, not only the games you keep archives for. **Every
  game** in the window, `a` in the console. A game with no pool is measured
  against an empty index, so its lists read as 0% held and the "to fetch"
  column is the full download.
- **Set up a pool** (`p N` in the console) lays out a new game's
  folder with the same `Master`, `Shared`, `Unmanaged` and `Trash` every other
  game has. Running it twice completes an existing folder rather than making a
  second one, and a folder already on the share is matched case-insensitively.
- The same prompt folds a game into a pool you already keep, the way Skyrim VR
  draws from Skyrim, and writes the `GAME_FOLDERS` entry to `wj_settings.json`
  so every tool in the suite agrees. Folder names that would collide with a
  pool folder, or carry a path separator, are refused before anything is made.
- Strays now say where they came from. A folder with a `.metadata` sidecar but
  no manifest is a list whose paperwork went missing, not a hand-built install,
  and when the gallery still publishes it both stage 5b and the Space tab say
  **adopt it** instead of offering to park it. Parking one would hide archives
  you own from the list that wants them. The published check reads only feeds
  already loaded that run, so stage 5 still never touches the network.
- A leftovers sweep, `5e` in the console and **Remove leftovers** in the Space
  tab. Every other step judges archives, so nothing ever reached the debris
  beside them: operating-system metadata, the `._name` files a Mac writes on a
  non-HFS share, and Wabbajack's `.metadata` sidecar for a modlist that is
  gone. It reports what it found, asks twice, and never touches the API key,
  the lock, a resumable `.part`, a running assist session's files, or the
  deletion records. `__pycache__` and the captured output of the last child run
  are deliberately excluded: the next run rewrites both, so sweeping them would
  prompt every time and reclaim nothing lasting.

### Fixed

- Switching tabs could show stale data. Deleting a modlist cleared Incomplete
  and Prepare but forgot Updates, so a list you had just deleted was still
  offered for updating until the window was reopened. Each tab now records
  what it was built from - the manifests, the archives on disk, the settings -
  and rebuilds only when something it actually reads has moved on. Fetching a
  manifest no longer throws away the pool index behind Browse, which is the
  expensive part.
- A job now holds you on the tab you started it from. Leaving mid-way landed
  you on a tab rebuilt from data the job had not finished writing.
- The Space tab gathered the orphaned-sidecar report and never displayed it,
  so in the window a stray folder had no explanation. It is shown now.
- A dozen games showed their raw feed token as a name, so the browse menu read
  `starfield` and `vtmb`. Every game the gallery publishes is now named.
- The browse summary read "220 published; 225 shown", which cannot both be
  true at a glance. The count was right, 220 live plus 5 retired lists still
  listed, and the line now says so.

Tests: 810 checks across 16 sandbox suites, up from 698 across 15.

## 1.0.0 — 2026-09-04

First release.

### What it does

Tracks published Wabbajack modlists, fetches their manifests and archives, and
organises the archives into per-game pools so Wabbajack can install any list
without re-downloading what is already on disk. A desktop window walks five
stages — browse the published gallery, updates, incomplete lists, prepare one
for Wabbajack, disk space — and a console wizard and a flag interface drive the
same functions for scripted use.

### Since the tooling was first imported

- A desktop GUI (`wj_gui.py`), launched by `run.bat`; the console wizard stays
  behind `--wizard`.
- Settings moved out of code into `wj_settings.json`, which the GUI's Settings
  tab edits; the shipped defaults live in `wj_config.py`.
- Consistent module names: `wj_downloader.py`, `wj_sorter.py`, `wj_cdn.py`.
- Stage 5 deletion hardened after three defects were reproduced in a sandbox:
  a wanted build renamed away and deleted, a Master pool emptied by deleting a
  manifest named `Master`, and a freshly adopted list reading as absent.
- Nothing is deleted outright: reclaimed files go to a per-game `Trash`,
  purged 30 days later, with a record of every file and why.
- Duplicates are proven by hash before they go; demand is counted per pool
  folder; a failed rebuild or an untrustworthy manifest disarms the step.
- Skyrim, Skyrim SE, Skyrim VR and Enderal SE share one pool folder;
  `wj_merge_pool.py` folds an existing folder into a shared one.
- Pool scans take sizes from the directory listing rather than a stat per
  file; the three tools share one manifest cache.
- Nothing that moves archives starts while a Wabbajack process is running.
- Windows only.

Tests: 698 checks across 15 sandbox suites, none of which touch a live pool.
