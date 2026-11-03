#!/usr/bin/env python3
"""Xtream Codes proxy over EasyPlay — gives TiviMate live TV + VOD in one login.

TiviMate only reads VOD from an Xtream Codes account (player_api.php), not from
an M3U file. This server speaks just enough of the XC protocol for TiviMate 5.x
and maps every call onto EasyPlay's real data.

Endpoints implemented
  player_api.php                              account info
  player_api.php?action=get_live_categories   group-title values
  player_api.php?action=get_live_streams      live channels (+category_id)
  player_api.php?action=get_vod_categories    movie genres
  player_api.php?action=get_vod_streams       movies (verified direct MP4s)
  player_api.php?action=get_vod_info&vod_id=  movie detail + stream URL
  player_api.php?action=get_series_categories series genres
  player_api.php?action=get_series            series list
  player_api.php?action=get_series_info&series_id=  seasons + episodes
  xmltv.php                                   EPG passthrough
  movie/<user>/<pass>/<id>.<ext>              live stream redirect
  series/<user>/<pass>/<episode_id>.<ext>     episode stream redirect
  /playlist.m3u                               M3U for players that prefer it

Streams are 302-redirected to the upstream URL. EasyPlay's direct MP4s are
signed and short-lived, so for those the proxy resolves a fresh URL on each
request instead of caching one; live URLs are long-lived and passed straight
through.

Auth: URL credentials only (TiviMate cannot send HTTP Basic). Set
EASYPLAY_USER / EASYPLAY_PASS, defaults below.
"""
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import vod

DIR = os.path.dirname(os.path.abspath(__file__))
PUB = os.path.join(DIR, "public")
PORT = int(os.environ.get("EASYPLAY_PORT", "8601"))
USER = os.environ.get("EASYPLAY_USER", "easyplay")
PASS = os.environ.get("EASYPLAY_PASS", "")
DEFAULT_LANG = os.environ.get("EASYPLAY_VOD_LANG", "en")
LIVE_UA = "EasyPlay-Android/1.2.119"

_state_lock = threading.Lock()
_state = {"live": None, "vod": None, "ts": 0.0}
LIVE_TTL = 15 * 60


def _load_json(name, default):
    path = os.path.join(PUB, name)
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("cannot load %s: %s" % (name, e), flush=True)
        return default


def live_channels():
    with _state_lock:
        if _state["live"] is None or time.time() - _state["ts"] > LIVE_TTL:
            # build.py writes channels.json; refresh it in-process if missing
            chans = _load_json("channels.json", [])
            if not chans:
                try:
                    import easyplay
                    chans = easyplay.entries()
                except Exception as e:
                    print("live refresh failed: %s" % e, flush=True)
            _state["live"] = chans
            _state["ts"] = time.time()
        return _state["live"]


def vod_data():
    with _state_lock:
        if _state["vod"] is None:
            _state["vod"] = _load_json("vod.json", {"movies": [], "series": []})
        return _state["vod"]


def _cat_id(name):
    """Stable numeric id per category name (XC ids must be ints)."""
    return abs(hash(name)) % 100000


# Stream id namespaces. Live and VOD ids must never collide, because the URL
# path /movie/…/<id> is decoded by range, not by which list it came from.
LIVE_BASE = 1
MOVIE_BASE = 1_000_000         # 1,000,000 + movie index
EPISODE_BASE = 100_000_000     # 100,000,000 + series*100000 + season*100 + ep


def movie_id(index):
    return MOVIE_BASE + index


def episode_id(series_index, season, episode):
    return EPISODE_BASE + int(series_index) * 100000 + int(season) * 100 + int(episode)


def split_episode_id(eid):
    rem = int(eid) - EPISODE_BASE
    if rem < 0:
        raise ValueError("not an episode id")
    sid, rem = divmod(rem, 100000)
    season, episode = divmod(rem, 100)
    return sid, season, episode


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------- helpers
    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._send(200, json.dumps(obj, ensure_ascii=False), "application/json")

    def _redirect(self, url, headers=None):
        self.send_response(302)
        self.send_header("Location", url)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass

    def _base(self):
        host = self.headers.get("Host") or "127.0.0.1:%d" % PORT
        return "http://" + host

    def _authed(self, q):
        if not USER and not PASS:
            return True
        return q.get("username", [""])[0] == USER and q.get("password", [""])[0] == PASS

    # -------------------------------------------------------------- routing
    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        path = u.path
        q = urllib.parse.parse_qs(u.query)

        if path in ("/player_api.php", "/get.php", "/panel_api.php"):
            if not self._authed(q):
                return self._send(401, "unauthorized", "text/plain")
            if path == "/get.php":
                return self._send(200, self._m3u(q), "audio/x-mpegurl")
            return self._player_api(q)

        if path == "/xmltv.php":
            return self._epg()

        if path in ("/", "/playlist.m3u"):
            return self._send(200, self._m3u(q), "audio/x-mpegurl")

        m = re.fullmatch(r"/(?:movie|live)/([^/]+)/([^/]+)/([^/]+?)(?:\.\w+)?", path)
        if m:
            return self._stream_live_or_movie(m.group(3))

        m = re.fullmatch(r"/series/([^/]+)/([^/]+)/([^/]+?)(?:\.\w+)?", path)
        if m:
            return self._stream_episode(m.group(3))

        self._send(404, "not found", "text/plain")

    # ------------------------------------------------------------ player api
    def _player_api(self, q):
        action = q.get("action", [""])[0]
        base = self._base()

        if not action:
            return self._json({
                "user_info": {
                    "username": USER or "easyplay",
                    "password": PASS,
                    "auth": 1,
                    "status": "Active",
                    "exp_date": None,
                    "is_trial": "0",
                    "active_cons": "0",
                    "created_at": "0",
                    "max_connections": "4",
                    "allowed_output_formats": ["m3u8", "ts", "mp4"],
                },
                "server_info": {
                    "url": self.headers.get("Host", "127.0.0.1"),
                    "port": str(PORT),
                    "https_port": "",
                    "server_protocol": "http",
                    "rtmp_port": "",
                    "timezone": "Asia/Kuala_Lumpur",
                    "timestamp_now": int(time.time()),
                    "time_now": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
            })

        live = live_channels()

        if action == "get_live_categories":
            cats = []
            seen = set()
            for c in live:
                g = c.get("group") or "Live TV"
                if g in seen:
                    continue
                seen.add(g)
                cats.append({"category_id": str(_cat_id(g)), "category_name": g,
                             "parent_id": 0})
            return self._json(cats)

        if action == "get_live_streams":
            want = q.get("category_id", [""])[0]
            out = []
            for i, c in enumerate(live, start=1):
                g = c.get("group") or "Live TV"
                if want and str(_cat_id(g)) != want:
                    continue
                out.append({
                    "num": i,
                    "name": c.get("name") or "",
                    "stream_type": "live",
                    "stream_id": i,
                    "stream_icon": c.get("logo") or "",
                    "epg_channel_id": c.get("tvg_id") or "",
                    "added": "0",
                    "category_id": str(_cat_id(g)),
                    "custom_sid": "",
                    "tv_archive": 0,
                    "direct_source": "",
                    "tv_archive_duration": 0,
                })
            return self._json(out)

        d = vod_data()
        movies, series = d.get("movies", []), d.get("series", [])

        if action == "get_vod_categories":
            cats, seen = [], set()
            for m in movies:
                for g in (m.get("genres") or ["Movies"]):
                    if g in seen:
                        continue
                    seen.add(g)
                    cats.append({"category_id": str(_cat_id(g)), "category_name": g,
                                 "parent_id": 0})
            return self._json(cats)

        if action == "get_vod_streams":
            want = q.get("category_id", [""])[0]
            out = []
            for i, m in enumerate(movies, start=1):
                gs = m.get("genres") or ["Movies"]
                if want and not any(str(_cat_id(g)) == want for g in gs):
                    continue
                out.append({
                    "num": i,
                    "name": m.get("name") or "",
                    "stream_type": "movie",
                    "stream_id": movie_id(i),
                    "stream_icon": m.get("poster") or "",
                    "rating": m.get("rating") or "0",
                    "rating_5based": 0,
                    "added": "0",
                    "category_id": str(_cat_id(gs[0])),
                    "container_extension": "mp4",
                    "custom_sid": "",
                    "direct_source": "",
                })
            return self._json(out)

        if action == "get_vod_info":
            try:
                rid = int(q.get("vod_id", ["0"])[0])
                m = movies[rid - MOVIE_BASE]
                idx = rid - MOVIE_BASE + 1
            except (ValueError, IndexError, TypeError):
                return self._send(404, "{}", "application/json")
            stream = "%s/movie/%s/%s/%d.mp4" % (
                base, urllib.parse.quote(USER or "easyplay"),
                urllib.parse.quote(PASS), rid)
            return self._json({
                "info": {
                    "name": m.get("name") or "",
                    "plot": m.get("description") or "",
                    "cover_big": m.get("poster") or "",
                    "movie_image": m.get("poster") or "",
                    "releasedate": (m.get("year") or "") + "-01-01",
                    "rating": m.get("rating") or "0",
                    "duration": "",
                    "genre": ", ".join(m.get("genres") or []),
                },
                "movie_data": {
                    "stream_id": rid,
                    "name": m.get("name") or "",
                    "container_extension": "mp4",
                    "category_id": str(_cat_id((m.get("genres") or ["Movies"])[0])),
                },
                "stream_url": stream,
            })

        if action == "get_series_categories":
            cats, seen = [], set()
            for s in series:
                for g in (s.get("genres") or ["Series"]):
                    if g in seen:
                        continue
                    seen.add(g)
                    cats.append({"category_id": str(_cat_id(g)), "category_name": g,
                                 "parent_id": 0})
            return self._json(cats)

        if action == "get_series":
            want = q.get("category_id", [""])[0]
            out = []
            for i, s in enumerate(series, start=1):
                gs = s.get("genres") or ["Series"]
                if want and not any(str(_cat_id(g)) == want for g in gs):
                    continue
                out.append({
                    "num": i,
                    "name": s.get("name") or "",
                    "series_id": i,
                    "cover": s.get("poster") or "",
                    "plot": s.get("description") or "",
                    "cast": "",
                    "director": "",
                    "genre": ", ".join(gs),
                    "releaseDate": (s.get("year") or "") + "-01-01",
                    "last_modified": "0",
                    "rating": s.get("rating") or "0",
                    "rating_5based": 0,
                    "backdrop_path": [s.get("background") or s.get("poster") or ""],
                    "youtube_trailer": "",
                    "episode_run_time": "0",
                    "category_id": str(_cat_id(gs[0])),
                })
            return self._json(out)

        if action == "get_series_info":
            try:
                sid = int(q.get("series_id", ["0"])[0])
                s = series[sid - 1]
            except (ValueError, IndexError):
                return self._send(404, "{}", "application/json")
            episodes = {}
            for ep in s.get("episodes") or []:
                se, no = ep.get("season"), ep.get("episode")
                if not se or not no:
                    continue
                eid = episode_id(sid, se, no)
                episodes.setdefault(str(se), []).append({
                    "id": str(eid),
                    "episode_num": no,
                    "title": "Episode %d" % no,
                    "container_extension": "mp4",
                    "info": {"plot": "", "duration": ""},
                    "season": se,
                })
            return self._json({
                "seasons": [{"season_number": int(k),
                             "name": "Season %s" % k,
                             "episode_count": len(v),
                             "cover": s.get("poster") or ""}
                            for k, v in sorted(episodes.items(), key=lambda kv: int(kv[0]))],
                "info": {
                    "name": s.get("name") or "",
                    "plot": s.get("description") or "",
                    "cover": s.get("poster") or "",
                    "genre": ", ".join(s.get("genres") or []),
                    "releaseDate": (s.get("year") or "") + "-01-01",
                    "rating": s.get("rating") or "0",
                },
                "episodes": episodes,
            })

        self._send(400, "unknown action", "text/plain")

    # -------------------------------------------------------------- streaming
    def _resolve_url(self, url, headers, hops=5):
        """Follow the redirect chain and return the final URL.

        Live URLs bounce through two edges; TiviMate handles that but the final
        hop only serves the actual TS payload, so resolving up front is safer
        and keeps the player's own redirect handling out of the path.
        """
        cur = url
        for _ in range(hops):
            req = urllib.request.Request(cur, headers=dict(headers or {}), method="GET")
            try:
                up = urllib.request.urlopen(req, timeout=20)
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                    cur = urllib.parse.urljoin(cur, e.headers["Location"])
                    continue
                return None
            except Exception:
                return None
            final = up.geturl() or cur
            up.close()
            return final
        return cur

    def _relay(self, url, headers):
        """Stream upstream bytes through the proxy.

        VOD (moviebox) requires a Referer header that TiviMate cannot send, so
        redirecting does not work — the bytes have to come through here. Range
        requests are forwarded so seeking still works.
        """
        req_headers = dict(headers or {})
        rng = self.headers.get("Range")
        if rng:
            req_headers["Range"] = rng
        req = urllib.request.Request(url, headers=req_headers)
        try:
            up = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            body = e.read()[:200]
            return self._send(e.code, body, "text/plain")
        except Exception as e:
            return self._send(502, str(e), "text/plain")

        status = up.getcode() or 200
        self.send_response(status)
        for h in ("Content-Type", "Content-Length", "Content-Range",
                  "Accept-Ranges", "Last-Modified", "ETag"):
            v = up.headers.get(h)
            if v:
                self.send_header(h, v)
        if not up.headers.get("Content-Length"):
            self.send_header("Connection", "close")
        self.end_headers()
        if getattr(self, "_head", False):
            up.close()
            return
        try:
            while True:
                chunk = up.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # player seeked/stopped — normal
        finally:
            up.close()

    def _stream_live_or_movie(self, ident):
        """Handle /movie/<u>/<p>/<id> — id is namespaced, so route by range."""
        try:
            idx = int(ident)
        except ValueError:
            return self._send(400, "bad id", "text/plain")

        live = live_channels()
        if idx >= EPISODE_BASE:
            return self._send(400, "bad id", "text/plain")
        if idx >= MOVIE_BASE:
            mi = idx - MOVIE_BASE
            movies = vod_data().get("movies") or []
            if mi < 0 or mi >= len(movies):
                return self._send(404, "not found", "text/plain")
            m = movies[mi]
            r = vod.best_stream(m["imdb_id"], langs=(DEFAULT_LANG, ""),
                                providers=("moviebox",))
            if not r:
                return self._send(404, "not available", "text/plain")
            return self._relay(r["url"], r.get("headers") or {})

        li = idx - LIVE_BASE
        if 0 <= li < len(live):
            c = live[li]
            url = c.get("url") or ""
            if not url:
                return self._send(404, "not found", "text/plain")
            # Resolve the edge chain here (up to two hops) and relay the TS
            # through the proxy: the final hop is picky about client headers and
            # some clients get reset on a bare redirect.
            final = self._resolve_url(url, {"User-Agent": LIVE_UA}) or url
            return self._relay(final, {"User-Agent": LIVE_UA})
        self._send(404, "not found", "text/plain")

    def _stream_episode(self, ident):
        try:
            sid, se, no = split_episode_id(int(ident))
        except (ValueError, TypeError):
            return self._send(400, "bad id", "text/plain")
        series = vod_data().get("series", [])
        try:
            s = series[sid - 1]
        except IndexError:
            return self._send(404, "not found", "text/plain")
        r = vod.best_stream(s["imdb_id"], se, no, langs=(DEFAULT_LANG, ""),
                            providers=("moviebox",))
        if not r:
            return self._send(404, "not available", "text/plain")
        return self._relay(r["url"], r.get("headers") or {})

    def do_HEAD(self):
        self._head = True
        try:
            self.do_GET()
        finally:
            self._head = False

    # ---------------------------------------------------------------- epg/m3u
    def _epg(self):
        path = os.path.join(PUB, "epg.xml")
        if not os.path.exists(path):
            return self._send(404, "no epg", "text/plain")
        with open(path, "rb") as f:
            body = f.read()
        self._send(200, body, "application/xml")

    def _m3u(self, q):
        base = self._base()
        u = urllib.parse.quote(USER or "easyplay")
        p = urllib.parse.quote(PASS)
        lines = ['#EXTM3U url-tvg="%s/xmltv.php?username=%s&password=%s"' % (base, u, p)]
        for i, c in enumerate(live_channels(), start=1):
            if c.get("tvg_id"):
                lines.append('#EXTINF:-1 tvg-id="%s" tvg-logo="%s" group-title="%s",%s'
                             % (c["tvg_id"], c.get("logo", ""), c.get("group", "Live TV"),
                                c.get("name", "")))
            else:
                lines.append('#EXTINF:-1 tvg-logo="%s" group-title="%s",%s'
                             % (c.get("logo", ""), c.get("group", "Live TV"),
                                c.get("name", "")))
            lines.append("%s/live/%s/%s/%d.ts" % (base, u, p, i))
        return "\n".join(lines) + "\n"


def main():
    os.makedirs(PUB, exist_ok=True)
    print("EasyPlay Xtream proxy on :%d (user=%s, epg+xc+vod)"
          % (PORT, USER or "open"), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
