"""Where the Nexus API key is kept, and moving it into Credential Manager.

The key is looked for on the command line, then in NEXUS_API_KEY, then in
Windows Credential Manager, then in nexus_api_key.txt beside the scripts.
Moving the file's key into Credential Manager deletes the file only once the
stored copy reads back the same.

The store here is a stand-in: the suite never reaches the key this Windows
account keeps. With WJ_TEST_CREDMAN=1 set, one round trip also goes through
the real Credential Manager, under a name of its own that is removed again.
"""
import io
import logging
import os
import sys
import types

BOXROOT = os.path.join(os.environ.get("TEMP", "/tmp"), "wj_tests")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import Sandbox, check, summary

print("=" * 70)
print("  T18  THE NEXUS API KEY")
print("=" * 70)

box = Sandbox(os.path.join(BOXROOT, "sb18"))
sys.path.insert(0, box.tools)
os.chdir(box.tools)
import wj_keystore as KS


class FakeStore:
    """Credential Manager as a dict, able to refuse a write or to hand back
    something other than what it was given."""

    def __init__(self):
        self.data, self.fail_write, self.garble = {}, False, False

    def read(self, target):
        return self.data.get(target)

    def write(self, target, secret, user):
        if self.fail_write:
            raise OSError(5, "Access is denied")
        self.data[target] = secret + ("?" if self.garble else "")

    def delete(self, target):
        return self.data.pop(target, None) is not None


fake = FakeStore()
KS.STORE = fake
os.environ.pop(KS.ENV_VAR, None)
folder = box.tools
keyfile = KS.key_file(folder)


def put_file(text):
    with io.open(keyfile, "w", encoding="utf-8") as f:
        f.write(text)


# ---------------------------------------------------------------- 18.1
print("\n 18.1  the order a key is looked for in")
check("with none anywhere there is none", KS.resolve(None, folder) == (None, None))
put_file("from-the-file\n")
check("the file is read", KS.resolve(None, folder) == ("from-the-file", KS.FROM_FILE))
fake.data[KS.TARGET] = "from-the-store"
check("Credential Manager comes before the file",
      KS.resolve(None, folder) == ("from-the-store", KS.FROM_STORE))
os.environ[KS.ENV_VAR] = "from-the-environment"
check("the environment comes before both",
      KS.resolve(None, folder) == ("from-the-environment", KS.FROM_ENV))
check("and --apikey before everything", KS.resolve("  given  ", folder) == ("given", KS.FROM_CLI))
del os.environ[KS.ENV_VAR]
check("the key is never in what describe() says",
      "from-the-store" not in KS.describe(folder) and "Credential Manager" in KS.describe(folder),
      KS.describe(folder))
check("  which also says the file beside the scripts is no longer read",
      "no longer read" in KS.describe(folder), KS.describe(folder))

# ---------------------------------------------------------------- 18.2
print("\n 18.2  moving the file's key into Credential Manager")
fake.data.clear()
put_file("abc123def456\n")
msg = KS.move_file_in(folder)
check("the key is stored", fake.data.get(KS.TARGET) == "abc123def456", str(fake.data))
check("  and the file is gone", not os.path.exists(keyfile))
check("  and the message never names the key", "abc123def456" not in msg, msg)
check("the downloader now finds it in the store",
      KS.resolve(None, folder) == ("abc123def456", KS.FROM_STORE))

put_file("second-key\n")
fake.data.clear()
fake.fail_write = True
refused = False
try:
    KS.move_file_in(folder)
except OSError:
    refused = True
fake.fail_write = False
check("a store that refuses leaves the file where it was",
      refused and io.open(keyfile, encoding="utf-8").read() == "second-key\n" and not fake.data)
fake.garble = True
refused = False
try:
    KS.move_file_in(folder)
except OSError:
    refused = True
fake.garble = False
check("so does one that hands back something else",
      refused and os.path.exists(keyfile), str(fake.data))
os.remove(keyfile)
refused = False
try:
    KS.move_file_in(folder)
except ValueError:
    refused = True
check("with no file there is nothing to move, and it says so", refused)

# ---------------------------------------------------------------- 18.3
print("\n 18.3  what cannot be a key is refused")
for bad in ("", "   ", "two words", "line\nbreak", None):
    refused = False
    try:
        KS.store(bad)
    except ValueError:
        refused = True
    check("18.3 %r is not stored" % (bad,), refused)
KS.store("  padded-key  ")
check("surrounding space is trimmed, not refused", fake.data.get(KS.TARGET) == "padded-key")

# ---------------------------------------------------------------- 18.4
print("\n 18.4  forgetting the stored key")
check("forget() removes it", KS.forget() is True and KS.TARGET not in fake.data)
check("  and a second time says there was none", KS.forget() is False)

# ---------------------------------------------------------------- 18.5
print("\n 18.5  where there is no Credential Manager")
KS.STORE = KS._NO_STORE
put_file("only-file\n")
check("nothing claims there is one", KS.available() is False)
check("the file is still read", KS.resolve(None, folder) == ("only-file", KS.FROM_FILE))
refused = False
try:
    KS.store("x")
except OSError:
    refused = True
check("storing says why it cannot", refused)
check("  and describe() offers nothing it cannot do",
      "plain text" in KS.describe(folder) and "Credential Manager" not in KS.describe(folder),
      KS.describe(folder))
KS.STORE = fake

# ---------------------------------------------------------------- 18.6
print("\n 18.6  the downloader asks in that order, and says when it read the file")
import wj_downloader as D
seen = []
handler = logging.Handler()
handler.emit = lambda record: seen.append(record.getMessage())
D.LOG.handlers = [handler]
D.LOG.setLevel(logging.INFO)
fake.data.clear()
check("from the file", D.resolve_api_key(None, folder, prompt=False) == "only-file")
check("  with a note naming Credential Manager, not the key",
      any("Credential Manager" in m and "only-file" not in m for m in seen), str(seen))
seen.clear()
fake.data[KS.TARGET] = "stored-key"
check("from the store, before the file", D.resolve_api_key(None, folder, prompt=False) == "stored-key")
check("  and then no note", not seen, str(seen))
fake.data.clear()
os.remove(keyfile)
check("with none anywhere and no prompt, none", D.resolve_api_key(None, folder, prompt=False) == "")

# ---------------------------------------------------------------- 18.7
print("\n 18.7  run.bat --store-nexus-key, --forget-nexus-key, --nexus-key-status")
import contextlib
import wj_autoupdate as A


def flags(**kw):
    ns = types.SimpleNamespace(store_nexus_key=False, forget_nexus_key=False,
                               nexus_key_status=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = A.cmd_nexus_key(ns)
    return rc, buf.getvalue()


put_file("console-key\n")
rc, text = flags(store_nexus_key=True)
check("storing moves the file's key in", rc == 0 and fake.data.get(KS.TARGET) == "console-key"
      and not os.path.exists(keyfile), text)
check("  and never prints it", "console-key" not in text, text)
rc, text = flags(nexus_key_status=True)
check("the status says where, not what", rc == 0 and "Credential Manager" in text
      and "console-key" not in text, text)
rc, text = flags(forget_nexus_key=True)
check("forgetting removes it", rc == 0 and KS.TARGET not in fake.data, text)
KS.STORE = KS._NO_STORE
rc, text = flags(store_nexus_key=True)
check("with no store, storing refuses", rc == 2, text)
KS.STORE = fake

# ---------------------------------------------------------------- 18.8
print("\n 18.8  the real Credential Manager, under a test name of its own")
if os.environ.get("WJ_TEST_CREDMAN") == "1" and sys.platform == "win32":
    real = KS.WindowsCredentials()
    target = "WabbajackWizard-test/%d" % os.getpid()
    try:
        real.write(target, "not-a-real-key-0123456789", "test")
        check("a written credential reads back the same",
              real.read(target) == "not-a-real-key-0123456789")
        check("deleting it says so", real.delete(target) is True)
        check("  and it is gone", real.read(target) is None)
        check("  and deleting again says there was none", real.delete(target) is False)
    finally:
        try:
            real.delete(target)
        except OSError:
            pass
else:
    print("   (skipped: set WJ_TEST_CREDMAN=1 to run it against the real store)")

KS.STORE = None
sys.exit(summary())
