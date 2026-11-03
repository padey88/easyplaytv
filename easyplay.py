#!/usr/bin/env python3
"""EasyPlay IPTV (easyplaytv.sh) live-TV client.

Reverse-engineered from EasyPlayIPTV.apk 1.2.119:

  1. POST https://easyapi.sh/live-tv/api/v1/acquire with headers
       X-Device-Id: <16 hex>          X-Installation-Token: <64 hex>
     -> lease JSON containing `playlist_url`, an XUI.one endpoint of the form
        https://<host>/get.php?username=...&password=...&type=m3u_plus&output=m3u8

  2. GET that URL -> ~24.8k channels, tagged by `group-title` per country.

  3. A channel's URL is http://<host>:80/play/<blob>, which 302s twice and
     finally lands on a tokenised .ts on an edge host. Those play URLs keep
     working after the lease expires; only the get.php playlist needs a fresh
     lease, so we re-acquire lazily and cache the parsed entries.

The device identity is generated once and cached on disk (0600) so that repeat
runs re-acquire the same lease instead of minting a new pseudo-account.
"""
import json
import os
import re
import time
import urllib.request

DIR = os.path.dirname(os.path.abspath(__file__))
DEVICE_FILE = os.path.join(DIR, ".easyplay_device.json")

API = "https://easyapi.sh/live-tv/api/v1"
APP_ID = "tv.easyplayiptv.android"
APP_VERSION = "1.2.119"

PLAYLIST_TTL = 20 * 60   # seconds to cache the parsed channel list
LEASE_MARGIN = 60        # re-acquire the lease when within this many seconds of expiry

_lease = {"body": None, "expires_at": 0.0}
_cache = {"ts": 0.0, "entries": []}


def _device():
    """Load or create {"device_id": 16 hex, "installation_token": 64 hex}."""
    try:
        with open(DEVICE_FILE) as f:
            d = json.load(f)
        if re.fullmatch(r"[0-9a-f]{16}", d.get("device_id", "")) and \
           re.fullmatch(r"[0-9a-f]{64}", d.get("installation_token", "")):
            return d
    except (OSError, ValueError):
        pass
    d = {"device_id": os.urandom(8).hex(), "installation_token": os.urandom(32).hex()}
    tmp = DEVICE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, DEVICE_FILE)
    try:
        os.chmod(DEVICE_FILE, 0o600)
    except OSError:
        pass
    return d


def acquire(force=False):
    """Return the acquire response (lease). Reused until it is about to expire."""
    if not force and _lease["body"] and _lease["expires_at"] - time.time() > LEASE_MARGIN:
        return _lease["body"]
    dev = _device()
    req = urllib.request.Request(
        API + "/acquire",
        data=json.dumps({"app_id": APP_ID, "app_version": APP_VERSION}).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Device-Id": dev["device_id"],
            "X-Installation-Token": dev["installation_token"],
            "User-Agent": "EasyPlay-Android/" + APP_VERSION,
        },
    )
    body = json.loads(urllib.request.urlopen(req, timeout=20).read())
    if not body.get("playlist_url"):
        raise RuntimeError("acquire returned no playlist_url: %r" % (body,))
    _lease["body"] = body
    _lease["expires_at"] = float(body.get("expires_at") or time.time() + 300)
    return body


def playlist_url():
    return acquire()["playlist_url"]


def _fetch_playlist():
    req = urllib.request.Request(playlist_url(), headers={
        "User-Agent": "EasyPlay-Android/" + APP_VERSION, "Accept": "*/*"})
    return urllib.request.urlopen(req, timeout=120).read().decode("utf-8", "replace")


def parse_m3u(text):
    """Parse m3u_plus text -> [{name, group, logo, url}].

    Rows whose title is a decorative section marker (contains the ✦ bullet) are
    dropped: they carry a real stream URL but are not channels.
    """
    out, cur = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF"):
            grp = re.search(r'group-title="([^"]*)"', line)
            logo = re.search(r'tvg-logo="([^"]*)"', line)
            cur = {
                "name": line.rsplit(",", 1)[-1].strip(),
                "group": grp.group(1) if grp else "",
                "logo": logo.group(1) if logo else "",
                "url": "",
            }
        elif cur is not None and line and not line.startswith("#"):
            cur["url"] = line
            if "✦" not in cur["name"] and cur["url"].startswith(("http://", "https://")):
                out.append(cur)
            cur = None
    return out


def entries(force=False):
    """All channels, cached for PLAYLIST_TTL. Falls back to the last good list."""
    if not force and _cache["entries"] and time.time() - _cache["ts"] < PLAYLIST_TTL:
        return _cache["entries"]
    try:
        got = parse_m3u(_fetch_playlist())
        if not got:
            raise RuntimeError("empty playlist")
        _cache["entries"], _cache["ts"] = got, time.time()
    except Exception as e:
        print("easyplay sync failed: %s" % e, flush=True)
        if not _cache["entries"]:
            raise
    return _cache["entries"]


def by_group(group, force=False):
    g = group.upper()
    return [e for e in entries(force) if e["group"].upper() == g]


if __name__ == "__main__":
    import collections
    es = entries(force=True)
    print("%d channels" % len(es))
    counts = collections.Counter(e["group"] for e in es)
    for g, n in sorted(counts.items(), key=lambda kv: -kv[1])[:20]:
        print("%6d  %s" % (n, g))
    print("\nlease host: %s" % re.sub(r"username=[^&]*&password=[^&]*", "username=***",
                                      playlist_url()))
