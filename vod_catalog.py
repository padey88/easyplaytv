#!/usr/bin/env python3
"""Build the on-demand catalogue that the Xtream proxy exposes to TiviMate.

Three stages, cheapest first:

  1. candidates  Cinemeta (Stremio's public IMDb catalogues), deep-paginated
                 across genres. Depth matters: the bare `top` feed ignores
                 `skip` and always returns the same 50 titles.
  2. filter      EasyPlay's own per-title lookup reports which providers carry
                 the title. Only ~6% of IMDb carry `moviebox`, and only
                 moviebox returns a direct MP4 — `vidsrc` returns a web-player
                 embed that TiviMate cannot play. Fetching this first is far
                 cheaper than attempting a resolve on every candidate.
  3. verify      Resolve a real stream (S1E1 for series). Anything that does
                 not come back kind="direct" is dropped, never shipped broken.

Output: public/vod.json
"""
import concurrent.futures
import json
import os
import sys
import time

import vod

DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(DIR, "public")

MOVIE_GENRES = ["Action", "Adventure", "Animation", "Comedy", "Crime", "Documentary",
                "Drama", "Family", "Fantasy", "History", "Horror", "Music",
                "Mystery", "Romance", "Sci-Fi", "Thriller", "War", "Western"]
SERIES_GENRES = MOVIE_GENRES + ["Reality-TV", "Talk-Show", "Game-Show"]

PAGES = 30       # 50 titles per page, per genre
WORKERS = 12
LANG = "en"


def collect(kind, genres, pages=PAGES):
    """Cinemeta per-genre catalogues, de-duplicated by IMDb id."""
    seen, out = set(), []
    for g in genres:
        for p in range(pages):
            path = "catalog/%s/top/genre=%s&skip=%d.json" % (kind, g, p * 50)
            try:
                page = vod.cinemeta(path).get("metas") or []
            except Exception as e:
                print("catalog %s/%s page %d failed: %s" % (kind, g, p, e), flush=True)
                break
            if not page:
                break
            for m in page:
                iid = m.get("imdb_id") or m.get("id") or ""
                if not iid.startswith("tt") or iid in seen:
                    continue
                seen.add(iid)
                out.append({
                    "imdb_id": iid,
                    "name": m.get("name") or "",
                    "year": str(m.get("releaseInfo") or m.get("year") or "")[:4],
                    "poster": m.get("poster") or "",
                    "background": m.get("background") or "",
                    "description": (m.get("description") or "").replace("\n", " ").strip(),
                    "genres": m.get("genre") or [],
                    "rating": m.get("imdbRating") or "",
                    "type": kind,
                })
        print("  %s/%s: %d unique so far" % (kind, g, len(out)), flush=True)
    return out


def provider_ok(item):
    """True when EasyPlay says moviebox carries this title."""
    d = vod.title(item["imdb_id"], item["type"], lang=LANG)
    return bool(d and "moviebox" in (d.get("providers") or []))


def verify(item):
    """Resolve a real stream; keep only direct MP4s."""
    try:
        if item["type"] == "series":
            r = vod.resolve(item["imdb_id"], 1, 1, lang=LANG, provider="moviebox")
        else:
            r = vod.resolve(item["imdb_id"], lang=LANG, provider="moviebox")
    except Exception:
        return None, None
    if r and r.get("kind") == "direct" and r.get("url"):
        return item, r
    return None, None


def episodes_of(imdb_id):
    try:
        eps = vod.episodes(imdb_id, lang=LANG)
    except Exception:
        return []
    return [{"season": s, "episode": e} for s, e in eps if s and e]


def _run(fn, items, label, workers=WORKERS):
    """Map fn over items concurrently, preserving nothing but results."""
    out = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(fn, i): i for i in items}
        for fut in concurrent.futures.as_completed(futs):
            done += 1
            try:
                out.append((futs[fut], fut.result()))
            except Exception:
                out.append((futs[fut], None))
            if done % 500 == 0:
                print("  %s %d/%d" % (label, done, len(futs)), flush=True)
    return out


def build():
    os.makedirs(OUT, exist_ok=True)
    started = time.time()

    limit = None
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    movies = collect("movie", MOVIE_GENRES, PAGES)
    series = collect("series", SERIES_GENRES, PAGES)
    if limit:
        movies, series = movies[:limit], series[:limit]
    print("candidates: %d movies, %d series" % (len(movies), len(series)), flush=True)

    # stage 2 — provider filter
    mb_movies = [i for i, ok in _run(provider_ok, movies, "filter movies") if ok]
    mb_series = [i for i, ok in _run(provider_ok, series, "filter series") if ok]
    print("moviebox carries: %d movies, %d series" % (len(mb_movies), len(mb_series)),
          flush=True)

    # stage 3 — verify real streams
    kept_m, kept_s = [], []
    for item, r in _run(verify, mb_movies + mb_series, "verify"):
        if not (item and r):
            continue
        rec = dict(item)
        rec["resolution"] = r.get("resolution") or 0
        (kept_m if item["type"] == "movie" else kept_s).append(rec)

    print("series: fetching episode lists ...", flush=True)
    ep_map = {}
    for s, eps in _run(episodes_of, [x["imdb_id"] for x in kept_s], "episodes"):
        ep_map[s] = eps or []
    for s in kept_s:
        s["episodes"] = ep_map.get(s["imdb_id"], [])

    kept_s = [s for s in kept_s if s["episodes"]]
    kept_m.sort(key=lambda m: (m["name"].lower(), m["year"]))
    kept_s.sort(key=lambda s: s["name"].lower())

    data = {
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "movies": kept_m,
        "series": kept_s,
    }
    with open(os.path.join(OUT, "vod.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

    print("kept %d movies, %d series in %.0fs"
          % (len(kept_m), len(kept_s), time.time() - started))
    return data


if __name__ == "__main__":
    build()
