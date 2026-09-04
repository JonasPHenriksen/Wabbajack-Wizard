#!/usr/bin/env python3
"""
Shared layout configuration
===========================
The one place the archive layout is defined.

wj_downloader.py decides where an archive is WRITTEN, and
wj_sorter.py decides where it is MOVED TO. When each carried
its own copy of these settings, editing one and forgetting the other meant the
two worked on different directory trees while both reported success - the sort
of fault that is cheap to prevent and miserable to diagnose. A start-up check
still compares the two scripts, but now it is comparing them to a single
source rather than to each other.

Edit this file, not the scripts.
"""

import copy
import json
import os
import re

__version__ = "1.1.0"
APP_NAME = "WabbajackWizard"
USER_AGENT = f"{APP_NAME}/{__version__}"

# ============================ CONFIGURATION ============================
# Everything in this block is a DEFAULT. Your own values go in
# wj_settings.json beside this file, which the GUI's Settings tab writes and
# a code update never touches. A dict setting (GAME_FOLDERS, GAME_DISPLAY)
# is merged over its default; every other setting replaces its default.
# wj_settings.example.json shows every key with its default value.
SETTINGS_FILE = "wj_settings.json"

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
#   are mostly Skyrim mods: of two Enderal SE lists measured, three quarters
#   of the Nexus files came from the Skyrim SE Nexus. wj_merge_pool.py folds
#   an existing folder into a shared one.
#
#   To point a game at a folder whose name is not its GameType - a folder
#   that predates this tooling, say - add an entry in wj_settings.json:
#       "GAME_FOLDERS": {"DarkSouls3": "DARK SOULS 3"}
#   It is merged over this default. Inert if the GameType never appears.
GAME_FOLDERS = {
    "SkyrimSpecialEdition":  "Skyrim",
    "Skyrim":                "Skyrim",
    "SkyrimVR":              "Skyrim",
    "EnderalSpecialEdition": "Skyrim",
    "Enderal":               "Skyrim",
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
    # The rest of what the gallery publishes. Naming a game here is what folds
    # the feeds' inconsistent case onto one spelling, so 'starfield' and
    # 'Starfield' are one game in a menu rather than two.
    "Starfield":             "Starfield",
    "Fallout4VR":            "Fallout 4 VR",
    "OblivionRemastered":    "Oblivion Remastered",
    "DragonAgeOrigins":      "Dragon Age: Origins",
    "DragonsDogma":          "Dragon's Dogma",
    "Dishonored":            "Dishonored",
    "NoMansSky":             "No Man's Sky",
    "NierAutomata":          "Nier: Automata",
    "SevenDaysToDie":        "7 Days to Die",
    "MountAndBlade2Bannerlord": "Mount & Blade II: Bannerlord",
    "VampireTheMasqueradeBloodlines": "Vampire: The Masquerade - Bloodlines",
    "VTMB":                  "Vampire: The Masquerade - Bloodlines",
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

# ---- What to follow, and how to fetch -----------------------------------
# Every .wabbajack in the manifest folder is matched against the published
# feeds on first sight and the match is cached, so adding a list is a matter
# of dropping the file in. Set False to follow only what TRACK pins.
AUTO_TRACK = True

# Where the .wabbajack manifests live. Not an absolute path? Then relative to
# the tools folder. "modlists" is the recommended arrangement; a pattern with
# "*" follows Wabbajack's own versioned downloaded_mod_lists folder, though a
# Wabbajack upgrade creates a fresh empty one, which then looks like every
# manifest went missing. Owning your own folder avoids that.
MANIFEST_DIR = "modlists"

# Lists to leave alone, by feed key ("LoreRim/LoreRim") or filename stem,
# case-insensitively. A .wabbajack can be several GB, so every list followed
# costs real bandwidth.
EXCLUDE = []

# Pins, as "<repo>/<machineURL>", for a list auto-matching cannot resolve.
TRACK = []

# Which list a scheduled (non-interactive) run stages at the end: a filename
# stem, or None for whichever list updated most recently. One list per run,
# because staging a second undoes the first.
STAGE_LIST = None

# Nexus archives: "skip" leaves them to Wabbajack, and is the only mode that
# works unattended. "assist" opens each mod's page in your browser and
# captures your click - the same one click per file Wabbajack asks of a free
# account, driven from here so the files land in the pools.
NEXUS_MODE = "skip"

# Superseded manifests are kept this long for a rollback, then pruned. They
# are multi-GB each.
KEEP_BACKUP_DAYS = 14

# Refuse to run without xxhash rather than quietly degrade to size checks.
# --allow-size-only overrides for one run.
REQUIRE_HASH = True

# Order archives are fetched in: "smallest" first finishes the most files
# soonest and loses the least to an interruption; "largest"; or "manifest",
# the list's own arbitrary order. Unknown sizes always go last.
DOWNLOAD_ORDER = "smallest"

# Refuse to start a download batch that would leave less than this free.
FREE_SPACE_MARGIN_GB = 20

# An ntfy topic URL or Discord webhook URL for a scheduled run's report. None
# disables.
NOTIFY_URL = None
# =======================================================================

# The settings above that wj_settings.json may override, in the order the
# Settings tab shows them.
USER_SETTINGS = (
    "ARCHIVE_ROOT", "MANIFEST_DIR", "GAME_FOLDERS", "GAME_DISPLAY",
    "EXCLUDE_POOL_DIRS", "UNMANAGED_FOLDER", "TRASH_FOLDER", "TRASH_KEEP_DAYS",
    "WABBAJACK_DIR", "INSTALL_MODE", "AUTO_TRACK", "EXCLUDE", "TRACK",
    "STAGE_LIST", "NEXUS_MODE", "KEEP_BACKUP_DAYS", "REQUIRE_HASH",
    "DOWNLOAD_ORDER", "FREE_SPACE_MARGIN_GB", "NOTIFY_URL",
)
_DEFAULTS = {k: copy.deepcopy(globals()[k]) for k in USER_SETTINGS}
# Anything wrong with wj_settings.json, in words. Never fatal: a bad value
# is reported and its default used, because refusing to start over a typo in
# a settings file would leave the tool unusable until the file was fixed by
# hand, with nothing to say what was wrong.
SETTINGS_PROBLEMS = []

CHOICES = {
    "INSTALL_MODE": ("cli", "gui", "off"),
    "NEXUS_MODE": ("skip", "assist"),
    "DOWNLOAD_ORDER": ("smallest", "largest", "manifest"),
}


def settings_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), SETTINGS_FILE)


def defaults():
    """The shipped defaults, as a fresh copy."""
    return copy.deepcopy(_DEFAULTS)


def current_settings():
    """Every user setting as it stands right now."""
    return {k: copy.deepcopy(globals()[k]) for k in USER_SETTINGS}


def apply_settings(values):
    """Set the module's settings from a dict. Returns a list of problems;
    a setting with a problem keeps its current value."""
    problems = []
    g = globals()
    for key, value in values.items():
        if key not in USER_SETTINGS:
            problems.append(f"{key}: not a setting, ignored")
            continue
        default = _DEFAULTS[key]
        if isinstance(default, dict):
            if not isinstance(value, dict):
                problems.append(f"{key}: expected an object, kept the default")
                continue
            merged = dict(default)
            merged.update({str(k): str(v) for k, v in value.items()})
            g[key] = merged
        elif isinstance(default, (set, frozenset)):
            if not isinstance(value, (list, tuple, set)):
                problems.append(f"{key}: expected a list, kept the default")
                continue
            g[key] = {str(v).lower() for v in value}
        elif isinstance(default, list):
            if not isinstance(value, list):
                problems.append(f"{key}: expected a list, kept the default")
                continue
            g[key] = [str(v) for v in value]
        elif isinstance(default, bool):
            if not isinstance(value, bool):
                problems.append(f"{key}: expected true or false, kept the default")
                continue
            g[key] = value
        elif isinstance(default, int):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                problems.append(f"{key}: expected a whole number, kept the default")
                continue
            g[key] = value
        else:                                   # a string, or None
            if value is not None and not isinstance(value, str):
                problems.append(f"{key}: expected text, kept the default")
                continue
            if key in CHOICES and value not in CHOICES[key]:
                problems.append(f"{key}: must be one of {', '.join(CHOICES[key])}, "
                                f"kept the default")
                continue
            g[key] = value
    return problems


def load_settings(path=None):
    """Overlay wj_settings.json onto the defaults. Returns (values, problems)."""
    p = path or settings_path()
    if not os.path.isfile(p):
        return {}, []
    try:
        with open(p, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return {}, [f"{os.path.basename(p)} could not be read ({e}); defaults in use"]
    if not isinstance(data, dict):
        return {}, [f"{os.path.basename(p)} is not a JSON object; defaults in use"]
    return data, apply_settings(data)


def save_settings(values, path=None):
    """Write the given settings to wj_settings.json and apply them.

    Only keys that differ from the shipped default are written, so the file
    stays a short list of your choices rather than a copy of every default,
    and a later change to a default reaches you unless you overrode it."""
    p = path or settings_path()
    out = {}
    for key in USER_SETTINGS:
        if key not in values:
            continue
        v = values[key]
        if isinstance(v, (set, frozenset)):
            v = sorted(v)
        d = _DEFAULTS[key]
        if v == (sorted(d) if isinstance(d, (set, frozenset)) else d):
            continue
        out[key] = v
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, p)
    # Reset to defaults first, so a key removed from the file goes back to
    # its default rather than lingering at the old value.
    apply_settings(_DEFAULTS)
    return p, apply_settings(out)


_LOADED, SETTINGS_PROBLEMS = load_settings()


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
