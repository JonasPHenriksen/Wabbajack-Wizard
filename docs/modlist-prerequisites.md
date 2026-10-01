# What a modlist needs before its install

On 2026-10-01 the question was whether the Wizard could take over the steps
modlist readmes ask for before a Wabbajack install: settings to change on the
PC, programs to install first, how the game must be set up. Mostly it cannot
do so reliably, so 1.4.0 opens the list's readme before every install and
waits for a yes. This note keeps what was learned, for picking it up again.

## In short

- Only the readme says what a list needs done first. A manifest has no field
  for it; the readmes are prose in no common form, often on pages a tool
  cannot read (Nexus answers automated requests with 403), and they go stale.
- The game is the exception. A manifest pins exactly which game files, and
  which game build, the list starts from. That is machine-readable and exact,
  and a wrong build is the costliest failure: it shows only when Wabbajack
  starts installing, after every archive is downloaded.

## What a manifest carries

A `.wabbajack` is a zip whose `modlist` JSON holds, in all 15 lists followed
here:

| Field | Holds |
| --- | --- |
| `Archives` | every file the list needs: size, hash, where it comes from |
| `Directives` | the install steps that rebuild the list from those files |
| `Name`, `Author`, `Version`, `Image` | identity |
| `Description` | a few sentences |
| `GameType`, `OtherGames` | the game, and any other it draws on |
| `Readme` | **a link**: GitHub, the author's own site, a Nexus page, a wiki |
| `Website`, `Community` | more links, a Discord among them |
| `IsNSFW`, `WabbajackVersion` | flags |

The zip also holds the inline files, named by id, and the list's image. The
feeds (`modlists.json`) add sizes, tags and dates. Neither has anything about
prerequisites.

## The game: pinned exactly

- `GameFileSourceDownloader` archives are files out of the user's own game
  install, each with its hash and the `GameVersion` it was taken from.
  Wabbajack finds them by hash and cannot download them - game files are not
  redistributable - so the game installed must be that build.
- Most lists then build their own copy of the game, "Stock Game" or "Game
  Root", with `PatchedFromArchive` steps. That is Wabbajack's own downgrade,
  and why the Fallout 4 readmes say to stay on the latest Steam build and never
  downgrade by hand: Magnum Opus patches it down to the pre-Next-Gen 1.10.163,
  and LoreOut to an older build it does not name. A list that copies the files
  as they are (`FromArchive`) needs exactly the build it pins.
- The build pinned is the one its author had installed when compiling. A Steam
  update past it breaks every list built on the old build until the author
  republishes; an install already made keeps working, having its own copy.
- Anniversary Edition lists need all 148 Creation Club files (LoreRim, Lost
  Legacy, Nordic Souls PBR); others a few (Life in the Ruins 27, Cursedelica 8,
  Magnum Opus 4). Missing DLC shows the same way, as game files not there.
- .NET tools name the runtime they need in a `*.runtimeconfig.json`. Seven of
  the 15 lists ship such tools, mostly inside mod archives (zip, 7z), a few
  inline - LoreRim's Reqtificator wants .NET 6.0 - so a list's runtimes could
  be read once its archives are downloaded.

The 15 lists followed here, 2026-10-01:

| List | Starts from | Its copy of the game |
| --- | --- | --- |
| LoreRim, Lost Legacy, Nordic Souls PBR | Skyrim SE 1.7.104.0 | the exe patched |
| Cursedelica | Skyrim SE 1.6.1170.0 | copied as is |
| Life in the Ruins, LoreOut, Magnum Opus | Fallout 4 1.11.240.0 | the exe patched |
| Begin Again | New Vegas 1.4.0.525 | the exe patched (4GB) |
| High & Dry | New Vegas 1.4.0.525 | copied as is |
| Last Seed | Oblivion 1.2.0416 | copied as is |
| Somnium | Enderal SE 1.5.97.0 | copied as is |
| Emissaries of Tux | Enderal SE 1.5.97.0 | none; its game files feed mods |
| PROJECT 2077, Welcome to Night City, The Bustling Valley | nothing pinned | none |

With Skyrim at 1.7.104.0 on this PC, Cursedelica cannot install until its
author updates it.

## What the readmes ask

The readmes of the 15 lists followed here and of 13 featured lists for the
same seven games, read on 2026-10-01; each summary was cross-checked against a
second reading. 25 were readable; the three on Nexus pages answered 403.
Counts are of the 25.

| Step | Lists | Notes |
| --- | --- | --- |
| A Steam copy, with the right DLC or content | nearly all | Anniversary Edition content for many Skyrim lists; Fallout 4: all DLC but the HD texture pack; Cyberpunk: Phantom Liberty and REDmod (app 2060310); Enderal: the free standalone app 976620 |
| The game build | nearly all | most say "the latest Steam version" and let Wabbajack patch it; builds named in readmes are often stale |
| Game language English, in Steam | 15 | |
| A clean game: reinstall, empty `Documents\My Games\<game>`, remove leftovers | about 20 | Cyberpunk: also its AppData caches and everything but `archive`; several Skyrim lists: never verify files, and take Rare Curios from the in-game Creations menu |
| Launch the game once | about 19 | for the INIs and the registry entry Wabbajack finds the game by; on Skyrim, for the Creation Club download |
| Game outside Program Files | about 16 | many link LostDragonist's Steam Library Setup Tool |
| Visual C++ 2015-2022 x64 | 16 | Mod Organizer needs it; the older games want every year, x86 and x64 |
| .NET runtimes | 11 | a different set per list: 6 and 9 desktop; 8 and 10 with the 10 SDK; 5.0; 9; 6 or newer; Framework 4.8 |
| Steam auto-update off ("only when I launch it") | 9 | |
| Steam overlay off | 7 | |
| Page file 20-40 GB | 8 of 11 Skyrim lists | 20480 MB, 40960 MB, "20-40 GB": no two agree |
| Antivirus exclusions, or warnings about products | about 12 | Webroot, Bitdefender, Norton, Kaspersky, Avast and others named |
| OneDrive kept out of the way | about 8 | |
| DirectX June 2010; Windows long paths | a few | |
| Folders: not under Program Files, Desktop, Downloads, Documents or OneDrive; short; on an SSD | nearly all | |
| Files to download by hand (Google Drive, Patreon, MediaFire) | about 6 | |

Rarer: Windows 10 22H2 or 11; Mandatory ASLR off (Viva New Vegas); file
extensions shown; an NVIDIA shader cache of 10 GB; Steam Cloud off (The
Bustling Valley, Elysium); UAC left on (Wildlander). None asks about
Controlled Folder Access, Game Mode or Steam's offline mode.

Stale or contradictory: LoreRim's readme names Skyrim 1.7.99 and Nordic Souls
PBR's 1.6.1170, while both manifests pin 1.7.104.0; Lost Legacy's says its own
.NET 5 note may be out of date.

Prior art: [CyberpunkModlistSetup](https://github.com/qcargile/CyberpunkModlistSetup),
linked by Chrome and Blood, installs the Visual C++, .NET 8 and DirectX
runtimes, turns on long paths, sets Steam to update on launch and cleans the
game, and checks for Phantom Liberty, REDmod, free space and the folder.

## What was built, and what was not

Built, in 1.4.0: every install - from Prepare, from the console's stage 4, and
an update in place - opens the list's readme, the manifest's `Readme` field
found in one pass over the manifest, and waits for a yes. Only a web address
is opened (the 1.3.2 rule, `wj_paths.web_url_problem`).

Considered and left out, on 2026-10-01, to keep it simple:

- **A game check from the manifest**: the installed build against the one
  pinned, the game files the list needs (DLC, Anniversary content), changed
  game files. Exact, and it would catch the costliest failure before the
  downloads rather than after them. The game's folder comes from
  `wabbajack-cli list-games`; then the exe's file version against
  `GameVersion`, file sizes, and hashes when asked.
- **Four hints nearly every list shares**: Steam auto-update on for a game
  whose lists pin a build, the game never launched, the game under Program
  Files, Documents on OneDrive.
- **A checklist of everything the readmes ask, and fixes for it** (runtime
  installers, page file, Defender exclusions): one-time setup, mostly specific
  to a list - noise when a list does not need it - and each fix adds upkeep
  and security surface for little gain.

## This PC on 2026-10-01

Steam in `C:\Games\Steam`, a second library on `D:\SteamLibrary`; Skyrim SE
1.7.104.0 with auto-update off; Stardew Valley 1.6.15; Fallout 4, New Vegas,
Oblivion, Enderal and Cyberpunk not installed through Steam. Visual C++
2015-2022 (x64 and x86), .NET 8 and 10, DirectX 9 and long paths already in
place; Documents not on OneDrive.
