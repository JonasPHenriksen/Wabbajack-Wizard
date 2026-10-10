#!/usr/bin/env python3
"""
Wabbajack CDN multi-part downloader
===================================
Files on the Wabbajack CDN (authored-files.wabbajack.org and friends) are NOT
plain HTTP downloads. The URL in a modlist feed or a manifest's State.Url is a
directory prefix, not a file. A plain GET on it returns a small BunnyCDN error
page, which is why a naive download produces a ~1 KB file no matter how big the
real one is.

The real protocol, matching Wabbajack.Downloaders.WabbajackCDN:

    GET <url>/definition.json.gz      -> gzipped JSON FileDefinition
        { "Hash": <base64 xxHash64>, "Size": <int>, "MungedName": <str>,
          "Parts": [ { "Index": n, "Offset": n, "Size": n, "Hash": <b64> }, ... ] }

    GET <url>/parts/<Index>           -> the raw bytes of that part

Each part is hash-checked on arrival and written at its own Offset, then the
assembled file is checked against the definition's Hash.

This module is used by both wj_autoupdate.py (for .wabbajack manifests, which
are served this way) and wj_downloader.py (for archives whose State is
WabbajackCDNDownloader).

Resume: completed part indices are recorded in a small "<dest>.parts.json"
sidecar, so an interrupted 4 GB download picks up where it stopped instead of
starting over. The sidecar is removed on success.

Memory: only a few parts are fetched or held at once (WINDOW_PER_WORKER per
worker), however many the file has. Holding every finished part until the
end held the whole file in memory - 4.7 GB for the largest manifest here.
"""

import os
import sys
import io
import json
import gzip
import time
import base64
import struct
import logging
import threading
import http.client
import urllib.error
import urllib.parse
import urllib.request
import concurrent.futures

LOG = logging.getLogger("wj_cdn")

try:
    import xxhash  # type: ignore
    _HAVE_XXHASH = True
except Exception:
    _HAVE_XXHASH = False

# From WabbajackCDNDownloader.DomainRemaps. Either side of each pair is a CDN
# URL; the public name is what we actually request.
DOMAIN_REMAPS = {
    "wabbajack.b-cdn.net":         "authored-files.wabbajack.org",
    "wabbajack-mirror.b-cdn.net":  "mirror.wabbajack.org",
    "wabbajack-patches.b-cdn.net": "patches.wabbajack.org",
    "wabbajacktest.b-cdn.net":     "test-files.wabbajack.org",
}

try:
    import wj_config as _CFG
    USER_AGENT = _CFG.USER_AGENT
except ImportError:                     # usable on its own, with a plain name
    USER_AGENT = "WabbajackWizard"

try:
    from wj_paths import web_url_problem, urlopen as _urlopen, publish as _publish
except ImportError:                     # on its own: the same rules, inline
    def web_url_problem(url):
        """Why `url` is not an address to fetch, or None: http and https
        only, as wj_paths.web_url_problem says at length."""
        if not isinstance(url, str) or not url.strip():
            return "there is no address"
        try:
            scheme = urllib.parse.urlsplit(url.strip()).scheme.lower()
        except ValueError as e:
            return f"it is not an address ({e})"
        if scheme not in ("http", "https"):
            return f"{scheme}: is not a web address" if scheme else "it is not a web address"
        return None

    def _urlopen(request, timeout=None):
        """urllib's own opener: the address is checked by the caller. A
        redirect is followed as urllib follows it - wj_paths.urlopen, which
        refuses any but a web one, is what the suite itself uses."""
        return urllib.request.urlopen(request, timeout=timeout)

    def _publish(part, dest, same=None):
        """wj_paths.publish, inline: a different file already at `dest` is
        moved to the first free 'Name~N.ext' beside it, never replaced."""
        aside = None
        if os.path.lexists(dest) and not (same is not None and same(dest)):
            root, ext = os.path.splitext(dest)
            i = 1
            while (os.path.lexists(f"{root}~{i}{ext}")
                   or os.path.lexists(f"{root}~{i}{ext}.meta")):
                i += 1
            aside = f"{root}~{i}{ext}"
            # Sidecar first, so the two move together or not at all.
            if os.path.isfile(dest + ".meta"):
                os.rename(dest + ".meta", aside + ".meta")
            os.rename(dest, aside)
        os.replace(part, dest)
        return aside
HTTP_TIMEOUT = 120
PART_RETRIES = 4
DEFAULT_WORKERS = 4
# Parts fetched or held at once, per worker: enough to keep every worker
# busy while one part is being written, and no more.
WINDOW_PER_WORKER = 2

# Set when the run is to stop: Ctrl+C, or the window asking. Every part
# fetch looks at it, so a stop costs a part's wait, not the rest of the file.
STOP = threading.Event()

_PROGRESS_BROKEN = False


def say_progress(text):
    """A progress line on stdout that can never fail the download. Under the
    window stdout is a pipe; once the window is closed every write to it
    raises, and a raise here used to end a multi-gigabyte transfer that was
    otherwise fine. The first failure switches progress off for the run."""
    global _PROGRESS_BROKEN
    if _PROGRESS_BROKEN:
        return
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except (OSError, ValueError, AttributeError):
        _PROGRESS_BROKEN = True


# ----------------------------------------------------------------------
def is_cdn_url(url):
    """True if this URL must be fetched with the multi-part protocol."""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        host = urllib.parse.urlsplit(url.strip()).netloc.lower()
    except ValueError:
        return False
    host = host.split("@")[-1].split(":")[0]
    return host in DOMAIN_REMAPS or host in set(DOMAIN_REMAPS.values())


def public_url(url):
    """Rewrite a b-cdn.net host to its public name, as Wabbajack does."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except (ValueError, AttributeError):
        return url
    host = parts.netloc.lower()
    if host in DOMAIN_REMAPS:
        parts = parts._replace(netloc=DOMAIN_REMAPS[host])
    return urllib.parse.urlunsplit(parts)


def sanitize(url):
    """Percent-encode spaces without double-encoding existing escapes. CDN
    URLs routinely contain spaces (e.g. 'Tempus Maledictum.wabbajack_<uuid>')
    and http.client rejects raw spaces outright."""
    try:
        p = urllib.parse.urlsplit(url.strip())
    except (ValueError, AttributeError):
        return url
    path = urllib.parse.quote(p.path, safe="/%:@!$&'()*+,;=~-._")
    query = urllib.parse.quote(p.query, safe="=&%:@!$'()*+,;/?~-._")
    return urllib.parse.urlunsplit((p.scheme, p.netloc, path, query, p.fragment))


def wj_hash(data):
    """base64 of little-endian xxHash64, the form Wabbajack stores."""
    if not _HAVE_XXHASH:
        return None
    h = xxhash.xxh64()
    h.update(data)
    return base64.b64encode(struct.pack("<Q", h.intdigest())).decode("ascii")


def hash_file(path, chunk=1 << 20):
    if not _HAVE_XXHASH:
        return None
    try:
        h = xxhash.xxh64()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(chunk), b""):
                h.update(block)
        return base64.b64encode(struct.pack("<Q", h.intdigest())).decode("ascii")
    except OSError:
        return None


def _field(d, *names, default=None):
    """Wabbajack serialises PascalCase, but be tolerant."""
    if not isinstance(d, dict):
        return default
    for n in names:
        for cand in (n, n[0].lower() + n[1:], n.lower()):
            if cand in d:
                return d[cand]
    return default


def _open(url, extra_headers=None):
    why = web_url_problem(url)
    if why:
        # A URLError, so every caller's handling of an unreachable CDN applies:
        # the definition is fetched first, and its failure is not retried.
        raise urllib.error.URLError(f"refusing {url!r}: {why}")
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(sanitize(url), headers=headers)
    return _urlopen(req, timeout=HTTP_TIMEOUT)


# ----------------------------------------------------------------------
def looks_multipart(url):
    """Quiet probe: True if <url>/definition.json.gz exists and parses.

    Host matching alone is fragile (Wabbajack can add CDN hosts, and authors
    occasionally use the b-cdn.net names directly), so callers that only handle
    a handful of URLs should probe instead of guessing."""
    base = public_url(url).rstrip("/")
    try:
        with _open(base + "/definition.json.gz") as resp:
            raw = resp.read(4096)
    except Exception:
        return False
    return raw[:2] == b"\x1f\x8b" or raw.lstrip()[:1] == b"{"


def get_definition(url):
    """Fetch and parse <url>/definition.json.gz. Returns the dict or None."""
    base = public_url(url).rstrip("/")
    target = base + "/definition.json.gz"
    raw = None
    for attempt in range(1, PART_RETRIES + 1):
        try:
            with _open(target) as resp:
                raw = resp.read()
            break
        except urllib.error.HTTPError as e:
            LOG.error("  [ERROR] CDN definition returned HTTP %s for %s", e.code, base)
            return None
        except http.client.HTTPException as e:
            # The connection dropped mid-body: worth another try, as a part is.
            if attempt == PART_RETRIES or STOP.is_set():
                LOG.error("  [ERROR] The CDN definition for %s kept arriving cut short: %s",
                          base, e)
                return None
            STOP.wait(min(20, 3 * attempt))
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            LOG.error("  [ERROR] Could not reach the CDN for %s: %s", base, e)
            return None

    # The endpoint is gzipped, but urllib may transparently decode it if the
    # server sends Content-Encoding: gzip, so handle both.
    try:
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
    except OSError as e:
        LOG.error("  [ERROR] CDN definition is not valid gzip: %s", e)
        return None

    try:
        definition = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        LOG.error("  [ERROR] CDN definition is not valid JSON: %s", e)
        return None

    parts = _field(definition, "Parts")
    if not isinstance(parts, list) or not parts:
        LOG.error("  [ERROR] CDN definition for %s lists no parts.", base)
        return None
    why = parts_problem(definition)
    if why:
        LOG.error("  [ERROR] CDN definition for %s cannot be used: %s.", base, why)
        return None
    return definition


def parts_problem(definition):
    """Why a definition's parts cannot be laid out as a file, or None.

    The definition is the server's word, and each part is written at the
    offset it gives: a negative or fractional one, a part reaching past the
    file's stated size, or two parts claiming one index would write
    somewhere that is not this file, or crash half-way through."""
    total = _field(definition, "Size")
    if total is not None and (not isinstance(total, int) or isinstance(total, bool)
                              or total < 0):
        return f"its Size {total!r} is not a byte count"
    seen = set()
    for p in _field(definition, "Parts", default=[]):
        idx = _field(p, "Index")
        offset = _field(p, "Offset", default=0)
        size = _field(p, "Size")
        for label, v in (("Index", idx), ("Offset", offset), ("Size", size)):
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                return f"a part's {label} is {v!r}"
        if idx in seen:
            return f"part {idx} is listed twice"
        seen.add(idx)
        if total is not None and offset + size > total:
            return f"part {idx} reaches past the file's end"
    return None


def definition_summary(definition):
    return (_field(definition, "Size"), _field(definition, "Hash"),
            len(_field(definition, "Parts", default=[])))


# ----------------------------------------------------------------------
def _pause(seconds, cancel=None):
    """Wait before a retry, ending at once on a stop or on `cancel`."""
    end = time.time() + seconds
    while time.time() < end:
        if STOP.is_set() or (cancel is not None and cancel.is_set()):
            return
        STOP.wait(min(0.25, max(0.0, end - time.time())))


def _fetch_part(base, part, cancel=None):
    """Download one part, verifying its hash. Returns (index, offset, bytes).

    `cancel` is set when the download this part belongs to has ended - a
    part that failed, a full disk: a part then gives up at its next attempt
    rather than going on retrying for a download nobody is waiting for."""
    idx = _field(part, "Index")
    offset = _field(part, "Offset", default=0) or 0
    size = _field(part, "Size")
    want = _field(part, "Hash")
    url = f"{base}/parts/{idx}"

    last = None
    for attempt in range(1, PART_RETRIES + 1):
        if STOP.is_set() or (cancel is not None and cancel.is_set()):
            raise RuntimeError("stopped; the download resumes next run")
        try:
            with _open(url) as resp:
                data = resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError,
                TimeoutError, OSError, http.client.HTTPException) as e:
            # HTTPException is a body that stopped short (IncompleteRead):
            # the same transient failure as a reset connection, which used to
            # fail the whole archive at once instead of being retried.
            last = e
            _pause(min(20, 3 * attempt), cancel)
            continue
        if size is not None and len(data) != size:
            last = RuntimeError(f"part {idx} is {len(data)} bytes, expected {size}")
            _pause(min(20, 3 * attempt), cancel)
            continue
        if want and _HAVE_XXHASH:
            got = wj_hash(data)
            if got != want:
                last = RuntimeError(f"part {idx} hash {got} != {want}")
                _pause(min(20, 3 * attempt), cancel)
                continue
        return idx, offset, data
    raise RuntimeError(f"part {idx} failed after {PART_RETRIES} attempts: {last}")


def _sidecar_path(dest):
    return dest + ".parts.json"


def _load_sidecar(dest, url, total_size):
    try:
        with open(_sidecar_path(dest), encoding="utf-8") as f:
            s = json.load(f)
        if s.get("url") == url and s.get("size") == total_size:
            return set(s.get("done", []))
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return set()


def _save_sidecar(dest, url, total_size, done):
    try:
        with open(_sidecar_path(dest), "w", encoding="utf-8") as f:
            json.dump({"url": url, "size": total_size, "done": sorted(done)}, f)
    except OSError:
        pass


def _drop_sidecar(dest):
    try:
        os.remove(_sidecar_path(dest))
    except OSError:
        pass


def download(url, dest, workers=DEFAULT_WORKERS, progress=True,
             expected_size=None, expected_hash=None, detail=False,
             strict_hash=False):
    """Fetch a Wabbajack CDN multi-part file to `dest`.

    Returns True on success. Writes to `dest + ".part"` and renames on
    completion, so a partial file never appears under the real name.

    strict_hash=True refuses, before fetching a single part, a definition
    whose whole-file Hash differs from `expected_hash`, or whose Size differs
    from `expected_size`. The definition describes what the CDN serves today;
    the caller's hash and size pin what it needs. When they differ the
    download can only produce a file the caller will reject, so the multi-GB
    transfer is better not started - and a file already under the name that
    holds the bytes the CDN serves is not replaced by a copy of itself only
    for the caller to discard it.
    """
    # detail=True changes the return type, so every exit has to go through
    # this. Updating only the success path left the failure paths returning a
    # bare False, which a caller unpacking a tuple cannot survive.
    def done_(ok, computed=None):
        return (ok, computed) if detail else ok

    base = public_url(url).rstrip("/")
    definition = get_definition(base)
    if definition is None:
        return done_(False)

    total_size, total_hash, n_parts = definition_summary(definition)
    parts = _field(definition, "Parts", default=[])

    # Cross-check against whatever the caller was told to expect.
    if expected_size is not None and total_size is not None and total_size != expected_size:
        if strict_hash:
            LOG.error("  [ERROR] The CDN is serving a different build: its definition says "
                      "%s bytes, the manifest/feed wants %s. Not downloading a file that "
                      "would fail verification.", total_size, expected_size)
            return done_(False)
        LOG.warning("  [WARN] CDN says %s bytes but the manifest/feed says %s. "
                    "Trusting the CDN definition.", total_size, expected_size)
    if expected_hash and total_hash and expected_hash != total_hash:
        if strict_hash:
            LOG.error("  [ERROR] The CDN is serving a different build: its "
                      "definition hash is %s, the manifest/feed wants %s. Not "
                      "downloading a file that would fail verification.",
                      total_hash, expected_hash)
            return done_(False)
        LOG.warning("  [WARN] CDN definition hash differs from the expected hash. "
                    "The published build may have been replaced.")

    part_file = dest + ".part"
    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)

    done = _load_sidecar(dest, base, total_size)
    # The sidecar is trustworthy only if the .part file actually reaches past
    # the parts it claims. The file is no longer pre-sized (see below), so it
    # grows as parts land and is exactly total_size only once complete.
    if done:
        try:
            need = max((_field(p, "Offset", default=0) or 0)
                       + (_field(p, "Size", default=0) or 0)
                       for p in parts if _field(p, "Index") in done)
            have = os.path.getsize(part_file)
            if have < need or (total_size and have > total_size):
                done = set()
        except (OSError, ValueError):
            done = set()
    if done:
        LOG.info("  Resuming: %d of %d part(s) already fetched.", len(done), n_parts)

    # Deliberately NOT pre-sized. truncate() to the final length looks free on
    # a local disk, but on an SMB share it is a synchronous zero-fill: measured
    # here at ~17.5 MB/s, so a 4.7 GB manifest spent about four and a half
    # minutes allocating before a single byte was downloaded - over half its
    # total time, and a cost proportional to size, which is why every download
    # appeared to plateau at the same ~9 MB/s regardless of how big it was.
    #
    # Writing at an absolute offset past EOF extends the file anyway, and the
    # CDN returns parts close to in order, so writes stay near the end and the
    # filesystem never has a large gap to zero.
    try:
        mode = "r+b" if (done and os.path.exists(part_file)) else "wb"
        fh = open(part_file, mode)
    except OSError as e:
        LOG.error("  [ERROR] Cannot open %s for writing: %s", part_file, e)
        return done_(False)

    todo = iter([p for p in parts if _field(p, "Index") not in done])
    written = sum(_field(p, "Size", default=0) or 0
                  for p in parts if _field(p, "Index") in done)
    started = time.time()
    failed = None

    # A few parts in flight, never the whole file. Every finished part used
    # to stay referenced until the last one arrived - peak memory the size of
    # the file - and an error writing one, a full disk say, or Ctrl+C, left
    # every queued part to be fetched before it took effect.
    window = max(2, WINDOW_PER_WORKER * max(1, workers))
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers))
    in_flight = {}
    # Set when this download ends, however it ends: a part still running
    # then gives up at its next attempt instead of retrying on for nobody,
    # which with the network down kept the process from exiting for minutes.
    cancel = threading.Event()

    def top_up():
        while len(in_flight) < window and not STOP.is_set() and failed is None:
            p = next(todo, None)
            if p is None:
                return
            in_flight[pool.submit(_fetch_part, base, p, cancel)] = p

    try:
        top_up()
        while in_flight and failed is None:
            finished, _pending = concurrent.futures.wait(
                list(in_flight), timeout=1.0,
                return_when=concurrent.futures.FIRST_COMPLETED)
            if STOP.is_set():
                failed = RuntimeError("stopped; the download resumes next run")
                break
            for fut in finished:
                in_flight.pop(fut, None)
                try:
                    idx, offset, data = fut.result()
                except Exception as e:        # part exhausted its retries
                    failed = e
                    break
                try:
                    fh.seek(offset)
                    fh.write(data)
                    fh.flush()
                except OSError as e:          # a full disk, a share gone away
                    failed = e
                    break
                done.add(idx)
                written += len(data)
                del data
                if len(done) % 25 == 0:
                    _save_sidecar(dest, base, total_size, done)
                if progress and total_size:
                    pct = written * 100 / total_size
                    rate = written / max(0.1, time.time() - started) / (1 << 20)
                    say_progress(f"\r    {pct:5.1f}%  {written / (1 << 20):.0f}/"
                                 f"{total_size / (1 << 20):.0f} MiB  "
                                 f"{len(done)}/{n_parts} parts  {rate:.1f} MiB/s   ")
            top_up()
    finally:
        # Whatever ended the loop - a failed part, a failed write, Ctrl+C -
        # nothing queued is fetched after it, and what has been written is
        # recorded for the resume.
        cancel.set()
        for fut in in_flight:
            fut.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        try:
            fh.close()
        except OSError:
            pass
        _save_sidecar(dest, base, total_size, done)
        if progress:
            say_progress("\r" + " " * 78 + "\r")

    if failed is not None:
        LOG.error("  [ERROR] %s", failed)
        LOG.info("  %d of %d part(s) kept; re-run to resume.", len(done), n_parts)
        return done_(False)

    if len(done) != n_parts:
        LOG.error("  [ERROR] Only %d of %d part(s) completed.", len(done), n_parts)
        return done_(False)

    # Every part is written, so the highest offset+size is the file length and
    # this should already hold. Guard anyway: a definition whose parts do not
    # tile the whole file would otherwise leave a short file to be hashed.
    try:
        if total_size and os.path.getsize(part_file) != total_size:
            with open(part_file, "r+b") as fx:
                fx.truncate(total_size)
    except OSError:
        pass

    # Whole-file check before publishing it under the real name.
    verified = None
    if total_hash and _HAVE_XXHASH:
        got = hash_file(part_file)
        verified = got
        if got is None:
            # Not read is not checked: the file is held open elsewhere, or
            # the share dropped. Kept, with its sidecar, for the next try.
            LOG.error("  [ERROR] The assembled file could not be read to hash it; "
                      "not publishing it. Re-run to try again.")
            return done_(False)
        if got != total_hash:
            LOG.error("  [ERROR] Assembled file hash %s != definition %s. "
                      "Discarding and clearing resume state.", got, total_hash)
            _drop_sidecar(dest)
            try:
                os.remove(part_file)
            except OSError:
                pass
            return done_(False)

    # A different file already under this name - another list's build of
    # it - is set aside under a '~N' name and kept, never replaced. One
    # holding these very bytes is simply replaced.
    def same(existing):
        try:
            if total_size is not None and os.path.getsize(existing) != total_size:
                return False
        except OSError:
            return False
        return bool(verified) and hash_file(existing) == verified
    try:
        aside = _publish(part_file, dest, same=same)
    except OSError as e:
        LOG.error("  [ERROR] Could not finalise %s: %s", dest, e)
        return done_(False)
    if aside:
        LOG.warning("  [KEPT] A different build of %s was already there; it is kept as %s "
                    "rather than replaced.", os.path.basename(dest), os.path.basename(aside))

    _drop_sidecar(dest)
    # detail=True hands back the hash we just computed, so a caller that also
    # wants to verify does not have to read the whole file off the share a
    # second time. On a 5 GB manifest over SMB that second pass costs about a
    # minute and cannot discover anything the first one missed.
    return done_(True, verified)
