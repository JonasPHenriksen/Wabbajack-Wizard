"""The desktop window.

It is built for real - Tk widgets, on a withdrawn root - against a sandbox
pool, with every dialog stubbed to "yes" and recorded, and its jobs pumped by
hand rather than by mainloop. Each tab is loaded, and the destructive flows
(delete a modlist, clear Master, park a stray, empty the trash) are driven
through the buttons' own handlers, so what the window does is what the
console wizard does: they call the same helpers. The Settings tab is
round-tripped against a settings file inside the sandbox; the real one beside
the scripts is never touched.
"""
import io
import json
import logging
import os
import shutil
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOXROOT = os.path.join(os.environ.get("TEMP", "/tmp"), "wj_tests")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Sandbox, check, summary

sys.path.insert(0, REPO)
os.chdir(REPO)
import tkinter as tk
import wj_autoupdate as A
A.setup_logging(False)
A.LOG.setLevel(logging.CRITICAL)
A.WJ = A.load_downloader_module()
import wj_config as CFG
import wj_gallery as G
import wj_wizard as W
import wj_wabbajack as WB
import wj_gui as GUI

BOX = os.path.join(BOXROOT, "sb15")
GAME = "Fallout4"
SAVED_SETTINGS = CFG.current_settings()
REAL_OUT, REAL_ERR, REAL_WOUT = sys.stdout, sys.stderr, W.out
REAL_RUNNING = WB.running_processes

print("=" * 70)
print("  T15  THE DESKTOP WINDOW")
print("=" * 70)


class Dialogs:
    """Every dialog answers yes and is remembered."""

    def __init__(self):
        self.calls = []
        self.answer = True

    def _ask(self, title="", text="", **k):
        self.calls.append(("ask", title, text))
        return self.answer

    def _show(self, title="", text="", **k):
        self.calls.append(("show", title, text))

    askyesno = askretrycancel = _ask
    showinfo = showerror = showwarning = _show

    def shown(self, word):
        return any(word in t or word in x for kind, t, x in self.calls if kind == "show")


DLG = Dialogs()
GUI.messagebox = DLG
WB.running_processes = lambda: []

# ---- the sandbox -----------------------------------------------------------
box = Sandbox(BOX)
A.MANIFESTS = box.manifests
A.HERE = box.tools
A.BACKUPS = os.path.join(box.tools, "manifest_backups")
A.WJ.ARCHIVE_ROOT = box.archives
CFG.ARCHIVE_ROOT = box.archives
G.HERE = box.tools
CFG.settings_path = lambda: os.path.join(box.tools, "wj_settings.json")
box.manifest("Keep", GAME, [("k.7z", 5, "nexus"), ("shared.7z", 20, "nexus"),
                            ("nowhere.7z", 9, "nexus")])      # nobody holds this
box.manifest("Doomed", GAME, [("d.7z", 50, "nexus"), ("shared.7z", 20, "nexus")])
box.pool_file(GAME, "Keep", "k.7z", 5)
box.pool_file(GAME, "Doomed", "d.7z", 50)
box.pool_file(GAME, "Doomed", "shared.7z", 20)
box.pool_file(GAME, "Master", "junk.7z", 999)
box.pool_file(GAME, "TTW", "hand-built.7z", 700)
box.pool_file(GAME, "Trash", "old.7z", 40)
A.WJ.set_known_list_dirs(A.WJ.known_list_dirs_from(box.manifests))
A.WJ.invalidate_existing_cache()

# A one-list gallery, served entirely from the sandbox's cache: no network.
feed = {"repo": "r", "key": "r/one", "machine": "one", "title": "One List",
        "game": "fallout4", "version": "1.0", "updated": "", "download": "https://x/one",
        "hash": "AAAAAAAAAAA=", "size": 1000, "archives": 1, "arch_size": 5,
        "force_down": False, "nsfw": False, "official": False, "tags": []}
A.reset_feed_cache()
A._FEED_CACHE["all"] = {"r/one": feed}
A._FEED_CACHE["complete"] = True
G._write_cache(G.SUMMARY_PATH, json.dumps([{"machineURL": "r/one", "failed": 0, "passed": 1,
                                             "updating": 0, "mirrored": 0,
                                             "modlist_missing": False, "has_failures": False}]).encode())
G._write_cache(G.FEATURED_PATH, b"[]")
G._write_cache(G.DISABLED_PATH, b"[]")
G._write_cache(G.report_rel("r/one"), json.dumps(
    {"MachineURL": "one", "Name": "One List", "Version": "1.0", "Status": "Available",
     "Failures": 0, "Archives": [{"Status": "Valid", "Original": {
         "Name": "k.7z", "Size": 5, "Hash": "A=", "Meta": "",
         "State": {"$type": "NexusDownloader, Wabbajack.Lib"}}}]}).encode())

# ---- the window ------------------------------------------------------------
root = tk.Tk()
root.withdraw()
app = GUI.App(root, None)
sys.stdout, sys.stderr = REAL_OUT, REAL_ERR          # the window took them; we need them


def settle(timeout=120):
    """Pump the queue until no job is running, as mainloop would."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        app._pump()
        if not app.busy:
            app._pump()
            if not app.busy:
                return True
        time.sleep(0.05)
    return False


def log_text():
    return app.log.get("1.0", "end")


def rows(table):
    t = table.tree
    return [t.item(i, "values") for i in t.get_children()]


# ---------------------------------------------------------------- 15.1
print("\n 15.1  it starts, and says what it found")
check("start-up finished", settle(), "still busy")
check("the manifests were read", sorted(app.records) == ["Doomed.wabbajack", "Keep.wabbajack"],
      str(sorted(app.records)))
check("  and the log pane says so", "manifest(s) in" in log_text(), log_text()[-300:])
check("the first tab loaded itself", "Browse" in app.tab_seen)
check("no dialog was needed to start", not any(k == "show" for k, _t, _x in DLG.calls),
      str(DLG.calls))

# ---------------------------------------------------------------- 15.2
print("\n 15.2  Browse")
check("the gallery loaded from the cache", len(app.browse["rows"]) == 1, app.browse_summary.get())
check("  the game list has our pooled game", app.browse_game["values"] and
      "Fallout 4" in app.browse_game["values"][0], str(app.browse_game["values"]))
check("  and the table shows the list with its overlap",
      len(rows(app.browse_table)) == 1 and rows(app.browse_table)[0][2] == "One List"
      and rows(app.browse_table)[0][5] == "100.0%", str(rows(app.browse_table)))
app.browse_table.tree.selection_set("0")
app.browse_detail()
check("selecting a row fills the detail pane",
      "One List" in app.browse_text.get("1.0", "end") and "already yours 1" in app.browse_text.get("1.0", "end"),
      app.browse_text.get("1.0", "end")[:200])

# ---------------------------------------------------------------- 15.3
print("\n 15.3  Updates")
app.updates_check()
settle()
check("with nothing tracked it says so, rather than crashing",
      "Could not read" in app.updates_summary.get() or "0 of 0" in app.updates_summary.get(),
      app.updates_summary.get())

# ---------------------------------------------------------------- 15.4
print("\n 15.4  Incomplete and Prepare")
app.status_refresh()
settle()
inc = {r[0]: r for r in rows(app.incomplete_table)}
check("both lists are measured", set(inc) == {"Doomed", "Keep"}, str(inc))
# Doomed's copy of shared.7z satisfies Keep too: a right-size copy anywhere
# in the pools counts as present. Only nowhere.7z is missing.
check("Keep is incomplete (one archive nobody holds), Doomed complete",
      inc["Keep"][6] == "incomplete" and inc["Keep"][2] == "1"
      and inc["Doomed"][6] == "complete", str(inc))
check("the Prepare table has the same two", len(rows(app.prepare_table)) == 2,
      str(rows(app.prepare_table)))

# ---------------------------------------------------------------- 15.5
print("\n 15.5  Space, and the destructive flows through the buttons")
app.space_measure()
settle()
check("manifests, masters, strays and trash are all measured",
      len(app.space["lists"]) == 2 and len(app.space["masters"]) == 1
      and [s[1] for s in app.space["strays"]] == ["TTW"] and len(app.space["trash"]) == 1,
      str((len(app.space["lists"]), len(app.space["masters"]), app.space["strays"], app.space["trash"])))
check("  the trash note names it", "1 file(s)" in app.trash_note.get(), app.trash_note.get())

# delete Doomed (row 0: lists sort by filename)
check("Doomed is row 0", app.space["lists"][0]["file"] == "Doomed.wabbajack")
app.lists_table.tree.selection_set("0")
n_before = len(DLG.calls)
app.space_delete()
settle()
check("deleting asked first", any(k == "ask" and "Delete" in t for k, t, _x in DLG.calls[n_before:]),
      str(DLG.calls[n_before:]))
check("the manifest is gone", not os.path.exists(os.path.join(box.manifests, "Doomed.wabbajack")))
check("its archive went to Master", os.path.isfile(os.path.join(box.archives, GAME, "Master", "d.7z")))
check("the shared build was pulled to Keep by the rebuild",
      os.path.isfile(os.path.join(box.archives, GAME, "Keep", "shared.7z")),
      str(box.where("shared.7z")))
check("the tables were re-measured", len(app.space["lists"]) == 1, str(app.space["lists"]))

# clear Master under the narrow rule
app.masters_table.tree.selection_set("0")
n_before = len(DLG.calls)
app.space_clear()
settle()
trash = os.path.join(box.archives, GAME, "Trash")
check("clearing asked first, naming the trash",
      any(k == "ask" and "Trash" in x for k, _t, x in DLG.calls[n_before:]), str(DLG.calls[n_before:]))
check("the unwanted files went to the trash, not away",
      sorted(os.listdir(trash)) == ["d.7z", "junk.7z", "old.7z"], str(sorted(os.listdir(trash))))
check("  and the wanted file stayed", os.path.isfile(os.path.join(box.archives, GAME, "Keep", "k.7z")))

# park the stray
app.strays_table.tree.selection_set("0")
app.space_park()
settle()
check("the stray was parked intact",
      os.path.isfile(os.path.join(box.archives, GAME, CFG.UNMANAGED_FOLDER, "TTW", "hand-built.7z")))
check("  and is no longer a stray", app.space["strays"] == [], str(app.space["strays"]))

# empty the trash
app.space_empty_trash()
settle()
check("emptying the trash removes everything in it", os.listdir(trash) == [], str(os.listdir(trash)))
check("  and a purge record was written",
      any(f.startswith("purged_trash_") for f in os.listdir(box.tools)), str(os.listdir(box.tools)))

# refusals still hold in the window
DLG.calls.clear()
app.lists_table.tree.selection_set(())
app.space_delete()
check("nothing selected is a message, not an action", DLG.shown("Select"), str(DLG.calls))

# ---------------------------------------------------------------- 15.6
print("\n 15.6  Settings round-trip, against the sandbox's file")
app.settings_fill()
check("the form shows the current values", app.setting_vars["NEXUS_MODE"].get() == CFG.NEXUS_MODE)
app.setting_vars["NEXUS_MODE"].set("assist")
app.setting_vars["TRASH_KEEP_DAYS"].set("7")
app.setting_vars["EXCLUDE"].set("A/b, C/d")
app.setting_vars["GAME_FOLDERS"].delete("1.0", "end")
app.setting_vars["GAME_FOLDERS"].insert("1.0", '{"DarkSouls3": "DARK SOULS 3"}')
DLG.calls.clear()
app.settings_save()
p = CFG.settings_path()
check("the settings file was written in the sandbox", os.path.isfile(p) and box.tools in p, p)
saved = json.load(io.open(p, encoding="utf-8"))
check("  holding only what differs from the defaults",
      set(saved) >= {"NEXUS_MODE", "TRASH_KEEP_DAYS", "EXCLUDE", "GAME_FOLDERS", "ARCHIVE_ROOT"}
      and "REQUIRE_HASH" not in saved, str(sorted(saved)))
check("  with the right values", saved["NEXUS_MODE"] == "assist" and saved["TRASH_KEEP_DAYS"] == 7
      and saved["EXCLUDE"] == ["A/b", "C/d"] and saved["GAME_FOLDERS"] == {"DarkSouls3": "DARK SOULS 3"},
      str(saved))
check("the running process took them at once",
      CFG.NEXUS_MODE == "assist" and A.NEXUS_MODE == "assist" and app.assist
      and CFG.game_folder("DarkSouls3") == "DARK SOULS 3" and CFG.game_folder("Skyrim") == "Skyrim",
      str((CFG.NEXUS_MODE, A.NEXUS_MODE, CFG.game_folder("DarkSouls3"))))
check("  without a warning", not DLG.shown("kept their defaults"), str(DLG.calls))
app.setting_vars["TRASH_KEEP_DAYS"].set("thirty")
DLG.calls.clear()
app.settings_save()
check("a bad value is refused with a message, and nothing is saved",
      DLG.shown("Not saved") and json.load(io.open(p, encoding="utf-8"))["TRASH_KEEP_DAYS"] == 7,
      str(DLG.calls))
check("the real settings file was never touched",
      not os.path.exists(os.path.join(REPO, "wj_settings.json"))
      or json.load(io.open(os.path.join(REPO, "wj_settings.json"), encoding="utf-8")).get("TRASH_KEEP_DAYS") != 7)

# ---------------------------------------------------------------- 15.7
print("\n 15.7  a running Wabbajack blocks moves until it is closed")
WB.running_processes = lambda: [(1234, "Wabbajack.exe")]
DLG.answer = False
DLG.calls.clear()
check("with Wabbajack running and 'cancel', nothing proceeds", app.wabbajack_clear("Test") is False)
check("  and the dialog named the process",
      any("Wabbajack.exe" in x and "pid 1234" in x for _k, _t, x in DLG.calls), str(DLG.calls))
DLG.answer = True
WB.running_processes = lambda: []
check("with it closed, it proceeds", app.wabbajack_clear("Test") is True)

# ---------------------------------------------------------------- 15.8
print("\n 15.8  a job that raises is reported, and the window recovers")
DLG.calls.clear()
app.run_job("Boom", lambda: 1 / 0)
settle()
check("the error is shown", DLG.shown("Boom"), str(DLG.calls))
check("  and logged with its traceback", "ZeroDivisionError" in log_text())
check("  and the window is no longer busy", not app.busy)
check("a second job cannot start while one runs",
      (app.run_job("Slow", lambda: time.sleep(0.5)) is True) and (app.run_job("Two", lambda: 1) is False))
settle()

# ---------------------------------------------------------------- 15.9
print("\n 15.9  browsing a game you keep no archives for, and giving it a pool")


class Ask:
    """The folder prompt. Answers whatever the test set, and remembers."""

    def __init__(self):
        self.answer = None
        self.prompts = []

    def askstring(self, title="", prompt="", **k):
        self.prompts.append(prompt)
        return self.answer


ASK = Ask()
GUI.simpledialog = ASK

for key, machine, title, game in (("r/star", "star", "Star List", "starfield"),
                                  ("r/obl", "obl", "Obl List", "oblivion")):
    A._FEED_CACHE["all"][key] = dict(feed, key=key, machine=machine, title=title,
                                     game=game, archives=2, arch_size=500)
G._write_cache(G.report_rel("r/star"), json.dumps(
    {"MachineURL": "star", "Name": "Star List", "Version": "1.0", "Status": "Available",
     "Failures": 0, "Archives": [
         {"Status": "Valid", "Original": {"Name": "s1.7z", "Size": 100, "Hash": "A=", "Meta": "",
                                          "State": {"$type": "NexusDownloader, Wabbajack.Lib"}}},
         {"Status": "Valid", "Original": {"Name": "s2.7z", "Size": 400, "Hash": "B=", "Meta": "",
                                          "State": {"$type": "NexusDownloader, Wabbajack.Lib"}}}]}).encode())

app.browse_all.set(False)
app.browse_load()
settle()
check("games with no pool are hidden by default",
      list(app.browse["gameinfo"]) == ["fallout4"], str(list(app.browse["gameinfo"])))

app.browse_all.set(True)
app.browse_load()
settle()
check("ticking 'every game' brings them in",
      "starfield" in app.browse["gameinfo"] and "oblivion" in app.browse["gameinfo"],
      str(list(app.browse["gameinfo"])))
check("  they are marked as having no pool",
      sum("[no pool]" in v for v in app.browse_game["values"]) == 2,
      str(app.browse_game["values"]))
check("  and the game you do keep still comes first",
      app.browse["games"][0] == "fallout4", str(app.browse["games"]))

app.browse_game.current(app.browse["games"].index("starfield"))
app.browse_show_game()
settle()
r = rows(app.browse_table)
check("its lists are measured against nothing owned",
      len(r) == 1 and r[0][5] == "0.0%", str(r))
check("  and the pane explains what a pool would do",
      "no pool for this game" in app.browse_text.get("1.0", "end"),
      app.browse_text.get("1.0", "end")[:160])

ASK.answer = "Starfield"
DLG.calls.clear()
app.browse_make_pool()
settle()
pool = os.path.join(box.archives, "Starfield")
check("the pool is laid out on the share", os.path.isdir(pool))
check("  with the same folders every other game has",
      sorted(os.listdir(pool)) == ["Master", "Shared", "Trash", "Unmanaged"],
      str(sorted(os.listdir(pool))))
check("  the window says where it is", DLG.shown("Starfield"), str(DLG.calls))
check("  and the game now reads as pooled",
      (app.browse["gameinfo"].get("starfield") or (None, None))[1] is not None,
      str(app.browse["gameinfo"].get("starfield")))

DLG.calls.clear()
before = sorted(os.listdir(box.archives))
app.browse_game.current(app.browse["games"].index("starfield"))
app.browse_show_game()
settle()
app.browse_make_pool()
settle()
check("asking again only says where it already is", DLG.shown("already draws from"),
      str(DLG.calls))
check("  and no second folder is made", sorted(os.listdir(box.archives)) == before)

ASK.answer = "Master"
DLG.calls.clear()
app.browse_game.current(app.browse["games"].index("oblivion"))
app.browse_show_game()
settle()
app.browse_make_pool()
settle()
check("a reserved folder name is refused", DLG.shown("reserved"), str(DLG.calls))
check("  before anything is created",
      not os.path.isdir(os.path.join(box.archives, "Master")))

ASK.answer = None
DLG.calls.clear()
app.browse_make_pool()
settle()
check("cancelling the prompt does nothing at all",
      not DLG.calls and not os.path.isdir(os.path.join(box.archives, "Oblivion")),
      str(DLG.calls))

# ---------------------------------------------------------------- 15.10
print("\n 15.10  a stray that is really a list, and the orphaned sidecars")

box.pool_file(GAME, "Gone_@@_gone", "x.7z", 11)
with io.open(os.path.join(box.manifests, "Gone_@@_gone.wabbajack.metadata"),
             "w", encoding="utf-8") as fh:
    json.dump({"title": "A List That Went Missing", "game": GAME}, fh)
A._FEED_CACHE["all"]["Gone/gone"] = dict(feed, key="Gone/gone", machine="gone",
                                         title="A List That Went Missing", repo="Gone")
A.WJ.set_known_list_dirs(A.WJ.known_list_dirs_from(box.manifests))
A.WJ.invalidate_existing_cache()
app.space_measure()
settle()

stray_rows = rows(app.strays_table)
row = [r for r in stray_rows if r[1] == "Gone_@@_gone"]
check("the stray is listed", len(row) == 1, str(stray_rows))
check("  and its row says where it came from",
      row and "adopt it" in row[0][4], str(row))
note = app.stray_note.get()
check("the pane names the list rather than the folder",
      "A List That Went Missing" in note, note[:180])
check("  and says not to park it", "Do not park" in note, note[:180])
check("the orphaned sidecar is reported at last",
      "sidecar(s) whose modlist is gone" in note, note[:240])

# The window used to gather this and drop it on the floor.
check("nothing measured is left unshown",
      all(k in ("wide",) or app.space.get(k) is not None
          for k in ("lists", "masters", "strays", "trash", "orphans")),
      str(sorted(app.space)))

# ---------------------------------------------------------------- 15.11
print("\n 15.11  Remove leftovers, through the button")

junk = os.path.join(box.archives, GAME, "Keep", ".DS_Store")
with io.open(junk, "wb") as fh:
    fh.write(b"junk")
key = os.path.join(box.tools, "nexus_api_key.txt")
with io.open(key, "wb") as fh:
    fh.write(b"secret")
kept_archive = os.path.join(box.archives, GAME, "Keep", "k.7z")

DLG.calls.clear()
app.space_leftovers()
settle()
check("the leftover is gone", not os.path.exists(junk))
check("  the archive beside it is not", os.path.exists(kept_archive))
check("  and neither is the API key", os.path.exists(key))
check("  the window asked before deleting", DLG.calls, str(DLG.calls))

DLG.calls.clear()
app.space_leftovers()
settle()
check("a second press says there is nothing to do", DLG.shown("Nothing left over"),
      str(DLG.calls))

# ---------------------------------------------------------------- 15.12
print("\n 15.12  a tab is rebuilt when what it reads has moved on")

app.tab_filled(*GUI.TAB_DEPENDS)
check("with nothing changed, no tab needs rebuilding",
      not any(app.tab_is_stale(n) for n in GUI.TAB_DEPENDS),
      str([n for n in GUI.TAB_DEPENDS if app.tab_is_stale(n)]))

app.data_changed("records")
check("a manifest change reaches Updates", app.tab_is_stale("Updates"))
check("  and Browse, which shows what you already own", app.tab_is_stale("Browse"))
check("  and Incomplete", app.tab_is_stale("Incomplete"))
check("  but not Settings", not app.tab_is_stale("Settings"))

app.tab_filled(*GUI.TAB_DEPENDS)
app.data_changed("pools")
check("moving archives reaches Space", app.tab_is_stale("Space"))
check("  and Prepare", app.tab_is_stale("Prepare"))
check("  but not Updates, which only reads the manifests",
      not app.tab_is_stale("Updates"))

print("\n      the reported bug: delete a modlist, Updates still offers it")
box.manifest("Spare", GAME, [("sp.7z", 4, "nexus")])
box.pool_file(GAME, "Spare", "sp.7z", 4)
A.WJ.set_known_list_dirs(A.WJ.known_list_dirs_from(box.manifests))
app.reload_records()
app.space_measure()
settle()
app.tab_filled(*GUI.TAB_DEPENDS)              # every tab is showing the world as it is
spare = [i for i, x in enumerate(app.space["lists"])
         if W.stem_of(x["file"]) == "Spare"]
check("Spare is on the Space tab", len(spare) == 1, str([x["file"] for x in app.space["lists"]]))
app.lists_table.tree.selection_set(str(spare[0]))
app.space_delete()
settle()
check("the manifest is gone", not os.path.exists(os.path.join(box.manifests, "Spare.wabbajack")))
check("Updates is now stale, so it cannot still offer it", app.tab_is_stale("Updates"))
check("  Incomplete and Prepare too",
      app.tab_is_stale("Incomplete") and app.tab_is_stale("Prepare"))
check("  while Space, which refreshed itself, is current",
      not app.tab_is_stale("Space"))

print("\n      the pool index survives a change that cannot have moved it")
app.browse_load()
settle()
kept = app.browse["indexes"]
app.data_changed("records")
app.browse_load()
settle()
check("a manifest change keeps the pool index", app.browse["indexes"] is kept)
app.data_changed("pools")
app.browse_load()
settle()
check("  and a change to the archives drops it", app.browse["indexes"] is not kept)

print("\n      a running job holds you on the tab you started it from")
current = app.nb.select()
app.run_job("Slow", lambda: time.sleep(0.3))
others = [app.nb.tab(t, "state") for t in app.nb.tabs() if t != current]
check("every other tab is locked while it runs", set(others) == {"disabled"}, str(others))
check("  and the one you are on is not", app.nb.tab(current, "state") == "normal")
settle()
check("  all of them unlock when it finishes",
      {app.nb.tab(t, "state") for t in app.nb.tabs()} == {"normal"},
      str([app.nb.tab(t, "state") for t in app.nb.tabs()]))

# ---- put everything back ---------------------------------------------------
root.destroy()
W.out = REAL_WOUT
A.set_output_hook(None)
WB.running_processes = REAL_RUNNING
CFG.apply_settings(CFG._DEFAULTS)
CFG.apply_settings(SAVED_SETTINGS)
A.reload_settings()
shutil.rmtree(BOX, ignore_errors=True)
sys.exit(summary())
