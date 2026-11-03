#!/usr/bin/env python3
"""Build the on-demand catalogue served by the Xtream proxy.

Stages:
  candidates  Cinemeta genre catalogues -> public/candidates.jsonl
  filter      EasyPlay provider support -> public/provider_cache.json
  verify      Direct MP4 resolution      -> public/vod.json

Every stage is resumable. Completed genre blocks and provider lookups are not
repeated on later runs. Catalogue data is written atomically so a reader never
sees a partial file. Use --rebuild to deliberately fetch a fresh catalogue.

The filter records *both* positive and negative answers. Otherwise every
unavailable IMDb title is fetched again on every run. A short-lived per-process
lock prevents concurrent writers from corrupting or duplicating the cache.
"""
import concurrent.futures
import contextlib
import fcntl
import json
import os
import sys
import time

import vod
from storage import atomic_write, cache_key, read_json, read_jsonl, write_json

DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(DIR, "public")
CAND = os.path.join(OUT, "candidates.jsonl")
LEGACY_CAND_MB = os.path.join(OUT, "candidates_mb.jsonl")
PROVIDER_CACHE = os.path.join(OUT, "provider_cache.json")
FILTER_LOCK = os.path.join(OUT, ".filter.lock")
VODJSON = os.path.join(OUT, "vod.json")
DONE = os.path.join(OUT, "genres_done.txt")

GENRES = ["Action", "Adventure", "Animation", "Comedy", "Crime", "Documentary",
          "Drama", "Family", "Fantasy", "History", "Horror", "Music",
          "Mystery", "Romance", "Sci-Fi", "Thriller", "War", "Western"]
SERIES_EXTRA = ["Reality-TV", "Talk-Show", "Game-Show"]
PAGES = int(os.environ.get("EASYPLAY_CATALOGUE_PAGES", "30"))
WORKERS = int(os.environ.get("EASYPLAY_CATALOGUE_WORKERS", "6"))
LANG = os.environ.get("EASYPLAY_VOD_LANG", "en")


def load_candidates():
    records, malformed = read_jsonl(CAND)
    out, seen = [], set()
    for item in records:
        if not isinstance(item, dict) or not item.get("imdb_id"):
            malformed = True
            continue
        iid = item["imdb_id"]
        if iid not in seen:
            seen.add(iid)
            out.append(item)
    if malformed:
        print("warning: ignored malformed candidate rows; rerun with --rebuild", flush=True)
    return out, seen


def _done_genres():
    try:
        with open(DONE, encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()


def _write_genres(keys):
    atomic_write(DONE, "".join(k + "\n" for k in sorted(keys)))


def stage_candidates(rebuild=False):
    os.makedirs(OUT, exist_ok=True)
    if rebuild:
        for path in (CAND, DONE, PROVIDER_CACHE):
            with contextlib.suppress(FileNotFoundError):
                os.remove(path)
    _, seen = load_candidates()
    done_keys = set() if rebuild else _done_genres()
    records = []

    for kind, genres in (("movie", GENRES), ("series", GENRES + SERIES_EXTRA)):
        for genre in genres:
            key = "%s/%s" % (kind, genre)
            if key in done_keys:
                continue
            genre_records = []
            complete = True
            for page_number in range(PAGES):
                path = "catalog/%s/top/genre=%s&skip=%d.json" % (
                    kind, genre, page_number * 50)
                page = None
                for attempt in range(3):
                    try:
                        page = vod.cinemeta(path).get("metas") or []
                        break
                    except Exception as e:
                        if attempt == 2:
                            print("catalog %s p%d failed: %s" % (key, page_number, e),
                                  flush=True)
                        else:
                            time.sleep(attempt + 1)
                if page is None:
                    # Do not mark an incomplete genre as complete.
                    complete = False
                    break
                if not page:
                    break
                for meta in page:
                    iid = meta.get("imdb_id") or meta.get("id") or ""
                    if not iid.startswith("tt") or iid in seen:
                        continue
                    seen.add(iid)
                    record = {
                        "imdb_id": iid,
                        "name": meta.get("name") or "",
                        "year": str(meta.get("releaseInfo") or meta.get("year") or "")[:4],
                        "poster": meta.get("poster") or "",
                        "background": meta.get("background") or "",
                        "description": (meta.get("description") or "").replace("\n", " ").strip(),
                        "genres": meta.get("genre") or [],
                        "rating": meta.get("imdbRating") or "",
                        "type": kind,
                    }
                    records.append(record)
                    genre_records.append(record)
                print("  %s page %d: %d total" % (key, page_number, len(seen)), flush=True)
            if complete:
                # A short feed also ends in `break` above and is complete.
                done_keys.add(key)
            existing, _ = load_candidates()
            atomic_write(CAND, "".join(json.dumps(r, ensure_ascii=False) + "\n"
                                       for r in existing + records))
            records = []
            _write_genres(done_keys)
            print("  %s complete: %d unique" % (key, len(seen)), flush=True)
    print("candidates: %d" % len(seen), flush=True)


def provider_result(item, attempts=3):
    """(supported, answer_known); a transport failure is never cached as no."""
    for attempt in range(attempts):
        try:
            detail = vod.title(item["imdb_id"], item["type"], lang=LANG)
            return bool(detail and "moviebox" in (detail.get("providers") or [])), True
        except Exception:
            if attempt < attempts - 1:
                time.sleep(attempt + 1)
    return False, False


def _legacy_provider_cache():
    """One-time import from the old positive-only JSONL, if it exists."""
    records, _ = read_jsonl(LEGACY_CAND_MB)
    return {r["imdb_id"]: {"supported": True, "key": ""}
            for r in records if isinstance(r, dict) and r.get("imdb_id")}


def stage_filter():
    items, _ = load_candidates()
    cache = read_json(PROVIDER_CACHE, None)
    if not isinstance(cache, dict):
        cache = _legacy_provider_cache()
    if cache:
        write_json(PROVIDER_CACHE, cache)
    os.makedirs(OUT, exist_ok=True)
    with open(FILTER_LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("another catalogue filter is already running")
        todo = [item for item in items
                if item["imdb_id"] not in cache
                or (cache[item["imdb_id"]].get("key")
                    and cache[item["imdb_id"]].get("key") != cache_key(item))]
        print("filter: %d lookups, %d cached" % (len(todo), len(items) - len(todo)),
              flush=True)
        if not todo:
            return

        completed = 0
        with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
            futures = {pool.submit(provider_result, item): item for item in todo}
            for future in concurrent.futures.as_completed(futures):
                item = futures[future]
                try:
                    supported, known = future.result()
                except Exception:
                    supported, known = False, False
                if known:
                    cache[item["imdb_id"]] = {
                        "key": cache_key(item),
                        "supported": supported,
                    }
                completed += 1
                if completed % 250 == 0 or completed == len(todo):
                    write_json(PROVIDER_CACHE, cache)
                    print("  filter %d/%d" % (completed, len(todo)), flush=True)
        supported = sum(1 for item in items
                        if cache.get(item["imdb_id"], {}).get("supported"))
        print("filter: %d moviebox titles" % supported, flush=True)


def verify(item, attempts=3):
    """Keep only a title whose real playback resolution returns a direct MP4."""
    for attempt in range(attempts):
        try:
            if item["type"] == "series":
                result = vod.resolve(item["imdb_id"], 1, 1, lang=LANG,
                                     provider="moviebox")
            else:
                result = vod.resolve(item["imdb_id"], lang=LANG,
                                     provider="moviebox")
        except Exception:
            result = None
        if result and result.get("kind") == "direct" and result.get("url"):
            record = dict(item)
            record["resolution"] = result.get("resolution") or 0
            return record
        if attempt < attempts - 1:
            time.sleep(1.5 * (attempt + 1))
    return None


def episodes_of(imdb_id):
    try:
        episodes = vod.episodes(imdb_id, lang=LANG)
    except Exception:
        return []
    return [{"season": season, "episode": episode}
            for season, episode, _availability in episodes if season and episode]


def stage_verify():
    candidates, _ = load_candidates()
    cache = read_json(PROVIDER_CACHE, {})
    if not cache:
        cache = _legacy_provider_cache()
    selected = [dict(item) for item in candidates
                if cache.get(item["imdb_id"], {}).get("supported")]
    print("verify: %d moviebox titles" % len(selected), flush=True)

    movies, series = [], []
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        for number, record in enumerate(pool.map(verify, selected), start=1):
            if record:
                (movies if record["type"] == "movie" else series).append(record)
            if number % 100 == 0:
                print("  verify %d/%d" % (number, len(selected)), flush=True)

    def get_episodes(item):
        try:
            return item["imdb_id"], episodes_of(item["imdb_id"])
        except Exception:
            return item["imdb_id"], []

    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        episode_map = dict(pool.map(get_episodes, series))
    for item in series:
        item["episodes"] = episode_map.get(item["imdb_id"], [])
    series = [item for item in series if item["episodes"]]
    movies.sort(key=lambda item: (item["name"].lower(), item["year"], item["imdb_id"]))
    series.sort(key=lambda item: (item["name"].lower(), item["imdb_id"]))
    write_json(VODJSON, {
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "movies": movies,
        "series": series,
    })
    print("wrote %s: %d movies, %d series" % (VODJSON, len(movies), len(series)),
          flush=True)


def main():
    args = sys.argv[1:]
    rebuild = "--rebuild" in args
    stages = [a for a in args if not a.startswith("--")] or [
        "candidates", "filter", "verify"]
    unknown = set(stages) - {"candidates", "filter", "verify"}
    if unknown:
        raise SystemExit("unknown stage: %s" % ", ".join(sorted(unknown)))
    started = time.time()
    for stage in stages:
        print("== %s ==" % stage, flush=True)
        if stage == "candidates":
            stage_candidates(rebuild=rebuild)
        elif stage == "filter":
            stage_filter()
        else:
            stage_verify()
    print("done in %.0fs" % (time.time() - started), flush=True)


if __name__ == "__main__":
    main()
