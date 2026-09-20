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
HTTP_TIMEOUT = 120
PART_RETRIES = 4
DEFAULT_WORKERS = 4

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
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(sanitize(url), headers=headers)
    return urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)


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
    try:
        with _open(target) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        LOG.error("  [ERROR] CDN definition returned HTTP %s for %s", e.code, base)
        return None
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
    return definition


def definition_summary(definition):
    return (_field(definition, "Size"), _field(definition, "Hash"),
            len(_field(definition, "Parts", default=[])))


# ----------------------------------------------------------------------
def _fetch_part(base, part, attempt_log):
    """Download one part, verifying its hash. Returns (index, offset, bytes)."""
    idx = _field(part, "Index")
    offset = _field(part, "Offset", default=0) or 0
    size = _field(part, "Size")
    want = _field(part, "Hash")
    url = f"{base}/parts/{idx}"

    last = None
    for attempt in range(1, PART_RETRIES + 1):
        try:
            with _open(url) as resp:
                data = resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError,
                TimeoutError, OSError) as e:
            last = e
            time.sleep(min(20, 3 * attempt))
            continue
        if size is not None and len(data) != size:
            last = RuntimeError(f"part {idx} is {len(data)} bytes, expected {size}")
            time.sleep(min(20, 3 * attempt))
            continue
        if want and _HAVE_XXHASH:
            got = wj_hash(data)
            if got != want:
                last = RuntimeError(f"part {idx} hash {got} != {want}")
                time.sleep(min(20, 3 * attempt))
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
    whose whole-file Hash differs from `expected_hash`. The definition
    describes what the CDN serves today; the caller's hash pins what it needs.
    When they differ the download can only produce a file the caller will
    reject, so the multi-GB transfer is better not started.
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

    todo = [p for p in parts if _field(p, "Index") not in done]
    written = sum(_field(p, "Size", default=0) or 0
                  for p in parts if _field(p, "Index") in done)
    started = time.time()
    failed = None

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(_fetch_part, base, p, None): p for p in todo}
            for fut in concurrent.futures.as_completed(futures):
                try:
                    idx, offset, data = fut.result()
                except Exception as e:        # part exhausted its retries
                    failed = e
                    for f in futures:
                        f.cancel()
                    break
                fh.seek(offset)
                fh.write(data)
                fh.flush()
                done.add(idx)
                written += len(data)
                if len(done) % 25 == 0:
                    _save_sidecar(dest, base, total_size, done)
                if progress and total_size:
                    pct = written * 100 / total_size
                    rate = written / max(0.1, time.time() - started) / (1 << 20)
                    say_progress(f"\r    {pct:5.1f}%  {written / (1 << 20):.0f}/"
                                 f"{total_size / (1 << 20):.0f} MiB  "
                                 f"{len(done)}/{n_parts} parts  {rate:.1f} MiB/s   ")
    finally:
        try:
            fh.close()
        except OSError:
            pass
        if progress:
            say_progress("\r" + " " * 78 + "\r")

    _save_sidecar(dest, base, total_size, done)

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
        if got != total_hash:
            LOG.error("  [ERROR] Assembled file hash %s != definition %s. "
                      "Discarding and clearing resume state.", got, total_hash)
            _drop_sidecar(dest)
            try:
                os.remove(part_file)
            except OSError:
                pass
            return done_(False)

    try:
        if os.path.exists(dest):
            os.remove(dest)
        os.replace(part_file, dest)
    except OSError as e:
        LOG.error("  [ERROR] Could not finalise %s: %s", dest, e)
        return done_(False)

    _drop_sidecar(dest)
    # detail=True hands back the hash we just computed, so a caller that also
    # wants to verify does not have to read the whole file off the share a
    # second time. On a 5 GB manifest over SMB that second pass costs about a
    # minute and cannot discover anything the first one missed.
    return done_(True, verified)
