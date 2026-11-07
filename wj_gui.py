#!/usr/bin/env python3
"""
Wabbajack Wizard - the desktop window
=====================================
One window, the same five stages as the console wizard, driving the same
functions - wj_wizard.py's stage helpers, and through them wj_autoupdate.py -
so the two front ends cannot drift apart. This module only decides what to
show and what to ask.

It opens on a Start tab: an overview built from what this session and the
last one measured, kept in wj_overview.json in the runtime folder. Opening the
window therefore costs nothing but reading the manifests; the gallery, the
feeds and the pools are read when their tab is opened, and what they find is
remembered for the next start.

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
import shutil
import sys
import threading
import traceback
import tkinter as tk
from datetime import datetime, timezone
from tkinter import ttk, messagebox, filedialog, simpledialog

import wj_autoupdate as A
import wj_wizard as W
import wj_gallery as G
import wj_wabbajack as WB
import wj_config as CFG
import wj_keystore as KS

APP_TITLE = f"Wabbajack Wizard {CFG.__version__}"
POLL_MS = 100
LOG_LINES = 4000

# What the Start tab shows is kept here between sessions: the last answer
# each tab produced, with when it was produced. Regenerated as tabs are used;
# losing it costs one measurement per tab, nothing else.
OVERVIEW_FILE = "wj_overview.json"

# The Game menu's entry for the lists the user has hidden. Not a game key any
# feed could produce.
HIDDEN_GAME = "__hidden__"

# The window's two looks. Light is the platform theme as Tk ships it. Dark
# is the 'clam' theme with these colours: it is the one ttk theme that draws
# every part of every widget itself, so the colours reach the notebook tabs,
# the table headings and the scrollbars, where 'vista' hands those parts to
# Windows and Windows paints them light whatever it is told.
DARK = {
    "bg": "#1f1f1f",         # the window and its frames
    "panel": "#2b2b2b",      # unselected tabs, headings, buttons
    "field": "#151515",      # tables, text panes, entries
    "fg": "#e4e4e4",
    "muted": "#9a9a9a",      # hints and disabled text
    "border": "#3d3d3d",
    "select": "#2d5b8c",     # selected rows and tabs
    "accent": "#4a90d9",     # the progress bar
    "warn": "#e2a45c",       # the stray-folder note
    "insert": "#ffffff",
}
LIGHT = {
    "bg": "#f0f0f0", "panel": "#e1e1e1", "field": "#ffffff", "fg": "#000000",
    "muted": "#666666", "border": "#a0a0a0", "select": "#0078d7", "accent": "#06b025",
    "warn": "#8a4b00", "insert": "#000000",
}


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


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def when_text(iso):
    """A stamp the way people say it: 'today 10:32', 'yesterday 18:05', or
    the date. Empty for no stamp; the raw text for one that will not parse."""
    if not iso:
        return ""
    try:
        t = datetime.fromisoformat(str(iso)).astimezone()
    except (TypeError, ValueError):
        return str(iso)
    days = (datetime.now().astimezone().date() - t.date()).days
    clock = t.strftime("%H:%M")
    if days == 0:
        return f"today {clock}"
    if days == 1:
        return f"yesterday {clock}"
    return t.strftime("%Y-%m-%d %H:%M")


def disk_free(path):
    """Free bytes on the volume holding `path`, or None if it cannot be asked."""
    try:
        return shutil.disk_usage(path).free
    except (OSError, ValueError):
        return None


def overview_path():
    return A.runtime_path(OVERVIEW_FILE)


def load_overview():
    """The last session's measurements, or {} on a first start or a bad file."""
    try:
        with open(overview_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_overview(data):
    """Written whole, through a temp file, so a crash mid-write leaves the
    old copy rather than half of a new one. A share that refuses the write
    only means the next start is not prefilled."""
    p = overview_path()
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, p)
    except OSError:
        pass


def _gap(r):
    return r.get("gap") or {}


def _effort(r):
    return r.get("effort") or {}


# The Browse columns a click on the heading sorts by: the key, whether the
# first click sorts it descending, and what a row needs to have a value there
# at all - a measured gap, or an estimate. A row without one sorts last,
# whichever way the column is sorted.
BROWSE_SORT = {
    "title": (lambda r: r["title"].lower(), False, None),
    "game": (lambda r: G.game_label(r["game"]).lower(), False, None),
    "yours": (lambda r: _gap(r).get("percent", 0.0), True, "gap"),
    "fetch": (lambda r: _gap(r).get("missing_bytes", 0), False, "gap"),
    "total": (lambda r: _gap(r).get("total_bytes") or r.get("arch_size") or 0, True, None),
    "updated": (lambda r: r.get("updated") or "", True, None),
    "clicks": (lambda r: _effort(r).get("clicks", 0), False, "effort"),
    "time": (lambda r: _effort(r).get("seconds", 0), False, "effort"),
}


def browse_sorted(rows, sort):
    """`rows` in the order `sort` - (column, descending) - says, the rows
    with nothing in that column after the rest."""
    if not sort:
        return list(rows)
    key, _first, needs = BROWSE_SORT[sort[0]]
    have = [r for r in rows if not needs or r.get(needs)]
    rest = [r for r in rows if needs and not r.get(needs)]
    return sorted(have, key=key, reverse=sort[1]) + rest


# What each tab's contents are built from. A tab is rebuilt when something it
# reads has moved on since it was last filled, and left alone otherwise.
#
#   records  the manifests held, and which published list each one is
#   pools    the archives on the share
#   settings the configuration
#   overview a measurement another tab made, which the Start tab reports
#
# Tracking them separately is what keeps the refresh cheap: fetching a
# manifest changes `records` but not `pools`, so Updates rebuilds while the
# expensive pool index behind Browse and Space is kept.
TAB_DEPENDS = {
    "Start": ("records", "pools", "settings", "overview"),
    "Browse": ("records", "pools"),
    "Updates": ("records",),
    "Incomplete": ("records", "pools"),
    "Prepare": ("records", "pools"),
    "Space": ("records", "pools"),
    "Settings": ("settings",),
}


# ----------------------------------------------------------------------
# The application
# ----------------------------------------------------------------------
class App:
    def __init__(self, root, lock_fd):
        self.root = root
        self.lock_fd = lock_fd
        self.q = queue.Queue()
        self.busy = False
        self.job_running = False        # a worker thread is out, its "done" not yet read
        self.job_title = ""
        self.closing = False            # the window closes once the job in hand is done
        self.action_buttons = []
        self.busy_widgets = []          # (widget, its state when idle): see quiet_while_busy
        self.state = A.load_state()
        self.tracked = {}
        self.records = {}
        self.failed_rebuilds = {}
        self.assist = (A.NEXUS_MODE == "assist")
        self.versions = {"records": 0, "pools": 0, "settings": 0, "overview": 0}
        self.tab_seen = {}
        # per-tab data
        self.browse = {"rows": [], "hidden": [], "pooled": {}, "indexes": {}, "games": [],
                       "gameinfo": {}, "grp": [], "detail": None, "view": None,
                       "all": False, "stats": {}}
        self.update_rows = []
        self.status_rows = []
        self.space = {"lists": [], "masters": [], "strays": [], "trash": [],
                      "wide": None}
        self.staged_fn = None
        self.overview = load_overview()
        # The plain Tk widgets a theme has to recolour by hand, and what they
        # looked like before any theme touched them.
        self._texts, self._text_defaults, self._combos, self._canvases = [], {}, [], []
        self._base_theme = ttk.Style(root).theme_use()
        self._root_bg = root.cget("background")
        self.theme = None

        root.title(APP_TITLE)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        # An error in a Tk callback reaches the log, not a stderr nobody reads.
        root.report_callback_exception = self._callback_error
        self._build()
        self.apply_theme(CFG.THEME)
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
        """Drain the queue onto the window, and always come back for more.

        One failure while handling an item - a completion handler meeting
        data of a shape it did not expect, say - used to end the pump for the
        session: Tk does not reschedule a callback that raised, and the
        re-arm came after the loop. The log pane froze, the next job's
        "done" was never read, and the window stayed Busy with every button
        off until it was closed. Each item is handled on its own now, an
        error is shown and logged, and the pump re-arms whatever happens."""
        try:
            for _ in range(500):          # then yield to Tk, and come straight back
                try:
                    item = self.q.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle(item)
                except Exception:
                    self._callback_error(*sys.exc_info())
        finally:
            self.root.after(POLL_MS, self._pump)

    def _handle(self, item):
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

    def _callback_error(self, exc_type, exc, tb):
        """An error in code run on the window's thread: into the log file and
        the log pane, and said once in a dialog. Tk's own handler wrote it
        to stderr - which here is the queue the pump drains, so a fault that
        stopped the pump also hid itself."""
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            A.LOG.error("[ERROR] in the window: %s", text)
        except Exception:
            pass
        try:
            self._append_log(text)
        except Exception:
            pass
        if self.busy and not self.job_running:
            # The job finished but its handler failed: the window is not busy.
            self.busy = False
            self._set_busy(False, "Ready")
        if not getattr(self, "_error_shown", False):
            self._error_shown = True
            try:
                messagebox.showerror("Something went wrong",
                                     f"{exc}\n\nThe details are in the log pane and in "
                                     f"wj_autoupdate.log. The window carries on.",
                                     parent=self.root)
            finally:
                self._error_shown = False

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
        if self.closing:
            return False                 # the window is closing: nothing new starts
        if self.busy:
            messagebox.showinfo("Busy", "Another job is still running. Wait for it "
                                        "to finish first.", parent=self.root)
            return False
        self.busy = True
        self.job_running = True
        self.job_title = title
        A.allow_children()               # a stop asked of an earlier job is spent
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
        self.job_running = False
        self._set_busy(False, f"{title}: done")
        if self.closing:
            # The close that was waiting for this job. Nothing chained to it
            # starts: the lock is released, and the window goes, only now
            # that nothing of this session is still at work on the pools.
            self._finish_close()
            return
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
        for w, idle in self.busy_widgets:
            w.configure(state="disabled" if busy else idle)
        if not busy:
            # Three of those depend on more than idleness: two on what is
            # selected, one on what the last Wabbajack check found.
            self._browse_buttons()
            self._install_button()

        # Hold the user on this tab until the job finishes. A job changes what
        # the other tabs are showing, and leaving mid-way used to land you on
        # a tab rebuilt from data the job had not finished writing.
        current = self.nb.select()
        for tab_id in self.nb.tabs():
            if tab_id == current:
                continue
            try:
                self.nb.tab(tab_id, state="disabled" if busy else "normal")
            except tk.TclError:
                pass

    def action(self, parent, text, command, **kw):
        b = ttk.Button(parent, text=text, command=command, **kw)
        self.action_buttons.append(b)
        return b

    def quiet_while_busy(self, widget, idle="normal"):
        """A control that is not a button but starts a job, or changes what one
        is showing - the Game menu, a filter, a rule - off while a job runs,
        as the buttons are. They used to stay live: a second game picked while
        the first was measured met the "Busy" box, and the menu then named a
        game the table was not showing."""
        self.busy_widgets.append((widget, idle))
        return widget

    def confirm(self, title, lines, total=None, total_label="Total to be freed",
                undo="This cannot be undone."):
        text = "\n".join(lines)
        if total is not None:
            text += f"\n\n{total_label}: {human(total)}"
        text += f"\n\n{undo}"
        return messagebox.askyesno(title, text, icon="warning", parent=self.root)

    def _readme_read(self, stem, url):
        """Before an install: open the list's readme and ask whether what it
        asks is done (WB.README_WHY). False stops it before anything starts."""
        if not url:
            return messagebox.askyesno(
                "No readme", f"{stem}'s manifest names no readme, so there is none to "
                             f"open.\n\nInstall anyway?", parent=self.root)
        why = WB.open_readme(url)
        where = ("It is open in your browser:" if why is None
                 else f"It could not be opened ({why}). It is at:")
        return messagebox.askyesno(
            f"Read {stem}'s readme first",
            f"{where}\n{url}\n\n{WB.README_WHY}\n\nHave you read it, and done what it asks?",
            parent=self.root)

    def wabbajack_clear(self, what):
        """Refuse to move archives under a running Wabbajack. Retry until it
        is closed, or give up."""
        while True:
            procs = WB.running_processes()
            if procs is None:
                # tasklist did not answer: asked, rather than assumed either way.
                return messagebox.askyesno(
                    "Is Wabbajack running?",
                    f"Whether Wabbajack is running could not be checked (tasklist did not "
                    f"answer).\n\n{what} moves archives it may be reading. Is it closed?",
                    parent=self.root)
            if not procs:
                return True
            names = ", ".join(f"{n} (pid {p})" for p, n in procs)
            if not messagebox.askretrycancel(
                    "Wabbajack is running",
                    f"{names}\n\n{what} moves archives Wabbajack may be reading. "
                    f"Close it, then Retry.", parent=self.root):
                return False

    def on_close(self):
        """Close the window - but never out from under a job.

        Closing mid-job used to release the run lock and destroy the window
        at once. A download or a sort runs as a child process, and went on
        running, unseen and now unlocked, so a second window or a scheduled
        run could start work on the same pools beside it; work done in the
        window's own process stopped wherever it happened to be. Now the
        window waits for the job, and the lock is released only when it is
        done. A child process can be asked to stop first: it stops at its
        next safe point, keeping what it has done - a download its .part, a
        sort every move already made."""
        if self.closing:
            return
        if not self.busy:
            self._finish_close()
            return
        if A.child_running():
            answer = messagebox.askyesnocancel(
                "A job is running",
                f"{self.job_title} is still running.\n\n"
                "Yes - stop it now, at its next safe point, then close. A download keeps "
                "what it has, and resumes next time.\n"
                "No - close as soon as it finishes.\n"
                "Cancel - keep the window open.", parent=self.root)
            if answer is None:
                return
            stop = answer
        else:
            # Work in the window's own process, moving files one by one: it
            # cannot be stopped half-way safely, so the window waits for it.
            if not messagebox.askokcancel(
                    "A job is running",
                    f"{self.job_title} is still running, and cannot be stopped half-way "
                    f"safely.\n\nOK - close as soon as it finishes.\n"
                    f"Cancel - keep the window open.", parent=self.root):
                return
            stop = False
        self.closing = True
        for b in self.action_buttons:
            b.configure(state="disabled")
        if stop and A.request_child_stop():
            self.status.set(f"Stopping {self.job_title} ... the window closes when it has.")
            W.out(f"Asked {self.job_title} to stop; the window closes when it has.")
        else:
            self.status.set(f"The window closes when {self.job_title} finishes.")
            W.out(f"The window closes when {self.job_title} finishes.")

    def _finish_close(self):
        A.release_lock(self.lock_fd)
        self.lock_fd = None
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
        for name, builder in (("Start", self._build_start),
                              ("Browse", self._build_browse),
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
        self.log = self._register_text(tk.Text(bottom, height=9, wrap="none", state="disabled",
                                               font=("Consolas", 9)))
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
        self.dark_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(status, text="Dark mode", variable=self.dark_var,
                        command=self.toggle_theme).grid(row=0, column=2, padx=(12, 0))
        ttk.Label(status, text=f"Manifests: {A.MANIFESTS}   Archives: {A.WJ.ARCHIVE_ROOT}"
                  f"   Nexus: {A.NEXUS_MODE}", anchor="e").grid(row=1, column=0, columnspan=3,
                                                                sticky="ew")

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _open_log_file(self):
        path = A.runtime_path("wj_autoupdate.log")
        try:
            os.startfile(path)
        except OSError as e:
            messagebox.showerror("Log file", f"Could not open {path}:\n{e}", parent=self.root)

    def data_changed(self, *topics):
        """Record that an operation moved something on.

        Every handler that changes the share or the manifests says so here,
        rather than reaching into other tabs' bookkeeping. Deleting a modlist
        used to clear Incomplete and Prepare and forget Updates, so a list you
        had just deleted was still offered for updating until the window was
        reopened. Naming the topic instead of the tabs means a new tab cannot
        be forgotten by an older handler."""
        for t in topics:
            self.versions[t] = self.versions.get(t, 0) + 1

    def tab_is_stale(self, name):
        """Has anything this tab reads changed since it was last filled?"""
        seen = self.tab_seen.get(name)
        if seen is None:
            return True
        return any(seen.get(t) != self.versions.get(t)
                   for t in TAB_DEPENDS.get(name, ()))

    def tab_filled(self, *names):
        """This tab now shows the data as it stands."""
        for name in names:
            self.tab_seen[name] = dict(self.versions)

    def _tab_changed(self):
        name = self.nb.tab(self.nb.select(), "text")
        # Browse reads the published gallery, not your manifests: with none
        # yet, it is where you go to find one, and used to stay "Not loaded".
        if self.busy or (not self.records and name not in ("Start", "Settings", "Browse")):
            return
        if not self.tab_is_stale(name):
            return
        {"Start": self.start_fill, "Browse": self.browse_load, "Updates": self.updates_check,
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
        # This start becomes the one the next start calls "last session".
        prev = self.overview.get("session")
        if prev:
            self.overview["previous_session"] = prev
        self.overview["session"] = now_iso()
        save_overview(self.overview)
        self.settings_fill()
        self._tab_changed()

    def reload_records(self):
        self.records = self.read_records()

    def read_records(self):
        """The manifests as they are now. A job that brought a new one in
        calls this in its worker and hands the result to its done: a manifest
        new to the cache is read whole, hundreds of MB for a large list, and
        read on the window's thread it froze the window after "done"."""
        A.WJ.invalidate_existing_cache()
        W.refresh_known_dirs()
        return W.load_records()

    # ==================================================================
    # Start
    # ==================================================================
    def _build_start(self, f):
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Where things stand, from this session and the last. Nothing here reads "
                          "the share or the network until asked: each tab measures when you open "
                          "it, this page remembers what it found, and the next start shows that "
                          "again at once.", wraplength=1000).grid(row=0, column=0, columnspan=2,
                                                                  sticky="w", pady=(0, 6))
        bar = ttk.Frame(f)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.action(bar, "Refresh everything", self.start_refresh).pack(side="left")
        ttk.Label(bar, text="the feeds, then what is on disk, then the pools: the three "
                            "measurements the tabs make, in one go",
                  style="Hint.TLabel").pack(side="left", padx=(8, 0))
        self.start_session = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.start_session).pack(side="right")
        self.cards = {}
        for i, (name, title, tab) in enumerate((("records", "Your modlists", "Prepare"),
                                                ("updates", "Updates", "Updates"),
                                                ("status", "Incomplete", "Incomplete"),
                                                ("space", "Disk", "Space"),
                                                ("gallery", "Gallery", "Browse"),
                                                ("settings", "Settings", "Settings"))):
            self.cards[name] = self._card(f, title, 2 + i // 2, i % 2, tab)
        for r in (2, 3, 4):
            f.rowconfigure(r, weight=1)

    def _card(self, parent, title, row, col, tab):
        """One box on the Start tab: a body, a line saying when its facts were
        measured, and the button to the tab that measures them."""
        box = ttk.LabelFrame(parent, text=title, padding=8)
        box.grid(row=row, column=col, sticky="nsew", padx=(0, 8) if col == 0 else (0, 0),
                 pady=(0, 8))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        body = tk.StringVar(value="")
        ttk.Label(box, textvariable=body, justify="left", anchor="nw",
                  wraplength=520).grid(row=0, column=0, columnspan=2, sticky="nsew")
        when = tk.StringVar(value="")
        ttk.Label(box, textvariable=when, style="Hint.TLabel").grid(row=1, column=0, sticky="w",
                                                                    pady=(6, 0))
        self.action(box, f"Open {tab}", lambda: self.goto_tab(tab)).grid(row=1, column=1,
                                                                            sticky="e", pady=(6, 0))
        return {"body": body, "when": when}

    def goto_tab(self, name):
        self.nb.select(self.tabs[name])

    def note_overview(self, section, **data):
        """Remember one tab's answer for the Start tab, this session and the next."""
        data["when"] = now_iso()
        self.overview[section] = data
        save_overview(self.overview)
        self.data_changed("overview")
        if self.nb.tab(self.nb.select(), "text") == "Start":
            self.start_fill()

    def start_fill(self):
        """Every card from what is known now: the cheap facts live, the
        expensive ones from whichever session last measured them."""
        self.tab_filled("Start")
        ov, c = self.overview, self.cards

        def listed(names, first=6):
            names = list(names or [])
            if not names:
                return ""
            more = f"\n    and {len(names) - first} more" if len(names) > first else ""
            return ":\n    " + "\n    ".join(names[:first]) + more

        by_game = {}
        for rec in self.records.values():
            g = A.WJ.game_label(rec.get("game")) if rec.get("game") else "game unknown"
            by_game[g] = by_game.get(g, 0) + 1
        lines = [f"{len(self.records)} manifest(s) in {A.MANIFESTS}",
                 f"following {len(self.tracked)} published list(s)"]
        lines += [f"    {g}: {n}" for g, n in sorted(by_game.items(), key=lambda kv: (-kv[1], kv[0]))]
        staged = self.state.get("staged") or {}
        if staged:
            lines.append("staged for Wabbajack: "
                         + ", ".join(f"{v} ({k})" for k, v in sorted(staged.items())))
        installed = self.state.get("installed") or {}
        if installed:
            lines.append(f"installed with Wabbajack: {len(installed)}"
                         + listed(f"{stem}  v{i.get('version') or '?'}  in {i.get('path')}"
                                  for stem, i in sorted(installed.items())))
        c["records"]["body"].set("\n".join(lines))
        c["records"]["when"].set("as it stands now")

        u = ov.get("updates")
        if u:
            behind = u.get("behind") or []
            c["updates"]["body"].set(
                f"{u.get('pending', 0)} of {u.get('total', 0)} tracked list(s) have a newer "
                f"published version" + (listed(u.get("names")) or ".")
                + (f"\n{len(behind)} install(s) behind what is published"
                   + (listed(behind) or "") if behind else ""))
            c["updates"]["when"].set(f"checked {when_text(u.get('when'))}")
        else:
            c["updates"]["body"].set("Not checked yet. The Updates tab asks the published feeds, "
                                     "which takes a few seconds.")
            c["updates"]["when"].set("")

        s = ov.get("status")
        if s:
            c["status"]["body"].set(
                f"{s.get('incomplete', 0)} incomplete, {human(s.get('bytes'))} still to fetch"
                + (f", {W.about(s['seconds'])} of downloading" if s.get("seconds") else "")
                + (f" and {s['clicks']} click(s)" if s.get("clicks") else "")
                + f"; {s.get('complete', 0)} complete" + (listed(s.get("names")) or "."))
            c["status"]["when"].set(f"measured {when_text(s.get('when'))}")
        else:
            c["status"]["body"].set("Not measured yet. The Incomplete tab compares every manifest "
                                    "with what is in the pools.")
            c["status"]["when"].set("")

        sp = ov.get("space")
        free = disk_free(A.WJ.ARCHIVE_ROOT)
        lines = [f"Free on the archive volume: {human(free)}" if free is not None
                 else f"The archive volume is not reachable: {A.WJ.ARCHIVE_ROOT}"]
        if sp:
            lines.append(f"Manifests: {human(sp.get('manifest_bytes'))} for {sp.get('lists', 0)} list(s)")
            for m in sp.get("masters") or []:
                lines.append(f"    {m.get('label')}: {human(m.get('bytes'))} in {m.get('files', 0)} "
                             f"file(s), {human(m.get('free_bytes'))} wanted by none of your lists")
            tf = sp.get("trash_files", 0)
            lines.append(f"Trash: {tf} file(s), {human(sp.get('trash_bytes'))}, purged "
                         f"{W.keep_days()} days after arrival" if tf else "Trash: empty")
            c["space"]["when"].set(f"measured {when_text(sp.get('when'))}")
        else:
            lines.append("Pools not measured yet. The Space tab walks them, which is the slow part.")
            c["space"]["when"].set("")
        c["space"]["body"].set("\n".join(lines))

        g = ov.get("gallery")
        if g:
            text = (f"{g.get('published', 0)} published list(s)"
                    + (f", {g['retired']} retired still listed" if g.get("retired") else "")
                    + f"; {g.get('shown', 0)} shown across {g.get('games', 0)} game(s), "
                    f"{g.get('tracked', 0)} of them tracked here; reports cached for "
                    f"{g.get('reports', 0)}"
                    + (f"; {g['hidden']} hidden" if g.get("hidden") else ""))
            if not g.get("complete", True):
                text += "\nSome feeds did not respond last time."
            c["gallery"]["when"].set(f"loaded {when_text(g.get('when'))}")
        else:
            text = ("Not loaded yet. The Browse tab reads the gallery index, cached for six "
                    "hours, and measures a game's lists against your pools when you pick it.")
            c["gallery"]["when"].set("")
        c["gallery"]["body"].set(text)

        current, defaults = CFG.current_settings(), CFG.defaults()
        overrides = sum(1 for k, v in current.items() if v != defaults.get(k))
        lines = [f"{CFG.SETTINGS_FILE}: {overrides} setting(s) changed from the defaults"
                 if overrides else f"{CFG.SETTINGS_FILE}: every setting at its default",
                 f"Archives: {A.WJ.ARCHIVE_ROOT}", f"Manifests: {A.MANIFESTS}",
                 f"Nexus: {A.NEXUS_MODE}    Install: {CFG.INSTALL_MODE}    Theme: {CFG.THEME}"]
        wb = ov.get("wabbajack")
        lines.append(f"Wabbajack: {wb.get('text')}, checked {when_text(wb.get('when'))}" if wb
                     else "Wabbajack: checked when a list is staged, or by Refresh everything")
        lines += [f"[WARN] {p}" for p in CFG.SETTINGS_PROBLEMS]
        c["settings"]["body"].set("\n".join(lines))
        c["settings"]["when"].set("")

        prev = ov.get("previous_session")
        self.start_session.set(f"Last session: {when_text(prev)}" if prev else "First session here.")

    def start_refresh(self):
        """The three measurements the tabs make, one after another, then the
        Wabbajack check, so a session is brought up to date from here in one
        press. Each tab is filled on the way, exactly as if it had been
        opened."""
        def after_space():
            self.wabbajack_check(then=self.start_fill)

        def after_status():
            self.space_measure(then=after_space)

        def after_updates():
            self.status_refresh(then=after_status)
        self.updates_check(then=after_updates)

    # ==================================================================
    # Theme
    # ==================================================================
    def _register_text(self, w):
        """A plain Tk text pane: ttk styles do not reach it, so its colours
        are set by hand, and what it had first is kept for going back."""
        self._texts.append(w)
        self._text_defaults[w] = {k: w.cget(k) for k in (
            "background", "foreground", "insertbackground", "selectbackground",
            "selectforeground")}
        return w

    def _register_combo(self, w):
        self._combos.append(w)
        return w

    def _register_canvas(self, w):
        """A plain Tk canvas used as a scrolling container: it has a background
        of its own that the theme has to set."""
        self._canvases.append(w)
        return w

    def toggle_theme(self):
        self.set_theme("dark" if self.dark_var.get() else "light")

    def set_theme(self, mode):
        """Switch the window's look, and remember it in wj_settings.json.

        Saved through the same call the Settings tab uses, from the settings
        as applied rather than from the form, so an edit typed there and not
        yet saved is neither saved nor lost."""
        self.apply_theme(mode)
        if CFG.THEME != mode:
            # This one setting, over the file as it is now (CFG.update_settings):
            # the in-memory settings written back whole used to undo any edit
            # made to the file since the window started.
            CFG.update_settings({"THEME": mode})
            A.reload_settings()
            var = getattr(self, "setting_vars", {}).get("THEME")
            if var is not None:
                var.set(mode)
            base = getattr(self, "_settings_baseline", None)
            if base is not None:
                base["THEME"] = mode

    def apply_theme(self, mode):
        """Paint the window: 'dark', or 'light' for the platform's own look."""
        style = ttk.Style(self.root)
        dark = (mode == "dark")
        self.theme = "dark" if dark else "light"
        self.dark_var.set(dark)
        if dark or self._base_theme == "clam":
            if "clam" in style.theme_names():
                style.theme_use("clam")
            self._paint_clam(style, DARK if dark else LIGHT)
        else:
            style.theme_use(self._base_theme)
            self._paint_native(style)
        self.titlebar_ok = self._paint_titlebar(dark)

    def _paint_titlebar(self, dark):
        """Ask Windows for a title bar to match, or for the light one back.

        Tk draws nothing of the title bar; the window manager does. Windows
        10 (build 17763 on) and 11 take the request through the Desktop
        Window Manager, and Windows 11 also takes exact colours, so the bar
        can be the window's own background rather than Windows' idea of
        dark. Anywhere else this quietly does nothing. Returns True when the
        request was accepted."""
        if sys.platform != "win32":
            return False
        try:
            import ctypes
            from ctypes import wintypes
            self.root.update_idletasks()
            # winfo_id() is Tk's own child window; its parent is the frame
            # Windows draws the caption on.
            hwnd = ctypes.windll.user32.GetParent(wintypes.HWND(self.root.winfo_id()))
            if not hwnd:
                return False
            dwm = ctypes.windll.dwmapi

            def put(attr, value):
                v = ctypes.c_uint32(value)
                return dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), ctypes.c_uint32(attr),
                                                 ctypes.byref(v), ctypes.sizeof(v)) == 0

            def colorref(hex6):
                r, g, b = (int(hex6[i:i + 2], 16) for i in (1, 3, 5))
                return r | (g << 8) | (b << 16)

            # DWMWA_USE_IMMERSIVE_DARK_MODE: 20 from Windows 10 build 18985,
            # 19 on the builds before it.
            ok = put(20, 1 if dark else 0) or put(19, 1 if dark else 0)
            # Windows 11 only: DWMWA_BORDER_COLOR 34, DWMWA_CAPTION_COLOR 35,
            # DWMWA_TEXT_COLOR 36; 0xFFFFFFFF is DWMWA_COLOR_DEFAULT. Refused
            # on Windows 10, which is fine: the dark-mode flag alone already
            # gave it a dark bar.
            if dark:
                put(35, colorref(DARK["bg"]))
                put(36, colorref(DARK["fg"]))
                put(34, colorref(DARK["border"]))
            else:
                for attr in (35, 36, 34):
                    put(attr, 0xFFFFFFFF)
            # DWM repaints the frame on the next activation; ask for it now.
            # SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED.
            ctypes.windll.user32.SetWindowPos(wintypes.HWND(hwnd), None, 0, 0, 0, 0,
                                              0x0001 | 0x0002 | 0x0004 | 0x0020)
            return bool(ok)
        except (OSError, AttributeError, ValueError, tk.TclError):
            return False

    def _paint_native(self, style):
        self.root.configure(background=self._root_bg)
        style.configure("Hint.TLabel", foreground=LIGHT["muted"])
        style.configure("Warn.TLabel", foreground=LIGHT["warn"])
        for w in self._texts:
            w.configure(**self._text_defaults[w])
        for w in self._canvases:
            w.configure(background=self._root_bg)
        self._paint_combos(LIGHT)

    def _paint_clam(self, style, p):
        """Every ttk style the window uses, coloured from palette `p`."""
        self.root.configure(background=p["bg"])
        style.configure(".", background=p["bg"], foreground=p["fg"], fieldbackground=p["field"],
                        bordercolor=p["border"], lightcolor=p["bg"], darkcolor=p["bg"],
                        troughcolor=p["panel"], selectbackground=p["select"],
                        selectforeground=p["fg"], insertcolor=p["insert"], focuscolor=p["select"])
        style.map(".", foreground=[("disabled", p["muted"])])
        style.configure("TFrame", background=p["bg"])
        style.configure("TLabel", background=p["bg"], foreground=p["fg"])
        style.configure("Hint.TLabel", foreground=p["muted"])
        style.configure("Warn.TLabel", foreground=p["warn"])
        style.configure("TLabelframe", background=p["bg"], bordercolor=p["border"],
                        lightcolor=p["bg"], darkcolor=p["bg"])
        style.configure("TLabelframe.Label", background=p["bg"], foreground=p["fg"])
        style.configure("TButton", background=p["panel"], foreground=p["fg"],
                        bordercolor=p["border"], lightcolor=p["panel"], darkcolor=p["panel"],
                        focuscolor=p["select"])
        style.map("TButton",
                  background=[("disabled", p["bg"]), ("pressed", p["select"]),
                              ("active", p["border"])],
                  foreground=[("disabled", p["muted"])])
        for s in ("TCheckbutton", "TRadiobutton"):
            style.configure(s, background=p["bg"], foreground=p["fg"],
                            indicatorbackground=p["field"], indicatorforeground=p["fg"],
                            upperbordercolor=p["border"], lowerbordercolor=p["border"])
            style.map(s, background=[("active", p["bg"])],
                      indicatorbackground=[("disabled", p["bg"]), ("pressed", p["panel"])],
                      foreground=[("disabled", p["muted"])])
        style.configure("TEntry", fieldbackground=p["field"], foreground=p["fg"],
                        insertcolor=p["insert"], bordercolor=p["border"],
                        lightcolor=p["border"], darkcolor=p["border"])
        style.configure("TCombobox", fieldbackground=p["field"], background=p["panel"],
                        foreground=p["fg"], arrowcolor=p["fg"], bordercolor=p["border"],
                        lightcolor=p["border"], darkcolor=p["border"], insertcolor=p["insert"])
        style.map("TCombobox",
                  fieldbackground=[("readonly", p["field"]), ("disabled", p["bg"])],
                  foreground=[("disabled", p["muted"])],
                  selectbackground=[("readonly", p["field"])],
                  selectforeground=[("readonly", p["fg"])])
        style.configure("TNotebook", background=p["bg"], bordercolor=p["border"],
                        lightcolor=p["bg"], darkcolor=p["bg"])
        style.configure("TNotebook.Tab", background=p["panel"], foreground=p["fg"],
                        bordercolor=p["border"], lightcolor=p["panel"], darkcolor=p["panel"],
                        padding=(10, 4))
        style.map("TNotebook.Tab",
                  background=[("selected", p["bg"]), ("active", p["border"])],
                  lightcolor=[("selected", p["bg"])],
                  foreground=[("disabled", p["muted"])])
        style.configure("Treeview", background=p["field"], fieldbackground=p["field"],
                        foreground=p["fg"], bordercolor=p["border"], lightcolor=p["border"],
                        darkcolor=p["border"])
        style.map("Treeview", background=[("selected", p["select"])],
                  foreground=[("selected", p["fg"])])
        style.configure("Treeview.Heading", background=p["panel"], foreground=p["fg"],
                        bordercolor=p["border"], lightcolor=p["panel"], darkcolor=p["panel"],
                        relief="flat")
        style.map("Treeview.Heading", background=[("active", p["border"])])
        style.configure("TScrollbar", background=p["panel"], troughcolor=p["bg"],
                        bordercolor=p["bg"], arrowcolor=p["fg"], lightcolor=p["panel"],
                        darkcolor=p["panel"])
        style.map("TScrollbar", background=[("active", p["border"]), ("disabled", p["bg"])],
                  arrowcolor=[("disabled", p["muted"])])
        style.configure("TProgressbar", background=p["accent"], troughcolor=p["panel"],
                        bordercolor=p["border"], lightcolor=p["accent"], darkcolor=p["accent"])
        for w in self._texts:
            w.configure(background=p["field"], foreground=p["fg"], insertbackground=p["insert"],
                        selectbackground=p["select"], selectforeground=p["fg"])
        for w in self._canvases:
            w.configure(background=p["bg"])
        self._paint_combos(p)

    def _paint_combos(self, p):
        """The drop-down under a combobox is a plain Tk listbox that ttk makes
        on first use: set the defaults for the ones still to be made, and
        recolour the ones already there."""
        sel_fg = p["fg"] if p is DARK else "#ffffff"
        for opt, val in (("*TCombobox*Listbox.background", p["field"]),
                         ("*TCombobox*Listbox.foreground", p["fg"]),
                         ("*TCombobox*Listbox.selectBackground", p["select"]),
                         ("*TCombobox*Listbox.selectForeground", sel_fg)):
            self.root.option_add(opt, val)
        for cb in self._combos:
            try:
                pd = self.root.tk.call("ttk::combobox::PopdownWindow", str(cb))
                self.root.tk.call(f"{pd}.f.l", "configure", "-background", p["field"],
                                  "-foreground", p["fg"], "-selectbackground", p["select"],
                                  "-selectforeground", sel_fg)
            except tk.TclError:
                pass

    # ==================================================================
    # Browse
    # ==================================================================
    def _build_browse(self, f):
        f.columnconfigure(0, weight=1)
        f.rowconfigure(4, weight=3)
        f.rowconfigure(5, weight=1)
        ttk.Label(f, text="Every modlist published for a game you keep archives for, and how "
                          "much of each is already in your pools. Read from Wabbajack's own "
                          "validation reports, before any .wabbajack is downloaded. By default "
                          "only lists you could finish are shown: every archive already in your "
                          "pools or still obtainable, or a list you track. Tick 'every game' to "
                          "look at games you have no pool for yet. A list you will never want "
                          "can be hidden: it then shows only under 'Hidden lists' in the Game "
                          "menu, and counts for nothing anywhere else.",
                  wraplength=1000).grid(row=0, column=0, sticky="w", pady=(0, 6))
        bar = ttk.Frame(f)
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(bar, text="Game:").pack(side="left")
        self.browse_game = self.quiet_while_busy(
            self._register_combo(ttk.Combobox(bar, state="readonly", width=36)), idle="readonly")
        self.browse_game.pack(side="left", padx=(4, 8))
        self.browse_game.bind("<<ComboboxSelected>>", lambda _e: self.browse_show_game())
        self.browse_only = tk.BooleanVar(value=True)
        self.quiet_while_busy(ttk.Checkbutton(bar, text="only lists that can be completed",
                                              variable=self.browse_only,
                                              command=self.browse_refilter)).pack(side="left",
                                                                                  padx=(0, 8))
        self.browse_all = tk.BooleanVar(value=False)
        self.quiet_while_busy(ttk.Checkbutton(bar, text="every game", variable=self.browse_all,
                                              command=self.browse_load)).pack(side="left",
                                                                              padx=(0, 8))
        self.action(bar, "Reload gallery", self.browse_reload).pack(side="left")
        self.action(bar, "Fetch missing reports", lambda: self.browse_sync(False)).pack(side="left", padx=4)
        self.action(bar, "Refresh every report", lambda: self.browse_sync(True)).pack(side="left")
        self.browse_pool_btn = self.action(bar, "Set up a pool", self.browse_make_pool)
        self.browse_pool_btn.pack(side="left", padx=4)
        # Searching and the NSFW filter narrow the current game's table from
        # the rows already measured, as the toggle above does: no job, no
        # read of the share.
        find = ttk.Frame(f)
        find.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(find, text="Search:").pack(side="left")
        self.browse_search = tk.StringVar(value="")
        self.quiet_while_busy(ttk.Entry(find, textvariable=self.browse_search, width=36)).pack(
            side="left", padx=(4, 8))
        self.browse_search.trace_add("write", lambda *_a: self.browse_refilter())
        ttk.Label(find, text="title, key, game or tag; every word must match",
                  style="Hint.TLabel").pack(side="left", padx=(0, 12))
        self.browse_nsfw = tk.BooleanVar(value=bool(CFG.HIDE_NSFW))
        self.quiet_while_busy(ttk.Checkbutton(find, text="hide NSFW lists", variable=self.browse_nsfw,
                                              command=self.browse_toggle_nsfw)).pack(side="left")
        # On a row of its own: beside the buttons it was cut off at the
        # window's default width.
        self.browse_summary = tk.StringVar(value="Not loaded yet.")
        ttk.Label(f, textvariable=self.browse_summary, style="Hint.TLabel").grid(
            row=3, column=0, sticky="w", pady=(0, 6))

        # New columns go on the end, so each existing one keeps its place.
        cols = [("own", "own", 40, "center"), ("mark", "", 44, "center"),
                ("title", "modlist", 300, "w"), ("game", "game", 160, "w"),
                ("version", "version", 100, "w"),
                ("arch", "archives", 80, "e"), ("yours", "yours", 70, "e"),
                ("fetch", "to fetch", 100, "e"), ("total", "total", 100, "e"),
                ("updated", "updated", 90, "w"),
                ("clicks", "clicks", 60, "e"), ("time", "time", 80, "e")]
        self.browse_table = make_table(f, cols, height=12, selectmode="browse")
        self.browse_table.grid(row=4, column=0, sticky="nsew")
        self.browse_table.tree.bind("<<TreeviewSelect>>", lambda _e: self.browse_detail())
        # A heading click sorts the table by that column, and a second click
        # the other way; the measured order, most complete first, is where
        # every game's table starts.
        self.browse_table.labels = {c[0]: c[1] for c in cols}
        for cid in BROWSE_SORT:
            self.browse_table.tree.heading(cid, command=lambda c=cid: self.browse_sort_by(c))

        det = ttk.Frame(f)
        det.grid(row=5, column=0, sticky="nsew", pady=(6, 0))
        det.columnconfigure(0, weight=1)
        det.rowconfigure(0, weight=1)
        self.browse_text = self._register_text(tk.Text(det, height=7, wrap="word",
                                                       state="disabled"))
        self.browse_text.grid(row=0, column=0, sticky="nsew")
        side = ttk.Frame(det)
        side.grid(row=0, column=1, sticky="n", padx=(8, 0))
        self.browse_track = self.action(side, "Download this .wabbajack and track it",
                                        self.browse_adopt)
        self.browse_track.pack(fill="x")
        self.browse_hide = self.action(side, "Hide this list", self.browse_toggle_hidden)
        self.browse_hide.pack(fill="x", pady=(4, 0))

    def browse_reload(self):
        """Reload gallery: the feeds asked afresh, not this session's snapshot."""
        A.reset_feed_cache()
        self.browse_load()

    def browse_load(self):
        # Read on this thread: the worker must not touch a tk variable.
        show_all = bool(self.browse_all.get())

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
            # Every row, then split. The hidden ones are gathered from every
            # game whatever the filter says, because the view that shows them
            # is also the place a flag is taken off again.
            every = G.build_rows(feeds, summary, featured, disabled, set(self.tracked))
            hidden = [r for r in every if r["hidden"]]
            rows = [r for r in every
                    if not r["hidden"] and (show_all or r["game_key"] in pooled)]
            have_reports = sum(1 for r in rows if G.have_report(r["key"]))
            return {"rows": rows, "hidden": hidden, "pooled": pooled, "complete": complete,
                    "all": show_all, "feeds": len(feeds), "have_reports": have_reports}

        def done(res):
            if "error" in res:
                self.browse_summary.set(res["error"])
                return
            self.tab_filled("Browse")
            # The pool index is the expensive part, 28 seconds for Skyrim.
            # Keep it when only the manifests moved on: the archives on disk
            # are what it describes, and those have not changed.
            keep = (self.browse.get("index_pools") == self.versions["pools"]
                    and self.browse.get("indexes"))
            self.browse.update({"rows": res["rows"], "hidden": res["hidden"],
                                "pooled": res["pooled"], "all": res["all"], "detail": None,
                                "stats": {"feeds": res["feeds"], "complete": res["complete"],
                                          "have_reports": res["have_reports"]},
                                "indexes": self.browse["indexes"] if keep else {},
                                "index_pools": self.versions["pools"]})
            self._browse_menu()
            self._browse_summary()
            if self.browse["games"]:
                self.browse_show_game()
        self.run_job("Loading the gallery", job, done)

    def _browse_menu(self, keep=None):
        """The Game menu: one entry per game in the rows, whether or not it
        has a pool, and one more for the hidden lists when there are any.

        The GameType is carried alongside the label because that, not the
        lowercased key, is what a new pool has to be named for. `keep` is the
        game key to leave selected, if it is still there."""
        rows, pooled = self.browse["rows"], self.browse["pooled"]
        info = {}
        for r in rows:
            gk = r["game_key"]
            if gk in info:
                continue
            pool = (pooled.get(gk) or (None, None))[1]
            info[gk] = (G.game_label(r["game"]), pool, r["game"])
        counts = {k: sum(1 for r in rows if r["game_key"] == k) for k in info}
        # Pools you already keep first, then the rest, each by list count.
        games = sorted(info, key=lambda k: (info[k][1] is None, -counts[k], k))
        labels = [f"{info[k][0]}  ({counts[k]} lists)"
                  + ("" if info[k][1] else "   [no pool]") for k in games]
        hidden = self.browse["hidden"]
        if hidden:
            info[HIDDEN_GAME] = ("Hidden lists", None, None)
            games.append(HIDDEN_GAME)
            labels.append(f"Hidden lists  ({len(hidden)})")
        self.browse["gameinfo"] = info
        self.browse["games"] = games
        self.browse_game["values"] = labels
        if keep in games:
            self.browse_game.current(games.index(keep))
        elif games:
            self.browse_game.current(0)

    def _browse_summary(self):
        """The line above the table, and the Start tab's gallery card, from
        what the rows hold right now."""
        st = self.browse.get("stats") or {}
        rows, hidden = self.browse["rows"], self.browse["hidden"]
        games = [k for k in self.browse["gameinfo"] if k != HIDDEN_GAME]
        unpooled = sum(1 for k in games if self.browse["gameinfo"][k][1] is None)
        retired = sum(1 for r in rows if r["retired"])
        tracked = sum(1 for r in rows if r["have"])
        self.browse_summary.set(
            f"{st.get('feeds', 0)} published"
            + (f" plus {retired} retired" if retired else "")
            + f"; {len(rows)} shown across {len(games)} game(s)"
            + (f", {unpooled} with no pool" if unpooled else "")
            + f"; you track {tracked}; "
            f"reports cached for {st.get('have_reports', 0)} of {len(rows)}"
            + (f"; {len(hidden)} hidden" if hidden else "")
            + ("" if st.get("complete", True) else "   [some feeds did not respond]"))
        self.note_overview("gallery", published=st.get("feeds", 0), retired=retired,
                           shown=len(rows), games=len(games), tracked=tracked,
                           reports=st.get("have_reports", 0), hidden=len(hidden),
                           complete=bool(st.get("complete", True)))

    def browse_sync(self, force):
        keys = [r["key"] for r in self.browse["rows"] if not r["retired"]]
        if not keys:
            messagebox.showinfo("Validation reports", "No published list is shown, so there is no "
                                "report to fetch. Load the gallery first, or tick 'every game' to "
                                "include games you keep no pool for.", parent=self.root)
            return
        self.run_job("Fetching validation reports",
                     lambda: W.sync_gallery_reports(keys, force=force),
                     lambda _r: self.browse_load())

    def browse_show_game(self):
        i = self.browse_game.current()
        if i < 0 or i >= len(self.browse["games"]):
            return
        gk = self.browse["games"][i]
        self.browse["detail"] = None
        if gk == HIDDEN_GAME:
            self._browse_show_hidden()
            return
        label, pool, _game = self.browse["gameinfo"][gk]
        grp = [r for r in self.browse["rows"] if r["game_key"] == gk]

        def job():
            W.evaluate_rows(grp, pool, self.browse["indexes"], label)
            grp.sort(key=W.gap_sort_key)
            return grp

        def done(rows):
            self.browse["measured"] = rows
            self.browse["view"] = gk
            self._browse_apply_filter()
        self.run_job(f"Measuring {label}", job, done)

    def browse_refilter(self):
        """A toggle flipped or the search changed: the current game's table
        is rebuilt from the rows already measured, so it costs nothing."""
        if self.busy:
            return
        if self.browse.get("view") not in (None, HIDDEN_GAME):
            self._browse_apply_filter()

    def browse_sort_by(self, cid):
        """A heading was clicked: sort by that column, the other way when it
        is clicked again, and mark the heading with the direction."""
        if self.busy:
            return                      # the table is the job's until it is done
        cur = self.browse.get("sort")
        rev = (not cur[1]) if cur and cur[0] == cid else BROWSE_SORT[cid][1]
        self.browse["sort"] = (cid, rev)
        self._browse_headings()
        self.browse_refilter()

    def _browse_headings(self):
        sort = self.browse.get("sort")
        for c, label in self.browse_table.labels.items():
            arrow = ((" ▼" if sort[1] else " ▲") if sort and sort[0] == c else "")
            self.browse_table.tree.heading(c, text=label + arrow)

    def browse_toggle_nsfw(self):
        """The NSFW box: remembered as HIDE_NSFW the way the Dark mode switch
        remembers THEME, through the call the Settings tab uses, and the
        table refiltered from what is already measured."""
        hide = bool(self.browse_nsfw.get())
        if bool(CFG.HIDE_NSFW) != hide:
            W.set_hide_nsfw(hide)
            var = getattr(self, "setting_vars", {}).get("HIDE_NSFW")
            if var is not None:
                var.set(hide)
            base = getattr(self, "_settings_baseline", None)
            if base is not None:
                base["HIDE_NSFW"] = hide
        self.browse_refilter()

    def _browse_apply_filter(self):
        """The current game's table from its measured rows, under the 'only
        lists that can be completed' rule when that is ticked."""
        gk = self.browse.get("view")
        if gk in (None, HIDDEN_GAME) or gk not in self.browse["gameinfo"]:
            return
        label, pool, _game = self.browse["gameinfo"][gk]
        rows = self.browse.get("measured") or []
        only = bool(self.browse_only.get())
        if only:
            shown, blocked, unknown = G.split_completable(rows)
        else:
            shown, blocked, unknown = list(rows), [], []
        search = self.browse_search.get()
        shown, nsfw = G.browse_filter(shown, search, bool(self.browse_nsfw.get()))
        shown = browse_sorted(shown, self.browse.get("sort"))
        self.browse["grp"] = shown
        self.browse["detail"] = None
        self._browse_fill(shown)

        lines = ["Select a list for its details."]
        narrowed = []
        if search.strip():
            narrowed.append(f"matching '{search.strip()}'")
        if nsfw:
            narrowed.append(f"{nsfw} marked NSFW hidden")
        if narrowed:
            lines.append(f"{len(shown)} list(s) " + ", ".join(narrowed) + ".")
        if only and (blocked or unknown):
            bits = []
            if blocked:
                bits.append(f"{len(blocked)} that cannot be completed (a dead archive not in your pools)")
            if unknown:
                bits.append(f"{len(unknown)} unmeasurable (retired, pulled, or no report cached yet)")
            lines.append(f"Showing {len(shown)} of {len(rows)} {label} list(s). Hidden: "
                         + "; ".join(bits) + ". Untick 'only lists that can be completed' to see them.")
        if pool is None:
            lines.append("You have no pool for this game, so every list reads as 0% held "
                         "and the 'to fetch' column is the whole download. 'Set up a pool for "
                         "this game' lays out the same Master, Shared, Unmanaged and Trash the "
                         "other games have; tracking a list without one still works, the pool "
                         "is simply made on the first sort.")
        legend = []
        if any(G.unavailable(r) for r in shown):
            legend.append("!  flagged Unavailable by Wabbajack")
        if any(r["retired"] for r in shown):
            legend.append(">  retired from the gallery")
        if any(G.completable(r) is False for r in shown):
            legend.append("x  cannot be completed: a dead archive is not in your pools")
        if any(r.get("nsfw") for r in shown):
            legend.append("n  marked NSFW by its author")
        if any(_effort(r).get("manual") for r in shown):
            legend.append("m  needs files you download by hand from a web page")
        if legend:
            lines.append("     ".join(legend))
        if any(r.get("effort") for r in shown):
            sp = W.current_speeds()
            lines.append(f"time: downloading what is still to fetch, Nexus files at "
                         f"{W.speed_note('nexus', sp)}, direct ones at {W.speed_note('direct', sp)}. "
                         f"clicks: one per Nexus file, which a free account needs. A heading "
                         f"sorts the table.")
        self._set_text(self.browse_text, "\n\n".join(lines))
        self._browse_buttons()

    def _browse_fill(self, rows, measured=True):
        """The table. With `measured` off the feeds' own figures are shown
        even for a row whose overlap happens to be known, so the Hidden view
        reads the same whether a list was hidden this session or last."""
        out_rows = []
        for r in rows:
            g = r.get("gap") if measured else None
            e = r.get("effort") if measured else None
            mark = (("!" if G.unavailable(r) else "") + (">" if r["retired"] else "")
                    + ("x" if measured and G.completable(r) is False else "")
                    + ("n" if r.get("nsfw") else "")
                    + ("m" if e and e["manual"] else ""))
            if g and g["total"]:
                tail = (g["total"], f"{g['percent']:.1f}%", human(g["missing_bytes"]),
                        human(g["total_bytes"]))
            else:
                tail = (r["archives"] or 0, "-", "-", human(r["arch_size"] or 0))
            out_rows.append(("[x]" if r["have"] else "", mark, r["title"],
                             G.game_label(r["game"]), r["version"]) + tail
                            + (r.get("updated") or "",) + W.effort_cells(e))
        fill(self.browse_table.tree, out_rows)

    def _browse_show_hidden(self):
        """The hidden lists, every game together, with the feeds' own figures.

        Not measured against the pools: never fetching a hidden list's report
        or reading it is most of what hiding the list saves."""
        rows = sorted(self.browse["hidden"],
                      key=lambda r: (G.game_label(r["game"]).lower(), r["title"].lower()))
        self.browse["grp"] = rows
        self.browse["view"] = HIDDEN_GAME
        self._browse_fill(rows, measured=False)
        self._set_text(self.browse_text,
                       "These lists are hidden. They appear in no other view or stage, their "
                       "validation reports are not fetched, and nothing protects their archives "
                       "when Master is cleared, whichever rule is chosen. The figures are the "
                       "feeds' own: a hidden list is never measured against your pools.\n\n"
                       f"Select one and press 'Unhide this list' to bring it back. HIDDEN in "
                       f"{CFG.SETTINGS_FILE} holds the flags.")
        self._browse_buttons()

    def _browse_buttons(self):
        """The two buttons beside the detail pane, from the selected row."""
        r = self.browse.get("detail")
        ent = (r or {}).get("entry")
        can_track = bool(r and ent and ent.get("download") and not r["have"] and not r["hidden"])
        can_hide = bool(r and not r["have"])
        idle = not self.busy
        self.browse_track.configure(state="normal" if can_track and idle else "disabled")
        self.browse_hide.configure(text="Unhide this list" if r and r["hidden"] else "Hide this list",
                                   state="normal" if can_hide and idle else "disabled")

    def browse_toggle_hidden(self):
        """Hide the selected list, or bring a hidden one back.

        Done in memory as well as in the settings file, so the table does not
        pay for a reload: the row moves between the visible rows and the
        hidden ones, the Game menu's counts follow, and a list brought back is
        the only one measured when its game is next shown."""
        r = self.browse.get("detail")
        if not r or r["have"]:
            return
        hide = not r["hidden"]
        W.set_list_hidden(r["key"], hide)
        r["hidden"] = hide
        st = self.browse.setdefault("stats", {})
        has_report = G.have_report(r["key"])
        if hide:
            self.browse["rows"] = [x for x in self.browse["rows"] if x is not r]
            self.browse["hidden"].append(r)
            if has_report:
                st["have_reports"] = max(0, st.get("have_reports", 0) - 1)
            W.out(f"Hidden: {r['title']} ({r['key']}). Shown only under 'Hidden lists' from "
                  f"now on; HIDDEN in {CFG.SETTINGS_FILE} keeps the flag.")
        else:
            self.browse["hidden"] = [x for x in self.browse["hidden"] if x is not r]
            if self.browse.get("all") or r["game_key"] in self.browse["pooled"]:
                self.browse["rows"].append(r)
                if has_report:
                    st["have_reports"] = st.get("have_reports", 0) + 1
            W.out(f"Unhidden: {r['title']}.")
        view = self.browse.get("view")
        self.browse["detail"] = None
        self.data_changed("settings")
        # What the Space tab's "any known modlist" rule protects has just
        # changed: a hidden list protects nothing. The demand it loaded is
        # dropped, so a clear made under that rule reads it again rather than
        # going by a snapshot that still had this list the other way round.
        if self.space.get("wide") is not None:
            self.space["wide"] = None
            if self.rule.get() == "a":
                self.rule.set("y")
        self._browse_menu(keep=view)
        self._browse_summary()
        self.browse_show_game()

    def browse_make_pool(self):
        i = self.browse_game.current()
        if i < 0 or i >= len(self.browse["games"]):
            messagebox.showinfo("Set up a pool", "Pick the game in the Game menu first. With no "
                                "game there, load the gallery, and tick 'every game' to list the "
                                "ones you keep no pool for.", parent=self.root)
            return
        gk = self.browse["games"][i]
        if gk == HIDDEN_GAME:
            messagebox.showinfo("Hidden lists", "Pick a game to set up a pool for.",
                                parent=self.root)
            return
        label, pool, game = self.browse["gameinfo"][gk]
        if pool:
            messagebox.showinfo("Pool already there",
                                f"{label} already draws from:\n\n{pool}",
                                parent=self.root)
            return

        existing = sorted({v[1] for v in self.browse["gameinfo"].values() if v[1]})
        prompt = (f"Folder name for {label}, under\n{CFG.resolved_archive_root()}\n\n"
                  "Leave it as it is to give this game its own pool. Type the name of a "
                  "pool you already keep to fold it in instead, the way Skyrim VR draws "
                  "from Skyrim: the two then share one Master and protect each other's "
                  "archives.")
        if existing:
            prompt += "\n\nPools you already keep: " + ", ".join(
                os.path.basename(p) for p in existing)
        folder = simpledialog.askstring("Set up a pool", prompt,
                                        initialvalue=CFG.game_folder(game),
                                        parent=self.root)
        if folder is None:
            return
        folder = folder.strip()
        problem = W.pool_folder_problem(game, folder)
        if problem:
            messagebox.showerror("Cannot use that name", problem, parent=self.root)
            return

        def done(res):
            path, created, mapped = res
            messagebox.showinfo(
                "Pool ready",
                f"{label} now draws from:\n\n{path}\n\n"
                + ("Created just now." if created
                   else "That folder was already there, and now has the full layout.")
                + (f"\n\n{CFG.SETTINGS_FILE} maps {game} onto {folder}, so its lists "
                   f"share that pool." if mapped else ""),
                parent=self.root)
            self.reload_records()
            # A fold writes GAME_FOLDERS: the Settings tab has to show it, or
            # its next save wrote back the mapping it was filled with and
            # undid the fold.
            self.data_changed("pools", "settings")
            if mapped:
                self.settings_fill()
            self.browse_load()
        self.run_job(f"Setting up a pool for {label}",
                     lambda: W.create_game_pool(game, folder), done)

    def browse_detail(self):
        sel = selected_indices(self.browse_table.tree)
        if not sel:
            return
        r = self.browse["grp"][sel[0]]
        self.browse["detail"] = r
        g = r.get("gap")
        lines = [f"{r['title']}   v{r['version']}    {r['key']}    {A.WJ.game_label(r['game'])}"]
        facts = []
        if r.get("updated"):
            facts.append(f"updated {r['updated']}")
        if r.get("tags"):
            facts.append("tags: " + ", ".join(r["tags"]))
        if r.get("nsfw"):
            facts.append("marked NSFW by its author")
        if facts:
            lines.append("   ".join(facts))
        if r["hidden"]:
            lines.append("HIDDEN from the gallery: shown only here, never measured against your "
                         "pools, its report never fetched, its archives unprotected when Master "
                         "is cleared. Unhide it to track it, or to see how much of it you hold.")
        if G.unavailable(r):
            lines.append("UNAVAILABLE in the Wabbajack gallery: " + "; ".join(G.why_unavailable(r)))
            lines.append("That only greys out the Install button on Wabbajack's own tile; the "
                         ".wabbajack stays on the CDN and the CLI installer never reads the flag.")
        if r["retired"]:
            lines.append("RETIRED from the gallery. Its download still works, but there is no "
                         "feed behind it, so its version can never be tracked.")
        if r["hidden"]:
            if r["archives"]:
                lines.append(f"Its feed claims {r['archives']} archive(s), {human(r['arch_size'] or 0)}.")
        elif g is None:
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
            e = r.get("effort")
            if e and g["missing"]:
                lines.append("Still to fetch: " + W.effort_text(e, W.current_speeds()) + ".")
            if g["dead_need"]:
                lines.append(f"CANNOT BE COMPLETED from here: {len(g['dead_need'])} archive(s) the "
                             f"validator can no longer download are not in your pools.")
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
            lines.append("You already track this list. The Updates tab handles its versions, and "
                         "a tracked list cannot be hidden.")
        elif not ent or not ent.get("download"):
            lines.append("Its feed carries no download link, so there is nothing to fetch.")
        else:
            lines.append(f"The .wabbajack itself is {human(ent.get('size') or 0)}; the archives "
                         f"are a separate job for the Incomplete tab.")
        self._set_text(self.browse_text, "\n".join(lines))
        self._browse_buttons()

    def browse_adopt(self):
        r = self.browse.get("detail")
        if not r:
            return
        if not messagebox.askyesno("Track this list",
                                   f"Download {r['title']} v{r['version']} "
                                   f"({human((r.get('entry') or {}).get('size') or 0)}) and start "
                                   f"tracking it?", parent=self.root):
            return

        def job():
            ok = W.adopt_gallery_list(self.state, r)
            return ok, (self.read_records() if ok else None)

        def done(res):
            ok, records = res
            if ok:
                self.tracked = A.resolve_tracking(self.state)
                self.records = records
                self.data_changed("records")
                self.browse_detail()
        self.run_job(f"Downloading {r['title']}", job, done)

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
        self.action(bar, "Update the selected install", self.updates_install).pack(side="left", padx=12)
        self.updates_summary = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.updates_summary).pack(side="right")
        # "installed" is what Wabbajack installed here, recorded when an
        # install finished; "reinstall" says the install is older than the
        # manifest held, which is itself the published one.
        cols = [("status", "", 90, "center"), ("modlist", "modlist", 380, "w"),
                ("have", "you have", 110, "w"), ("pub", "published", 110, "w"),
                ("size", "manifest", 100, "e"), ("installed", "installed", 110, "w")]
        self.updates_table = make_table(f, cols, height=16)
        self.updates_table.grid(row=2, column=0, sticky="nsew")

    def updates_check(self, then=None):
        """Ask the feeds. `then`, if given, runs on the main thread afterwards,
        whatever the answer: the Start tab chains the three measurements."""
        def job():
            seen, pending, resume = A.pending_updates(self.state, self.tracked)
            return seen

        def done(seen):
            if seen is None:
                self.updates_summary.set("Could not read the published feeds.")
            else:
                self.tab_filled("Updates")
                self.update_rows = sorted(seen, key=lambda t: (not t[4], W.stem_of(t[3]).lower()))
                rows, behind = [], []
                for key, e, p, fn, changed, on_disk in self.update_rows:
                    inst = W.install_status(self.state, e, fn, changed)
                    if inst and inst["behind"]:
                        behind.append(W.stem_of(fn))
                    status = ("UPDATE" if changed
                              else "reinstall" if inst and inst["behind"] else "ok")
                    rows.append((status, W.stem_of(fn),
                                 p.get("version") or ("absent" if not on_disk else "unknown"),
                                 e["version"], human(e["size"] or 0),
                                 f"v{inst['version'] or '?'}" if inst else ""))
                fill(self.updates_table.tree, rows)
                n = sum(1 for t in self.update_rows if t[4])
                self.updates_summary.set(f"{n} of {len(self.update_rows)} tracked list(s) have updates"
                                         + (f"; {len(behind)} install(s) behind" if behind else "") + ".")
                self.note_overview("updates", total=len(self.update_rows), pending=n,
                                   names=[W.stem_of(t[3]) for t in self.update_rows if t[4]],
                                   behind=behind)
            if then is not None:
                then()
        return self.run_job("Checking the feeds", job, done)

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
                return 0, self.records
            n = W.fetch_updates(self.state, want)
            return n, self.read_records()

        def done(res):
            n, self.records = res
            W.out(f"  {n} of {len(want)} manifest(s) updated.")
            self.data_changed("records")
            self.updates_check()
        self.run_job("Fetching manifests", job, done)

    def updates_install(self):
        """Update the selected installed list end to end: the checks an
        install makes first, off the main thread; one confirmation naming
        every step; then update_install() as one job."""
        sel = selected_indices(self.updates_table.tree)
        if not sel:
            messagebox.showinfo("Update an install", "Select an installed list.", parent=self.root)
            return
        row = self.update_rows[sel[0]]
        key, ent, _p, fn, changed, _d = row
        inst = W.install_status(self.state, ent, fn, changed)
        if not inst:
            messagebox.showinfo("Update an install",
                                f"No install of {W.stem_of(fn)} is recorded here. Install it once "
                                f"from Prepare; after that this updates it in place.", parent=self.root)
            return
        game = (self.records.get(fn) or {}).get("game")

        def check():
            wj = self._find_wabbajack()
            if not wj or not wj.get("cli"):
                return {"wj": wj}
            wj, update, not_updated = self._bring_up_to_date(wj)
            ready, detail = WB.game_ready(wj.get("cli"), game)
            # The manifest on disk: a new version's arrives in step 1, and a
            # list's readme stays at one address.
            return {"wj": wj, "ready": ready, "detail": detail, "update": update,
                    "not_updated": not_updated,
                    "readme": WB.manifest_readme(os.path.join(A.MANIFESTS, fn))}

        def checked(res):
            wj = res["wj"]
            if not wj or not wj.get("cli"):
                messagebox.showerror("Update an install", "wabbajack-cli.exe was not found, so the "
                                     "install cannot be driven from here.", parent=self.root)
                return
            if res["ready"] is False:
                messagebox.showerror("Update an install", f"Not updating: {res['detail']}.",
                                     parent=self.root)
                return
            upd = res["update"]
            self._note_wabbajack(upd)
            why = f"\n\nIt could not be updated: {res['not_updated']}." if res.get("not_updated") else ""
            if upd.get("outdated") and not messagebox.askyesno(
                    "Wabbajack is out of date", f"{upd['text']}.{why}\n\nA modlist built for a newer "
                    f"Wabbajack can fail to install on an older one. Carry on anyway?",
                    icon="warning", parent=self.root):
                return
            if not self._readme_read(W.stem_of(fn), res.get("readme")):
                return
            lines = [f"{W.stem_of(fn)}   v{inst['version'] or '?'} -> v{ent['version']}",
                     f"into {inst['path']}", ""]
            if changed:
                lines.append(f"1. fetch the new manifest ({human(ent.get('size') or 0)})")
            lines += ["2. download what it is missing"
                      + (", a click per Nexus file" if self.assist else ""),
                      "3. stage it, which un-stages anything else staged for that game",
                      "4. run Wabbajack's installer into the same folder"]
            if not self.confirm("Update an install", lines,
                                undo="Wabbajack updates the folder in place, as it does for any "
                                     "reinstall. This runs for a long time; its output follows in "
                                     "the log."):
                return
            if not self.wabbajack_clear("Updating an install"):
                return

            def done(res2):
                ok, message = res2
                (messagebox.showinfo if ok else messagebox.showwarning)(
                    "Update an install", ("Done: " if ok else "Stopped: ") + message + ".",
                    parent=self.root)
                self.reload_records()
                self.data_changed("records", "pools")
                self.updates_check()
            self.run_job(f"Updating {W.stem_of(fn)}",
                         lambda: W.update_install(self.state, row, self.assist, wj), done)
        self.run_job("Checking Wabbajack", check, checked)

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

    def status_refresh(self, then=None):
        def done(rows):
            self.status_rows = rows
            self._fill_incomplete()
            self._fill_prepare()
            self.tab_filled("Incomplete", "Prepare")
            missing = sorted(((st["bytes"], W.stem_of(fn)) for fn, st in rows if st["missing"]),
                             reverse=True)
            efforts = [st.get("effort") or {} for _fn, st in rows if st["missing"]]
            self.note_overview("status", incomplete=len(missing), complete=len(rows) - len(missing),
                               bytes=sum(b for b, _n in missing), names=[n for _b, n in missing],
                               seconds=sum(e.get("seconds", 0) for e in efforts),
                               clicks=sum(e.get("clicks", 0) for e in efforts))
            if then is not None:
                then()
        return self.run_job("Checking what is on disk", self._status_job, done)

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
                ("state", "", 110, "w"), ("time", "time", 80, "e")]
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
        if any(st.get("unsafe") for _f, st in self.status_rows):
            note += (" A list marked 'refused: edited' names archives with something other "
                     "than a plain file name, which only an edited manifest does; nothing is "
                     "downloaded for it until the manifest is fetched again.")
        missing = [(fn, st) for fn, st in self.status_rows if st["missing"]]
        if missing:
            # The time column is the downloading alone; the nexus column
            # is also the clicks, one per file, on a free account.
            note += "\n" + W.incomplete_total(missing)
        self.incomplete_note.configure(text=note)
        rows = []
        for fn, st in sorted(self.status_rows, key=lambda r: -r[1]["bytes"]):
            state = "incomplete" if st["missing"] else "complete"
            if st.get("unsafe"):
                # Named archives outside the pool: the downloader refuses it.
                state = "refused: edited"
            rows.append((W.stem_of(fn), A.WJ.game_label(st["game"]),
                         st["missing"] - st["stale"], st["stale"] or "-", human(st["bytes"]),
                         st["nexus"] or "-", state, W.effort_cells(st.get("effort"))[1]))
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
        self.run_job("Downloading", job, lambda _r: (self.data_changed("pools"),
                                                     self.status_refresh()))

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
        # The last check was for a list that is staged no more - deleted,
        # a cleanup put back, or another list staged over it: its verdict
        # goes with it, and the panel says what is staged now.
        ctx = getattr(self, "install_ctx", None)
        if ctx and not self._still_staged(ctx):
            self.install_ctx = None
            self.install_info.set("Stage a list first.")
        # A list staged by an earlier session is in the table as staged, but
        # nothing has checked Wabbajack for it yet: say so, rather than "Stage
        # a list first" beside a list marked staged.
        ready = self._staged_lists()
        if not getattr(self, "install_ctx", None) and ready and CFG.INSTALL_MODE != "off":
            if len(ready) == 1:
                self.install_info.set(f"{W.stem_of(ready[0][0])} is staged. Starting the install "
                                      f"checks Wabbajack and the game first.")
            else:
                self.install_info.set("Staged: " + ", ".join(W.stem_of(fn) for fn, _st in ready)
                                      + ". Select the one to install, then start the install.")
        self._install_button()

    def _staged_lists(self):
        """The Prepare table's staged lists, as (fn, st): one per game at most."""
        staged = self.state.get("staged") or {}
        return [(fn, st) for fn, st in getattr(self, "prepare_order", [])
                if staged.get(A.WJ.folder_key(st["game"])) == W.stem_of(fn)]

    def _install_button(self):
        """On when the last Wabbajack check allows an install - or, before any
        check this session, when a list is staged, so the click can make the
        check. Every action button is turned back on after each job, and this
        one used to come on with nothing checked behind it: the click did
        nothing at all."""
        ctx = getattr(self, "install_ctx", None)
        if ctx:
            can = self._install_blocker(ctx) is None
        else:
            can = CFG.INSTALL_MODE != "off" and bool(self._staged_lists())
        self.install_button.configure(state="normal" if can and not self.busy else "disabled")

    def _still_staged(self, ctx):
        staged = self.state.get("staged") or {}
        return staged.get(A.WJ.folder_key(ctx["st"]["game"])) == W.stem_of(ctx["fn"])

    def _install_blocker(self, ctx):
        """Why the checked list cannot be handed to Wabbajack, or None."""
        if CFG.INSTALL_MODE == "off":
            return "Installing is switched off (INSTALL_MODE)."
        if not self._still_staged(ctx):
            return (f"{W.stem_of(ctx['fn'])} is not staged any more: stage it again, and "
                    f"Wabbajack is checked for it again.")
        wj = ctx.get("wj")
        if not wj:
            return ("Wabbajack was not found. Set WABBAJACK_DIR in Settings if it is "
                    "installed somewhere other than beside this folder.")
        if ctx["ready"] is False:
            return f"The game check has to pass first: {ctx['detail']}"
        if ctx["mode"] == "gui" and not wj["exe"]:
            return f"Wabbajack.exe was not found in {wj['dir']}."
        if ctx["mode"] == "cli" and not wj.get("cli"):
            return "wabbajack-cli.exe was not found; set INSTALL_MODE to 'gui' to open the window."
        return None

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
            self.data_changed("pools")
            if rc == 0:
                self.staged_fn = fn
                # One job after the other: the check used to start alongside
                # the refresh, which then refused with a "Busy" box and left
                # the table as it was before staging.
                self.status_refresh(then=lambda: self._offer_install(fn))
            else:
                self.status_refresh()
        self.run_job(f"Staging {W.stem_of(fn)}", lambda: W.stage_list(self.state, fn, st), done)

    def _offer_install(self, fn, then=None):
        """After staging: find Wabbajack, look up whether it is the newest
        release and bring it up to date when it is not (WABBAJACK_AUTO_UPDATE),
        and ask it about the game - a folder listing, a cached web request, a
        download and a child process, so off the main thread - then fill the
        panel and remember the verdict for the Start tab."""
        mode = CFG.INSTALL_MODE
        st = dict(self.status_rows).get(fn)
        if mode == "off" or st is None:
            self.install_info.set("Installing is switched off (INSTALL_MODE).")
            return
        game = st["game"]

        def job():
            readme = WB.manifest_readme(os.path.join(A.MANIFESTS, fn))
            # Measured again rather than taken from the table: staging has
            # just read what it gathered, and sent any archive holding the
            # wrong bytes to the trash (W.verify_staged).
            rec = self.records.get(fn)
            fresh = (W.list_status(rec) if rec else None) or st
            wj = self._find_wabbajack()
            if not wj:
                return {"wj": None, "update": WB.update_status(None), "readme": readme, "st": fresh}
            wj, update, not_updated = self._bring_up_to_date(wj)
            ready, detail = WB.game_ready(wj.get("cli"), game)
            return {"wj": wj, "ready": ready, "detail": detail, "update": update,
                    "not_updated": not_updated, "readme": readme, "st": fresh}

        def done(res):
            self._show_install_offer(fn, res.get("st") or st, mode, res)
            if then is not None:
                then()
        self.run_job("Checking Wabbajack", job, done)

    def _find_wabbajack(self):
        """Wabbajack, with the version folders an update superseded removed
        on the way: the launcher leaves them behind, at some 700 MB each.
        Worker-thread code; it only writes to the log."""
        wj = WB.find_wabbajack()
        for name in WB.prune_old_versions(wj, keep=[A.MANIFESTS]):
            A.LOG.info("  Removed Wabbajack %s, superseded by %s.", name, wj["version"])
        return wj

    def _bring_up_to_date(self, wj):
        """WB.bring_up_to_date, as the window's settings have it. What it says
        goes through the log - to the pane, and kept in wj_autoupdate.log -
        since it downloads a release and removes the folder of the one it
        replaces. Worker-thread code; it only writes to the log."""
        return WB.bring_up_to_date(wj, CFG.WABBAJACK_AUTO_UPDATE, keep=[A.MANIFESTS],
                                   on_line=lambda line: A.LOG.info("  %s", line))

    def wabbajack_check(self, then=None):
        """Is Wabbajack there, and is it the newest release? Cheap: a folder
        listing and a release lookup cached for six hours."""
        def done(update):
            self._note_wabbajack(update)
            W.out(f"  {update['text']}." + (f"  {update['url']}" if update["outdated"] else ""))
            if then is not None:
                then()
        return self.run_job("Checking Wabbajack",
                            lambda: WB.update_status(self._find_wabbajack()), done)

    def _note_wabbajack(self, update):
        self.note_overview("wabbajack", **{k: update.get(k) for k in
                                           ("installed", "latest", "outdated", "text", "url")})

    def _show_install_offer(self, fn, st, mode, res):
        wj, update = res["wj"], res["update"]
        self._note_wabbajack(update)
        stem = W.stem_of(fn)
        if not wj:
            # Remembered all the same, so an earlier check's verdict cannot
            # turn the button back on.
            self.install_ctx = {"fn": fn, "st": st, "wj": None, "mode": mode, "ready": None}
            self.install_info.set(self._install_blocker(self.install_ctx))
            self._install_button()
            return
        downloads = os.path.join(A.WJ.resolve_game_dirs(st["game"])[0], stem)
        manifest = os.path.join(A.MANIFESTS, fn)
        ready, detail = res["ready"], res["detail"]
        mark = {True: "ok", False: "BLOCKED", None: "unknown"}[ready]
        lines = [f"Wabbajack   {wj['dir']}" + (f"  ({wj['version']})" if wj["version"] else ""),
                 f"            {update['text']}"
                 + (f"   {update['url']}" if update["outdated"] else "")]
        if update["outdated"] and res.get("not_updated"):
            lines.append(f"            Not updated: {res['not_updated']}. Run Wabbajack.exe once to "
                         f"let it update itself, or stage the list again to retry.")
        elif update["outdated"]:
            lines.append("            WABBAJACK_AUTO_UPDATE is off: run Wabbajack.exe once to let it "
                         "update itself; the old version's folder is removed from here the next "
                         "time it is checked.")
        lines += [f"Modlist     {manifest}", f"Downloads   {downloads}",
                  f"Game        {A.WJ.game_label(st['game'])}  [{mark}]   {detail}",
                  f"Readme      {res.get('readme') or 'none named in the manifest'}"
                  + ("   (opens when you install)" if res.get("readme") else "")]
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
                            "mode": mode, "ready": ready, "detail": detail, "update": update,
                            "not_updated": res.get("not_updated"), "readme": res.get("readme")}
        remembered = (self.state.get("install") or {}).get(stem)
        if remembered:
            self.install_path.set(remembered)
        self._install_button()

    def prepare_install(self):
        ctx = getattr(self, "install_ctx", None)
        if not ctx:
            # Staged by an earlier session, so not checked in this one: check
            # it now, and carry on into the install from there.
            staged = self._staged_lists()
            chosen = {self.prepare_order[i][0] for i in selected_indices(self.prepare_table.tree)}
            pick = [r for r in staged if r[0] in chosen] or staged
            if len(pick) != 1:
                messagebox.showinfo("Install", "Select the staged list to install." if pick else
                                    "Stage a list first: select it above, then Stage selected list.",
                                    parent=self.root)
                return
            W.out(f"Checking Wabbajack before installing {W.stem_of(pick[0][0])}...")
            self._offer_install(pick[0][0], then=self.prepare_install)
            return
        blocker = self._install_blocker(ctx)
        if blocker:
            messagebox.showinfo("Install", f"Nothing started. {blocker}", parent=self.root)
            return
        upd = ctx.get("update") or {}
        why = f"\n\nIt could not be updated: {ctx['not_updated']}." if ctx.get("not_updated") else ""
        if upd.get("outdated") and not messagebox.askyesno(
                "Wabbajack is out of date",
                f"{upd['text']}.{why}\n\nA modlist built for a newer Wabbajack can fail to install on "
                f"an older one. The newest release is at\n{upd['url']}\n\n"
                f"Install with the version you have anyway?", icon="warning", parent=self.root):
            return
        if not self._readme_read(W.stem_of(ctx["fn"]), ctx.get("readme")):
            return
        if ctx["mode"] == "gui":
            if WB.launch_gui(ctx["wj"]["exe"]):
                W.out(f"Wabbajack is starting. Modlist: {ctx['manifest']}   Downloads: {ctx['downloads']}")
            else:
                messagebox.showerror("Install", f"{ctx['wj']['exe']} could not be started.",
                                     parent=self.root)
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
            return W.install_with_wabbajack(ctx["wj"]["cli"], ctx["manifest"], ctx["downloads"],
                                            target, on_line=lambda l: W.out("  | " + l))

        def done(res):
            rc, log = res
            if rc == 0:
                W.record_install(self.state, ctx["fn"], target)
                self.data_changed("records")
                messagebox.showinfo("Install", f"Wabbajack finished. {stem} is installed at {target}.",
                                    parent=self.root)
            else:
                said = f"What it said is kept in\n{log}\n\n" if log else "The log pane says why. "
                messagebox.showwarning("Install", f"Wabbajack exited {rc}. {said}The staged folder is "
                                                  f"untouched, so re-running is safe.",
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
        self.quiet_while_busy(ttk.Radiobutton(bar2, text="your modlists only", variable=self.rule,
                                              value="y", command=self._fill_masters)).pack(
            side="left", padx=(6, 0))
        self.quiet_while_busy(ttk.Radiobutton(
            bar2, text="any known modlist, yours or published (hidden lists excluded)",
            variable=self.rule, value="a", command=self.space_wide)).pack(side="left", padx=(6, 0))
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
        self.action(bar3, "Undo a cleanup...", self.space_undo).pack(side="right", padx=(0, 8))
        self.action(bar3, "Remove leftovers", self.space_leftovers).pack(side="right", padx=(0, 8))
        cols = [("game", "game folder", 150, "w"), ("folder", "folder", 300, "w"),
                ("files", "files", 70, "e"), ("size", "size", 90, "e"),
                ("note", "where it came from", 250, "w")]
        self.strays_table = make_table(f, cols, height=4)
        self.strays_table.grid(row=8, column=0, sticky="nsew")
        self.stray_note = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.stray_note, wraplength=1000,
                  style="Warn.TLabel").grid(row=9, column=0, sticky="w", pady=(4, 0))

    def space_measure(self, then=None):
        def job():
            # The manifests as they are now, not as the window found them at
            # its start: a list dropped into the folder since is demand, and
            # measured without it its archives read as reclaimable.
            records = W.load_records()
            lists, masters = W.gather_space(records)
            return {"records": records, "lists": lists, "masters": masters,
                    "strays": W.all_stray_folders(), "trash": W.trash_report(),
                    "trash_loose": W.unrecorded_trash(),
                    "parked": W.unmanaged_report(), "orphans": W.orphan_sidecars()}

        def done(res):
            if sorted(res["records"]) != sorted(self.records):
                self.records = res["records"]
                self.data_changed("records")
            self.tab_filled("Space")
            self.space.update(res)
            self.space["wide"] = None
            self.rule.set("y")
            self._fill_space()
            trash = res["trash"]
            self.note_overview("space", lists=len(res["lists"]),
                               manifest_bytes=sum(x["manifest_bytes"] for x in res["lists"]),
                               masters=[{"label": m["label"], "files": m["files"], "bytes": m["bytes"],
                                         "free_bytes": m["free_bytes"], "free_files": m["free_files"]}
                                        for m in res["masters"]],
                               trash_files=sum(fl for _g, fl, _b, _o in trash),
                               trash_bytes=sum(b for _g, _f, b, _o in trash))
            if then is not None:
                then()
        return self.run_job("Measuring the pools", job, done)

    def _fill_space(self):
        rows = []
        for x in self.space["lists"]:
            stem = W.stem_of(x["file"])
            why = CFG.list_name_problem(stem)
            rows.append((stem, A.WJ.game_label(x["game"]) if x["game"] else "?",
                         human(x["manifest_bytes"]),
                         f"{x['folder_files']} / {human(x['folder_bytes'])}" if x["folder_files"] else "-",
                         f"cannot be a list's folder ({why}); cannot be deleted here" if why else ""))
        fill(self.lists_table.tree, rows)
        try:
            import shutil
            free = shutil.disk_usage(A.WJ.ARCHIVE_ROOT).free
            self.space_free.set(f"Free on the volume: {human(free)}")
        except OSError:
            self.space_free.set("")
        self._fill_masters()
        noted = W.strays_with_notes(self.space["strays"])
        fill(self.strays_table.tree,
             [(g, n, files, human(b), W.stray_note(o)) for g, n, files, b, o in noted])

        # A stray that is a list the gallery still publishes should be adopted,
        # not parked: parking hides archives you own from the list wanting them.
        adoptable = [(n, o) for _g, n, _f, _b, o in noted if o and o["published"]]
        stale = [n for _g, n, _f, _b, o in noted
                 if o and o["sidecar"] and not o["published"]]
        bits = []
        if adoptable:
            bits.append("Do not park " + ", ".join(o["title"] or n for n, o in adoptable)
                        + ": the gallery still publishes it and only the manifest is "
                          "missing here. Adopt it in Browse and the folder is legitimate "
                          "again, with its archives back under protection.")
        elif stale:
            bits.append("Wabbajack downloaded " + ", ".join(stale) + " once, and only "
                        "the .metadata sidecar is left. Load Browse to see whether the "
                        "list is still published and could be adopted back.")
        orphans = self.space.get("orphans") or []
        if orphans:
            bits.append(f"{len(orphans)} .metadata sidecar(s) whose modlist is gone: "
                        + ", ".join(o[:-len('.wabbajack.metadata')] for o in orphans[:4])
                        + (f" and {len(orphans) - 4} more" if len(orphans) > 4 else "")
                        + ". Tiny, and left alone: an orphan is usually the last trace "
                          "of a list that went missing.")
        self.stray_note.set("  ".join(bits))
        trash = self.space["trash"]
        # Files in a trash folder that no deletion record accounts for are
        # never purged, so they are said rather than left to sit unseen.
        loose = self.space.get("trash_loose") or []
        if trash:
            total = sum(b for _g, _f, b, _o in trash)
            note = (f"Trash: {sum(f for _g, f, _b, _o in trash)} file(s), {human(total)}, "
                    f"purged {W.keep_days()} days after arrival")
        else:
            note = "Trash: nothing a cleanup put there" if loose else "Trash: empty"
        if loose:
            note += (f"; {sum(f for _g, f, _b in loose)} file(s) in no deletion record, "
                     f"never purged")
        self.trash_note.set(note)

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
            info = {}
            want, note, pulled = W.gallery_protection(pools, info=info)
            if want is None:
                return {"error": note}
            # The hidden lists for these pools, by title, for the confirmation:
            # they are the one thing this rule deliberately leaves unprotected,
            # so they are named where the deletion is agreed to. The feeds are
            # this run's snapshot; gallery_protection just read them.
            feeds, _complete = A.load_feeds()
            hidden_keys = info.get("hidden") or []
            hidden = sorted(str((feeds.get(k) or {}).get("title") or k) for k in hidden_keys)
            return {"want": want, "note": note, "pulled": pulled, "hidden": hidden,
                    "hidden_keys": hidden_keys, "behind": info.get("behind") or [],
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
            self.space["hidden"] = res["hidden"]
            self.space["hidden_keys"] = res["hidden_keys"]
            self.space["behind"] = res["behind"]
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
        # Judged as Windows reads the name, as delete_modlist() judges it:
        # 'Master.' is Master as surely as 'Master' is.
        named = [f"{W.stem_of(x['file'])} ({CFG.list_name_problem(W.stem_of(x['file']))})"
                 for x in chosen if CFG.list_name_problem(W.stem_of(x["file"]))]
        if named:
            messagebox.showerror("Space", f"Cannot delete {', '.join(named)}: a list's folder is "
                                          f"named after its manifest, and this one's would be a pool "
                                          f"or no folder of its own - deleting it could empty a "
                                          f"pool. Rename the .wabbajack file instead.",
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
            self.data_changed("records", "pools")
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
        # Nothing that moves archives starts under a running Wabbajack - a
        # folder an install is reading from is exactly the kind parked here.
        if not self.wabbajack_clear("Parking folders"):
            return

        def job():
            for g, n, _f, _b in chosen:
                dst = W.park_unmanaged_dir(os.path.join(A.WJ.ARCHIVE_ROOT, g), n)
                if dst:
                    W.out(f"    {n} -> {dst}")
            A.WJ.invalidate_existing_cache()
            return True
        self.run_job("Parking", job, lambda _r: (self.data_changed("pools"),
                                                 self.space_measure()))

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
            lines.append("\nProtected by: your modlists AND every published list, hidden lists excluded.")
            hidden = self.space.get("hidden") or []
            if hidden:
                lines.append(f"{len(hidden)} hidden list(s) protect nothing, so their archives go "
                             f"unless another list wants them: {', '.join(hidden[:6])}"
                             + (f" and {len(hidden) - 6} more" if len(hidden) > 6 else ""))
            pulled = self.space.get("pulled") or []
            if pulled:
                lines.append(f"{len(pulled)} published list(s) have been pulled by their authors, so "
                             f"nothing is protected on their behalf: {', '.join(pulled[:6])}")
            behind = self.space.get("behind") or []
            if behind:
                lines.append(f"{len(behind)} published list(s) were last validated for an older build "
                             f"than their feed now publishes, so what only the new build wants is "
                             f"not protected yet: {', '.join(behind[:6])}"
                             + (f" and {len(behind) - 6} more" if len(behind) > 6 else ""))
        else:
            lines.append("\nProtected by: your modlists only. Archives a published list wants WILL go.")
        lines.append("Every file goes to that game's Trash, and into a CSV record, as it happens.")
        if not self.confirm(f"Clear Master for {len(chosen)} pool(s)", lines,
                            total=sum(free_bytes(m) for m in chosen), total_label="Total moving to Trash",
                            undo=f"Trash is purged {W.keep_days()} days after a file arrives, or at once "
                                 f"with 'Empty the trash now'. Until then 'Undo a cleanup' puts it "
                                 f"back."):
            return
        if not self.wabbajack_clear("Clearing Master"):
            return
        seen = sorted(self.records)
        confirmed_hidden = sorted(self.space.get("hidden_keys") or [])
        pools = {m["folder"] for m in self.space["masters"]}

        def job():
            # Read again here, as the console reads them right before it asks.
            # What the window measured may be hours old: a manifest dropped
            # into the folder since, or a list hidden or brought back in
            # Browse, changes what this clear may take, and a snapshot taken
            # before either used to be what it went by.
            records = W.load_records()
            if sorted(records) != seen:
                return {"refused": "the manifests in the folder have changed since the "
                                   "Space tab was measured. Measure it again, and check "
                                   "the table, before clearing."}
            # The same names may hold other contents now - a manifest copied
            # over, or read back damaged - and one that cannot be read, or
            # lists nothing, protects nothing.
            unreadable, blocked = W.master_refusals(records, self.failed_rebuilds)
            if unreadable or any(m["folder"] in blocked for m in chosen):
                return {"refused": "a manifest changed since the Space tab was measured, and "
                                   "can no longer be trusted to say what is wanted ("
                                   + ", ".join(unreadable[:3] or [blocked[m["folder"]] for m in chosen
                                                                  if m["folder"] in blocked][:1])
                                   + "). Measure it again, and check the table, before clearing."}
            wide = None
            if mode == "a":
                info = {}
                wide, note, _pulled = W.gallery_protection(pools, progress=False, info=info)
                if wide is None:
                    return {"refused": f"{note}. Deleting on the strength of a list of "
                                       f"published modlists known to be incomplete would "
                                       f"remove archives that are still wanted."}
                if sorted(info.get("hidden") or []) != confirmed_hidden:
                    return {"refused": "the hidden lists have changed since the gallery's "
                                       "demand was loaded, so what this would keep is not "
                                       "what was confirmed. Choose the rule again."}
            return {"grand": W.clear_masters(records, chosen, mode, wide)}

        def done(res):
            if "refused" in res:
                messagebox.showwarning("Not clearing Master", res["refused"], parent=self.root)
                self.space["wide"] = None
                self.rule.set("y")
                self._fill_masters()
                return
            W.out(f"  {human(res['grand'])} moved to Trash in total.")
            self.data_changed("pools")
            self.space_measure()
        self.run_job("Clearing Master", job, done)

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
            self.data_changed("pools")
            self.space_measure()
        self.run_job("Emptying the trash", lambda: A.purge_trash(days=0), done)

    def space_undo(self):
        """The cleanups whose files are still in the trash, in a window of
        their own: pick one, and it is put back into Master and sorted.

        Which files are still there is a look at each one a record names, on
        the share - tens of thousands of them once a few cleanups have run - so
        it is a job, where it used to freeze the window before this one opened."""
        self.run_job("Reading the cleanup records",
                     lambda: [r for r in W.cleanup_records() if r["waiting"]], self._undo_window)

    def _undo_window(self, records):
        if not records:
            messagebox.showinfo("Undo a cleanup", "No cleanup has files left in the trash: "
                                                  "nothing to put back.", parent=self.root)
            return
        win = tk.Toplevel(self.root)
        win.title("Undo a cleanup")
        win.transient(self.root)
        # Modal: a job started from the main window while this one is open
        # would have met "Put it back" with the "Busy" box, after the window
        # it was chosen in was gone.
        try:
            win.grab_set()
        except tk.TclError:             # not viewable yet, or no display to grab
            pass
        win.configure(background=self.root.cget("background"))
        frame = ttk.Frame(win, padding=8)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="What stage 5c moved into a trash, newest first. The files still "
                              "there go back into Master, and the pools are rebuilt so each one "
                              "a list wants goes home.", wraplength=640).pack(anchor="w", pady=(0, 6))
        cols = [("when", "when", 130, "w"), ("folder", "game folder", 120, "w"),
                ("waiting", "still in the trash", 130, "e"), ("size", "size", 90, "e"),
                ("rule", "rule", 220, "w")]
        table = make_table(frame, cols, height=min(10, len(records)), selectmode="browse")
        table.pack(fill="both", expand=True)
        fill(table.tree, [((r["when"].strftime("%Y-%m-%d %H:%M") if r["when"] else r["stamp"]),
                           r["folder"], f"{r['waiting']} of {r['files']}",
                           human(r["waiting_bytes"]), r["rule"]) for r in records])
        bar = ttk.Frame(frame)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="Put it back", command=self.undo_restore).pack(side="left")
        ttk.Button(bar, text="Close", command=win.destroy).pack(side="left", padx=6)
        self.undo_win, self.undo_table, self.undo_records = win, table, records

    def undo_restore(self):
        sel = selected_indices(self.undo_table.tree)
        if not sel:
            messagebox.showinfo("Undo a cleanup", "Select the cleanup to put back.",
                                parent=self.undo_win)
            return
        rec = self.undo_records[sel[0]]
        lines = [f"{rec['waiting']} file(s), {human(rec['waiting_bytes'])}, back into "
                 f"{rec['folder']}'s Master, from {rec['file']}."]
        staged = (self.state.get("staged") or {}).get(rec["folder"].lower())
        if staged:
            lines.append(f"\nRebuilding the {rec['folder']} pools afterwards UN-STAGES {staged}: "
                         f"its shared archives go back to Shared. Stage it again before "
                         f"installing it.")
        if not self.confirm("Put a cleanup back", lines, total=rec["waiting_bytes"],
                            total_label="Total moving back",
                            undo="Nothing is deleted: the files move back into Master, and the "
                                 "rebuild sorts home whatever a list wants."):
            return
        if not self.wabbajack_clear("Putting a cleanup back"):
            return
        self.undo_win.destroy()

        def done(res):
            self.data_changed("pools")
            self.space_measure()
        self.run_job("Putting a cleanup back",
                     lambda: W.undo_cleanup(self.state, self.records, rec), done)

    def space_leftovers(self):
        """Debris no other step reaches: OS metadata, and a sidecar whose
        modlist is gone. Never an archive, and never live state a running
        session owns."""
        def scan():
            return W.leftover_report()

        def found(rows):
            if not rows:
                messagebox.showinfo("Leftovers", "Nothing left over. Every file here is "
                                                 "either in use or something a stage manages.",
                                    parent=self.root)
                return
            by_cat = {}
            for cat, _p, b in rows:
                n, t = by_cat.get(cat, (0, 0))
                by_cat[cat] = (n + 1, t + b)
            total = sum(b for _c, _p, b in rows)
            lines = [f"{cat}: {n} item(s), {human(t)}" for cat, (n, t) in sorted(by_cat.items())]
            lines.append("")
            lines += [p for _c, p, _b in rows[:8]]
            if len(rows) > 8:
                lines.append(f"... and {len(rows) - 8} more")
            if not self.confirm("Delete leftovers", lines, total=total, total_label="Total",
                                undo="This is the deletion itself. There is no undo."):
                return

            def done(res):
                count, freed, failures = res
                W.out(f"  {count} leftover(s) deleted, {human(freed)} freed.")
                for p, err in failures:
                    W.out(f"  [WARN] {p}: {err}")
                self.data_changed("records", "pools")
                self.space_measure()
            self.run_job("Deleting leftovers",
                         lambda: W.remove_leftovers(rows, lambda c, p, b: W.out(f"    {p}")),
                         done)
        self.run_job("Looking for leftovers", scan, found)

    # ==================================================================
    # Settings
    # ==================================================================
    def _build_settings(self, f):
        f.columnconfigure(0, weight=1)
        f.rowconfigure(3, weight=1)
        ttk.Label(f, text=f"Your settings live in {CFG.SETTINGS_FILE} beside the scripts; only what "
                          f"differs from the shipped defaults is written. Paths and the manifest "
                          f"folder take effect after a restart; everything else at once.",
                  wraplength=1000).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        # The buttons sit above the form, not below it: the form is longer
        # than the tab at the default window size, and a Save button that has
        # to be scrolled to is a Save button that is not there.
        bar = ttk.Frame(f)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        ttk.Button(bar, text="Save", command=self.settings_save).pack(side="left")
        ttk.Button(bar, text="Reset to defaults", command=lambda: self.settings_fill(CFG.defaults())).pack(
            side="left", padx=6)
        ttk.Button(bar, text="Open the file", command=self._open_settings_file).pack(side="left")
        self.settings_note = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.settings_note).pack(side="left", padx=(12, 0))

        # The API key is not a setting - it is a secret, and has no place in
        # the settings file - so it has a section of its own. It is never
        # shown here, only where it is kept.
        keybox = ttk.LabelFrame(f, text="Nexus API key", padding=8)
        keybox.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        self.key_status = tk.StringVar(value="")
        ttk.Label(keybox, textvariable=self.key_status, wraplength=1000,
                  justify="left").pack(side="top", anchor="w")
        kb = ttk.Frame(keybox)
        kb.pack(side="top", anchor="w", pady=(6, 0))
        self.key_move_btn = ttk.Button(kb, text=f"Move {KS.KEY_FILE} into Credential Manager",
                                       command=self.key_move_in)
        self.key_move_btn.pack(side="left")
        self.key_set_btn = ttk.Button(kb, text="Set a key...", command=self.key_set)
        self.key_set_btn.pack(side="left", padx=6)
        self.key_forget_btn = ttk.Button(kb, text="Forget the stored key",
                                         command=self.key_forget_stored)
        self.key_forget_btn.pack(side="left")

        # The form scrolls. Twenty-odd rows outgrow the tab at the default
        # window size, more so under the dark theme's taller rows, and a
        # setting that cannot be reached without enlarging the window is one
        # that will not be changed. A canvas is the only Tk container that
        # scrolls; the frame it carries is the form.
        canvas = self._register_canvas(tk.Canvas(f, highlightthickness=0, borderwidth=0))
        vsb = ttk.Scrollbar(f, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.grid(row=3, column=0, sticky="nsew")
        vsb.grid(row=3, column=1, sticky="ns")
        form = ttk.Frame(canvas)
        item = canvas.create_window((0, 0), window=form, anchor="nw")
        form.columnconfigure(1, weight=1)

        def resize(_e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(item, width=canvas.winfo_width())

        def wheel(e):
            canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
        form.bind("<Configure>", resize)
        canvas.bind("<Configure>", resize)
        # The wheel scrolls the form while the pointer is over it, and nothing
        # else the rest of the time: a global binding would scroll this tab
        # from every other one.
        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        self.setting_vars = {}
        row = 0
        for key in CFG.USER_SETTINGS:
            default = CFG._DEFAULTS[key]
            ttk.Label(form, text=key).grid(row=row, column=0, sticky="nw", padx=(0, 10), pady=2)
            if isinstance(default, bool):
                var = tk.BooleanVar()
                ttk.Checkbutton(form, variable=var).grid(row=row, column=1, sticky="w")
            elif key in CFG.CHOICES:
                var = tk.StringVar()
                self._register_combo(ttk.Combobox(form, textvariable=var, values=list(CFG.CHOICES[key]),
                                                  state="readonly", width=14)).grid(
                    row=row, column=1, sticky="w")
            elif isinstance(default, dict):
                var = self._register_text(tk.Text(form, height=3, width=80))
                var.grid(row=row, column=1, sticky="ew")
            else:
                var = tk.StringVar()
                ttk.Entry(form, textvariable=var).grid(row=row, column=1, sticky="ew")
            hint = {"GAME_FOLDERS": "JSON object, merged over the defaults",
                    "GAME_DISPLAY": "JSON object, merged over the defaults",
                    "EXCLUDE_POOL_DIRS": "comma-separated", "EXCLUDE": "comma-separated",
                    "TRACK": "comma-separated",
                    "HIDDEN": "comma-separated feed keys; the Browse tab's Hide button writes this",
                    "HIDE_NSFW": "the Browse tab's 'hide NSFW lists' box writes this",
                    "STAGE_LIST": "empty = most recently updated",
                    "WABBAJACK_DIR": "empty = auto-detect", "NOTIFY_URL": "empty = off",
                    "WABBAJACK_AUTO_UPDATE": "bring Wabbajack up to its newest release before "
                                             "an install",
                    "THEME": "the Dark mode switch beside the progress bar writes this",
                    "PARALLEL_SMALL_DOWNLOADS": "files under the threshold in flight at once from "
                                                "direct sources; 1 = one at a time",
                    "SMALL_DOWNLOAD_MB": "a file this size or under counts as small",
                    "ASSIST_CLICK_AHEAD": "open the next Nexus page while the current file downloads",
                    "ASSIST_QUEUE_DEPTH": "with click-ahead, how many clicked files may wait behind the "
                                          "one downloading; 1 keeps you in step with it",
                    "ASSIST_WINDOW": "show the Nexus pages in a window of their own, not your browser",
                    "ARCHIVE_ROOT": "restart to apply", "MANIFEST_DIR": "restart to apply"}.get(key, "")
            ttk.Label(form, text=hint, style="Hint.TLabel").grid(row=row, column=2, sticky="w",
                                                                  padx=(8, 12))
            self.setting_vars[key] = var
            row += 1

    def key_fill(self):
        """The API key section, from where the downloader would find the key
        now: a line saying where, and the buttons that apply to that."""
        self.key_status.set(f"The key the downloader uses is {KS.describe(A.HERE)}. It is "
                            f"never shown or logged. Credential Manager keeps it with this "
                            f"Windows account, so a copy of this folder on another machine "
                            f"needs it again.")
        can = KS.available()
        self.key_move_btn.configure(state="normal" if can and KS.read_file(A.HERE) else "disabled")
        self.key_set_btn.configure(state="normal" if can else "disabled")
        self.key_forget_btn.configure(state="normal" if can and KS.read_stored() else "disabled")

    def key_move_in(self):
        if not messagebox.askyesno(
                "Nexus API key",
                f"Move the key in {KS.KEY_FILE} into Windows Credential Manager, and delete the "
                f"file once the stored copy reads back the same?\n\nA copy of this folder on "
                f"another machine will then need the key given to it again.", parent=self.root):
            return
        try:
            W.out(KS.move_file_in(A.HERE))
        except (OSError, ValueError) as e:
            messagebox.showerror("Nexus API key", f"Not moved: {e}.\n\n{KS.KEY_FILE} is untouched.",
                                 parent=self.root)
        self.key_fill()

    def key_set(self):
        key = simpledialog.askstring(
            "Nexus API key", "Paste your Personal API key from nexusmods.com (your account, "
                             "API Keys). It is kept in Windows Credential Manager, and not "
                             "shown again.", show="*", parent=self.root)
        if key is None:
            return
        try:
            KS.store(key)
        except (OSError, ValueError) as e:
            messagebox.showerror("Nexus API key", f"Not stored: {e}.", parent=self.root)
        else:
            W.out(f"The Nexus API key is stored in Windows Credential Manager as {KS.TARGET}.")
        self.key_fill()

    def key_forget_stored(self):
        if not messagebox.askyesno(
                "Nexus API key", "Remove the key from Windows Credential Manager? Nexus "
                                 "downloads will then need one again.", parent=self.root):
            return
        KS.forget()
        W.out("The Nexus API key was removed from Windows Credential Manager.")
        self.key_fill()

    def settings_fill(self, values=None):
        """Fill the form with the settings as they stand - or, given `values`
        (Reset to defaults), with those, keeping what the form was last filled
        from as the baseline, so that a save writes every field the reset
        changed rather than nothing at all."""
        self.tab_filled("Settings")
        self.key_fill()
        reset = values is not None
        if not reset:
            values = CFG.current_settings()
            # The two paths as the next start will use them, which is what
            # the file says: once a new one is saved, the running value is the
            # old one, and shown that, a change could not be undone by typing
            # the old value back - it read as no change at all.
            upcoming = CFG.next_start_settings()
            for key in CFG.RESTART_SETTINGS:
                values[key] = upcoming[key]
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
        # What the form says untouched, so a save can tell which fields were
        # actually changed (settings_save).
        if not reset:
            self._settings_baseline, _problems = self.settings_collect()

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
        """Save the fields changed in the form, and only those.

        The rest of the form is what was in it when it was filled, which can
        be older than the file: a pool fold writes GAME_FOLDERS, another
        window or a hand edit writes anything. Saving the whole form wrote
        all of it back, so a fold was undone by the next save of any field,
        and a TRASH_KEEP_DAYS typed into the file went back to what the
        window started with. The changes go over the file as it is now."""
        values, problems = self.settings_collect()
        if problems:
            messagebox.showerror("Settings", "Not saved:\n\n" + "\n".join(problems), parent=self.root)
            return
        base = getattr(self, "_settings_baseline", None) or {}
        changes = {k: v for k, v in values.items() if base.get(k) != v}
        if not changes:
            self.settings_note.set("Nothing in the form was changed, so nothing was saved.")
            return
        path, more = CFG.update_settings(changes)
        A.reload_settings()
        self.assist = (A.NEXUS_MODE == "assist")
        self.apply_theme(CFG.THEME)
        self.browse_nsfw.set(bool(CFG.HIDE_NSFW))
        # A setting can move the archive root or rename a pool folder, so every
        # other tab is rebuilt from scratch rather than guessing what survived.
        self.data_changed("settings", "records", "pools")
        self.settings_fill()
        if more:
            messagebox.showwarning("Settings", "Saved, but these kept their defaults:\n\n" + "\n".join(more),
                                   parent=self.root)
        self.settings_note.set(f"Saved to {path}." + ("  Restart for path changes to take effect."
                                                    if any(k in changes for k in ("ARCHIVE_ROOT", "MANIFEST_DIR"))
                                                    else ""))
        W.out(f"Settings saved to {os.path.basename(path)}: {', '.join(sorted(changes))}.")

    def _open_settings_file(self):
        path = CFG.settings_path()
        if not os.path.isfile(path):
            CFG.update_settings({})
        try:
            os.startfile(path)
        except OSError as e:
            messagebox.showerror("Settings", f"Could not open {path}:\n{e}", parent=self.root)


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
               f"Start-up failed (exit {rc}). wj_autoupdate.log in {A.runtime_path()} says why - "
               f"usually the archive share is not connected, or xxhash is missing from the "
               f"environment.")
        return rc
    lock_fd = None
    if not no_lock:
        lock_fd = A.acquire_lock(A.runtime_path(A.LOCK_FILE))
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


def cli(argv=None):
    """The window's flags, parsed in one place.

    Kept out of the __main__ block so a build's launcher can hand the window
    the same flags the command line would, rather than reproducing them."""
    import argparse
    ap = argparse.ArgumentParser(description="Wabbajack Wizard, the desktop window.")
    ap.add_argument("--nexus-mode", choices=("skip", "assist"))
    ap.add_argument("--allow-size-only", action="store_true")
    ap.add_argument("--no-lock", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    return main(nexus_mode=a.nexus_mode, allow_size_only=a.allow_size_only,
                no_lock=a.no_lock, verbose=a.verbose)


if __name__ == "__main__":
    sys.exit(cli())
