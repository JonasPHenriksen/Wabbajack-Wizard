# Changelog

## 1.3.0 — unreleased

The first five additions and three fixes below were made after the 1.2.0
commit, on 2026-09-19 and 20, and are in the build that was zipped as 1.2.0
on the 20th. They are recorded here, under the release that carries them.

### Added

- **Only lists you could finish**, by default. A game's table shows the lists
  whose every archive is already in your pools or still obtainable, plus the
  ones you track; a list with a dead archive you do not hold is kept out, and
  so are the ones that cannot be measured - retired, pulled, or with no report
  yet - which are counted separately as unmeasurable. A dead archive you
  already hold hides nothing. **Only lists that can be completed** in the
  window, `c` in the console, shows everything, with an `x` on what cannot be
  finished. Display only: the Space tab's protection is unchanged by it.
- **Several small files at once, opt-in.** Downloads have always run one
  file at a time, and that stays the default: a stalled connection competes
  with nothing, costs at most its two-minute timeout, and resumes. What it
  wastes is the fixed cost before each file's bytes flow, which for a small
  file is most of the download. `PARALLEL_SMALL_DOWNLOADS` lets up to that
  many files under `SMALL_DOWNLOAD_MB` from direct sources run together;
  anything larger, or of unknown size, still runs alone, starting as soon as
  a slot frees and with nothing new starting beside it. Every stream keeps its
  own timeout, resume and retries, so a stall in one slot holds up nothing
  else. Nexus files on a free account are one click each and are not pooled.
- **Click-ahead for assist mode, opt-in.** `ASSIST_CLICK_AHEAD` opens the next
  Nexus page while the current file is still downloading, so you are never
  waiting on a transfer and a transfer is never waiting on you. The captured
  link is resolved to a download URL at once. `ASSIST_QUEUE_DEPTH` says how
  many clicked files may wait behind the one downloading: 1, the default,
  opens the next page when the waiting file starts, so you stay in step with
  the transfer; more lets you click that many pages ahead and walk away. The
  downloads stay one at a time. The log records, for every click, how much
  longer the key and the minted URL were good for, read off the timestamps
  both carry: measured at some 48 hours for the key and 4 hours for the URL.
  A file whose URL has aged out by its turn in a deep queue is minted a fresh
  one from the key before it starts, and closing the window with files
  waiting says how many were dropped; they are offered again next run.
- **A download window of the wizard's own.** Assist mode used to open a
  browser tab per file, several hundred for a large list. The pages now show
  in one window, the way Wabbajack's installer does it: Edge's WebView2
  engine, which Windows ships, through the pywebview library. One page at a
  time, replaced for each file, a banner across the top saying which file
  this is and how many are left, and a profile of its own under `.webview\`
  so the Nexus login is done there once and stays inside the folder. The click
  is caught inside the window: the engine's external-scheme event is answered
  by cancelling the `nxm://` launch and handing the link to the run, so no
  protocol handler or prompt is involved there. Closing the window ends the
  run, and the run waits for that to finish before summing up. The banner
  has a **Skip this file** button for a page with nothing to click, which
  records the file as not downloaded and moves on. `ASSIST_WINDOW`
  turns it off, and the browser is
  the fallback wherever the engine or the library is missing;
  `--check-window` says which it would be. The build carries pywebview and
  pythonnet, and writes a `.exe.config` beside each binary so .NET will load
  them from a network share.
- **A mod taken off Nexus is passed over at once.** Assist mode asks the API
  which game's site serves each mod before opening its page; a mod its own
  game reports as hidden, removed or unpublished now has no page opened for
  it at all. It is recorded as not downloaded, with Nexus's word for why, and
  the run goes on, instead of showing a dead page and waiting three minutes
  for a click that cannot come. The other dead page - the mod is up but the
  author has replaced the pinned file since the list was built - is caught
  from the same listing: the page opened is the current file's of the same
  name or size, the hash check after the download says whether it was the
  same bytes, and a file nothing matches is recorded and passed over too.
  The listing is asked for once per mod and remembered for the run.

### Fixed

- **Closing the window failed every download still running.** The download
  child kept going, but its progress lines went to a pipe the window had
  been reading, every write to it then raised, and the raise sat inside the
  transfer loop, so each chunk read as a broken transfer: retried four times
  and given up, file after file, with the network fine. Progress output can
  no longer fail a transfer; the first failed write switches it off for the
  rest of the run, in the downloader and in the CDN module alike.
- **Assist mode's clicks were lost from Firefox when running from the
  scripts.** Firefox will not launch a protocol handler whose program is
  `python.exe`; the click ends on Windows' "no app associated" dialog, though
  Windows runs the same command from a shell. A source run now registers the
  console exe beside the scripts as the handler whenever there is one, as the
  build itself always did. Registering and unregistering also tell the shell
  the associations changed, so a browser that had already asked about
  `nxm://` does not keep a stale answer, and unregistering removes the whole
  `nxm` class rather than leaving one with no command behind, which made every
  `nxm://` click fail between runs.
- **Assist mode could open the wrong game's page, and then keep doing it.**
  When the manifest's own game would not serve a mod, the fallback to a
  related game accepted any mod under the same id there. Mod ids are per game
  and dense, so a Special Edition mod removed from Nexus sent the run to old
  Skyrim's page for an unrelated mod - "Object not found" once the file id
  was added - and old Skyrim was then remembered as the site for every
  Special Edition mod after it in the run. Another game is now believed only
  when it lists the wanted file, and the manifest's own game is asked why it
  will not serve the mod before any fallback is opened.
- **A folder setting could point the trash at a pool.** `TRASH_FOLDER` and
  `UNMANAGED_FOLDER` were only checked for being text, and the trash is
  emptied for good, at every start, of whatever has sat in
  `<game>\<TRASH_FOLDER>` for `TRASH_KEEP_DAYS`. Set to `Master`, it would
  have deleted every Master archive older than that; set to an empty string,
  the loose files in each game folder. The Settings tab writes an emptied
  field as null, and after that no tool would start. Both folder settings,
  the entries of `EXCLUDE_POOL_DIRS` and the values of `GAME_FOLDERS` must
  now be plain folder names - no path in them, no device name, not Master or
  Shared - and the trash may not also be the unmanaged store or another
  installer's folder. A text setting that has to name something may not be
  empty. A refused value keeps its default and is reported, and the Settings
  tab no longer writes it to the file at all; a `GAME_FOLDERS` mapping is
  written as just the entries that differ from the default. The purge and
  stage 5c ask again before acting, and also refuse a trash folder that is a
  modlist's own.
- **A manifest could make the downloader write outside the pool.** An
  archive's name was used as a path just as it stood, so a `.wabbajack`
  naming one `..\..\escaped.txt` had its download written two folders above
  Master, and it passed verification, since the manifest brings the hash
  too. Reproduced in a sandbox. An archive name must now be a plain file
  name, checked where a manifest is read, again as the light cache is read,
  and again where a download is written. A manifest with such a name is
  refused whole, since Wabbajack never writes one, and the Incomplete tab
  marks it. The sorter was never exposed: it moves only files it finds on
  disk, under their own names. The same rule now covers the keys the
  gallery caches reports under, which come from the authors' own feeds: an
  entry or a feed with such a key is left out and counted as a feed that did
  not answer, so the "any known modlist" rule does not delete on the gap, and
  a cache path that would leave the cache is refused.
- **A change of settings was half applied in the running window.** The
  downloader module keeps its own copies of part of the configuration, the
  pool layout above all, and the window plans with it; saving the Settings
  tab refreshed only the updater's copies. After Set up a pool folded
  Fallout4VR into Fallout4, `wj_config` said Fallout4 while everything the
  window measured still looked in Fallout4VR, until the next start. Every
  copy now follows at once. Only `ARCHIVE_ROOT` and `MANIFEST_DIR` still wait
  for a restart, as the Settings tab says.

Tests: 1081 checks across 17 sandbox suites, up from 895 across 16.

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
