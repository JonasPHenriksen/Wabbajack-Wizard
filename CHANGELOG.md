# Changelog

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
