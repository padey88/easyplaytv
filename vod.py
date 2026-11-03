#!/usr/bin/env python3
"""EasyPlay on-demand: catalog browsing + playback resolution.

Two providers sit behind the app's /playback/v1/resolve endpoint:

  moviebox  -> kind="direct", a signed MP4 (+ Referer/User-Agent headers).
               Needs audio_language set explicitly ("en") or the API returns
               language_episode_unavailable.
  vidsrc    -> kind="embed", a web player URL. Not directly playable in
               TiviMate, so it is only used as a fallback and emitted as an
               .m3u8-less "open in browser" entry.

Catalog metadata comes from Cinemeta (Stremio's public IMDb catalog) plus
EasyPlay's own per-title lookup for descriptions / poster / availability.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

import easyplay

PLAYBACK = "https://easyapi.sh/playback/v1/resolve"
TITLE = "https://easyapi.sh/catalog/languages/v2/title"
EPISODES = "https://easyapi.sh/catalog/languages/v2/episodes"
CINEMETA = "https://v3-cinemeta.strem.io"
UA = "Mozilla/5.0"


def _headers():
    dev = easyplay._device()
    return {
        "Content-Type": "application/json",
        "X-Device-Id": dev["device_id"],
        "X-Installation-Token": dev["installation_token"],
        "User-Agent": "EasyPlay-Android/" + easyplay.APP_VERSION,
    }


def _get_json(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def _post_json(url, body, timeout=40):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=_headers())
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


# ------------------------------------------------------------------- catalog

def cinemeta(path):
    """path like 'catalog/movie/top.json' — follows the 307 to the real host."""
    return _get_json(CINEMETA + "/" + path)


def catalog(kind="movie", which="top", skip=0, limit=100, genre=""):
    """Cinemeta catalog page -> [{imdb_id, name, year, poster, description, ...}]."""
    q = "?skip=%d" % skip
    if genre:
        q += "&genre=" + urllib.parse.quote(genre)
    raw = cinemeta("catalog/%s/%s.json%s" % (kind, which, q))
    metas = raw.get("metas") or []
    out = []
    for m in metas[:limit]:
        out.append({
            "imdb_id": m.get("imdb_id") or m.get("id") or "",
            "name": m.get("name") or "",
            "year": str(m.get("releaseInfo") or m.get("year") or "")[:4],
            "poster": m.get("poster") or "",
            "background": m.get("background") or "",
            "description": (m.get("description") or "").replace("\n", " ").strip(),
            "genres": m.get("genre") or [],
            "rating": m.get("imdbRating") or "",
            "type": kind,
        })
    return out


def title(imdb_id, kind="movie", lang=""):
    """EasyPlay catalog detail (adds availability + audio languages)."""
    q = "%s?kind=%s&id=%s" % (TITLE, kind, urllib.parse.quote(imdb_id))
    if lang:
        q += "&language=" + urllib.parse.quote(lang)
    try:
        d = _get_json(q)
    except Exception as e:
        print("title lookup failed %s: %s" % (imdb_id, e), flush=True)
        return None
    metas = d.get("metas") or []
    return metas[0] if metas else None


def episodes(imdb_id, lang=""):
    """[(season, episode, availability)] for a series."""
    q = "%s?id=%s" % (EPISODES, urllib.parse.quote(imdb_id))
    if lang:
        q += "&language=" + urllib.parse.quote(lang)
    try:
        d = _get_json(q)
    except Exception as e:
        print("episodes lookup failed %s: %s" % (imdb_id, e), flush=True)
        return []
    return [(e.get("season"), e.get("episode"), e.get("availability"))
            for e in (d.get("episodes") or [])]


# ------------------------------------------------------------------ playback

def resolve(imdb_id, season=0, episode=0, lang="en", provider="moviebox",
            resolution=1080):
    """Resolve a playable stream.

    Returns {"kind": "direct"|"embed", "url", "headers": {...}, "expires_at": int}
    or None when nothing is available.
    """
    body = {
        "title_id": imdb_id,
        "mode": "audio",
        "preferred_provider": provider,
        "maximum_resolution": resolution,
    }
    if season:
        body["season"] = int(season)
        body["episode"] = int(episode)
    if lang:
        body["audio_language"] = lang
    try:
        d = _post_json(PLAYBACK, body)
    except urllib.error.HTTPError as e:
        # 404 == unavailable for this provider; caller falls back
        return None
    except Exception as e:
        print("resolve failed %s: %s" % (imdb_id, e), flush=True)
        return None
    pb = d.get("playback") or {}
    if not pb.get("url"):
        return None
    return {
        "kind": pb.get("kind") or "direct",
        "url": pb["url"],
        "headers": pb.get("headers") or {},
        "expires_at": pb.get("expires_at") or 0,
        "resolution": pb.get("resolution") or 0,
        "mime": pb.get("mime_type") or "",
        "duration": pb.get("duration") or 0,
        "provider": d.get("provider") or provider,
    }


def best_stream(imdb_id, season=0, episode=0, langs=("en", ""),
                providers=("moviebox", "vidsrc")):
    """Try providers/languages in order, direct streams before embeds."""
    fallback = None
    for prov in providers:
        for lang in langs:
            r = resolve(imdb_id, season, episode, lang=lang, provider=prov)
            if not r:
                continue
            if r["kind"] == "direct":
                return r
            fallback = fallback or r
    return fallback


if __name__ == "__main__":
    print("catalog sample:")
    for m in catalog("movie", "top", limit=5):
        print("  %s  %s (%s) %s" % (m["imdb_id"], m["name"], m["year"], m["rating"]))
    print("\ndetail:", title("tt0111161"))
    print("\nepisodes (Breaking Bad):", episodes("tt0903747")[:3])
    print("\nresolve series:", (best_stream("tt0903747", 1, 1) or {}).get("kind"),
          (best_stream("tt0903747", 1, 1) or {}).get("resolution"))
