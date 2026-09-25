#!/usr/bin/env python3
"""
Where the Nexus API key is kept
===============================
The downloader needs a Nexus API key for any Nexus file, Premium or free: it
is what redeems each download link. The key is looked for in this order:

  1. --apikey, for one run;
  2. the NEXUS_API_KEY environment variable;
  3. Windows Credential Manager, as a generic credential named TARGET -
     where git's token for this checkout already goes, and for the same
     reason: so the folder itself holds no secret;
  4. nexus_api_key.txt beside the scripts, in plain text. That is how the key
     was always kept and it is still read, but it is a secret sitting in a
     folder on a share that another machine mounts too.

store() puts a key into Credential Manager, move_file_in() moves the one in
nexus_api_key.txt there and deletes the file once the stored copy reads back
the same, and forget() removes the stored one. The key itself is never
logged, printed or shown by anything here.

What Credential Manager costs is what the git token already pays: the key
belongs to this Windows account on this machine, so a copy of the folder on
another machine has to be given it again.

Nothing is installed for this. Credential Manager is part of Windows, and it
is reached through its own API (advapi32) with ctypes. Anywhere else there is
no store, and the environment variable and the file are all there is.
"""

import os
import sys

# The credential's name in Credential Manager (Control Panel > Credential
# Manager > Windows Credentials > Generic Credentials).
TARGET = "WabbajackWizard/NexusApiKey"
KEY_FILE = "nexus_api_key.txt"
ENV_VAR = "NEXUS_API_KEY"

# What resolve() reports as a key's source.
FROM_CLI = "--apikey"
FROM_ENV = ENV_VAR
FROM_STORE = "Windows Credential Manager"
FROM_FILE = KEY_FILE

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168


class WindowsCredentials:
    """The real store: Windows Credential Manager, through advapi32.

    A generic credential holds a name, a user name and a blob. The key is
    kept as UTF-16, as Windows keeps a password, so Credential Manager's own
    window shows it the way it shows any other."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes as wt
        self.ctypes = ctypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wt.DWORD), ("dwHighDateTime", wt.DWORD)]

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [("Flags", wt.DWORD), ("Type", wt.DWORD),
                        ("TargetName", wt.LPWSTR), ("Comment", wt.LPWSTR),
                        ("LastWritten", FILETIME),
                        ("CredentialBlobSize", wt.DWORD),
                        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                        ("Persist", wt.DWORD), ("AttributeCount", wt.DWORD),
                        ("Attributes", ctypes.c_void_p),
                        ("TargetAlias", wt.LPWSTR), ("UserName", wt.LPWSTR)]

        self.CREDENTIAL = CREDENTIAL
        api = ctypes.WinDLL("advapi32", use_last_error=True)
        api.CredReadW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD,
                                  ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
        api.CredReadW.restype = wt.BOOL
        api.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wt.DWORD]
        api.CredWriteW.restype = wt.BOOL
        api.CredDeleteW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD]
        api.CredDeleteW.restype = wt.BOOL
        api.CredFree.argtypes = [ctypes.c_void_p]
        api.CredFree.restype = None
        self.api = api

    def _fail(self):
        err = self.ctypes.get_last_error()
        return OSError(err, self.ctypes.FormatError(err).strip())

    def read(self, target):
        """The secret stored under `target`, or None when there is none."""
        ctypes = self.ctypes
        p = ctypes.POINTER(self.CREDENTIAL)()
        if not self.api.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(p)):
            if ctypes.get_last_error() == ERROR_NOT_FOUND:
                return None
            raise self._fail()
        try:
            c = p.contents
            blob = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)
        finally:
            self.api.CredFree(p)
        return blob.decode("utf-16-le", errors="replace")

    def write(self, target, secret, user):
        ctypes = self.ctypes
        blob = secret.encode("utf-16-le")
        buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        c = self.CREDENTIAL()
        c.Type = CRED_TYPE_GENERIC
        c.TargetName = target
        c.Comment = "Nexus Mods API key, for Wabbajack Wizard"
        c.CredentialBlobSize = len(blob)
        c.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
        c.Persist = CRED_PERSIST_LOCAL_MACHINE
        c.UserName = user
        if not self.api.CredWriteW(ctypes.byref(c), 0):
            raise self._fail()

    def delete(self, target):
        """True when something was removed, False when there was nothing."""
        if not self.api.CredDeleteW(target, CRED_TYPE_GENERIC, 0):
            if self.ctypes.get_last_error() == ERROR_NOT_FOUND:
                return False
            raise self._fail()
        return True


# The backend. None until first asked for; a test puts a stand-in here.
STORE = None
_NO_STORE = object()

# Set in the environment, this process uses no credential store at all: for
# a process that must not reach this Windows account's credentials. The test
# sandboxes set it for every script they run.
NO_STORE_ENV = "WJ_NO_CREDENTIAL_STORE"


def _store():
    global STORE
    if STORE is None:
        if os.environ.get(NO_STORE_ENV):
            STORE = _NO_STORE
        else:
            try:
                STORE = WindowsCredentials() if sys.platform == "win32" else _NO_STORE
            except (OSError, AttributeError):
                STORE = _NO_STORE
    return None if STORE is _NO_STORE else STORE


def available():
    """Is there a Credential Manager to keep the key in?"""
    return _store() is not None


def key_problem(key):
    """Why `key` cannot be a Nexus API key, or None. Only the obvious: the
    key's own check is Nexus's, when the downloader first uses it."""
    if not isinstance(key, str) or not key.strip():
        return "it is empty"
    if any(c.isspace() for c in key.strip()):
        return "it has a space or a line break in it"
    return None


def read_stored():
    """The key in Credential Manager, or None. A store that cannot be read
    answers None: the caller then looks where it looked before."""
    s = _store()
    if s is None:
        return None
    try:
        key = (s.read(TARGET) or "").strip()
    except OSError:
        return None
    return key or None


def store(key):
    """Keep `key` in Credential Manager, and prove it by reading it back.
    Raises ValueError for a key that cannot be one, OSError when the store
    refuses or answers with something else."""
    why = key_problem(key)
    if why:
        raise ValueError(f"not a Nexus API key: {why}")
    s = _store()
    if s is None:
        raise OSError("there is no Windows Credential Manager here")
    key = key.strip()
    s.write(TARGET, key, "nexus")
    if read_stored() != key:
        raise OSError("Credential Manager did not give the key back as it was stored")


def forget():
    """Remove the stored key. True when there was one."""
    s = _store()
    if s is None:
        return False
    return bool(s.delete(TARGET))


def key_file(folder):
    return os.path.join(folder, KEY_FILE)


def read_file(folder):
    """The key in nexus_api_key.txt in `folder`: its first line, or None."""
    try:
        with open(key_file(folder), encoding="utf-8-sig") as f:
            key = f.readline().strip()
    except OSError:
        return None
    return key or None


def resolve(cli_key, folder):
    """(key, source): the key to use and where it came from, in the order the
    docstring gives, or (None, None) when there is none anywhere."""
    if cli_key and cli_key.strip():
        return cli_key.strip(), FROM_CLI
    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        return env, FROM_ENV
    key = read_stored()
    if key:
        return key, FROM_STORE
    key = read_file(folder)
    if key:
        return key, FROM_FILE
    return None, None


def move_file_in(folder):
    """Move the key in nexus_api_key.txt into Credential Manager.

    The file is deleted only once the stored copy has been read back and is
    the same key, so a store that fails leaves the key where it was. Returns
    a sentence saying what happened; raises OSError or ValueError, the file
    untouched, when it could not be done."""
    key = read_file(folder)
    if key is None:
        raise ValueError(f"there is no key in {key_file(folder)}")
    store(key)
    os.remove(key_file(folder))
    return (f"The key from {KEY_FILE} is in Windows Credential Manager now, as "
            f"{TARGET}, and the file is gone.")


def describe(folder):
    """Where the key the downloader would use comes from, in words, for the
    Settings tab and --nexus-key-status. Never the key itself."""
    _key, source = resolve(None, folder)
    if source == FROM_ENV:
        return f"from the {ENV_VAR} environment variable"
    if source == FROM_STORE:
        also = (f"; {KEY_FILE} is still beside the scripts, and is no longer read"
                if read_file(folder) else "")
        return f"in Windows Credential Manager, as {TARGET}{also}"
    if source == FROM_FILE:
        return (f"in {KEY_FILE} beside the scripts, in plain text"
                + ("; Credential Manager can keep it instead" if available() else ""))
    return "not set: Nexus downloads need one"
