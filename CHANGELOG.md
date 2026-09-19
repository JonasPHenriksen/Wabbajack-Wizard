# Changelog

## 1.2.0 — 2026-09-19

### Added

- **A Start tab.** The window used to open on Browse and go straight to
  measuring the first game's lists against its pool - 28 seconds for Skyrim,
  with every other tab locked until it finished. It now opens on an overview:
  your modlists, pending updates, incomplete lists, the pools and the trash,
  the gallery, the settings, each card showing what the last session measured
  and when. Opening the window reads the manifests and nothing else. Each tab
  still measures when it is opened, and what it finds is written to
  `wj_overview.json` beside the scripts, so the next start shows it again at
  once. **Refresh everything** runs the three measurements in a row and fills
  those tabs on the way.
- **Dark mode.** A switch beside the progress bar, remembered as `THEME` in
  `wj_settings.json` and editable in Settings too. Dark is the `clam` ttk theme
  under a dark palette, the one theme that draws every part of every widget
  itself; the platform theme hands tabs, headings and scrollbars to Windows,
  which paints them light whatever it is told. Light is that platform look,
  untouched. The title bar follows on Windows 10 and 11, asked of the Desktop
  Window Manager directly since Tk draws none of it; Windows 11 takes the
  window's own colours for it.
- **Hidden lists.** A published list you will never want can be hidden from the
  Browse tab (**Hide this list**, under the detail pane; `[h]` on a list's
  detail screen in the console). It then appears nowhere but the **Hidden
  lists** entry at the end of the Game menu (`[h]` in the console's games menu),
  where the flag comes off again. Its validation report is left out of every
  sweep, it is never measured against the pools, and it protects nothing: under
  the "any known modlist" rule its archives are as reclaimable as if the list
  did not exist, and a missing report for it no longer blocks that rule. The
  rule's label in Space says so, and its confirmation names the hidden lists
  it is leaving unprotected. A list you track cannot be hidden, and a flag on
  a tracked list is ignored, because its own manifest is what protects its
  archives. The flags are `HIDDEN` in `wj_settings.json`.
- The Browse table names each list's game, which the Hidden view needs since
  it spans every game.
- **Wabbajack is checked before an install.** Staging a list now also asks
  whether the Wabbajack it found is the newest release, read from Wabbajack's
  own release feed and cached for six hours beside the gallery reports. The
  Prepare panel says which version is installed and which is newest, with the
  download page when they differ, and starting an install with an older one
  asks first: a modlist built for a newer Wabbajack can fail on it. The Start
  tab's Settings card remembers the verdict, and **Refresh everything** runs
  the check too. A lookup that fails is "cannot tell", which neither blocks an
  install nor blesses one. The finding, the game preflight and the lookup now
  run off the window's main thread.
- **Wabbajack can live inside the folder**, in `Wabbajack\`, which is looked in
  first and is gitignored, so tools, Python, git and Wabbajack move as one.
  Wabbajack's launcher leaves every superseded version folder behind at some
  700 MB each; whenever the wizard next looks at Wabbajack it removes them,
  only ever version-named folders holding the program, older than a newest
  that is whole, and never while a Wabbajack process is running.
- **Nothing installed, nothing absolute.** An interpreter unpacked into
  `.python\` inside the folder (a python-build-standalone release, tkinter
  included) is now the whole Python side: `setup_env.bat` installs the
  requirements straight into it and makes no virtual environment, and
  `run.bat`, the tests and `build.py` prefer it. A venv records where its
  Python is, which was the one absolute path in the setup; without one the
  folder can be moved or copied whole. The machine-wide-Python arrangement
  with `.venv-win` still works where that is what there is. The README
  describes the same arrangement for git: MinGit under `.mingit\`, the
  portable Git Credential Manager inside it, and a `git.bat` in front.

### Fixed

- The Settings form outgrew the tab at the default window size, and its Save
  button with it. The buttons now sit above the form, and the form scrolls.

Tests: 895 checks across 16 sandbox suites, up from 810.

## 1.1.0 — 2026-09-05

### Added

- **A build for people with no Python.** `WabbajackWizard-v<version>-win64.zip`
  unzips to two binaries and an `_internal` folder beside them: the window, and
  its console twin, which differ the way `python.exe` and `pythonw.exe` do.
  Everything is inside, `xxhash` included, so verification is the real xxHash64
  and not a fallback to comparing sizes. Nothing is installed and nothing is
  written outside the folder you unzip it into - settings, `modlists/`, logs and
  the state file all land beside the exe, exactly where they land beside the
  scripts, so a build can be dropped into an existing setup and pick up what is
  already there. `python build.py` makes it; the scripts are unchanged and still
  run as scripts.
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
