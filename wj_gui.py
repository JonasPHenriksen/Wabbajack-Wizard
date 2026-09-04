#!/usr/bin/env python3
"""
Wabbajack Wizard - the desktop window
=====================================
One window, the same five stages as the console wizard, driving the same
functions - wj_wizard.py's stage helpers, and through them wj_autoupdate.py -
so the two front ends cannot drift apart. This module only decides what to
show and what to ask.

Why Tkinter: it ships with the interpreter run.bat already uses, so the window
costs no new dependency and the tool keeps to its one third-party package.

How it stays responsive: every job that reads the share or runs a child script
runs on a worker thread, and everything the job wants to say comes back
through a queue the main thread drains ten times a second. Tk widgets are only
ever touched on the main thread. One job at a time: the pools are one thing,
and two jobs moving files at once is exactly what the lock exists to prevent.

Anything destructive asks first, in the same words the console does, and
nothing that moves archives starts while a Wabbajack process is running.
"""

import json
import logging
import os
import queue
import sys
import threading
import traceback
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import wj_autoupdate as A
import wj_wizard as W
import wj_gallery as G
import wj_wabbajack as WB
import wj_config as CFG

APP_TITLE = f"Wabbajack Wizard {CFG.__version__}"
POLL_MS = 100
LOG_LINES = 4000


# ----------------------------------------------------------------------
# Getting text from worker threads to the window
# ----------------------------------------------------------------------
class QueueHandler(logging.Handler):
    """A logging handler that posts each record to the window's queue."""

    def __init__(self, q):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            self.q.put(("log", self.format(record)))
        except Exception:
            pass


class QueueWriter:
    """A stand-in for stdout. The CDN downloader prints its progress straight
    to stdout with carriage returns; each piece becomes a queue item."""

    def __init__(self, q, kind):
        self.q, self.kind, self.buf = q, kind, ""

    def write(self, s):
        self.buf += s
        while True:
            cuts = [i for i in (self.buf.find("\n"), self.buf.find("\r")) if i >= 0]
            if not cuts:
                break
            i = min(cuts)
            piece, self.buf = self.buf[:i], self.buf[i + 1:]
            if piece.strip():
                self.q.put((self.kind, piece))
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False


def is_progress(line):
    """A download progress piece, better shown in the status bar than kept."""
    return "%" in line and ("/" in line or "MiB" in line or "MB" in line)


# ----------------------------------------------------------------------
# Widgets
# ----------------------------------------------------------------------
def make_table(parent, columns, height=8, selectmode="extended"):
    """A Treeview with headings, widths and a scrollbar. `columns` is a list
    of (id, heading, width, anchor)."""
    frame = ttk.Frame(parent)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings",
                        height=height, selectmode=selectmode)
    for cid, heading, width, anchor in columns:
        tree.heading(cid, text=heading, anchor=anchor)
        tree.column(cid, width=width, minwidth=40, anchor=anchor,
                    stretch=(cid == columns[0][0] or width >= 200))
    sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    sb.grid(row=0, column=1, sticky="ns")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    frame.tree = tree
    return frame


def fill(tree, rows):
    tree.delete(*tree.get_children())
    for i, values in enumerate(rows):
        tree.insert("", "end", iid=str(i), values=values)


def selected_indices(tree):
    return sorted(int(i) for i in tree.selection())


def human(n):
    return W.human(n)


# ----------------------------------------------------------------------
# The application
# ----------------------------------------------------------------------
class App:
    def __init__(self, root, lock_fd):
        self.root = root
        self.lock_fd = lock_fd
        self.q = queue.Queue()
        self.busy = False
        self.action_buttons = []
        self.state = A.load_state()
        self.tracked = {}
        self.records = {}
        self.failed_rebuilds = {}
        self.assist = (A.NEXUS_MODE == "assist")
        self.loaded = set()
        # per-tab data
        self.browse = {"rows": [], "pooled": {}, "indexes": {}, "games": [],
                       "grp": [], "detail": None}
        self.update_rows = []
        self.status_rows = []
        self.space = {"lists": [], "masters": [], "strays": [], "trash": [],
                      "wide": None}
        self.staged_fn = None

        root.title(APP_TITLE)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()
        self._route_output()
        root.after(POLL_MS, self._pump)
        self.run_job("Starting", self._startup, self._after_startup)

    # -- plumbing --------------------------------------------------------
    def _route_output(self):
        """Everything the stages print goes to the log pane."""
        W.out = lambda msg="": self.q.put(("log", str(msg)))
        A.set_output_hook(lambda line: self.q.put(("child", line)))
        handler = QueueHandler(self.q)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(message)s"))
        loggers = [A.LOG, A.WJ.LOG] + ([A.CDN.LOG] if getattr(A, "CDN", None) else [])
        for lg in loggers:
            # The console handler from bootstrap has nowhere to print under
            # pythonw; the file handler stays.
            for h in list(lg.handlers):
                if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                    lg.removeHandler(h)
            lg.addHandler(handler)
        sys.stdout = QueueWriter(self.q, "child")
        sys.stderr = QueueWriter(self.q, "log")

    def _pump(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append_log(item[1])
                elif kind == "child":
                    line = item[1]
                    if is_progress(line):
                        self.status.set(line.strip()[:140])
                    else:
                        self._append_log(line)
                elif kind == "done":
                    self._job_done(*item[1:])
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._pump)

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip("\n") + "\n")
        n = int(self.log.index("end-1c").split(".")[0])
        if n > LOG_LINES:
            self.log.delete("1.0", f"{n - LOG_LINES}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def run_job(self, title, fn, on_done=None, *args):
        """Run fn(*args) on a worker thread; on_done(result) on the main one."""
        if self.busy:
            messagebox.showinfo("Busy", "Another job is still running. Wait for it "
                                        "to finish first.", parent=self.root)
            return False
        self.busy = True
        self._set_busy(True, title)

        def work():
            try:
                result, exc = fn(*args), None
            except Exception as e:                       # shown, never swallowed
                result, exc = None, (e, traceback.format_exc())
            self.q.put(("done", title, result, exc, on_done))
        threading.Thread(target=work, daemon=True, name=title).start()
        return True

    def _job_done(self, title, result, exc, on_done):
        self.busy = False
        self._set_busy(False, f"{title}: done")
        if exc is not None:
            self._append_log(exc[1])
            messagebox.showerror(title, f"{title} failed:\n\n{exc[0]}\n\nThe "
                                        f"traceback is in the log pane.", parent=self.root)
            return
        if on_done is not None:
            on_done(result)

    def _set_busy(self, busy, title):
        self.status.set(title if busy else title)
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()
        for b in self.action_buttons:
            b.configure(state="disabled" if busy else "normal")

    def action(self, parent, text, command, **kw):
        b = ttk.Button(parent, text=text, command=command, **kw)
        self.action_buttons.append(b)
        return b

    def confirm(self, title, lines, total=None, total_label="Total to be freed",
                undo="This cannot be undone."):
        text = "\n".join(lines)
        if total is not None:
            text += f"\n\n{total_label}: {human(total)}"
        text += f"\n\n{undo}"
        return messagebox.askyesno(title, text, icon="warning", parent=self.root)

    def wabbajack_clear(self, what):
        """Refuse to move archives under a running Wabbajack. Retry until it
        is closed, or give up."""
        while True:
            procs = WB.running_processes()
            if not procs:
                return True
            names = ", ".join(f"{n} (pid {p})" for p, n in procs)
            if not messagebox.askretrycancel(
                    "Wabbajack is running",
                    f"{names}\n\n{what} moves archives Wabbajack may be reading. "
                    f"Close it, then Retry.", parent=self.root):
                return False

    def on_close(self):
        if self.busy and not messagebox.askyesno(
                "A job is running",
                "Closing now may leave it half done - a download resumes, but a "
                "sort in progress leaves the pools as they are. Close anyway?",
                parent=self.root):
            return
        A.release_lock(self.lock_fd)
        self.root.destroy()

    # -- layout ------------------------------------------------------------
    def _build(self):
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=3)
        root.rowconfigure(1, weight=1)

        self.nb = ttk.Notebook(root)
        self.nb.grid(row=0, column=0, sticky="nsew", padx=6, pady=(6, 0))
        self.tabs = {}
        for name, builder in (("Browse", self._build_browse),
                              ("Updates", self._build_updates),
                              ("Incomplete", self._build_incomplete),
                              ("Prepare", self._build_prepare),
                              ("Space", self._build_space),
                              ("Settings", self._build_settings)):
            frame = ttk.Frame(self.nb, padding=8)
            self.nb.add(frame, text=name)
            self.tabs[name] = frame
            builder(frame)
        self.nb.bind("<<NotebookTabChanged>>", lambda _e: self._tab_changed())

        bottom = ttk.Frame(root, padding=(6, 4))
        bottom.grid(row=1, column=0, sticky="nsew")
        bottom.columnconfigure(0, weight=1)
        bottom.rowconfigure(1, weight=1)
        head = ttk.Frame(bottom)
        head.grid(row=0, column=0, sticky="ew")
        ttk.Label(head, text="Log").pack(side="left")
        ttk.Button(head, text="Open log file", command=self._open_log_file).pack(side="right")
        ttk.Button(head, text="Clear", command=self._clear_log).pack(side="right", padx=(0, 4))
        self.log = tk.Text(bottom, height=9, wrap="none", state="disabled",
                           font=("Consolas", 9))
        lsb = ttk.Scrollbar(bottom, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.grid(row=1, column=0, sticky="nsew")
        lsb.grid(row=1, column=1, sticky="ns")

        status = ttk.Frame(root, padding=(6, 2))
        status.grid(row=2, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)
        self.status = tk.StringVar(value="")
        ttk.Label(status, textvariable=self.status, anchor="w").grid(row=0, column=0, sticky="ew")
        self.progress = ttk.Progressbar(status, mode="indeterminate", length=160)
        self.progress.grid(row=0, column=1, padx=(8, 0))
        ttk.Label(status, text=f"Manifests: {A.MANIFESTS}   Archives: {A.WJ.ARCHIVE_ROOT}"
                  f"   Nexus: {A.NEXUS_MODE}", anchor="e").grid(row=1, column=0, columnspan=2,
                                                                sticky="ew")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _open_log_file(self):
        path = os.path.join(A.HERE, "wj_autoupdate.log")
        try:
            os.startfile(path)
        except OSError as e:
            messagebox.showerror("Log file", f"Could not open {path}:\n{e}", parent=self.root)

    def _tab_changed(self):
        name = self.nb.tab(self.nb.select(), "text")
        if name in self.loaded or self.busy or not self.records and name != "Settings":
            if name not in self.loaded and not self.busy and name in ("Updates", "Incomplete", "Prepare", "Space"):
                pass
            return
        self.loaded.add(name)
        {"Browse": self.browse_load, "Updates": self.updates_check,
         "Incomplete": self.status_refresh, "Prepare": self.status_refresh,
         "Space": self.space_measure, "Settings": self.settings_fill}.get(name, lambda: None)()

    # -- start-up --------------------------------------------------------
    def _startup(self):
        purged, rec = A.purge_trash()
        if purged:
            W.out(f"Trash: {len(purged)} file(s) older than {W.keep_days()} days purged, "
                  f"{human(sum(r[2] for r in purged))} freed"
                  + (f" ({os.path.basename(rec)})" if rec else ""))
        self.tracked = A.resolve_tracking(self.state)
        self.records = W.load_records()
        return len(self.records)

    def _after_startup(self, n):
        W.out(f"{n} manifest(s) in {A.MANIFESTS}; following {len(self.tracked)} published list(s).")
        for p in CFG.SETTINGS_PROBLEMS:
            W.out(f"[WARN] {CFG.SETTINGS_FILE}: {p}")
        self.settings_fill()
        self.loaded.add("Settings")
        self._tab_changed()

    def reload_records(self):
        A.WJ.invalidate_existing_cache()
        W.refresh_known_dirs()
        self.records = W.load_records()

    # ==================================================================
    # Browse
    # ==================================================================
    def _build_browse(self, f):
        f.columnconfigure(0, weight=1)
        f.rowconfigure(2, weight=3)
        f.rowconfigure(3, weight=1)
        ttk.Label(f, text="Every modlist published for a game you keep archives for, and how "
                          "much of each is already in your pools. Read from Wabbajack's own "
                          "validation reports, before any .wabbajack is downloaded.",
                  wraplength=1000).grid(row=0, column=0, sticky="w", pady=(0, 6))
        bar = ttk.Frame(f)
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(bar, text="Game:").pack(side="left")
        self.browse_game = ttk.Combobox(bar, state="readonly", width=40)
        self.browse_game.pack(side="left", padx=(4, 8))
        self.browse_game.bind("<<ComboboxSelected>>", lambda _e: self.browse_show_game())
        self.action(bar, "Reload gallery", self.browse_load).pack(side="left")
        self.action(bar, "Fetch missing reports", lambda: self.browse_sync(False)).pack(side="left", padx=4)
        self.action(bar, "Refresh every report", lambda: self.browse_sync(True)).pack(side="left")
        self.browse_summary = tk.StringVar(value="Not loaded yet.")
        ttk.Label(bar, textvariable=self.browse_summary).pack(side="right")

        cols = [("own", "own", 40, "center"), ("mark", "", 30, "center"),
                ("title", "modlist", 320, "w"), ("version", "version", 110, "w"),
                ("arch", "archives", 80, "e"), ("yours", "yours", 70, "e"),
                ("fetch", "to fetch", 100, "e"), ("total", "total", 100, "e")]
        self.browse_table = make_table(f, cols, height=12, selectmode="browse")
        self.browse_table.grid(row=2, column=0, sticky="nsew")
        self.browse_table.tree.bind("<<TreeviewSelect>>", lambda _e: self.browse_detail())

        det = ttk.Frame(f)
        det.grid(row=3, column=0, sticky="nsew", pady=(6, 0))
        det.columnconfigure(0, weight=1)
        det.rowconfigure(0, weight=1)
        self.browse_text = tk.Text(det, height=7, wrap="word", state="disabled")
        self.browse_text.grid(row=0, column=0, sticky="nsew")
        self.browse_track = self.action(det, "Download this .wabbajack and track it",
                                        self.browse_adopt)
        self.browse_track.grid(row=0, column=1, sticky="n", padx=(8, 0))

    def browse_load(self):
        def job():
            try:
                summary = G.load_summary()
                featured = G.load_featured()
                disabled = G.load_disabled()
            except G.GalleryError as e:
                return {"error": f"The gallery index could not be read ({e})."}
            feeds, complete = A.load_feeds()
            if not feeds:
                return {"error": "No feed could be read, so there is no gallery to show."}
            pooled = G.pooled_games({e.get("game") for e in feeds.values()})
            rows = G.build_rows(feeds, summary, featured, disabled, set(self.tracked),
                                games=set(pooled))
            have_reports = sum(1 for r in rows if G.have_report(r["key"]))
            return {"rows": rows, "pooled": pooled, "complete": complete,
                    "feeds": len(feeds), "have_reports": have_reports}

        def done(res):
            if "error" in res:
                self.browse_summary.set(res["error"])
                return
            self.browse.update({"rows": res["rows"], "pooled": res["pooled"], "indexes": {}})
            games = sorted(res["pooled"], key=lambda k: (
                -sum(1 for r in res["rows"] if r["game_key"] == k), k))
            self.browse["games"] = games
            labels = [f"{res['pooled'][k][0]}  ({sum(1 for r in res['rows'] if r['game_key'] == k)} lists)"
                      for k in games]
            self.browse_game["values"] = labels
            self.browse_summary.set(
                f"{res['feeds']} published; {len(res['rows'])} for your {len(res['pooled'])} game(s); "
                f"you track {sum(1 for r in res['rows'] if r['have'])}; "
                f"reports cached for {res['have_reports']} of {len(res['rows'])}"
                + ("" if res["complete"] else "   [some feeds did not respond]"))
            if labels:
                self.browse_game.current(0)
                self.browse_show_game()
        self.run_job("Loading the gallery", job, done)

    def browse_sync(self, force):
        keys = [r["key"] for r in self.browse["rows"] if not r["retired"]]
        if not keys:
            return
        self.run_job("Fetching validation reports",
                     lambda: W.sync_gallery_reports(keys, force=force),
                     lambda _r: self.browse_load())

    def browse_show_game(self):
        i = self.browse_game.current()
        if i < 0 or i >= len(self.browse["games"]):
            return
        gk = self.browse["games"][i]
        label, pool = self.browse["pooled"][gk]
        grp = [r for r in self.browse["rows"] if r["game_key"] == gk]

        def job():
            W.evaluate_rows(grp, pool, self.browse["indexes"], label)
            grp.sort(key=W.gap_sort_key)
            return grp

        def done(rows):
            self.browse["grp"] = rows
            out_rows = []
            for r in rows:
                g = r.get("gap")
                mark = "!" if G.unavailable(r) else (">" if r["retired"] else "")
                if g and g["total"]:
                    tail = (g["total"], f"{g['percent']:.1f}%", human(g["missing_bytes"]),
                            human(g["total_bytes"]))
                else:
                    tail = (r["archives"] or 0, "-", "-", human(r["arch_size"] or 0))
                out_rows.append(("[x]" if r["have"] else "", mark, r["title"], r["version"]) + tail)
            fill(self.browse_table.tree, out_rows)
            self._set_text(self.browse_text, "Select a list for its details."
                           + ("\n\n!  flagged Unavailable by Wabbajack     >  retired from the gallery"
                              if any(GUI_MARK(r) for r in rows) else ""))
        self.run_job(f"Measuring {label}", job, done)

    def browse_detail(self):
        sel = selected_indices(self.browse_table.tree)
        if not sel:
            return
        r = self.browse["grp"][sel[0]]
        self.browse["detail"] = r
        g = r.get("gap")
        lines = [f"{r['title']}   v{r['version']}    {r['key']}    {A.WJ.game_label(r['game'])}"]
        if G.unavailable(r):
            lines.append("UNAVAILABLE in the Wabbajack gallery: " + "; ".join(G.why_unavailable(r)))
            lines.append("That only greys out the Install button on Wabbajack's own tile; the "
                         ".wabbajack stays on the CDN and the CLI installer never reads the flag.")
        if r["retired"]:
            lines.append("RETIRED from the gallery. Its download still works, but there is no "
                         "feed behind it, so its version can never be tracked.")
        if g is None:
            st = r.get("report_status")
            if st == "ForcedDown":
                lines.append("Its report carries no archives: the validator does not walk a list "
                             "its author has pulled. The zeros upstream mean NOT CHECKED.")
            else:
                lines.append("No usable validation report, so the overlap cannot be worked out "
                             "without the .wabbajack itself.")
            if r["archives"]:
                lines.append(f"Its feed claims {r['archives']} archive(s), {human(r['arch_size'] or 0)}.")
        else:
            lines.append(f"archives {g['total']}   already yours {g['present']} ({g['percent']:.1f}%)"
                         f"   still to fetch {g['missing']} = {human(g['missing_bytes'])}"
                         f"   list total {human(g['total_bytes'])}"
                         + (f"   from the game {g['gamefile']}" if g["gamefile"] else ""))
            dead = g["dead_have"] + g["dead_need"]
            if dead:
                lines.append(f"{len(dead)} archive(s) the validator can no longer download: "
                             f"{len(g['dead_have'])} already in your pools, "
                             f"{len(g['dead_need'])} NOT on disk"
                             + (" - nothing here blocks the install." if not g["dead_need"]
                                else " - these are what would stop the install:"))
                for a in g["dead_need"][:8]:
                    lines.append(f"    {a['name']}   {human(a['size'] or 0)}")
        ent = r.get("entry")
        if r["have"]:
            lines.append("You already track this list. The Updates tab handles its versions.")
        elif not ent or not ent.get("download"):
            lines.append("Its feed carries no download link, so there is nothing to fetch.")
        else:
            lines.append(f"The .wabbajack itself is {human(ent.get('size') or 0)}; the archives "
                         f"are a separate job for the Incomplete tab.")
        self._set_text(self.browse_text, "\n".join(lines))
        can = bool(ent and ent.get("download") and not r["have"])
        self.browse_track.configure(state="normal" if can and not self.busy else "disabled")

    def browse_adopt(self):
        r = self.browse.get("detail")
        if not r:
            return
        if not messagebox.askyesno("Track this list",
                                   f"Download {r['title']} v{r['version']} "
                                   f"({human((r.get('entry') or {}).get('size') or 0)}) and start "
                                   f"tracking it?", parent=self.root):
            return

        def done(ok):
            if ok:
                self.tracked = A.resolve_tracking(self.state)
                self.reload_records()
                self.loaded.discard("Updates")
                self.loaded.discard("Incomplete")
                self.loaded.discard("Prepare")
                self.browse_detail()
        self.run_job(f"Downloading {r['title']}", lambda: W.adopt_gallery_list(self.state, r), done)

    def _set_text(self, widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    # ==================================================================
    # Updates
    # ==================================================================
    def _build_updates(self, f):
        f.columnconfigure(0, weight=1)
        f.rowconfigure(2, weight=1)
        ttk.Label(f, text="Which tracked modlists have a newer published version. Fetching a "
                          "manifest replaces yours only after the new one is downloaded and "
                          "verified; the old copy is kept for a while as a backup.",
                  wraplength=1000).grid(row=0, column=0, sticky="w", pady=(0, 6))
        bar = ttk.Frame(f)
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        self.action(bar, "Check the feeds", self.updates_check).pack(side="left")
        self.action(bar, "Fetch selected", self.updates_fetch).pack(side="left", padx=4)
        self.action(bar, "Fetch all updates", lambda: self.updates_fetch(all_updates=True)).pack(side="left")
        self.updates_summary = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.updates_summary).pack(side="right")
        cols = [("status", "", 90, "center"), ("modlist", "modlist", 380, "w"),
                ("have", "you have", 110, "w"), ("pub", "published", 110, "w"),
                ("size", "manifest", 100, "e")]
        self.updates_table = make_table(f, cols, height=16)
        self.updates_table.grid(row=2, column=0, sticky="nsew")

    def updates_check(self):
        def job():
            seen, pending, resume = A.pending_updates(self.state, self.tracked)
            return seen

        def done(seen):
            if seen is None:
                self.updates_summary.set("Could not read the published feeds.")
                return
            self.update_rows = sorted(seen, key=lambda t: (not t[4], W.stem_of(t[3]).lower()))
            rows = []
            for key, e, p, fn, changed, on_disk in self.update_rows:
                rows.append(("UPDATE" if changed else "ok", W.stem_of(fn),
                             p.get("version") or ("absent" if not on_disk else "unknown"),
                             e["version"], human(e["size"] or 0)))
            fill(self.updates_table.tree, rows)
            n = sum(1 for t in self.update_rows if t[4])
            self.updates_summary.set(f"{n} of {len(self.update_rows)} tracked list(s) have updates.")
        self.run_job("Checking the feeds", job, done)

    def updates_fetch(self, all_updates=False):
        if all_updates:
            picks = [i for i, t in enumerate(self.update_rows) if t[4]]
        else:
            picks = [i for i in selected_indices(self.updates_table.tree) if self.update_rows[i][4]]
        if not picks:
            messagebox.showinfo("Updates", "Nothing selected that has an update.", parent=self.root)
            return
        want = [self.update_rows[i][:4] for i in picks]
        need = sum(e["size"] or 0 for _k, e, _p, _f in want)
        if not messagebox.askyesno("Fetch manifests",
                                   f"Fetch {len(want)} manifest(s), {human(need)}?", parent=self.root):
            return

        def job():
            if not A.free_space_ok(A.MANIFESTS, need):
                W.out("  Not enough free space for these manifests.")
                return 0
            return W.fetch_updates(self.state, want)

        def done(n):
            W.out(f"  {n} of {len(want)} manifest(s) updated.")
            self.reload_records()
            self.loaded.discard("Incomplete")
            self.loaded.discard("Prepare")
            self.updates_check()
        self.run_job("Fetching manifests", job, done)

    # ==================================================================
    # Incomplete + Prepare share the per-list status
    # ==================================================================
    def _status_job(self):
        A.WJ.invalidate_existing_cache()
        rows = []
        for fn in sorted(self.records):
            st = W.list_status(self.records[fn])
            if st is not None:
                rows.append((fn, st))
        statuses = dict(rows)
        stale = W.prune_stale_csvs(self.records, statuses)
        if stale:
            W.out(f"Removed {len(stale)} stale to-do CSV(s) for lists that are complete or gone.")
        return rows

    def status_refresh(self):
        def done(rows):
            self.status_rows = rows
            self._fill_incomplete()
            self._fill_prepare()
            self.loaded.update({"Incomplete", "Prepare"})
        self.run_job("Checking what is on disk", self._status_job, done)

    def _build_incomplete(self, f):
        f.columnconfigure(0, weight=1)
        f.rowconfigure(2, weight=1)
        self.incomplete_note = ttk.Label(f, wraplength=1000)
        self.incomplete_note.grid(row=0, column=0, sticky="w", pady=(0, 6))
        bar = ttk.Frame(f)
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        self.action(bar, "Refresh", self.status_refresh).pack(side="left")
        self.action(bar, "Download selected", self.incomplete_download).pack(side="left", padx=4)
        self.incomplete_summary = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.incomplete_summary).pack(side="right")
        cols = [("modlist", "modlist", 340, "w"), ("game", "game", 190, "w"),
                ("absent", "absent", 70, "e"), ("stale", "wrong size", 80, "e"),
                ("size", "size", 100, "e"), ("nexus", "nexus", 70, "e"),
                ("state", "", 110, "w")]
        self.incomplete_table = make_table(f, cols, height=16)
        self.incomplete_table.grid(row=2, column=0, sticky="nsew")

    def _fill_incomplete(self):
        note = ("Which modlists are missing archives. Downloads run one list at a time, each "
                "finished before the next.")
        if self.assist:
            note += (" Nexus is in assist mode: each Nexus file opens its page in your browser "
                     "and waits for you to click 'Mod manager download'.")
        else:
            note += " Nexus files are left to Wabbajack (NEXUS_MODE is 'skip')."
        self.incomplete_note.configure(text=note)
        rows = []
        for fn, st in sorted(self.status_rows, key=lambda r: -r[1]["bytes"]):
            rows.append((W.stem_of(fn), A.WJ.game_label(st["game"]),
                         st["missing"] - st["stale"], st["stale"] or "-", human(st["bytes"]),
                         st["nexus"] or "-", "incomplete" if st["missing"] else "complete"))
        self.incomplete_order = sorted(self.status_rows, key=lambda r: -r[1]["bytes"])
        fill(self.incomplete_table.tree, rows)
        n = sum(1 for _f, st in self.status_rows if st["missing"])
        self.incomplete_summary.set(f"{n} incomplete, {len(self.status_rows) - n} complete.")

    def incomplete_download(self):
        picks = [i for i in selected_indices(self.incomplete_table.tree)
                 if self.incomplete_order[i][1]["missing"]]
        if not picks:
            messagebox.showinfo("Incomplete", "Select one or more incomplete lists.", parent=self.root)
            return
        want = [self.incomplete_order[i] for i in picks]
        total = sum(st["bytes"] for _f, st in want)
        if not messagebox.askyesno(
                "Download", f"Download the missing archives of {len(want)} list(s), about "
                            f"{human(total)}?" + ("\n\nEach Nexus file needs a click in your browser."
                                                  if self.assist else ""), parent=self.root):
            return

        def job():
            for n, (fn, st) in enumerate(want, 1):
                W.out(f"[{n}/{len(want)}]  {W.stem_of(fn)}   {st['missing']} archive(s), {human(st['bytes'])}")
                W.download_list(self.records, fn, st, self.assist)
            return True
        self.run_job("Downloading", job, lambda _r: self.status_refresh())

    # ==================================================================
    # Prepare
    # ==================================================================
    def _build_prepare(self, f):
        f.columnconfigure(0, weight=1)
        f.rowconfigure(2, weight=1)
        ttk.Label(f, text="Gathers everything one modlist needs into its own folder, so Wabbajack "
                          "can install it. One at a time: staging a second list pulls the shared "
                          "archives back out of the first.", wraplength=1000).grid(
            row=0, column=0, sticky="w", pady=(0, 6))
        bar = ttk.Frame(f)
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        self.action(bar, "Refresh", self.status_refresh).pack(side="left")
        self.action(bar, "Stage selected list", self.prepare_stage).pack(side="left", padx=4)
        cols = [("modlist", "modlist", 380, "w"), ("game", "game", 200, "w"),
                ("disk", "on disk", 90, "e"), ("missing", "missing", 90, "e"),
                ("staged", "", 90, "center")]
        self.prepare_table = make_table(f, cols, height=10, selectmode="browse")
        self.prepare_table.grid(row=2, column=0, sticky="nsew")

        inst = ttk.LabelFrame(f, text="Hand it to Wabbajack", padding=8)
        inst.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        inst.columnconfigure(1, weight=1)
        self.install_info = tk.StringVar(value="Stage a list first.")
        ttk.Label(inst, textvariable=self.install_info, justify="left", wraplength=1000).grid(
            row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(inst, text="Install to:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.install_path = tk.StringVar()
        ttk.Entry(inst, textvariable=self.install_path).grid(row=1, column=1, sticky="ew", pady=(6, 0))
        ttk.Button(inst, text="Browse...", command=self._pick_install_dir).grid(row=1, column=2, padx=(4, 0), pady=(6, 0))
        self.install_button = self.action(inst, "Start the Wabbajack install", self.prepare_install)
        self.install_button.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.install_button.configure(state="disabled")

    def _fill_prepare(self):
        staged = self.state.get("staged") or {}
        rows = []
        self.prepare_order = sorted(self.status_rows, key=lambda r: W.stem_of(r[0]).lower())
        for fn, st in self.prepare_order:
            is_staged = staged.get(A.WJ.folder_key(st["game"])) == W.stem_of(fn)
            rows.append((W.stem_of(fn), A.WJ.game_label(st["game"]), st["present"],
                         st["missing"], "staged" if is_staged else ""))
        fill(self.prepare_table.tree, rows)

    def _pick_install_dir(self):
        d = filedialog.askdirectory(parent=self.root, title="Where should Wabbajack install this list?")
        if d:
            self.install_path.set(os.path.normpath(d))

    def prepare_stage(self):
        sel = selected_indices(self.prepare_table.tree)
        if not sel:
            messagebox.showinfo("Prepare", "Select one list.", parent=self.root)
            return
        fn, st = self.prepare_order[sel[0]]
        note = (f"\n\n{st['missing']} archive(s) are still missing, so this prepares what you have."
                if st["missing"] else "")
        if not messagebox.askyesno("Stage", f"Stage {W.stem_of(fn)} ({A.WJ.game_label(st['game'])}) "
                                            f"for Wabbajack?{note}", parent=self.root):
            return
        if not self.wabbajack_clear("Staging"):
            return

        def done(rc):
            if rc == 0:
                self.staged_fn = fn
                self._offer_install(fn)
            self.status_refresh()
        self.run_job(f"Staging {W.stem_of(fn)}", lambda: W.stage_list(self.state, fn, st), done)

    def _offer_install(self, fn):
        mode = CFG.INSTALL_MODE
        st = dict(self.status_rows)[fn] if fn in dict(self.status_rows) else None
        if mode == "off" or st is None:
            self.install_info.set("Installing is switched off (INSTALL_MODE).")
            return
        wj = WB.find_wabbajack()
        stem = W.stem_of(fn)
        if not wj:
            self.install_info.set("Wabbajack was not found. Set WABBAJACK_DIR in Settings if it is "
                                  "installed somewhere other than beside this folder.")
            return
        downloads = os.path.join(A.WJ.resolve_game_dirs(st["game"])[0], stem)
        manifest = os.path.join(A.MANIFESTS, fn)
        ready, detail = WB.game_ready(wj.get("cli"), st["game"])
        mark = {True: "ok", False: "BLOCKED", None: "unknown"}[ready]
        lines = [f"Wabbajack   {wj['dir']}" + (f"  ({wj['version']})" if wj["version"] else ""),
                 f"Modlist     {manifest}", f"Downloads   {downloads}",
                 f"Game        {A.WJ.game_label(st['game'])}  [{mark}]   {detail}"]
        if st["missing"]:
            lines.append(f"Missing     {st['missing']} archive(s) ({human(st['bytes'])}) not downloaded "
                         f"yet; Wabbajack will try to fetch them itself.")
        if ready is False:
            lines.append("Not offering to install: the game check has to pass first.")
        elif mode == "gui":
            lines.append("INSTALL_MODE is 'gui': the button opens Wabbajack; paste the two paths.")
        elif not wj.get("cli"):
            lines.append("wabbajack-cli.exe was not found; set INSTALL_MODE to 'gui' to open the window.")
        self.install_info.set("\n".join(lines))
        self.install_ctx = {"fn": fn, "st": st, "wj": wj, "manifest": manifest, "downloads": downloads,
                            "mode": mode, "ready": ready}
        remembered = (self.state.get("install") or {}).get(stem)
        if remembered:
            self.install_path.set(remembered)
        can = ready is not False and (mode == "gui" and wj["exe"] or mode == "cli" and wj.get("cli"))
        self.install_button.configure(state="normal" if can else "disabled")

    def prepare_install(self):
        ctx = getattr(self, "install_ctx", None)
        if not ctx:
            return
        if ctx["mode"] == "gui":
            if WB.launch_gui(ctx["wj"]["exe"]):
                W.out(f"Wabbajack is starting. Modlist: {ctx['manifest']}   Downloads: {ctx['downloads']}")
            return
        target = self.install_path.get().strip().strip('"')
        if not target:
            messagebox.showinfo("Install", "Choose where Wabbajack should install this list.", parent=self.root)
            return
        target = os.path.abspath(os.path.expandvars(os.path.expanduser(target)))
        problems = WB.check_paths(ctx["manifest"], ctx["downloads"], target)
        if problems:
            messagebox.showerror("Install", "Nothing started:\n\n" + "\n".join(problems), parent=self.root)
            return
        lines = [f"modlist   {W.stem_of(ctx['fn'])}", f"downloads {ctx['downloads']}", f"install   {target}"]
        if os.path.isdir(target) and os.listdir(target):
            lines.append("\nThat folder already has something in it. Wabbajack will overwrite "
                         "anything belonging to a modlist install.")
        if not self.confirm("Start a Wabbajack install", lines, undo="This runs for a long time; "
                                                                      "its output follows in the log."):
            return
        stem = W.stem_of(ctx["fn"])
        self.state.setdefault("install", {})[stem] = target
        A.save_state(self.state)

        def job():
            return WB.run_install(ctx["wj"]["cli"], ctx["manifest"], ctx["downloads"], target,
                                  on_line=lambda l: W.out("  | " + l))

        def done(rc):
            if rc == 0:
                messagebox.showinfo("Install", f"Wabbajack finished. {stem} is installed at {target}.",
                                    parent=self.root)
            else:
                messagebox.showwarning("Install", f"Wabbajack exited {rc}. The log says why; the staged "
                                                  f"folder is untouched, so re-running is safe.",
                                       parent=self.root)
        self.run_job("Installing with Wabbajack", job, done)

    # ==================================================================
    # Space
    # ==================================================================
    def _build_space(self, f):
        f.columnconfigure(0, weight=1)
        for r in (2, 5, 8):
            f.rowconfigure(r, weight=1)
        ttk.Label(f, text="What the manifests and the per-game Master pools are costing. Deleting a "
                          "modlist moves its archives into Master; clearing Master moves what nothing "
                          "wants into that game's Trash, purged 30 days later.", wraplength=1000).grid(
            row=0, column=0, sticky="w", pady=(0, 6))
        bar = ttk.Frame(f)
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        self.action(bar, "Measure", self.space_measure).pack(side="left")
        self.action(bar, "Delete selected modlists", self.space_delete).pack(side="left", padx=4)
        self.space_free = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.space_free).pack(side="right")
        cols = [("modlist", "modlist", 380, "w"), ("game", "game", 190, "w"),
                ("manifest", "manifest", 100, "e"), ("archives", "its archives", 120, "e"),
                ("note", "", 200, "w")]
        self.lists_table = make_table(f, cols, height=6)
        self.lists_table.grid(row=2, column=0, sticky="nsew")

        bar2 = ttk.Frame(f)
        bar2.grid(row=4, column=0, sticky="ew", pady=(10, 4))
        ttk.Label(bar2, text="Master pools - protected by:").pack(side="left")
        self.rule = tk.StringVar(value="y")
        ttk.Radiobutton(bar2, text="your modlists only", variable=self.rule, value="y",
                        command=self._fill_masters).pack(side="left", padx=(6, 0))
        ttk.Radiobutton(bar2, text="any known modlist, yours or published", variable=self.rule,
                        value="a", command=self.space_wide).pack(side="left", padx=(6, 0))
        self.action(bar2, "Clear Master for selected", self.space_clear).pack(side="left", padx=12)
        self.masters_note = tk.StringVar(value="")
        ttk.Label(bar2, textvariable=self.masters_note).pack(side="right")
        cols = [("game", "pool", 320, "w"), ("files", "files", 80, "e"), ("size", "size", 100, "e"),
                ("free", "reclaimable", 110, "e"), ("dups", "of which duplicates", 130, "e"),
                ("kept", "protected", 100, "e"), ("state", "", 260, "w")]
        self.masters_table = make_table(f, cols, height=5)
        self.masters_table.grid(row=5, column=0, sticky="nsew")

        bar3 = ttk.Frame(f)
        bar3.grid(row=7, column=0, sticky="ew", pady=(10, 4))
        ttk.Label(bar3, text="Folders that belong to nothing this tooling follows:").pack(side="left")
        self.action(bar3, "Park selected in Unmanaged", self.space_park).pack(side="left", padx=12)
        self.trash_note = tk.StringVar(value="")
        ttk.Label(bar3, textvariable=self.trash_note).pack(side="right")
        self.action(bar3, "Empty the trash now", self.space_empty_trash).pack(side="right", padx=(0, 8))
        cols = [("game", "game folder", 200, "w"), ("folder", "folder", 360, "w"),
                ("files", "files", 80, "e"), ("size", "size", 100, "e")]
        self.strays_table = make_table(f, cols, height=4)
        self.strays_table.grid(row=8, column=0, sticky="nsew")

    def space_measure(self):
        def job():
            lists, masters = W.gather_space(self.records)
            return {"lists": lists, "masters": masters, "strays": W.all_stray_folders(),
                    "trash": W.trash_report(), "parked": W.unmanaged_report(),
                    "orphans": W.orphan_sidecars()}

        def done(res):
            self.space.update(res)
            self.space["wide"] = None
            self.rule.set("y")
            self._fill_space()
        self.run_job("Measuring the pools", job, done)

    def _fill_space(self):
        reserved = A.WJ.reserved_list_names()
        rows = []
        for x in self.space["lists"]:
            stem = W.stem_of(x["file"])
            rows.append((stem, A.WJ.game_label(x["game"]) if x["game"] else "?",
                         human(x["manifest_bytes"]),
                         f"{x['folder_files']} / {human(x['folder_bytes'])}" if x["folder_files"] else "-",
                         "named after a pool folder; cannot be deleted here" if stem.lower() in reserved else ""))
        fill(self.lists_table.tree, rows)
        try:
            import shutil
            free = shutil.disk_usage(A.WJ.ARCHIVE_ROOT).free
            self.space_free.set(f"Free on the volume: {human(free)}")
        except OSError:
            self.space_free.set("")
        self._fill_masters()
        fill(self.strays_table.tree, [(g, n, files, human(b)) for g, n, files, b in self.space["strays"]])
        trash = self.space["trash"]
        if trash:
            total = sum(b for _g, _f, b, _o in trash)
            self.trash_note.set(f"Trash: {sum(f for _g, f, _b, _o in trash)} file(s), {human(total)}, "
                                f"purged {W.keep_days()} days after arrival")
        else:
            self.trash_note.set("Trash: empty")

    def _fill_masters(self):
        mode = self.rule.get()
        unreadable, blocked = W.master_refusals(self.records, self.failed_rebuilds)
        rows = []
        for m in self.space["masters"]:
            free = m["wide_free_bytes"] if mode == "a" else m["free_bytes"]
            nfree = m["wide_free_files"] if mode == "a" else m["free_files"]
            kept = m["wide_kept_files"] if mode == "a" else m["kept_files"]
            state = ""
            if unreadable:
                state = "not offered: a manifest could not be read"
            elif m["folder"] in blocked:
                state = "not offered: " + blocked[m["folder"]]
            elif mode == "a" and self.space["wide"] is None:
                state = "gallery protection not loaded"
            rows.append((m["label"], m["files"], human(m["bytes"]), f"{human(free)} ({nfree})",
                         f"{human(m['dup_bytes'])} ({m['dup_files']})", kept, state))
        fill(self.masters_table.tree, rows)
        if mode == "a" and self.space["wide"] is not None:
            held = sum(m["free_bytes"] - m["wide_free_bytes"] for m in self.space["masters"])
            self.masters_note.set(f"{human(held)} kept that the narrow rule would take, because a "
                                  f"published list still wants it")
        else:
            self.masters_note.set("")

    def space_wide(self):
        """The wide rule needs the gallery's demand; load it, or fall back."""
        if self.space["wide"] is not None:
            self._fill_masters()
            return
        pools = {m["folder"] for m in self.space["masters"]}

        def job():
            want, note, pulled = W.gallery_protection(pools)
            if want is None:
                return {"error": note}
            return {"want": want, "note": note, "pulled": pulled,
                    "masters": W.measure_masters(self.records, want)}

        def done(res):
            if "error" in res:
                self.rule.set("y")
                messagebox.showwarning("Cannot do that safely", f"{res['error']}.\n\nDeleting on the "
                                       "strength of a list of published modlists known to be incomplete "
                                       "would remove archives that are in fact still wanted, so this is "
                                       "refused rather than guessed.", parent=self.root)
                self._fill_masters()
                return
            self.space["wide"] = res["want"]
            self.space["pulled"] = res["pulled"]
            self.space["masters"] = res["masters"]
            W.out(f"  {res['note']}.")
            self._fill_masters()
        self.run_job("Reading what the gallery wants", job, done)

    def space_delete(self):
        sel = selected_indices(self.lists_table.tree)
        if not sel:
            messagebox.showinfo("Space", "Select one or more modlists to delete.", parent=self.root)
            return
        chosen = [self.space["lists"][i] for i in sel]
        reserved = A.WJ.reserved_list_names()
        named = [W.stem_of(x["file"]) for x in chosen if W.stem_of(x["file"]).lower() in reserved]
        if named:
            messagebox.showerror("Space", f"Cannot delete {', '.join(named)}: a manifest named after a "
                                          f"pool folder claims that pool as its own folder, and deleting "
                                          f"it would empty the pool. Rename the .wabbajack file instead.",
                                 parent=self.root)
            return
        lines = [f"{W.stem_of(x['file'])}  -  manifest {human(x['manifest_bytes'])}, "
                 f"{x['folder_files']} archive(s) ({human(x['folder_bytes'])}) moved to Master"
                 for x in chosen]
        staged = self.state.get("staged") or {}
        going = {W.stem_of(x["file"]) for x in chosen}
        for x in chosen:
            if x["game"]:
                s = staged.get(A.WJ.folder_key(x["game"]))
                if s and s not in going:
                    lines.append(f"\nRebuilding the {A.WJ.game_label(x['game'])} pools afterwards UN-STAGES "
                                 f"{s}: its shared archives go back to Shared. Stage it again before "
                                 f"installing it.")
        if not self.confirm(f"Delete {len(chosen)} modlist(s)", lines,
                            total=sum(x["manifest_bytes"] for x in chosen)):
            return
        if not self.wabbajack_clear("Deleting a modlist"):
            return

        def done(res):
            failed, moved, freed = res
            self.failed_rebuilds.update(failed)
            self.reload_records()
            self.loaded.discard("Incomplete")
            self.loaded.discard("Prepare")
            self.space_measure()
        self.run_job("Deleting", lambda: W.delete_modlists(self.state, self.records, chosen), done)

    def space_park(self):
        sel = selected_indices(self.strays_table.tree)
        if not sel:
            messagebox.showinfo("Space", "Select one or more folders to park.", parent=self.root)
            return
        chosen = [self.space["strays"][i] for i in sel]
        lines = [f"{g} / {n}   {files} file(s), {human(b)}" for g, n, files, b in chosen]
        lines.append(f"\nNothing is deleted: each folder is moved intact into that game's "
                     f"{CFG.UNMANAGED_FOLDER} folder, which every tool then leaves alone.")
        if not self.confirm("Park folders", lines, undo="Reversible: move the folder back by hand."):
            return

        def job():
            for g, n, _f, _b in chosen:
                dst = W.park_unmanaged_dir(os.path.join(A.WJ.ARCHIVE_ROOT, g), n)
                if dst:
                    W.out(f"    {n} -> {dst}")
            A.WJ.invalidate_existing_cache()
            return True
        self.run_job("Parking", job, lambda _r: self.space_measure())

    def space_clear(self):
        mode = self.rule.get()
        unreadable, blocked = W.master_refusals(self.records, self.failed_rebuilds)
        if unreadable:
            messagebox.showerror("Not clearing any Master",
                                 f"{len(unreadable)} manifest(s) could not be read "
                                 f"({', '.join(unreadable[:4])}). A list whose manifest cannot be read "
                                 f"protects nothing, and does not even say which game's Master it would "
                                 f"have protected. Re-download or remove it first.", parent=self.root)
            return
        if mode == "a" and self.space["wide"] is None:
            messagebox.showinfo("Space", "Load the gallery's demand first by choosing that rule.",
                                parent=self.root)
            return
        sel = selected_indices(self.masters_table.tree)
        chosen = [self.space["masters"][i] for i in sel if self.space["masters"][i]["folder"] not in blocked]
        refused = [self.space["masters"][i] for i in sel if self.space["masters"][i]["folder"] in blocked]
        for m in refused:
            W.out(f"  {m['label']}: not offered - {blocked[m['folder']]}.")

        def free_files(m):
            return m["wide_free_files"] if mode == "a" else m["free_files"]

        def free_bytes(m):
            return m["wide_free_bytes"] if mode == "a" else m["free_bytes"]

        chosen = [m for m in chosen if free_files(m)]
        if not chosen:
            messagebox.showinfo("Space", "Nothing reclaimable in the selected pools under that rule.",
                                parent=self.root)
            return
        lines = [f"{m['label']}  -  {free_files(m)} file(s), {human(free_bytes(m))}   "
                 f"({(m['wide_kept_files'] if mode == 'a' else m['kept_files'])} still wanted, kept)"
                 for m in chosen]
        dups = sum(m["dup_files"] for m in chosen)
        if dups:
            lines.append(f"\nUp to {dups} of those are duplicates of builds also held in Shared or a "
                         f"modlist's folder ({human(sum(m['dup_bytes'] for m in chosen))}). Each is "
                         f"hashed against its other copy first; a pair that differs is kept.")
        if mode == "a":
            lines.append("\nProtected by: your modlists AND every published list.")
            pulled = self.space.get("pulled") or []
            if pulled:
                lines.append(f"{len(pulled)} published list(s) have been pulled by their authors, so "
                             f"nothing is protected on their behalf: {', '.join(pulled[:6])}")
        else:
            lines.append("\nProtected by: your modlists only. Archives a published list wants WILL go.")
        lines.append("Every file goes to that game's Trash, and into a CSV record, as it happens.")
        if not self.confirm(f"Clear Master for {len(chosen)} pool(s)", lines,
                            total=sum(free_bytes(m) for m in chosen), total_label="Total moving to Trash",
                            undo=f"Trash is purged {W.keep_days()} days after a file arrives, or at once "
                                 f"with 'Empty the trash now'. Until then a file can be moved back into "
                                 f"Master by hand."):
            return
        if not self.wabbajack_clear("Clearing Master"):
            return

        def done(grand):
            W.out(f"  {human(grand)} moved to Trash in total.")
            self.space_measure()
        self.run_job("Clearing Master", lambda: W.clear_masters(self.records, chosen, mode, self.space["wide"]),
                     done)

    def space_empty_trash(self):
        trash = self.space["trash"]
        if not trash:
            messagebox.showinfo("Trash", "The trash is empty.", parent=self.root)
            return
        total = sum(b for _g, _f, b, _o in trash)
        if not self.confirm("Empty the trash", [f"{g}: {f} file(s), {human(b)}" for g, f, b, _o in trash],
                            total=total, undo="This is the deletion itself. There is no further undo."):
            return
        if not self.wabbajack_clear("Emptying the trash"):
            return

        def done(res):
            purged, rec = res
            W.out(f"  Purged {len(purged)} file(s), {human(sum(r[2] for r in purged))} freed."
                  + (f" Record: {os.path.basename(rec)}" if rec else ""))
            self.space_measure()
        self.run_job("Emptying the trash", lambda: A.purge_trash(days=0), done)

    # ==================================================================
    # Settings
    # ==================================================================
    def _build_settings(self, f):
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text=f"Your settings live in {CFG.SETTINGS_FILE} beside the scripts; only what "
                          f"differs from the shipped defaults is written. Paths and the manifest "
                          f"folder take effect after a restart; everything else at once.",
                  wraplength=1000).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        self.setting_vars = {}
        row = 1
        for key in CFG.USER_SETTINGS:
            default = CFG._DEFAULTS[key]
            ttk.Label(f, text=key).grid(row=row, column=0, sticky="nw", padx=(0, 10), pady=2)
            if isinstance(default, bool):
                var = tk.BooleanVar()
                ttk.Checkbutton(f, variable=var).grid(row=row, column=1, sticky="w")
            elif key in CFG.CHOICES:
                var = tk.StringVar()
                ttk.Combobox(f, textvariable=var, values=list(CFG.CHOICES[key]), state="readonly",
                             width=14).grid(row=row, column=1, sticky="w")
            elif isinstance(default, dict):
                var = tk.Text(f, height=3, width=80)
                var.grid(row=row, column=1, sticky="ew")
            else:
                var = tk.StringVar()
                ttk.Entry(f, textvariable=var).grid(row=row, column=1, sticky="ew")
            hint = {"GAME_FOLDERS": "JSON object, merged over the defaults",
                    "GAME_DISPLAY": "JSON object, merged over the defaults",
                    "EXCLUDE_POOL_DIRS": "comma-separated", "EXCLUDE": "comma-separated",
                    "TRACK": "comma-separated", "STAGE_LIST": "empty = most recently updated",
                    "WABBAJACK_DIR": "empty = auto-detect", "NOTIFY_URL": "empty = off",
                    "ARCHIVE_ROOT": "restart to apply", "MANIFEST_DIR": "restart to apply"}.get(key, "")
            ttk.Label(f, text=hint, foreground="#666").grid(row=row, column=2, sticky="w", padx=(8, 0))
            self.setting_vars[key] = var
            row += 1
        bar = ttk.Frame(f)
        bar.grid(row=row, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Button(bar, text="Save", command=self.settings_save).pack(side="left")
        ttk.Button(bar, text="Reset to defaults", command=lambda: self.settings_fill(CFG.defaults())).pack(
            side="left", padx=6)
        ttk.Button(bar, text="Open the file", command=self._open_settings_file).pack(side="left")
        self.settings_note = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.settings_note, wraplength=1000).grid(
            row=row + 1, column=0, columnspan=3, sticky="w", pady=(6, 0))

    def settings_fill(self, values=None):
        values = values or CFG.current_settings()
        for key, var in self.setting_vars.items():
            v = values.get(key)
            if isinstance(var, tk.BooleanVar):
                var.set(bool(v))
            elif isinstance(var, tk.Text):
                var.delete("1.0", "end")
                var.insert("1.0", json.dumps(v or {}, indent=1))
            elif isinstance(v, (set, frozenset, list, tuple)):
                var.set(", ".join(sorted(v) if isinstance(v, (set, frozenset)) else v))
            else:
                var.set("" if v is None else str(v))

    def settings_collect(self):
        values, problems = {}, []
        for key, var in self.setting_vars.items():
            default = CFG._DEFAULTS[key]
            if isinstance(var, tk.BooleanVar):
                values[key] = bool(var.get())
            elif isinstance(var, tk.Text):
                text = var.get("1.0", "end").strip() or "{}"
                try:
                    values[key] = json.loads(text)
                except ValueError as e:
                    problems.append(f"{key}: not valid JSON ({e})")
            else:
                text = var.get().strip()
                if isinstance(default, (list, set, frozenset)):
                    values[key] = [t.strip() for t in text.split(",") if t.strip()]
                elif isinstance(default, int) and not isinstance(default, bool):
                    try:
                        values[key] = int(text)
                    except ValueError:
                        problems.append(f"{key}: expected a whole number")
                else:
                    values[key] = text or None
        return values, problems

    def settings_save(self):
        values, problems = self.settings_collect()
        if problems:
            messagebox.showerror("Settings", "Not saved:\n\n" + "\n".join(problems), parent=self.root)
            return
        path, more = CFG.save_settings(values)
        A.reload_settings()
        self.assist = (A.NEXUS_MODE == "assist")
        self.settings_fill()
        if more:
            messagebox.showwarning("Settings", "Saved, but these kept their defaults:\n\n" + "\n".join(more),
                                   parent=self.root)
        self.settings_note.set(f"Saved to {path}." + ("  Restart for path changes to take effect."
                                                    if any(k in values for k in ("ARCHIVE_ROOT", "MANIFEST_DIR"))
                                                    else ""))
        W.out(f"Settings saved to {os.path.basename(path)}.")

    def _open_settings_file(self):
        path = CFG.settings_path()
        if not os.path.isfile(path):
            CFG.save_settings(CFG.current_settings())
        try:
            os.startfile(path)
        except OSError as e:
            messagebox.showerror("Settings", f"Could not open {path}:\n{e}", parent=self.root)


def GUI_MARK(row):
    return G.unavailable(row) or row["retired"]


# ----------------------------------------------------------------------
def _fatal(title, text):
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(title, text)
    root.destroy()


def main(nexus_mode=None, allow_size_only=False, no_lock=False, verbose=False):
    rc = A.bootstrap(verbose=verbose, nexus_mode=nexus_mode, allow_size_only=allow_size_only)
    if rc:
        _fatal("Wabbajack Wizard could not start",
               f"Start-up failed (exit {rc}). wj_autoupdate.log in {A.HERE} says why - usually the "
               f"archive share is not connected, or xxhash is missing from the environment.")
        return rc
    lock_fd = None
    if not no_lock:
        lock_fd = A.acquire_lock(os.path.join(A.HERE, A.LOCK_FILE))
        if lock_fd is A.HELD:
            _fatal("Wabbajack Wizard", "Another run holds the lock - a scheduled run or another window. "
                                       "Nothing was done.")
            return 3
        if lock_fd is A.UNAVAILABLE:
            lock_fd = None
    root = tk.Tk()
    root.geometry("1200x820")
    root.minsize(900, 600)
    style = ttk.Style(root)
    for theme in ("vista", "winnative", "clam"):
        if theme in style.theme_names():
            style.theme_use(theme)
            break
    App(root, lock_fd)
    root.mainloop()
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Wabbajack Wizard, the desktop window.")
    ap.add_argument("--nexus-mode", choices=("skip", "assist"))
    ap.add_argument("--allow-size-only", action="store_true")
    ap.add_argument("--no-lock", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    sys.exit(main(nexus_mode=a.nexus_mode, allow_size_only=a.allow_size_only,
                  no_lock=a.no_lock, verbose=a.verbose))
