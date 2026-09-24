# Nexus collections: an experiment, parked

On 2026-09-24 the Wizard was taught to install Nexus collections without
Vortex, the way it prepares Wabbajack lists: into a portable Mod Organizer 2
instance with the game copied into a Stock Game folder, so the store's install
is never touched, on a free Nexus account. It worked, on three collections
across two games. It is parked, because a survey of every published collection
found little that Wabbajack does not already serve (see [The market](#the-market)).
This note keeps what was learned, and how to pick it up again.

The code lives on the branch `nexus-collections` (commit `a9410fa`, five
commits on top of `27b43d2`) and is not merged. Nothing of it is on `main`.

## What was built, on the branch

| piece | what it does |
| --- | --- |
| `wj_collection.py` | `fetch` turns a collection into `<name>_@@_<slug>.collection` in the manifest folder: a zip whose `modlist` member has the shape of a `.wabbajack`'s (a GameType, one Archives[] entry per file with its name, size, MD5 and a Nexus State), with `collection.json`, `wizard.json`, and the archive's bundled mods and patches under `archive/`. Also `inspect`, `install`, `setup-c2mo2` and `assemble-game`. |
| downloader, sorter, Space, Prepare | read a `.collection` like any other list, so they needed almost no change. The downloader checks an MD5 when an archive carries one. |
| Bethesda games | built by [collections2mo2](https://github.com/spooknik/collections2mo2) (c2mo2), pinned at commit `ca460c7` and refused unless GitHub's archive of it has the SHA-256 `1056d1b1242431946722f4bddc7477ae4f33fd24c013f26ea9585aa106805aae`; installed into `.c2mo2`, its libraries in `.c2mo2\site`, run as a program of its own (it is GPL-3). The branch works around its gaps; see below. |
| other games | built by the Wizard itself, `wj_mo2.py`: Stardew Valley so far. |
| the window and the console | a **Nexus collection** box on the Browse tab and `run.bat --collection <address>`; Prepare builds the instance, with a Game folder row for a collection. |
| tests | `t17_collection` (182 checks), `t18_mo2` (110) and additions to `t15_gui`: 1288 checks on the branch. |

## The three test collections

| collection | game | mods | Nexus clicks | result |
| --- | --- | --- | --- | --- |
| TESTING, 1 2 3 (`mi01mo`, revision 1) | Skyrim SE 1.7.104 | 11, 3 already pooled | 8 | built by c2mo2 in 27 seconds; played in game |
| Basic Essential Mods For SSE v1.5.97 (`wpbutx`, revision 11) | Skyrim SE 1.5.97 | 26 | 6 | needed the game at 1.5.97 (below); 25 built, one pinned file gone from Nexus; exposed most of c2mo2's gaps |
| Stardew Valley VERY Expanded (`tckf0m`, revision 112) | Stardew Valley 1.6.15 | 100, 35 already pooled from The Bustling Valley | 64, in about four minutes | 99 built by `wj_mo2` in about 90 seconds, all 5 patches applied; SMAPI 4.5.2 loaded 56 mods and 63 content packs, none skipped |

## The market

Surveyed on 2026-09-24: all 16,076 published collections across 270 games,
read from Nexus's GraphQL API, against the 224 lists in Wabbajack's gallery,
which cover 21 of those games.

"Good" here means 10,000 or more users (unique downloads), a success rating of
70% or better from at least 50 votes (the share of users who say it installed
and worked), a revision published within the last twelve months, and no adult
content. The rating is harsh: for Skyrim collections with 10,000+ users the
median is 73%.

| | games | collections | with 10k+ users | good |
| --- | --- | --- | --- | --- |
| no Wabbajack list | 249 | 3,725 | 29 | 1 (Subnautica++, 22k users) |
| Wabbajack covers | 21 | 12,351 | 147 | 36 |
| all | 270 | 16,076 | 176 | 37, and 39 more that are adult |

At 1,000+ users there are 141 good collections in all, 25 of them for games
with no Wabbajack list, spread thinly: S.T.A.L.K.E.R. 2 and Kingdom Hearts III
have three each, then Subnautica: Below Zero, Darktide and a few others. Big
games with no Wabbajack list have collections but none that pass: The Witcher
3 and Palworld each have four with 10k+ users, all out of date or poorly rated,
and Valheim's best has 14k users at 61%.

The good ones, by game:

| game | current Wabbajack lists | good collections | the biggest | built by |
| --- | --- | --- | --- | --- |
| Fallout 4 | 12 | 12 | small modular packs (aSW Expanded, the "Module" series) | c2mo2 |
| Skyrim SE | 50 | 8 | Essential Mods for Skyrim (152k users, 73%) | c2mo2 |
| Cyberpunk 2077 | 7 | 5 | Welcome to Night City 2.31a (431k, 80%) | nothing yet |
| Fallout New Vegas | 6 | 5 | Gopher's Stable New Vegas (186k, 74%) | c2mo2 |
| Stardew Valley | 1 | 4 | Stardew Valley VERY Expanded (292k, 84%) | `wj_mo2` |
| Subnautica | 0 | 1 | Subnautica++ (22k, 78%) | nothing yet |
| Oblivion Remastered | 1 | 1 | Echoes of Oblivion (39k, 76%) | no MO2 plugin |
| Baldur's Gate 3 | 2 | 1 | BG3 Essentials (33k, 76%) | no MO2 plugin |

A "current" Wabbajack list is listed, not NSFW, not marked down, passing
Wabbajack's download checks, and updated within the last twelve months.

So the branch can build 29 of the 37 good collections (25 Bethesda, 4
Stardew). A Cyberpunk builder would add five more, among them the most-used
good collection on Nexus. The two most-used collections of all are adult
Skyrim ones, with 612k and 375k users. Collections also fill a niche Wabbajack
lists rarely do: small, modular setups of 5 to 30 mods.

## What a collection is made of

- `collection.json` holds `info` (the game's Nexus domain and `gameVersions`),
  `mods[]`, `modRules`, `plugins` and `pluginRules`, `collectionConfig` and
  `tools`.
- Each mod has a `source`: its `type` (nexus, bundle, browse, manual or
  direct), `modId`, `fileId`, `md5`, `fileSize`, `logicalFilename`,
  `fileExpression`, `tag` and `updatePolicy`. It may also carry `choices` (the
  curator's FOMOD answers), `hashes` (install file for file: every file's path
  and MD5), `patches` (path to CRC32), `details.type` (the Vortex mod type:
  `dinput`, `enb`, `SMAPI` and so on), `phase` and `fileOverrides`.
- The collection archive, often a 7z, holds `collection.json`, `bundled/` (a
  mod packed into the collection itself, as its staging folder or as its
  archive), `patches/<mod name>/<path>.diff` and `Ini Tweaks/`.
- Patches are bsdiff 4 (`BSDIFF40`). The CRC32 is that of the original file,
  so a patch applies only to the exact file it was made from; this was checked
  on the real Ridgeside Village and Eli and Dylan files.
- A curator's rules come from their whole Vortex setup. Only the rules naming
  two of the collection's own mods decide anything, matched by MD5: 22 of 66
  in Stardew Valley VERY Expanded. In a `before` rule the source loses
  conflicts to the reference; `after` is the reverse.
- Collections published before 2023 carry no per-mod `tag`.
- Pinned files decay. When an author replaces a file, Nexus archives the pinned
  one and no account can download it any more. Two of the three test
  collections had one: eFPS in Basic Essential Mods, and "They Deserve It Too -
  Portraits for Vendors" in Stardew Valley VERY Expanded. Vortex stops there
  too; the branch names the mod and builds without it.

## The Nexus API

- Collection metadata is anonymous GraphQL at
  `https://api.nexusmods.com/v2/graphql`. `collectionRevision(slug, revision)`
  gives the archive's `downloadLink`. `collectionsV2` searches, at most 80 per
  page (more exceeds the query complexity limit of 10,000), and gives the
  success ratings (`overallRating`, `recentRating` and their vote counts),
  downloads, endorsements, `lastPublishedAt` and staff badges ("Top pick",
  "Easy install").
- A free account's API key can fetch the collection archive: the
  `downloadLink`, requested with the `apikey` header, answers with
  `download_links[0].URI`.
- GraphQL's `modFiles.file.uri` is a storage path for files uploaded since
  2026, not the name a file downloads under. The v1 `files/{id}.json`
  `file_name` is that name, and it is the one the pools and c2mo2 know an
  archive by.
- On a free account every file is a click on the website. The assist window
  made 64 of them take about four minutes.

## collections2mo2's gaps, and what the branch does about them

c2mo2 builds a portable MO2 with a Stock Game copy, replays FOMOD answers,
orders mods by the rules and plugins by the curator's order, and routes
game-root mods through Root Builder. The test collections found these gaps,
each handled around it with its code left exactly as pinned:

| gap | effect | the branch's answer |
| --- | --- | --- |
| a collection without per-mod tags | every mod installed into one folder, since it keys installs by tag | gives each mod a tag, in a copy of the manifest it reads |
| file-for-file mods matched by path | a FOMOD-packed archive (SMIM) placed 0 of its 1,427 files | places each listed file by MD5, as Vortex does, then has it re-render the profile |
| a pinned file Nexus no longer serves | a Premium error on a free account, naming no file | named, left out, built with `--allow-missing` |
| `LocalSaves=false` written on every render | the instance's saves shared with every other setup | profile saves turned back on after each render |
| its version check reads the numeric version field | warns "1.0.0" for 1.5.97; cosmetic | the Wizard reads the string table |
| MO2 and Root Builder archives and bundled zips copied into its downloads folder, which is the pool | clutter in the pool | none; the sorter moves them to Master |
| no binary patches, no free-account downloads, no mods from other sites | | the Wizard downloads everything first and checks every MD5 |

A mod's readme can ask for a step no installer does. DLL Plugin Loader needs
the game's `binkw64.dll` beside it as `binkw64_.dll`, or SKSE stops at
"binkw64_.dll failed to load"; the branch copies it into the mod's Root folder
(`ROOT_FIXUPS`). None of these gaps has been reported upstream.

## A collection built for an older game

Skyrim 1.5.97 came from Steam's console (`steam://open/console`), which
downloads one depot at an old manifest into
`steamapps\content\app_489830\depot_<id>`:

```
download_depot 489830 489831 7848722008564294070
download_depot 489830 489832 8702665189575304780
download_depot 489830 489833 2289561010626853674
```

`wj_collection.py assemble-game --out <folder>` laid them into a game folder of
their own (12.7 GB), and never into a Steam library. The 1.5.97 `SkyrimSE.exe`
says 1.0.0.0 in its numeric version field; its string table says 1.5.97.0.

## Stardew Valley, and the Wizard's own builder

- Vortex's Stardew extension lays a mod out by its SMAPI manifests. Each folder
  holding a `manifest.json` stays at its path in the archive, anything outside
  one is dropped, a `manifest.json` at the top of the archive gets a folder
  named after the mod's Name, and one under a `locale` folder is ignored. An
  archive with no manifest goes in as it is. MO2 maps `mods\<name>\` onto the
  game's `Mods` folder, so the same layout serves both, and the curator's
  patches and config files land where they were made: all 30 of the config
  files in the curator's own config mod for Stardew Valley VERY Expanded lined
  up.
- SMAPI's installer package carries the loader as
  `internal\windows\install.dat`, which belongs in the game folder, with the
  game's `Stardew Valley.deps.json` copied as `StardewModdingAPI.deps.json`; it
  ships its own `steam_appid.txt`. The builder puts it into Stock Game
  directly rather than through Root Builder, because MO2 would also map a Root
  folder into `Mods`, where SMAPI would see a mod with no manifest. Its bundled
  mods, Console Commands and Save Backup, stay in its MO2 mod.
- The game never asks Steam to relaunch it (it makes no `RestartAppIfNecessary`
  call), so a Stock Game copy runs from the copy.
- Saves cannot be kept apart. Stardew writes them to
  `%APPDATA%\StardewValley\Saves` wherever it runs from, MO2's Stardew plugin
  registers no profile saves, and Steam Cloud syncs that folder, so a modded
  farm sits beside the vanilla ones.
- Mod Organizer 2.5.2's release archive, `Mod.Organizer-2.5.2.7z`, is
  149,660,212 bytes with the SHA-256
  `e6376efd87fd5ddd95aee959405e8f067afa526ea6c2c0c5aa03c5108bf4a815`. GitHub
  lists no digest for assets that old, so the branch pins this one.
- A Wabbajack list for the same game is a good blueprint: The Bustling Valley's
  directives showed how a list author lays Stardew out in MO2.
- MO2 2.5.2 has game plugins for 75 games (15 compiled, 60 in basic_games),
  but none for Baldur's Gate 3, Oblivion Remastered or 7 Days to Die.

## Picking it up again

For Stardew Valley VERY Expanded:

1. `.\git.bat checkout nexus-collections`, and `.\git.bat merge main` if
   `main` has moved on since.
2. Fetch the collection:
   `run.bat --collection https://www.nexusmods.com/games/stardewvalley/collections/tckf0m`,
   or the **Nexus collection** box on the Browse tab.
3. Download what is missing: the Incomplete stage in assist mode, or
   `.python\python.exe wj_downloader.py --game StardewValley --list "Stardew Valley VERY Expanded_@@_tckf0m" --yes --nxm-assist --order smallest`.
   The 63 archives downloaded on 2026-09-24 were left in
   `Z:\Mod Archive\StardewValley\Master`, so unless Master has been cleared
   since, only the files the curator has changed need a click.
4. Prepare: stage it and build the instance into a new, empty folder on a
   local disk (last time `D:\Modding\SVVE`). Mod Organizer is downloaded on the
   first build.
5. Start `ModOrganizer.exe` there and run SMAPI, which is already chosen. Start
   a new farm.

A Skyrim or Fallout collection needs c2mo2 as well:
`wj_collection.py setup-c2mo2`, which Prepare also offers.

## Open ends

- Moving an instance to a curator's newer revision: the builder only makes new
  instances.
- A Cyberpunk builder, the largest gain the survey shows: five good
  collections, Welcome to Night City among them. It is harder than Stardew,
  since Cyberpunk mods go into several of the game's folders.
- Mods from other sites (browse and manual sources), FOMOD answers for games
  c2mo2 does not build, and Vortex's `fileOverrides`.
- Reporting c2mo2's gaps upstream.

## What was cleaned up afterwards

Once the note was written, the session's changes were undone, apart from the
branch and the Stardew archives kept in Master:

- The three `.collection` lists were deleted the Wizard's way (stage 5a), which
  emptied their pool folders into Master and rebuilt both pools. The Bustling
  Valley's folder has its 273 archives again, and both pools re-sort to zero
  moves.
- The archives downloaded for the two Skyrim test collections, and c2mo2's
  copies of MO2, Root Builder and a bundled SKSE, went to
  `Z:\Mod Archive\Skyrim\Trash`; `deleted_Skyrim_20260924_210617.csv` and
  `deleted_Skyrim_20260924_210643.csv` say where each went.
- The four instances (`D:\Modding\TESTING`, `BasicEssentials`,
  `BasicEssentials (broken build)` and `SVVE`), the assembled 1.5.97 game,
  Steam's depot downloads and their manifests, the TESTING character's
  autosave, and SMAPI's files in `%APPDATA%\StardewValley` went to the Recycle
  Bin.
- `.c2mo2`, the MO2 cache, the session's download logs and the test sandboxes
  were removed.
