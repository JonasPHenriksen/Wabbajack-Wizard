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
#   Skyrim and SkyrimSpecialEdition deliberately share one folder: the same
#   archives serve both, and splitting them would mean two copies of
#   everything.
#
#   DarkSouls3 points at an existing folder that predates this tooling. Without
#   the entry a Dark Souls 3 list would create a fresh "DarkSouls3" folder and
#   orphan the one already on the share. Inert if the GameType turns out to be
#   spelled differently.
GAME_FOLDERS = {
    "SkyrimSpecialEdition": "Skyrim",
    "Skyrim":               "Skyrim",
    "DarkSouls3":           "DARK SOULS 3",
}

# Cosmetic only: friendly labels for menus and tables. An unknown game falls
# back to its raw GameType string.
GAME_DISPLAY = {
    "SkyrimSpecialEdition":  "Skyrim Special Edition",
    "Skyrim":                "Skyrim (Legendary Edition)",
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
    """Absolute-path test that recognises Windows forms even on Linux, where
    posixpath would call 'Z:\\x' relative."""
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
    touch: another installer's, plus the user's own unmanaged store."""
    return set(EXCLUDE_POOL_DIRS) | {UNMANAGED_FOLDER.lower()}


def unmanaged_dir(game):
    """Where a game's unmanaged archives are parked."""
    return os.path.join(resolved_archive_root(), game_folder(game),
                        UNMANAGED_FOLDER)


def game_folder(game):
    """Pool folder name for a GameType."""
    return GAME_FOLDERS.get(game, game)


def game_label(game):
    """Friendly name for a GameType."""
    return GAME_DISPLAY.get(game, game)


def resolve_game_dirs(game):
    """(output_base, master, shared) for one game."""
    base = os.path.join(resolved_archive_root(), game_folder(game))
    return base, os.path.join(base, "Master"), os.path.join(base, "Shared")
