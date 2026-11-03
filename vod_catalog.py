#!/usr/bin/env python3
"""Build the on-demand catalogue that the Xtream proxy exposes to TiviMate.

Staged and resumable — each stage caches to disk, so a crash or a kill costs
only the stage in flight, not the whole run:

  1. candidates  Cinemeta (Stremio's public IMDb catalogues), deep-paginated
                 across genres  -> public/candidates.jsonl
  2. filter      EasyPlay's per-title lookup reports which providers carry a
                 title. Only ~6% carry `moviebox`, and only moviebox returns a
                 direct MP4 (`vidsrc` returns a web-player embed TiviMate cannot
                 play). Far cheaper than resolving every candidate.
                 -> public/candidates_mb.jsonl
  3. verify      Resolve a real stream (S1E1 for series); keep only kind=direct.
                 -> public/vod.json

Run `python3 vod_catalog.py` to do all three, or pass a stage name:
  candidates | filter | verify

Depth note: the bare `top` Cinemeta feed ignores `skip` and always returns the
same 50 titles, so breadth has to come from the genre feeds, which do paginate.
"""
import concurrent.futures
import json
import os
import sys
import time

import vod

DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(DIR, "public")

CAND = os.path.join(OUT, "candidates.jsonl")
CAND_MB = os.path.join(OUT, "candidates_mb.jsonl")
VODJSON = os.path.join(OUT, "vod.json")

GENRES = ["Action", "Adventure", "Animation", "Comedy", "Crime", "Documentary",
          "Drama", "Family", "Fantasy", "History", "Horror", "Music",
          "Mystery", "Romance", "Sci-Fi", "Thriller", "War", "Western"]
SERIES_EXTRA = ["Reality-TV", "Talk-Show", "Game-Show"]

PAGES = 30       # 50 titles per page, per genre
WORKERS = 6      # this box has 4 GB; 12 workers is enough to OOM it
LANG = "en"


# --------------------------------------------------------------- stage 1
def load_candidates():
    seen, out = set(), []
    if os.path.exists(CAND):
        with open(CAND, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r["imdb_id"] not in seen:
                    seen.add(r["imdb_id"])
                    out.append(r)
    return out, seen


def stage_candidates():
    os.makedirs(OUT, exist_ok=True)
    existing, seen = load_candidates()
    done_keys = _done_genres()
    # resume from whatever is already on disk
    with open(CAND, "a", encoding="utf-8") as fh:
        for kind, genres in (("movie", GENRES), ("series", GENRES + SERIES_EXTRA)):
            for g in genres:
                key = "%s/%s" % (kind, g)
                if key in done_keys:
                    continue
                for p in range(PAGES):
                    path = "catalog/%s/top/genre=%s&skip=%d.json" % (kind, g, p * 50)
                    try:
                        page = vod.cinemeta(path).get("metas") or []
                    except Exception as e:
                        print("catalog %s/%s p%d: %s" % (kind, g, p, e), flush=True)
                        break
                    if not page:
                        break
                    for m in page:
                        iid = m.get("imdb_id") or m.get("id") or ""
                        if not iid.startswith("tt") or iid in seen:
                            continue
                        seen.add(iid)
                        rec = {
                            "imdb_id": iid,
                            "name": m.get("name") or "",
                            "year": str(m.get("releaseInfo") or m.get("year") or "")[:4],
                            "poster": m.get("poster") or "",
                            "background": m.get("background") or "",
                            "description": (m.get("description") or "").replace("\n", " ").strip(),
                            "genres": m.get("genre") or [],
                            "rating": m.get("imdbRating") or "",
                            "type": kind,
                        }
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    fh.flush()
                print("  %s/%-12s total %d" % (kind, g, len(seen)), flush=True)
                _mark_genre(key)
    print("candidates: %d" % len(seen), flush=True)


DONE = os.path.join(OUT, "genres_done.txt")


def _done_genres():
    if not os.path.exists(DONE):
        return set()
    with open(DONE, encoding="utf-8") as f:
        return {l.strip() for l in f if l.strip()}


def _mark_genre(key):
    with open(DONE, "a", encoding="utf-8") as f:
        f.write(key + "\n")


# --------------------------------------------------------------- stage 2
def provider_ok(item):
    try:
        d = vod.title(item["imdb_id"], item["type"], lang=LANG)
    except Exception:
        return False          # 404/502 for an unknown title is normal, not fatal
    return bool(d and "moviebox" in (d.get("providers") or []))


def stage_filter():
    items, _ = load_candidates()
    todo = [i for i in items if i["imdb_id"] not in _mb_ids()]
    print("filter: %d candidates, %d already done" % (len(todo), len(_mb_ids())), flush=True)
    done = 0
    with open(CAND_MB, "a", encoding="utf-8") as out, \
            concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(provider_ok, i): i for i in todo}
        for fut in concurrent.futures.as_completed(futs):
            item = futs[fut]
            done += 1
            try:
                ok = fut.result()
            except Exception:
                ok = False
            if ok:
                out.write(json.dumps({k: item[k] for k in
                                      ("imdb_id", "name", "year", "type",
                                       "poster", "genres")}, ensure_ascii=False) + "\n")
                out.flush()
            if done % 2000 == 0:
                print("  filter %d/%d" % (done, len(todo)), flush=True)
    print("filter: %d moviebox titles" % len(_mb_ids()), flush=True)


def _mb_ids():
    ids = set()
    if os.path.exists(CAND_MB):
        with open(CAND_MB, encoding="utf-8") as f:
            for line in f:
                try:
                    ids.add(json.loads(line)["imdb_id"])
                except Exception:
                    pass
    return ids


# --------------------------------------------------------------- stage 3
def verify(item):
    try:
        if item["type"] == "series":
            r = vod.resolve(item["imdb_id"], 1, 1, lang=LANG, provider="moviebox")
        else:
            r = vod.resolve(item["imdb_id"], lang=LANG, provider="moviebox")
    except Exception:
        return None
    if r and r.get("kind") == "direct" and r.get("url"):
        item = dict(item)
        item["resolution"] = r.get("resolution") or 0
        return item
    return None


def episodes_of(imdb_id):
    try:
        eps = vod.episodes(imdb_id, lang=LANG)
    except Exception:
        return []
    return [{"season": s, "episode": e} for s, e in eps if s and e]


def stage_verify():
    cands = {}
    for item in load_candidates()[0]:
        cands[item["imdb_id"]] = item
    mb = []
    with open(CAND_MB, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            full = cands.get(rec["imdb_id"], rec)
            full.update({k: v for k, v in rec.items() if v})
            mb.append(full)
    print("verify: %d moviebox titles" % len(mb), flush=True)

    kept_m, kept_s = [], []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
        for rec in ex.map(verify, mb):
            done += 1
            if rec:
                (kept_m if rec["type"] == "movie" else kept_s).append(rec)
            if done % 500 == 0:
                print("  verify %d/%d -> %d m, %d s"
                      % (done, len(mb), len(kept_m), len(kept_s)), flush=True)

    print("episodes: %d series" % len(kept_s), flush=True)
    ep_map = {}
    with concurrent.futures.ThreadPoolExecutor(WORKERS) as ex:
        for sid, eps in zip([s["imdb_id"] for s in kept_s],
                            ex.map(episodes_of, [s["imdb_id"] for s in kept_s])):
            ep_map[sid] = eps or []
    for s in kept_s:
        s["episodes"] = ep_map.get(s["imdb_id"], [])
    kept_s = [s for s in kept_s if s["episodes"]]

    kept_m.sort(key=lambda m: (m["name"].lower(), m["year"]))
    kept_s.sort(key=lambda s: s["name"].lower())
    data = {"built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "movies": kept_m, "series": kept_s}
    with open(VODJSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print("wrote %s: %d movies, %d series" % (VODJSON, len(kept_m), len(kept_s)),
          flush=True)


def main():
    stages = sys.argv[1:] or ["candidates", "filter", "verify"]
    started = time.time()
    for s in stages:
        print("== %s ==" % s, flush=True)
        {"candidates": stage_candidates,
         "filter": stage_filter,
         "verify": stage_verify}[s]()
    print("done in %.0fs" % (time.time() - started), flush=True)


if __name__ == "__main__":
    main()
