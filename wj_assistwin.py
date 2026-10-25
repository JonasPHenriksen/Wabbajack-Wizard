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

The click is caught inside the window. The engine announces every navigation
before it starts, and a navigation to an nxm:// link is cancelled there and
handed to the run instead - no protocol handler, no "open this program?"
prompt, nothing leaving the process. The handler stays registered as the
fallback for an engine that will not say. Nothing here reads the page or
presses anything in it; it only shows it and listens for that one link.

The banner also carries a Skip button, for a page with nothing on it to
click - a mod taken off Nexus since the list was built. It is an nxm:// link
of our own under SKIP_URI, so it travels the same road as a click: caught in
the window it raises `skip`, and through the protocol handler it reaches the
run's listener, which knows the host.

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
SKIP_URI = "nxm://wabbajackwizard/skip"     # wj_downloader.NXM_SKIP_PREFIX + "skip"

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
<p>A page with nothing to click - a mod taken off Nexus, say - has <b>Skip this file</b> in the
banner; the file is recorded as not downloaded and the next page follows.</p>
<p>Closing this window ends the run. Everything downloaded so far is kept.</p>
</div></body></html>"""

# Injected into every page once it has loaded. Runs from outside the page,
# so the site's own content-security rules do not apply to it. Idempotent:
# a page that reloads itself gets the banner once. The Skip button is a
# plain link to SKIP_URI: a click on it is a user-initiated launch of an
# external scheme, which is what the window's hook listens for. No literal
# percent sign in here; the text goes in through %-formatting.
BANNER_JS = """(function () {
  var b = document.getElementById('wj-assist-banner');
  if (!b) {
    b = document.createElement('div');
    b.id = 'wj-assist-banner';
    b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;' +
      'display:flex;align-items:center;gap:16px;' +
      'background:#1f1f1f;color:#e4e4e4;font:14px/1.4 "Segoe UI",sans-serif;' +
      'padding:9px 16px;box-shadow:0 2px 8px rgba(0,0,0,.6);border-bottom:2px solid #4a90d9';
    var t = document.createElement('span');
    t.id = 'wj-assist-text';
    t.style.cssText = 'flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
    var s = document.createElement('a');
    s.id = 'wj-assist-skip';
    s.href = '""" + SKIP_URI + """';
    s.textContent = 'Skip this file';
    s.title = 'Nothing to click here? Record this file as not downloaded and go on to the next.';
    s.style.cssText = 'flex:none;color:#e4e4e4;background:#3a3a3a;border:1px solid #666;' +
      'border-radius:4px;padding:3px 10px;text-decoration:none;font-size:13px;cursor:pointer';
    b.appendChild(t);
    b.appendChild(s);
    document.documentElement.appendChild(b);
    document.documentElement.style.marginTop = '42px';
  }
  document.getElementById('wj-assist-text').textContent = %s;
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
    across its top. The banner's Skip button sets `skip`, which work clears
    when it has acted on it."""

    def __init__(self, title=TITLE, on_link=None):
        self.title = title
        self.window = None
        self.closed = threading.Event()
        self.finished = threading.Event()
        self.skip = threading.Event()
        self._banner = ""
        self.error = None
        # Called with each nxm:// link the page tries to open, from the
        # engine's thread. The run sets it to whatever delivers the link to
        # its listener.
        self.on_link = on_link
        self.hooked = False
        self.hook_error = None

    def run(self, work):
        self.window = webview.create_window(self.title, html=WELCOME_HTML,
                                            width=1280, height=920, min_size=(800, 600),
                                            text_select=True)
        self.window.events.closed += self._on_closed
        self.window.events.loaded += self._on_loaded
        self.window.events.shown += self._hook_navigation

        def go():
            # Shown on purpose, not left to the process's start-up state. A
            # child launched with its window hidden - as a console child is,
            # to keep its console off the screen - hands that state to the
            # first window it makes, and this window is that first window.
            try:
                self.window.show()
                self.window.restore()
            except Exception:
                pass
            try:
                work(self)
            except Exception as e:             # surfaced to the caller, not lost in a thread
                self.error = e
            finally:
                self.finished.set()
                if not self.closed.is_set():
                    try:
                        self.window.destroy()
                    except Exception:
                        pass
        webview.start(func=go, gui="edgechromium", private_mode=False,
                      storage_path=storage_dir())
        # The window closing returns control here at once, while the work is
        # still noticing and winding down: stopping the download in flight,
        # counting what it had. Without this wait the caller handed the nxm
        # handler back, printed its summary and went on before that had
        # happened.
        self.finished.wait(60)
        if self.error is not None:
            raise self.error

    def _on_closed(self):
        self.closed.set()

    def close(self):
        """Close the window from any thread: the run being told to stop, which
        ends it the way a person closing the window does - the capture loop
        notices `closed`, hands the nxm:// handler back, and returns."""
        self.closed.set()
        try:
            if self.window is not None:
                self.window.destroy()
        except Exception:
            pass

    def _on_loaded(self):
        self._hook_navigation()
        self._inject()

    def _hook_navigation(self):
        """Subscribe to the engine's NavigationStarting, once.

        Reached through pywebview's Windows backend: the form it keeps per
        window, the Edge control on it, and that control's event. A pywebview
        that arranges those differently leaves `hooked` False, and the run
        then relies on the registered protocol handler as before."""
        if self.hooked or self.window is None:
            return
        try:
            from webview.platforms import winforms
            import System.Windows.Forms as WinForms          # via pythonnet
            form = winforms.BrowserView.instances[self.window.uid]
        except Exception as e:
            self.hook_error = f"{type(e).__name__}: {e}"
            return

        def subscribe():
            # On the window's own thread: the control refuses to be touched
            # from any other, and pywebview raises its events elsewhere.
            try:
                control = form.browser.webview
                core = control.CoreWebView2
                if core is None:               # not initialised yet; the next event retries
                    return
                # An external scheme is not a navigation to the engine: it
                # raises this instead, with the decision left to the host.
                # Unhandled, a launch that did not come straight from a click
                # - Nexus fires its link from a timer after the click - is
                # dropped without a word, which is exactly what a person sees
                # as 'nothing happened'.
                core.LaunchingExternalUriScheme += self._on_external
                control.NavigationStarting += self._on_navigating
                self.hooked = True
            except Exception as e:
                self.hooked = False
                self.hook_error = f"{type(e).__name__}: {e}"
        try:
            form.Invoke(WinForms.MethodInvoker(subscribe))
        except Exception as e:
            self.hook_error = f"{type(e).__name__}: {e}"

    def _take(self, args):
        """Keep an nxm:// link in the process: cancel whatever the engine was
        about to do with it and hand it to the run - or, for the banner's own
        Skip link, raise `skip`. True when it was one."""
        try:
            uri = str(args.Uri)
        except Exception:
            return False
        if not uri.lower().startswith("nxm://"):
            return False
        try:
            args.Cancel = True
        except Exception:
            pass
        if uri.lower().startswith(SKIP_URI):
            self.skip.set()
            return True
        if self.on_link is not None:
            try:
                self.on_link(uri)
            except Exception:
                pass
        return True

    def _on_external(self, sender, args):
        self._take(args)

    def _on_navigating(self, sender, args):
        self._take(args)

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
