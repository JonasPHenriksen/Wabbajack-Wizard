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
check("the first tab loaded itself", "Browse" in app.loaded)
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
