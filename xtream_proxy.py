#!/usr/bin/env python3
"""A small Xtream Codes-compatible server for EasyPlay live TV and VOD.

Run build.py and vod_catalog.py first. No Python packages outside the standard
library are required. Set EASYPLAY_USER and EASYPLAY_PASS before serving it.
All stream routes are authenticated. Live and VOD bytes are relayed with the
upstream headers a TV player cannot supply, including HTTP Range support.
"""
import hmac
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
from identifiers import EPISODE_BASE, LIVE_BASE, MOVIE_BASE, category_id, episode_id, movie_id
from storage import read_json

DIR = os.path.dirname(os.path.abspath(__file__))
PUB = os.path.join(DIR, "public")
PORT = int(os.environ.get("EASYPLAY_PORT", "8601"))
HOST = os.environ.get("EASYPLAY_HOST", "0.0.0.0")
USER = os.environ.get("EASYPLAY_USER", "easyplay")
PASS = os.environ.get("EASYPLAY_PASS", "")
DEFAULT_LANG = os.environ.get("EASYPLAY_VOD_LANG", "en")
PUBLIC_URL = os.environ.get("EASYPLAY_PUBLIC_URL", "").rstrip("/")
LIVE_UA = "EasyPlay-Android/1.2.119"
MAX_CONNECTIONS = int(os.environ.get("EASYPLAY_MAX_CONNECTIONS", "4"))

_state_lock = threading.Lock()
_state = {"live": [], "vod": {"movies": [], "series": []}, "movies": {}, "series": {},
          "episodes": {}, "live_mtime": None, "vod_mtime": None, "live_checked": 0.0}
LIVE_CHECK_INTERVAL = 60


def _mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _index_vod(data):
    movies, series, episodes = {}, {}, {}
    for movie in data.get("movies") or []:
        if movie.get("imdb_id"):
            movies[movie_id(movie["imdb_id"])] = movie
    for item in data.get("series") or []:
        if not item.get("imdb_id"):
            continue
        series[item["imdb_id"]] = item
        for episode in item.get("episodes") or []:
            season, number = episode.get("season"), episode.get("episode")
            if season and number:
                eid = episode_id(item["imdb_id"], season, number)
                episodes[eid] = (item, int(season), int(number))
    return movies, series, episodes


def live_channels():
    with _state_lock:
        now = time.monotonic()
        path = os.path.join(PUB, "channels.json")
        if now - _state["live_checked"] >= LIVE_CHECK_INTERVAL:
            mtime = _mtime(path)
            if mtime != _state["live_mtime"]:
                channels = read_json(path, [])
                if isinstance(channels, list):
                    _state["live"] = channels
                    _state["live_mtime"] = mtime
            _state["live_checked"] = now
        return _state["live"]


def vod_data():
    with _state_lock:
        path = os.path.join(PUB, "vod.json")
        mtime = _mtime(path)
        if mtime != _state["vod_mtime"]:
            data = read_json(path, None)
            if isinstance(data, dict):
                _state["vod"] = data
                _state["movies"], _state["series"], _state["episodes"] = _index_vod(data)
                _state["vod_mtime"] = mtime
        return _state["vod"]


def _movie(ident):
    vod_data()
    with _state_lock:
        return _state["movies"].get(ident)


def _episode(ident):
    vod_data()
    with _state_lock:
        return _state["episodes"].get(ident)


def _categories(items, default):
    categories, seen = [], set()
    for item in items:
        for genre in item.get("genres") or [default]:
            if genre not in seen:
                seen.add(genre)
                categories.append({"category_id": str(category_id(genre)),
                                   "category_name": genre, "parent_id": 0})
    return categories


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, content_type="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not getattr(self, "_head", False):
            self.wfile.write(body)

    def _json(self, obj):
        self._send(200, json.dumps(obj, ensure_ascii=False))

    def log_message(self, format, *args):
        # Access logs are disabled: Xtream request paths contain credentials.
        return

    def _base(self):
        if PUBLIC_URL:
            return PUBLIC_URL
        host = self.headers.get("Host") or "127.0.0.1:%d" % PORT
        return "http://" + host

    @staticmethod
    def _credentials(username, password):
        return hmac.compare_digest(username, USER) and hmac.compare_digest(password, PASS)

    def _authorized(self, query):
        return bool(PASS) and self._credentials(
            query.get("username", [""])[0], query.get("password", [""])[0])

    def do_GET(self):
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            print("request failed: %s" % exc, flush=True)
            if not self.wfile.closed:
                try:
                    self._send(500, "internal error", "text/plain")
                except (BrokenPipeError, ConnectionResetError):
                    pass

    def _route_get(self):
        parsed = urllib.parse.urlsplit(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        self._route = path.split("?", 1)[0]

        if path == "/healthz":
            data = vod_data()
            return self._json({"status": "ok", "live_channels": len(live_channels()),
                               "movies": len(data.get("movies") or []),
                               "series": len(data.get("series") or [])})

        if path == "/player_api.php":
            if not self._authorized(query):
                return self._send(401, "unauthorized", "text/plain")
            return self._player_api(query)

        if path in ("/get.php", "/playlist.m3u"):
            if not self._authorized(query):
                return self._send(401, "unauthorized", "text/plain")
            return self._send(200, self._m3u(), "audio/x-mpegurl; charset=utf-8")

        if path == "/xmltv.php":
            if not self._authorized(query):
                return self._send(401, "unauthorized", "text/plain")
            return self._epg()

        match = re.fullmatch(r"/(?:movie|live)/([^/]+)/([^/]+)/(\d+)(?:\.\w+)?", path)
        if match:
            if not self._credentials(match.group(1), match.group(2)):
                return self._send(401, "unauthorized", "text/plain")
            return self._stream_live_or_movie(int(match.group(3)))

        match = re.fullmatch(r"/series/([^/]+)/([^/]+)/(\d+)(?:\.\w+)?", path)
        if match:
            if not self._credentials(match.group(1), match.group(2)):
                return self._send(401, "unauthorized", "text/plain")
            return self._stream_episode(int(match.group(3)))

        self._send(404, "not found", "text/plain")

    def _player_api(self, query):
        action = query.get("action", [""])[0]
        if not action:
            return self._json({
                "user_info": {
                    "username": USER, "password": PASS, "auth": 1,
                    "status": "Active", "exp_date": None, "is_trial": "0",
                    "active_cons": "0", "created_at": "0",
                    "max_connections": str(MAX_CONNECTIONS),
                    "allowed_output_formats": ["m3u8", "ts", "mp4"],
                },
                "server_info": {
                    "url": urllib.parse.urlsplit(self._base()).hostname or "",
                    "port": str(PORT), "https_port": "", "server_protocol": "http",
                    "rtmp_port": "", "timezone": "Asia/Kuala_Lumpur",
                    "timestamp_now": int(time.time()),
                    "time_now": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
            })

        live = live_channels()
        if action == "get_live_categories":
            groups = list(dict.fromkeys(c.get("group") or "Live TV" for c in live))
            return self._json([{"category_id": str(category_id(group)),
                                "category_name": group, "parent_id": 0} for group in groups])
        if action == "get_live_streams":
            wanted = query.get("category_id", [""])[0]
            result = []
            for number, channel in enumerate(live, start=LIVE_BASE):
                group = channel.get("group") or "Live TV"
                if wanted and str(category_id(group)) != wanted:
                    continue
                result.append({
                    "num": number, "name": channel.get("name") or "",
                    "stream_type": "live", "stream_id": number,
                    "stream_icon": channel.get("logo") or "",
                    "epg_channel_id": channel.get("tvg_id") or "", "added": "0",
                    "category_id": str(category_id(group)), "custom_sid": "",
                    "tv_archive": 0, "direct_source": "", "tv_archive_duration": 0,
                })
            return self._json(result)

        data = vod_data()
        movies, series = data.get("movies") or [], data.get("series") or []
        if action == "get_vod_categories":
            return self._json(_categories(movies, "Movies"))
        if action == "get_vod_streams":
            wanted = query.get("category_id", [""])[0]
            result = []
            for number, movie in enumerate(movies, start=1):
                genres = movie.get("genres") or ["Movies"]
                if wanted and not any(str(category_id(g)) == wanted for g in genres):
                    continue
                result.append({
                    "num": number, "name": movie.get("name") or "",
                    "stream_type": "movie", "stream_id": movie_id(movie["imdb_id"]),
                    "stream_icon": movie.get("poster") or "",
                    "rating": movie.get("rating") or "0", "rating_5based": 0,
                    "added": "0", "category_id": str(category_id(genres[0])),
                    "container_extension": "mp4", "custom_sid": "", "direct_source": "",
                })
            return self._json(result)
        if action == "get_vod_info":
            try:
                ident = int(query.get("vod_id", ["0"])[0])
            except ValueError:
                return self._send(404, "{}")
            movie = _movie(ident)
            if not movie:
                return self._send(404, "{}")
            credentials = "%s/%s" % (urllib.parse.quote(USER, safe=""),
                                     urllib.parse.quote(PASS, safe=""))
            return self._json({
                "info": self._movie_info(movie),
                "movie_data": {"stream_id": ident, "name": movie.get("name") or "",
                               "container_extension": "mp4"},
                "stream_url": "%s/movie/%s/%d.mp4" % (self._base(), credentials, ident),
            })
        if action == "get_series_categories":
            return self._json(_categories(series, "Series"))
        if action == "get_series":
            wanted = query.get("category_id", [""])[0]
            result = []
            for number, item in enumerate(series, start=1):
                genres = item.get("genres") or ["Series"]
                if wanted and not any(str(category_id(g)) == wanted for g in genres):
                    continue
                result.append(dict(self._series_info(item), num=number,
                                   series_id=item["imdb_id"],
                                   category_id=str(category_id(genres[0])),
                                   last_modified="0", rating_5based=0))
            return self._json(result)
        if action == "get_series_info":
            wanted = query.get("series_id", [""])[0]
            item = next((s for s in series if s.get("imdb_id") == wanted), None)
            if item is None and wanted.isdigit() and 0 < int(wanted) <= len(series):
                # Compatibility with the old position-based IDs.
                item = series[int(wanted) - 1]
            if item is None:
                return self._send(404, "{}")
            return self._json(self._series_detail(item))
        self._send(400, "unknown action", "text/plain")

    @staticmethod
    def _movie_info(movie):
        return {"name": movie.get("name") or "", "plot": movie.get("description") or "",
                "cover_big": movie.get("poster") or "", "movie_image": movie.get("poster") or "",
                "releasedate": (movie.get("year") or "") + "-01-01",
                "rating": movie.get("rating") or "0", "duration": "",
                "genre": ", ".join(movie.get("genres") or [])}

    @staticmethod
    def _series_info(item):
        return {"name": item.get("name") or "", "cover": item.get("poster") or "",
                "plot": item.get("description") or "", "cast": "", "director": "",
                "genre": ", ".join(item.get("genres") or []),
                "releaseDate": (item.get("year") or "") + "-01-01",
                "rating": item.get("rating") or "0",
                "backdrop_path": [item.get("background") or item.get("poster") or ""],
                "youtube_trailer": "", "episode_run_time": "0"}

    def _series_detail(self, item):
        episodes = {}
        for episode in item.get("episodes") or []:
            season, number = episode.get("season"), episode.get("episode")
            if not season or not number:
                continue
            ident = episode_id(item["imdb_id"], season, number)
            episodes.setdefault(str(season), []).append({
                "id": str(ident), "episode_num": number, "title": "Episode %d" % number,
                "container_extension": "mp4", "info": {"plot": "", "duration": ""},
                "season": season,
            })
        seasons = [{"season_number": int(season), "name": "Season %s" % season,
                    "episode_count": len(rows), "cover": item.get("poster") or ""}
                   for season, rows in sorted(episodes.items(), key=lambda pair: int(pair[0]))]
        return {"seasons": seasons, "info": self._series_info(item), "episodes": episodes}

    @staticmethod
    def _upstream(url, headers):
        request = urllib.request.Request(url, headers=dict(headers or {}))
        return urllib.request.urlopen(request, timeout=60)

    def _resolve_live(self, channel):
        # The final tokenized live URL is obtained by the relay request itself;
        # do not probe and then replay a single-use stream token.
        return channel.get("url") or ""

    def _relay(self, url, headers):
        request_headers = dict(headers or {})
        if self.headers.get("Range"):
            request_headers["Range"] = self.headers["Range"]
        request = urllib.request.Request(url, headers=request_headers)
        try:
            upstream = urllib.request.urlopen(request, timeout=60)
        except urllib.error.HTTPError as exc:
            return self._send(exc.code, "upstream unavailable", "text/plain")
        except Exception:
            return self._send(502, "upstream unavailable", "text/plain")
        self.send_response(upstream.getcode() or 200)
        for header in ("Content-Type", "Content-Length", "Content-Range",
                       "Accept-Ranges", "Last-Modified", "ETag"):
            if upstream.headers.get(header):
                self.send_header(header, upstream.headers[header])
        if not upstream.headers.get("Content-Length"):
            self.send_header("Connection", "close")
        self.end_headers()
        try:
            if not getattr(self, "_head", False):
                while True:
                    chunk = upstream.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            upstream.close()

    def _stream_live_or_movie(self, ident):
        if ident >= EPISODE_BASE:
            return self._send(404, "not found", "text/plain")
        if ident >= MOVIE_BASE:
            movie = _movie(ident)
            if not movie:
                return self._send(404, "not found", "text/plain")
            result = vod.best_stream(movie["imdb_id"], langs=(DEFAULT_LANG, ""),
                                     providers=("moviebox",))
            if not result or result.get("kind") != "direct":
                return self._send(404, "not available", "text/plain")
            return self._relay(result["url"], result.get("headers") or {})
        live = live_channels()
        index = ident - LIVE_BASE
        if not 0 <= index < len(live):
            return self._send(404, "not found", "text/plain")
        url = self._resolve_live(live[index])
        if not url:
            return self._send(404, "not found", "text/plain")
        return self._relay(url, {"User-Agent": LIVE_UA})

    def _stream_episode(self, ident):
        episode = _episode(ident)
        if not episode:
            return self._send(404, "not found", "text/plain")
        item, season, number = episode
        result = vod.best_stream(item["imdb_id"], season, number,
                                 langs=(DEFAULT_LANG, ""), providers=("moviebox",))
        if not result or result.get("kind") != "direct":
            return self._send(404, "not available", "text/plain")
        return self._relay(result["url"], result.get("headers") or {})

    def do_HEAD(self):
        self._head = True
        try:
            self.do_GET()
        finally:
            self._head = False

    def _epg(self):
        path = os.path.join(PUB, "epg.xml")
        try:
            with open(path, "rb") as stream:
                body = stream.read()
        except OSError:
            return self._send(404, "EPG not built", "text/plain")
        self._send(200, body, "application/xml; charset=utf-8")

    def _m3u(self):
        credentials = "%s/%s" % (urllib.parse.quote(USER, safe=""),
                                 urllib.parse.quote(PASS, safe=""))
        base = self._base()
        lines = ['#EXTM3U url-tvg="%s/xmltv.php?username=%s&password=%s"'
                 % (base, urllib.parse.quote(USER), urllib.parse.quote(PASS))]
        for number, channel in enumerate(live_channels(), start=LIVE_BASE):
            attributes = ""
            if channel.get("tvg_id"):
                attributes += ' tvg-id="%s"' % channel["tvg_id"]
            if channel.get("logo"):
                attributes += ' tvg-logo="%s"' % channel["logo"]
            attributes += ' group-title="%s"' % (channel.get("group") or "Live TV")
            lines.append("#EXTINF:-1%s,%s" % (attributes, channel.get("name") or ""))
            lines.append("%s/live/%s/%d.ts" % (base, credentials, number))
        return "\n".join(lines) + "\n"


def main():
    if not PASS:
        raise SystemExit("Set EASYPLAY_PASS in .env; refusing to start without authentication")
    os.makedirs(PUB, exist_ok=True)
    live_channels()
    vod_data()
    print("EasyPlayTV listening on %s:%d" % (HOST, PORT), flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()


if __name__ == "__main__":
    main()
