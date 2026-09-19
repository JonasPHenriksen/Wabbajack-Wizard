#!/usr/bin/env python3
"""
The build's front door: one bundle, several programs
===================================================

A PyInstaller build has one entry point, and this suite has several: the
window, the console updater, and the downloader and sorter that the updater
launches as child processes. Rather than build four bundles that would each
carry their own copy of Python, one bundle carries them all and this decides,
on each start, which one it is being asked to be.

Two binaries come out of the build, and they differ the way python.exe and
pythonw.exe differ - one has a console, one has none:

    WabbajackWizard.exe            the window
    WabbajackWizard-console.exe    the console side, and every stage

Anything after a `--wj-stage=` token is a stage: the updater puts that token
in front of the flags it was already passing, and the child wakes up as the
downloader or the sorter with its own argv intact. Nothing else in the suite
knows this file exists; from source, the scripts still run as scripts.

Renaming the binaries is not supported. The window finds its console twin by
name to hand it work, and the choice below is made the same way.
"""

import importlib
import os
import sys

import wj_paths

# Everything that can be asked for by name. The updater only ever asks for the
# downloader and the sorter; the rest are here because on a machine with no
# Python, a build is the only way left to run them by hand.
STAGES = ("wj_autoupdate", "wj_downloader", "wj_sorter", "wj_merge_pool",
          "wj_gui")


class _Sink:
    """Somewhere for output to go when there is no console to write to.

    The window has no stdout - true of pythonw for as long as this tool has
    existed, and true of the windowed binary now. That is fine for the code
    that knows it (everything goes to the log pane and the log file), but
    argparse writes errors to stderr before any of our code gets a say, and a
    stray print() in a shared module should not be able to end a run. Both are
    better swallowed than fatal; the log file has the real story either way."""

    def write(self, s):
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False


def run_stage(name, argv):
    """Become one of the programs in this bundle.

    Its argv is rebuilt as it would have looked on a real command line, so the
    argument parsers underneath need no idea any of this happened. A stage
    that returns nothing finished cleanly; one that calls sys.exit is left
    alone to do it."""
    if name not in STAGES:
        sys.stderr.write(f"Unknown stage: {name}\n")
        return 2
    sys.argv = [name + ".py"] + list(argv)
    mod = importlib.import_module(name)
    # The window takes flags the others do not, so it parses its own.
    entry = getattr(mod, "cli", None) or mod.main
    try:
        rc = entry()
    except KeyboardInterrupt:
        sys.stderr.write("\n[ABORT] Interrupted.\n")
        return 130
    return 0 if rc is None else rc


def main():
    if sys.stdout is None:
        sys.stdout = _Sink()
    if sys.stderr is None:
        sys.stderr = _Sink()

    argv = sys.argv[1:]
    if argv and argv[0].startswith(wj_paths.STAGE_FLAG):
        return run_stage(argv[0][len(wj_paths.STAGE_FLAG):], argv[1:])

    if wj_paths.frozen():
        # Which binary am I? The one with a console does the console work.
        console = (os.path.basename(sys.executable).lower()
                   == wj_paths.CONSOLE_EXE.lower())
    else:
        # From source there is one interpreter and no such distinction, so
        # fall back to run.bat's rule: bare means the window, flags mean the
        # console side. Only used when this file is run directly, for testing.
        console = bool(argv)

    return run_stage("wj_autoupdate" if console else "wj_gui", argv)


if __name__ == "__main__":
    sys.exit(main())
