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
from harness import Sandbox, check, summary, quiet_logging

sys.path.insert(0, REPO)
os.chdir(REPO)
import tkinter as tk
import wj_autoupdate as A
quiet_logging(A)
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
# The window is built from the shipped defaults, not from whatever the real
# wj_settings.json says today: a list hidden or a theme chosen in the real
# window must not change what these checks see. SAVED_SETTINGS goes back at
# the end.
CFG.apply_settings(CFG._DEFAULTS)
CFG.ARCHIVE_ROOT = box.archives
A.reload_settings()
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
check("the window opens on Start, and Browse did not load itself",
      app.nb.tab(app.nb.select(), "text") == "Start" and "Start" in app.tab_seen
      and "Browse" not in app.tab_seen, str(sorted(app.tab_seen)))
check("  the modlists card counts the manifests without asking the share",
      "2 manifest(s)" in app.cards["records"]["body"].get(), app.cards["records"]["body"].get())
check("  the expensive cards say they have not measured yet",
      "Not checked yet" in app.cards["updates"]["body"].get()
      and "not measured yet" in app.cards["space"]["body"].get(), app.cards["space"]["body"].get())
check("  and the session was stamped in the sandbox",
      os.path.isfile(os.path.join(box.runtime, GUI.OVERVIEW_FILE))
      and GUI.load_overview().get("session"), str(GUI.load_overview()))
check("no dialog was needed to start", not any(k == "show" for k, _t, _x in DLG.calls),
      str(DLG.calls))

# ---------------------------------------------------------------- 15.2
print("\n 15.2  Browse")
app.browse_load()
settle()
check("the gallery loaded from the cache", len(app.browse["rows"]) == 1, app.browse_summary.get())
check("  the game list has our pooled game", app.browse_game["values"] and
      "Fallout 4" in app.browse_game["values"][0], str(app.browse_game["values"]))
check("  and the table shows the list with its overlap",
      len(rows(app.browse_table)) == 1 and rows(app.browse_table)[0][2] == "One List"
      and rows(app.browse_table)[0][3] == "Fallout 4"
      and rows(app.browse_table)[0][6] == "100.0%", str(rows(app.browse_table)))
check("  the Start tab's gallery card now knows the count",
      (GUI.load_overview().get("gallery") or {}).get("published") == 1,
      str(GUI.load_overview().get("gallery")))
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
      any(f.startswith("purged_trash_") for f in box.runtime_files()), str(box.runtime_files()))

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
# An emptied field is saved as null, and an emptied TRASH_FOLDER used to be
# written that way and stop every tool starting; Master as the trash would
# have been emptied at the next start.
app.setting_vars["TRASH_KEEP_DAYS"].set("7")
app.setting_vars["TRASH_FOLDER"].set("")
app.setting_vars["UNMANAGED_FOLDER"].set("Master")
DLG.calls.clear()
app.settings_save()
saved = json.load(io.open(p, encoding="utf-8"))
check("an emptied or dangerous folder setting is never written",
      "TRASH_FOLDER" not in saved and "UNMANAGED_FOLDER" not in saved
      and saved.get("NEXUS_MODE") == "assist", str(saved))
check("  the window says which settings kept their defaults",
      DLG.shown("kept their defaults") and DLG.shown("TRASH_FOLDER")
      and DLG.shown("UNMANAGED_FOLDER"), str(DLG.calls))
check("  the running process has the defaults", CFG.TRASH_FOLDER == "Trash"
      and CFG.UNMANAGED_FOLDER == "Unmanaged", str((CFG.TRASH_FOLDER, CFG.UNMANAGED_FOLDER)))
check("  and the form shows them again", app.setting_vars["TRASH_FOLDER"].get() == "Trash"
      and app.setting_vars["UNMANAGED_FOLDER"].get() == "Unmanaged")

# The API key section, against a stand-in for Credential Manager: this
# suite never reaches the key the machine's account keeps.
print("\n 15.6b  the Nexus API key section")
import wj_keystore as KS


class KeyStore:
    def __init__(self):
        self.data = {}

    def read(self, t):
        return self.data.get(t)

    def write(self, t, s, u):
        self.data[t] = s

    def delete(self, t):
        return self.data.pop(t, None) is not None


keys = KeyStore()
KS.STORE = keys
keyfile = os.path.join(box.tools, KS.KEY_FILE)
io.open(keyfile, "w", encoding="utf-8").write("gui-test-key\n")
app.settings_fill()
check("the section says the key is in the file, in plain text",
      "plain text" in app.key_status.get() and "gui-test-key" not in app.key_status.get(),
      app.key_status.get())
check("  and offers to move it", str(app.key_move_btn.cget("state")) == "normal"
      and str(app.key_forget_btn.cget("state")) == "disabled")
DLG.calls.clear()
app.key_move_in()
check("moving asks first, then stores it and deletes the file",
      any(k == "ask" for k, _t, _x in DLG.calls) and keys.data.get(KS.TARGET) == "gui-test-key"
      and not os.path.exists(keyfile), str(DLG.calls))
check("  and the section now says Credential Manager",
      "Credential Manager, as" in app.key_status.get()
      and str(app.key_move_btn.cget("state")) == "disabled"
      and str(app.key_forget_btn.cget("state")) == "normal", app.key_status.get())
REAL_ASK = GUI.simpledialog.askstring
GUI.simpledialog.askstring = lambda *a, **k: "typed-key"
app.key_set()
GUI.simpledialog.askstring = REAL_ASK
check("a key typed in is stored", keys.data.get(KS.TARGET) == "typed-key")
check("  and never reaches the log", "typed-key" not in log_text())
app.key_forget_stored()
check("forgetting it empties the store", KS.TARGET not in keys.data)
check("  and the section says so", "not set" in app.key_status.get(), app.key_status.get())
KS.STORE = None

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
      len(r) == 1 and r[0][6] == "0.0%", str(r))
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

# ---------------------------------------------------------------- 15.13
print("\n 15.13  hiding a list from the gallery, and bringing it back")


def settings_file():
    return json.load(io.open(CFG.settings_path(), encoding="utf-8"))


app.browse_all.set(False)
app.browse_load()
settle()
app.browse_game.current(app.browse["games"].index("fallout4"))
app.browse_show_game()
settle()
idx = [i for i, r in enumerate(app.browse["grp"]) if r["key"] == "r/one"]
check("One List is in the Fallout 4 view", len(idx) == 1, str([r["key"] for r in app.browse["grp"]]))
app.browse_table.tree.selection_set(str(idx[0]))
app.browse_detail()
check("  and the pane offers to hide it",
      str(app.browse_hide.cget("state")) == "normal" and app.browse_hide.cget("text") == "Hide this list",
      str((app.browse_hide.cget("state"), app.browse_hide.cget("text"))))
n_rows = len(app.browse["rows"])
app.browse_toggle_hidden()
settle()
check("HIDDEN names it", [k.lower() for k in CFG.HIDDEN] == ["r/one"], str(CFG.HIDDEN))
check("  in the sandbox's settings file", settings_file().get("HIDDEN") == ["r/one"],
      str(settings_file().get("HIDDEN")))
check("it left the game's rows, which is what a report sync fetches for",
      len(app.browse["rows"]) == n_rows - 1 and all(r["key"] != "r/one" for r in app.browse["rows"]),
      str([r["key"] for r in app.browse["rows"]]))
check("  the Game menu gained a Hidden entry",
      any(v.startswith("Hidden lists") for v in app.browse_game["values"]), str(app.browse_game["values"]))
check("  the summary counts it", "1 hidden" in app.browse_summary.get(), app.browse_summary.get())
check("  and so does the Start tab's card",
      (GUI.load_overview().get("gallery") or {}).get("hidden") == 1, str(GUI.load_overview().get("gallery")))
check("  the Settings tab is due a refresh", app.tab_is_stale("Settings"))
app.settings_fill()
check("  and shows the flag", app.setting_vars["HIDDEN"].get() == "r/one", app.setting_vars["HIDDEN"].get())

app.browse_game.current(app.browse["games"].index(GUI.HIDDEN_GAME))
app.browse_show_game()
settle()
r = rows(app.browse_table)
check("the Hidden view lists it, with the feed's figures and no measurement",
      len(r) == 1 and r[0][2] == "One List" and r[0][6] == "-", str(r))
app.browse_table.tree.selection_set("0")
app.browse_detail()
check("  tracking is not offered while hidden", str(app.browse_track.cget("state")) == "disabled")
check("  unhiding is",
      app.browse_hide.cget("text") == "Unhide this list" and str(app.browse_hide.cget("state")) == "normal")
check("  and the pane says what hidden means", "HIDDEN" in app.browse_text.get("1.0", "end"))
DLG.calls.clear()
app.browse_make_pool()
check("  a pool cannot be set up for 'Hidden lists'", DLG.shown("Pick a game"), str(DLG.calls))
app.browse_toggle_hidden()
settle()
check("unhiding clears the flag", CFG.HIDDEN == [], str(CFG.HIDDEN))
check("  and takes it out of the file", "HIDDEN" not in settings_file(), str(sorted(settings_file())))
check("  the list is back in its game", any(r["key"] == "r/one" for r in app.browse["rows"]))
check("  the Hidden entry is gone",
      not any(v.startswith("Hidden lists") for v in app.browse_game["values"]), str(app.browse_game["values"]))
check("  and the view fell back to a game", app.browse["view"] == "fallout4", str(app.browse["view"]))

print("\n      a tracked list cannot be hidden")
app.tracked["r/one"] = "One.wabbajack"
app.browse_load()
settle()
app.browse_game.current(app.browse["games"].index("fallout4"))
app.browse_show_game()
settle()
idx = [i for i, r in enumerate(app.browse["grp"]) if r["key"] == "r/one"]
app.browse_table.tree.selection_set(str(idx[0]))
app.browse_detail()
check("the button is disabled for it", str(app.browse_hide.cget("state")) == "disabled")
app.browse_toggle_hidden()
check("  and the handler refuses too", CFG.HIDDEN == [], str(CFG.HIDDEN))
CFG.HIDDEN = ["r/one"]                 # flagged by hand in the file, then tracked
app.browse_load()
settle()
check("a flag on a tracked list is ignored: the list stays in its game",
      any(r["key"] == "r/one" and not r["hidden"] for r in app.browse["rows"]) and not app.browse["hidden"],
      str([(r["key"], r["hidden"]) for r in app.browse["rows"]]))
CFG.HIDDEN = []
del app.tracked["r/one"]

# ---------------------------------------------------------------- 15.14
print("\n 15.14  dark mode, remembered in the settings file")
style = GUI.ttk.Style(root)
base = style.theme_use()
app.set_theme("dark")
check("dark mode is the clam theme", style.theme_use() == "clam", style.theme_use())
check("  the log pane follows", app.log.cget("background") == GUI.DARK["field"], str(app.log.cget("background")))
if sys.platform == "win32" and sys.getwindowsversion().build >= 17763:
    check("  and Windows was asked for a matching title bar", app.titlebar_ok is True,
          str(app.titlebar_ok))
check("  the switch shows it", app.dark_var.get() is True)
check("  and THEME is saved", settings_file().get("THEME") == "dark" and CFG.THEME == "dark",
      str(settings_file().get("THEME")))
app.set_theme("light")
check("light mode is the platform theme again", style.theme_use() == base, style.theme_use())
check("  with the log pane's own colours",
      app.log.cget("background") == app._text_defaults[app.log]["background"], str(app.log.cget("background")))
check("  and THEME, back at its default, is out of the file",
      CFG.THEME == "light" and "THEME" not in settings_file(), str(sorted(settings_file())))
app.settings_fill()
app.setting_vars["THEME"].set("dark")
app.settings_save()
check("saving THEME from the Settings tab applies it too",
      style.theme_use() == "clam" and app.dark_var.get() is True, style.theme_use())
app.setting_vars["THEME"].set("light")
app.settings_save()
check("  and back", style.theme_use() == base and app.dark_var.get() is False, style.theme_use())

# ---------------------------------------------------------------- 15.15
print("\n 15.15  the Start tab: cheap to open, remembering the expensive answers")
app.tab_filled(*GUI.TAB_DEPENDS)
app.start_refresh()
settle()
ov = GUI.load_overview()
check("Refresh everything measured the disk and the pools",
      "status" in ov and "space" in ov, str(sorted(ov)))
check("  filling those tabs on the way",
      not any(app.tab_is_stale(n) for n in ("Incomplete", "Prepare", "Space")),
      str([n for n in ("Incomplete", "Prepare", "Space") if app.tab_is_stale(n)]))
check("  and the file is what the window holds", ov == app.overview)
app.start_fill()
body = app.cards["status"]["body"].get()
check("the Incomplete card reads from it", "incomplete" in body and "complete" in body, body)
check("  saying when", app.cards["status"]["when"].get().startswith("measured today"),
      app.cards["status"]["when"].get())
check("  the Disk card names the pool",
      "Fallout 4" in app.cards["space"]["body"].get() and "Free on the archive volume" in app.cards["space"]["body"].get(),
      app.cards["space"]["body"].get())
app.tab_filled(*GUI.TAB_DEPENDS)
app.data_changed("overview")
check("a measurement makes Start stale, and nothing else",
      app.tab_is_stale("Start")
      and not any(app.tab_is_stale(n) for n in GUI.TAB_DEPENDS if n != "Start"),
      str([n for n in GUI.TAB_DEPENDS if app.tab_is_stale(n)]))

print("\n      a second window starts from the same file, without measuring")
root2 = tk.Tk()
root2.withdraw()
app2 = GUI.App(root2, None)
sys.stdout, sys.stderr = REAL_OUT, REAL_ERR
t0 = time.time()
while app2.busy and time.time() - t0 < 60:
    app2._pump()
    time.sleep(0.05)
sys.stdout, sys.stderr = REAL_OUT, REAL_ERR
check("it opens on Start with the last session's figures",
      "incomplete" in app2.cards["status"]["body"].get() and "Fallout 4" in app2.cards["space"]["body"].get(),
      app2.cards["status"]["body"].get())
check("  says when the last session was", app2.start_session.get().startswith("Last session: today"),
      app2.start_session.get())
check("  and loaded nothing expensive to do it",
      "Browse" not in app2.tab_seen and "Space" not in app2.tab_seen, str(sorted(app2.tab_seen)))
root2.destroy()
W.out = lambda msg="": app.q.put(("log", str(msg)))
A.set_output_hook(lambda line: app.q.put(("child", line)))

# ---------------------------------------------------------------- 15.16
print("\n 15.16  before an install, Wabbajack is checked for being there and current")
real_find, real_latest = WB.find_wabbajack, WB.latest_release
WB.find_wabbajack = lambda hint=None: {"dir": "stub-dir", "exe": r"stub-dir\Wabbajack.exe",
                                       "cli": None, "version": "4.2.2.1"}
WB.latest_release = lambda force=False: {"version": "4.2.3.0", "url": "https://x/rel", "published": ""}
try:
    app.status_refresh()
    settle()
    app._offer_install("Keep.wabbajack")
    settle()
    info = app.install_info.get()
    check("the panel names the installed and the newest version",
          "4.2.2.1" in info and "4.2.3.0" in info and "update is available" in info, info[:300])
    check("  with where to get it", "https://x/rel" in info, info[:300])
    check("  the Start tab remembers the verdict",
          (GUI.load_overview().get("wabbajack") or {}).get("outdated") is True,
          str(GUI.load_overview().get("wabbajack")))
    app.start_fill()
    check("  and shows it", "update is available" in app.cards["settings"]["body"].get(),
          app.cards["settings"]["body"].get())
    DLG.answer = False
    DLG.calls.clear()
    app.install_ctx["mode"] = "gui"       # so only the version question stands between the click and Wabbajack
    app.prepare_install()
    check("installing asks first when Wabbajack is out of date",
          any(k == "ask" and "out of date" in t for k, t, _x in DLG.calls), str(DLG.calls))
    check("  and 'no' starts nothing", "Wabbajack is starting" not in log_text())
    DLG.answer = True
    WB.find_wabbajack = lambda hint=None: {"dir": "stub-dir", "exe": None, "cli": None,
                                           "version": "4.2.3.0"}
    app._offer_install("Keep.wabbajack")
    settle()
    info = app.install_info.get()
    check("a current install is reported as such",
          "newest release" in info and "update is available" not in info, info[:200])
    check("  and installing would not ask about it", not (app.install_ctx.get("update") or {}).get("outdated"))
    WB.find_wabbajack = lambda hint=None: None
    app._offer_install("Keep.wabbajack")
    settle()
    check("no Wabbajack at all is said plainly, and the button stays off",
          "not found" in app.install_info.get() and str(app.install_button.cget("state")) == "disabled",
          app.install_info.get()[:120])
finally:
    WB.find_wabbajack, WB.latest_release = real_find, real_latest
    DLG.answer = True

# ---------------------------------------------------------------- 15.17
print("\n 15.17  only lists that can be completed, on by default")


def rep_for(machine, title, archives, status="Available"):
    return json.dumps({"MachineURL": machine, "Name": title, "Version": "1.0", "Status": status,
                       "Failures": sum(1 for a in archives if a[2] != "Valid"),
                       "Archives": [{"Status": st, "Original": {
                           "Name": n, "Size": s, "Hash": "A=", "Meta": "",
                           "State": {"$type": "NexusDownloader, Wabbajack.Lib"}}}
                                    for n, s, st in archives]}).encode()


for key, machine, title in (("r/deadheld", "deadheld", "Dead But Held"),
                            ("r/deadmissing", "deadmissing", "Dead And Missing"),
                            ("r/needs", "needs", "Needs A Download"),
                            ("r/pulledx", "pulledx", "Pulled List")):
    A._FEED_CACHE["all"][key] = dict(feed, key=key, machine=machine, title=title, game="fallout4")
A._FEED_CACHE["all"]["r/pulledx"]["force_down"] = True
G._write_cache(G.report_rel("r/deadheld"), rep_for("deadheld", "Dead But Held",
                                                   [("k.7z", 5, "Valid"), ("held-dead.7z", 33, "Failed")]))
G._write_cache(G.report_rel("r/deadmissing"), rep_for("deadmissing", "Dead And Missing",
                                                      [("k.7z", 5, "Valid"), ("gone-dead.7z", 44, "Failed")]))
G._write_cache(G.report_rel("r/needs"), rep_for("needs", "Needs A Download",
                                                [("k.7z", 5, "Valid"), ("need.7z", 9, "Valid")]))
G._write_cache(G.report_rel("r/pulledx"), rep_for("pulledx", "Pulled List", [], "ForcedDown"))
box.pool_file(GAME, "Master", "held-dead.7z", 33)
A.WJ.invalidate_existing_cache()
app.data_changed("pools")
app.browse_all.set(False)
check("the toggle starts on", app.browse_only.get() is True)
app.browse_load()
settle()
app.browse_game.current(app.browse["games"].index("fallout4"))
app.browse_show_game()
settle()
titles = [r[2] for r in rows(app.browse_table)]
check("a list with nothing dead is shown", "One List" in titles, str(titles))
check("  so is one still missing an obtainable archive", "Needs A Download" in titles, str(titles))
check("  and one whose dead archive you already hold", "Dead But Held" in titles, str(titles))
check("a dead archive you do not hold hides the list", "Dead And Missing" not in titles, str(titles))
check("  and so does an empty report", "Pulled List" not in titles, str(titles))
shown_r, blocked_r, unknown_r = G.split_completable(app.browse["measured"])
check("  blocked is exactly the dead-and-missing list",
      [r["key"] for r in blocked_r] == ["r/deadmissing"], str([r["key"] for r in blocked_r]))
check("  unmeasurable are the pulled list and the one with no report",
      sorted(r["key"] for r in unknown_r) == ["Gone/gone", "r/pulledx"], str([r["key"] for r in unknown_r]))
note = app.browse_text.get("1.0", "end")
check("the pane says how many were kept out, and why",
      f"{len(blocked_r)} that cannot be completed" in note and f"{len(unknown_r)} unmeasurable" in note,
      note[:300])
total = sum(1 for r in app.browse["rows"] if r["game_key"] == "fallout4")
check("  the Game menu still counts every list",
      any(v.startswith("Fallout 4") and f"({total} lists)" in v for v in app.browse_game["values"]),
      str(app.browse_game["values"]))

app.browse_only.set(False)
app.browse_refilter()
check("flipping the toggle needs no job", not app.busy)
marks = {r[2]: r[1] for r in rows(app.browse_table)}
check("toggle off shows them all", "Dead And Missing" in marks and "Pulled List" in marks, str(sorted(marks)))
check("  with an x on the one that cannot be completed", marks["Dead And Missing"] == "x", str(marks))
check("  nothing on the one whose dead archive you hold", marks["Dead But Held"] == "", str(marks))
check("  and Wabbajack's own mark alone on the pulled list", marks["Pulled List"] == "!", str(marks))
check("  the legend explains the x", "cannot be completed" in app.browse_text.get("1.0", "end"))
idx = [r[2] for r in rows(app.browse_table)].index("Dead And Missing")
app.browse_table.tree.selection_set(str(idx))
app.browse_detail()
check("  and its details say so outright", "CANNOT BE COMPLETED" in app.browse_text.get("1.0", "end"),
      app.browse_text.get("1.0", "end")[:300])

app.browse_only.set(True)
app.tracked["r/deadmissing"] = "Dead.wabbajack"
app.browse_load()
settle()
app.browse_game.current(app.browse["games"].index("fallout4"))
app.browse_show_game()
settle()
check("a tracked list is shown whatever its state",
      "Dead And Missing" in [r[2] for r in rows(app.browse_table)], str([r[2] for r in rows(app.browse_table)]))
del app.tracked["r/deadmissing"]

# ---------------------------------------------------------------- 15.18
print("\n 15.18  searching Browse, and hiding NSFW lists")
A._FEED_CACHE["all"]["r/needs"].update(tags=["Survival", "Gameplay"], updated="2026-09-01T00:00:00")
A._FEED_CACHE["all"]["r/deadheld"].update(nsfw=True, tags=["Adult"])
app.browse_only.set(False)
app.browse_load()
settle()
app.browse_game.current(app.browse["games"].index("fallout4"))
app.browse_show_game()
settle()
table = {r[2]: r for r in rows(app.browse_table)}
check("the updated column gives the date", table["Needs A Download"][9] == "2026-09-01",
      str(table.get("Needs A Download")))
check("  and an NSFW list carries an n", "n" in table["Dead But Held"][1], str(table.get("Dead But Held")))
app.browse_search.set("survival")
check("a search narrows the table as it is typed, with no job",
      not app.busy and [r[2] for r in rows(app.browse_table)] == ["Needs A Download"],
      str([r[2] for r in rows(app.browse_table)]))
app.browse_search.set("")
check("  and clearing it brings every list back", len(rows(app.browse_table)) == len(table))
app.browse_nsfw.set(True)
app.browse_toggle_nsfw()
check("hiding NSFW lists takes it out of the table",
      "Dead But Held" not in [r[2] for r in rows(app.browse_table)])
check("  remembered as HIDE_NSFW, in the sandbox's settings file",
      CFG.HIDE_NSFW is True
      and json.load(io.open(CFG.settings_path(), encoding="utf-8")).get("HIDE_NSFW") is True)
check("  and the pane says how many it hid",
      "1 marked NSFW hidden" in app.browse_text.get("1.0", "end"), app.browse_text.get("1.0", "end")[:200])
app.browse_nsfw.set(False)
app.browse_toggle_nsfw()
check("unticking it brings the list back",
      "Dead But Held" in [r[2] for r in rows(app.browse_table)] and CFG.HIDE_NSFW is False)
idx = [r[2] for r in rows(app.browse_table)].index("Needs A Download")
app.browse_table.tree.selection_set(str(idx))
app.browse_detail()
detail = app.browse_text.get("1.0", "end")
check("the detail pane gives the date and the tags",
      "updated 2026-09-01" in detail and "tags: Survival, Gameplay" in detail, detail[:200])
app.browse_only.set(True)

# The console's browse stage, driven with typed answers: the same filter.
import builtins


def drive(fn, answers, *args):
    said, feed_in = [], iter(answers)
    real_in, real_out = builtins.input, W.out
    builtins.input = lambda prompt="": next(feed_in)
    W.out = lambda msg="": said.append(str(msg))
    try:
        fn(*args)
    except (StopIteration, W.Quit):
        pass
    finally:
        builtins.input, W.out = real_in, real_out
    return "\n".join(said)


text = drive(W.stage_browse, ["c", "1", "c", "/survival", "n", "n", "b", "s"],
             app.state, app.tracked)
after_search = text.split("matching 'survival'", 1)[-1]
check("in the console, /words searches the table",
      "matching 'survival'" in text and "Needs A Download" in after_search.split("[q] quit")[0]
      and "One List" not in after_search.split("[q] quit")[0], text[-1500:])
check("  [n] hides the NSFW lists, and says so", "marked NSFW are hidden" in text, text[-800:])
check("  and the second [n] shows them again, the setting following",
      CFG.HIDE_NSFW is False, str(CFG.HIDE_NSFW))

# ---------------------------------------------------------------- 15.19
print("\n 15.19  time and clicks, and sorting by them")
app.browse_only.set(False)
app.browse_refilter()
table = {r[2]: r for r in rows(app.browse_table)}
check("a list still missing a Nexus file shows its click and its time",
      table["Needs A Download"][10] == "1" and table["Needs A Download"][11] == "<1m",
      str(table["Needs A Download"]))
check("  one missing nothing shows neither", table["One List"][10] == "" and table["One List"][11] == "",
      str(table["One List"]))
check("  and one with no report shows nothing, rather than a zero",
      table["Pulled List"][11] == "", str(table["Pulled List"]))
app.browse_sort_by("time")
order = [r[2] for r in rows(app.browse_table)]
check("a click on the time heading sorts by it, quickest first",
      order.index("Needs A Download") < order.index("Dead And Missing") and order[-1] == "Pulled List",
      str(order))
check("  and marks the heading", app.browse_table.tree.heading("time")["text"].endswith("▲"))
app.browse_sort_by("time")
order = [r[2] for r in rows(app.browse_table)]
check("a second click reverses it, and the unmeasured list stays last",
      order.index("Dead And Missing") < order.index("Needs A Download") and order[-1] == "Pulled List"
      and app.browse_table.tree.heading("time")["text"].endswith("▼"), str(order))
idx = order.index("Needs A Download")
app.browse_table.tree.selection_set(str(idx))
app.browse_detail()
check("the detail pane says what fetching the rest costs",
      "Still to fetch: 1 from Nexus" in app.browse_text.get("1.0", "end")
      and "a click each" in app.browse_text.get("1.0", "end"), app.browse_text.get("1.0", "end")[:400])
app.browse["sort"] = None
app._browse_headings()
app.browse_only.set(True)
app.status_refresh()
settle()
inc = {r[0]: r for r in rows(app.incomplete_table)}
check("the Incomplete tab has a time for each list",
      all(len(r) == 8 for r in inc.values()) and inc["Keep"][7] == "<1m", str(inc))
check("  and says what all of it costs", "All of it:" in app.incomplete_note.cget("text"),
      app.incomplete_note.cget("text"))
check("  as the Start tab does", "of downloading" in app.cards["status"]["body"].get()
      or "under a minute" in app.cards["status"]["body"].get(), app.cards["status"]["body"].get())
text = drive(W.stage_browse, ["c", "1", "c", "t", "b", "s"], app.state, app.tracked)
check("in the console the table has clicks and time, and [t] puts the quickest first",
      "clicks" in text and "time" in text and "quickest to finish first" in text, text[-1200:])

# ---------------------------------------------------------------- 15.20
print("\n 15.20  putting a cleanup back, from the Space tab")
box.pool_file(GAME, "Master", "undo-me.7z", 77)
A.WJ.invalidate_existing_cache()
app.data_changed("pools")
app.space_measure()
settle()
app.rule.set("y")
app.masters_table.tree.selection_set("0")
app.space_clear()
settle()
trash = os.path.join(box.archives, GAME, "Trash")
check("clearing Master sent the file to the trash", os.path.isfile(os.path.join(trash, "undo-me.7z")),
      str(os.listdir(trash)))
app.space_undo()
check("the undo window lists that cleanup first",
      bool(app.undo_records) and rows(app.undo_table)
      and app.undo_records[0]["waiting"] >= 1, str(app.undo_records))
app.undo_table.tree.selection_set("0")
DLG.calls.clear()
app.undo_restore()
settle()
check("putting it back asks first", any(k == "ask" and "Put a cleanup back" in t
                                        for k, t, _x in DLG.calls), str(DLG.calls))
check("  and the file is back in Master",
      os.path.isfile(os.path.join(box.archives, GAME, "Master", "undo-me.7z")))
check("  and out of the trash", not os.path.exists(os.path.join(trash, "undo-me.7z")))
check("  and the Space tab was measured again", not app.busy and app.space["masters"])

# ---------------------------------------------------------------- 15.21
print("\n 15.21  an install Wabbajack finishes is remembered")
real = (WB.run_install, WB.find_wabbajack, WB.latest_release, WB.game_ready)
WB.run_install = lambda cli, manifest, downloads, into, on_line=None: 0
WB.find_wabbajack = lambda hint=None: {"dir": "stub-dir", "exe": None, "cli": "stub-cli",
                                       "version": "4.2.3.0"}
WB.latest_release = lambda force=False: {"version": "4.2.3.0", "url": "u", "published": ""}
WB.game_ready = lambda cli, game: (True, "found")
try:
    app.status_refresh()
    settle()
    app._offer_install("Keep.wabbajack")
    settle()
    into = os.path.join(box.root, "KeepInstall")
    app.install_path.set(into)
    DLG.calls.clear()
    app.prepare_install()
    settle()
    inst = (app.state.get("installed") or {}).get("Keep")
    check("the install is recorded where it went", bool(inst) and inst["path"] == into, str(inst))
    app.start_fill()
    check("  and the Start tab lists it", "installed with Wabbajack: 1" in app.cards["records"]["body"].get(),
          app.cards["records"]["body"].get())
finally:
    WB.run_install, WB.find_wabbajack, WB.latest_release, WB.game_ready = real

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
