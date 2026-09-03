#!/usr/bin/env python3
"""
Shared layout configuration
===========================
The one place the archive layout is defined.

wabbajack_archive_downloader.py decides where an archive is WRITTEN, and
wabbajack_archive_sorter_v2.py decides where it is MOVED TO. When each carried
its own copy of these settings, editing one and forgetting the other meant the
two worked on different directory trees while both reported success - the sort
of fault that is cheap to prevent and miserable to diagnose. A start-up check
still compares the two scripts, but now it is comparing them to a single
source rather than to each other.

Edit this file, not the scripts.
"""

import os
import re

# ============================ CONFIGURATION ============================
# Where every game's archive pools live.
#
# A relative path is resolved against THIS FILE's folder, never the working
# directory, so the whole tree can be copied around or reached through a
# different drive letter or a UNC path and still work. With the tools in
# Z:\Wabbajack_Tools, "../Mod Archive" means Z:\Mod Archive.
ARCHIVE_ROOT = "../Mod Archive"

# Maps a GameType onto a pool folder whose name is not the GameType itself.
# Anything not listed here uses its GameType verbatim.
#
#   Skyrim, SkyrimSpecialEdition and SkyrimVR deliberately share one folder:
#   VR lists are built almost entirely from Special Edition archives, LE lists
#   overlap heavily too, and splitting them would mean a second copy of
#   everything. Every list drawing from a shared folder also protects it - see
#   folder_key() below.
#
#   Enderal is a total conversion running on Skyrim's engine, and its lists
#   are mostly Skyrim mods: of the two Enderal SE lists held here, three
#   quarters of the Nexus files come from the Skyrim SE Nexus, and its own
#   pool held 332 archives (31 GB) that the Skyrim pool already had.
#   wj_merge_pool.py folds an existing folder into the shared one.
#
#   DarkSouls3 points at an existing folder that predates this tooling. Without
#   the entry a Dark Souls 3 list would create a fresh "DarkSouls3" folder and
#   orphan the one already on the share. Inert if the GameType turns out to be
#   spelled differently.
GAME_FOLDERS = {
    "SkyrimSpecialEdition":  "Skyrim",
    "Skyrim":                "Skyrim",
    "SkyrimVR":              "Skyrim",
    "EnderalSpecialEdition": "Skyrim",
    "Enderal":               "Skyrim",
    "DarkSouls3":            "DARK SOULS 3",
}

# Cosmetic only: friendly labels for menus and tables. An unknown game falls
# back to its raw GameType string.
GAME_DISPLAY = {
    "SkyrimSpecialEdition":  "Skyrim Special Edition",
    "Skyrim":                "Skyrim (Legendary Edition)",
    "SkyrimVR":              "Skyrim VR",
    "Fallout4":              "Fallout 4",
    "FalloutNewVegas":       "Fallout: New Vegas",
    "Fallout3":              "Fallout 3",
    "Oblivion":              "Oblivion",
    "Morrowind":             "Morrowind",
    "StardewValley":         "Stardew Valley",
    "Enderal":               "Enderal",
    "EnderalSpecialEdition": "Enderal Special Edition",
    "BaldursGate3":          "Baldur's Gate 3",
    "Cyberpunk2077":         "Cyberpunk 2077",
    "Witcher3":              "The Witcher 3",
    "DarkSouls3":            "Dark Souls III",
}

# Pool sub-folders that sit alongside the per-list folders under each game.
POOL_FOLDERS = ("Master", "Shared")

# Folders directly under a game root that belong to a DIFFERENT installer and
# must never be indexed, counted as "already present", moved, or deleted.
# Nolvus ships its own installer and its own archive layout. Matched
# case-insensitively against the folder name.
EXCLUDE_POOL_DIRS = {"nolvus"}

# A folder under each game root for archives this tooling must never manage:
# hand-built installs (Tale of Two Wastelands), tool drops (ModEngine), and
# anything acquired outside Wabbajack.
#
# It exists because the alternative is worse. Any subfolder of a game root that
# is not a pool gets read as a modlist's folder, so its contents count as
# "already downloaded" while no modlist asks for them - which makes every file
# in it look like reclaimable junk to a cleanup that judges by demand. Parking
# such content here states plainly that it is not ours, and both tools then
# leave it alone completely: never indexed, never moved, never deleted.
UNMANAGED_FOLDER = "Unmanaged"

# Where stage 5c puts what it reclaims from Master, one folder per game root,
# instead of deleting it outright. A file there is invisible to every tool -
# never indexed, never counted as present, never moved - and is deleted for
# good once it has sat there TRASH_KEEP_DAYS days, at the start of the next
# run. Until then it can be moved back into Master by hand and the next
# rebuild sorts it home. The record of what went in, and why, is the
# deleted_<game>_<stamp>.csv stage 5c writes; the record of what was purged
# is purged_trash_<stamp>.csv.
TRASH_FOLDER = "Trash"
TRASH_KEEP_DAYS = 30

# Where the .wabbajack manifests live, relative to the tools folder, when
# WJ_MANIFEST_DIR is not set.
MANIFEST_SUBDIR = "modlists"

# Where Wabbajack itself is installed. None auto-detects: a sibling of the
# tools folder first (the arrangement on this share), then the per-user
# install location. Set it explicitly if yours is somewhere else.
WABBAJACK_DIR = None

# How a staged list is handed over.
#   "cli" - run wabbajack-cli install with the paths already filled in. The
#           only thing left to decide is where to install.
#   "gui" - open the Wabbajack window and print the two paths to paste.
#   "off" - do not offer to install at all.
INSTALL_MODE = "cli"
# =======================================================================


def looks_absolute(path):
    """Absolute-path test that recognises drive-letter and UNC forms whatever
    flavour of path handling the interpreter was built with."""
    if not path:
        return False
    return (os.path.isabs(path)
            or path.startswith("\\\\") or path.startswith("//")
            or (len(path) > 1 and path[1] == ":"))


def resolved_archive_root():
    """ARCHIVE_ROOT as an absolute path, anchored to this file's folder."""
    if looks_absolute(ARCHIVE_ROOT):
        return ARCHIVE_ROOT
    return os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ARCHIVE_ROOT))


def excluded_dirs():
    """Lowercased folder names under a game root that this tooling must not
    index, move or count: another installer's, the user's own unmanaged
    store, and the trash stage 5c fills."""
    return set(EXCLUDE_POOL_DIRS) | {UNMANAGED_FOLDER.lower(), TRASH_FOLDER.lower()}


def trash_dir(game):
    """Where a game's reclaimed archives wait out TRASH_KEEP_DAYS."""
    return os.path.join(resolved_archive_root(), game_folder(game), TRASH_FOLDER)


def reserved_list_names():
    """Lowercased names a modlist may NOT have.

    A modlist's folder is named after its manifest, so a manifest called
    Master.wabbajack would claim the Master pool as its own folder. Deleting
    that "modlist" would then empty the pool. The sorter has always refused
    these; the wizard and the downloader refuse them for the same reason."""
    return {p.lower() for p in POOL_FOLDERS} | excluded_dirs()


_SUFFIX = re.compile(r"^(.*)~(\d+)$")


def canonical_name(name):
    """The archive name a '~N' disambiguator was added to.

    When two different builds share one filename and both have to survive in
    the same folder, the second is stored as 'Name~1.ext', 'Name~2.ext', ...
    (see unique_path() in the sorter and the wizard). That suffix is storage
    only: no manifest ever asks for a file by that name, so every place that
    matches a file on disk against a manifest has to look through it, or the
    suffixed copy is invisible to demand - never staged, never counted as
    present, and reclaimable by a cleanup that judges by name.

    Checked against every archive name in the 22 manifests held here (26,835)
    and in all 224 published validation reports (304,477): none ends its stem
    in '~<digits>', so a real name is never mis-read as a suffixed one.

    Applied repeatedly, because parking an already-suffixed file yields
    'Name~1~1.ext'."""
    root, ext = os.path.splitext(name)
    while True:
        m = _SUFFIX.match(root)
        if not m:
            return root + ext
        root = m.group(1)


def is_suffixed(name):
    """True if `name` carries a '~N' disambiguator."""
    return canonical_name(name) != name


def unmanaged_dir(game):
    """Where a game's unmanaged archives are parked."""
    return os.path.join(resolved_archive_root(), game_folder(game),
                        UNMANAGED_FOLDER)


def game_folder(game):
    """Pool folder name for a GameType."""
    return GAME_FOLDERS.get(game, game)


def valid_game_type(gt):
    """Can this GameType safely become a folder name under the archive root?

    Wabbajack serialises GameType from an enum, so it is always a plain word.
    But a .wabbajack is a zip anyone could have edited, and a value carrying a
    path separator would put that list's pools outside the archive root."""
    gt = str(gt or "")
    return bool(gt) and gt not in (".", "..") and not any(c in gt for c in "/\\:")


def folder_key(game):
    """A case-stable key identifying the POOL a GameType's lists share.

    Two GameTypes can map onto one folder (Skyrim and SkyrimSpecialEdition do
    here), and then their lists draw from, and protect, one Master. Anything
    that groups or protects "per game" must group by this and not by the
    GameType string, or one game's lists read as not wanting what the other
    game's Master holds."""
    return str(game_folder(game) or "").lower()


def same_pool(game_a, game_b):
    """Do two GameTypes share a pool folder?"""
    return bool(game_a) and bool(game_b) and folder_key(game_a) == folder_key(game_b)


def game_label(game):
    """Friendly name for a GameType."""
    return GAME_DISPLAY.get(game, game)


def resolve_game_dirs(game):
    """(output_base, master, shared) for one game."""
    base = os.path.join(resolved_archive_root(), game_folder(game))
    return base, os.path.join(base, "Master"), os.path.join(base, "Shared")
