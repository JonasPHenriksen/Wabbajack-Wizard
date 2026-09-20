#!/usr/bin/env python3
"""
A window of our own for the Nexus click flow
============================================
Assist mode needs a person to click "Slow download" on one Nexus page per
file. Doing that in the normal browser means a tab per file, several hundred
of them for a large list, and a browser that keeps coming to the front. This
module gives the flow a window of its own, the way Wabbajack's installer does
it: one window, one page at a time, replaced for each file, with a banner
across the top saying which file this is and how many are left.

It is Edge's WebView2 engine, which Windows 10 and 11 ship, reached through
the pywebview library. The window keeps its own browser profile - cookies,
and so the Nexus login - under STORAGE_DIRNAME beside the scripts, so the
sign-in is done once in this window and stays inside the folder with
everything else.

The click itself is captured exactly as before. The page hands the nxm://
link to Windows, Windows runs the registered handler, and the handler
forwards the link to the run that is waiting for it. Nothing here reads the
page or presses anything in it; it only shows it.

Threads: the engine owns the process's main thread from webview.start() to
the window closing. The capture loop therefore runs on a thread the engine
starts for it, talks to the window only through show(), and watches `closed`
so that a window the user shuts ends the run instead of stranding it.
"""

import json
import os
import threading

import wj_paths

try:
    import webview
except Exception:                      # not installed, or .NET refused to load
    webview = None

STORAGE_DIRNAME = ".webview"
TITLE = "Wabbajack Wizard - Nexus downloads"

# WebView2's Evergreen runtime installs here on 64-bit Windows; Windows 11
# ships it, Windows 10 gets it with Edge. A folder named for a version is the
# runtime being present.
RUNTIME_DIRS = (
    os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 "Microsoft", "EdgeWebView", "Application"),
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                 "Microsoft", "EdgeWebView", "Application"),
)

WELCOME_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Wabbajack Wizard</title>
<style>body{margin:0;background:#1f1f1f;color:#e4e4e4;font:15px/1.5 "Segoe UI",sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh}
div{max-width:40em;padding:2em}h1{font-size:1.3em;font-weight:600;margin:0 0 .6em}
p{margin:.4em 0;color:#bdbdbd}</style></head><body><div>
<h1>Nexus downloads</h1>
<p>Each file's Nexus page opens here in turn. Click <b>Slow download</b> on it, wait out
Nexus's short timer, and the file lands in your pools; the next page follows on its own.</p>
<p>Not signed in to Nexus in this window yet? Sign in when the first page asks; this window
keeps its own login, so that is once.</p>
<p>Closing this window ends the run. Everything downloaded so far is kept.</p>
</div></body></html>"""

# Injected into every page once it has loaded. Runs from outside the page,
# so the site's own content-security rules do not apply to it. Idempotent:
# a page that reloads itself gets the banner once.
BANNER_JS = """(function () {
  var b = document.getElementById('wj-assist-banner');
  if (!b) {
    b = document.createElement('div');
    b.id = 'wj-assist-banner';
    b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;' +
      'background:#1f1f1f;color:#e4e4e4;font:14px/1.4 "Segoe UI",sans-serif;' +
      'padding:9px 16px;box-shadow:0 2px 8px rgba(0,0,0,.6);border-bottom:2px solid #4a90d9';
    document.documentElement.appendChild(b);
    document.documentElement.style.marginTop = '42px';
  }
  b.textContent = %s;
})();"""


def runtime_present():
    """Is the WebView2 runtime installed on this machine?"""
    for root in RUNTIME_DIRS:
        try:
            for name in os.listdir(root):
                if name[:1].isdigit() and os.path.isdir(os.path.join(root, name)):
                    return True
        except OSError:
            continue
    return False


def available():
    """Can a window be opened here: Windows, the library, and the engine."""
    return os.name == "nt" and webview is not None and runtime_present()


def why_not():
    """One phrase for the log when the browser is used instead."""
    if os.name != "nt":
        return "not Windows"
    if webview is None:
        return "pywebview is not installed"
    if not runtime_present():
        return "the WebView2 runtime is not installed"
    return "ASSIST_WINDOW is off"


def storage_dir():
    return os.path.join(wj_paths.app_dir(__file__), STORAGE_DIRNAME)


class AssistWindow:
    """One window, one page at a time.

    run(work) opens the window and calls work(self) on a thread of its own;
    it returns when work returns, which closes the window, or when the user
    closes the window, which sets `closed` for work to notice. show(url,
    banner) replaces the page and, once it has loaded, writes the banner
    across its top."""

    def __init__(self, title=TITLE):
        self.title = title
        self.window = None
        self.closed = threading.Event()
        self._banner = ""
        self.error = None

    def run(self, work):
        self.window = webview.create_window(self.title, html=WELCOME_HTML,
                                            width=1280, height=920, min_size=(800, 600),
                                            text_select=True)
        self.window.events.closed += self._on_closed
        self.window.events.loaded += self._on_loaded

        def go():
            try:
                work(self)
            except Exception as e:             # surfaced to the caller, not lost in a thread
                self.error = e
            finally:
                if not self.closed.is_set():
                    try:
                        self.window.destroy()
                    except Exception:
                        pass
        webview.start(func=go, gui="edgechromium", private_mode=False,
                      storage_path=storage_dir())
        if self.error is not None:
            raise self.error

    def _on_closed(self):
        self.closed.set()

    def _on_loaded(self):
        self._inject()

    def show(self, url, banner=""):
        """Replace the page. The banner is written when the page has loaded."""
        self._banner = banner
        if self.closed.is_set():
            return False
        try:
            self.window.load_url(url)
            return True
        except Exception:
            return False

    def _inject(self):
        if not self._banner or self.closed.is_set():
            return
        try:
            self.window.evaluate_js(BANNER_JS % json.dumps(self._banner))
        except Exception:
            pass
