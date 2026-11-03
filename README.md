# EasyPlayTV

An independent, self-hosted **Xtream Codes-compatible proxy for TiviMate**. It
combines EasyPlay live television, an XMLTV guide, and a verified on-demand
catalogue behind one TiviMate login.

TiviMate can import live channels from M3U, but its integrated Movies and TV
Shows interface requires an Xtream Codes account. This project supplies that
interface and handles the upstream headers and expiring playback URLs that a TV
player cannot handle directly.

**Python 3.10+ · standard library only · Linux/systemd · no external database**

## Important notice

This is an unofficial interoperability project. It is not affiliated with,
endorsed by, or supported by EasyPlay, TiviMate, Cinemeta, or any broadcaster.
It does not host or include streams, channel lists, credentials, or media.

Only use it with services and content you are legally permitted to access in
your jurisdiction. You are responsible for the provider's terms and applicable
copyright law. Upstream APIs can change or disappear without notice. The proxy
must remain on a trusted private network; do not expose it to the public
internet or use it to redistribute content.

## Features

- Live TV organised into Malaysia, UK, and US categories.
- XMLTV guide matched to carried channels and channel logo resolution.
- Movies and series through the Xtream Codes interface, not just live M3U.
- Every catalogue title checked for a direct playable stream; browser-only
  embeds are excluded.
- Stable content-derived movie and episode IDs: catalogue sorting or rebuilds
  do not change what an ID plays.
- Authenticated Xtream, M3U, XMLTV, and stream routes. The server refuses to
  start without a password.
- Byte relay with upstream headers and HTTP `Range` forwarding for seeking.
- Resumable catalogue builds, cached negative lookups, atomic file writes,
  in-process build locking, and automatic catalogue reloads.
- No dependencies beyond the Python standard library.

## Quick start

### 1. Install

```bash
git clone https://github.com/padey88/easyplaytv.git
cd easyplaytv
cp .env.example .env
```

Replace `EASYPLAY_PASS` in `.env` with a long random password:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(24))'
chmod 600 .env
```

The default listener is `127.0.0.1:8601`. For TiviMate on another LAN device,
change `EASYPLAY_HOST` in `.env` to `0.0.0.0` and restrict port 8601 to your
LAN using the host firewall.

### 2. Build the data

```bash
python3 build.py
python3 vod_catalog.py
```

The first live build downloads and matches several XMLTV sources. The first
VOD build scans Cinemeta's genre catalogues, checks provider availability, and
verifies playback. That one-time process can take tens of minutes depending
on network conditions and configured catalogue depth. Subsequent filter runs
reuse both positive and negative cached answers.

Generated data stays in the ignored `public/` directory. Nothing generated is
required in the Git repository.

For a smaller initial VOD build, set `EASYPLAY_CATALOGUE_PAGES=2` in `.env` or
the shell before running the builder. The default is 30 pages per genre.

### 3. Start the proxy

```bash
set -a; . ./.env; set +a
python3 xtream_proxy.py
```

Check it locally:

```bash
curl http://127.0.0.1:8601/healthz
```

### 4. Add it to TiviMate

1. Open **Settings → Playlists → Add playlist**.
2. Choose **Xtream Codes**.
3. Enter:
   - **Server:** `http://YOUR_LAN_IP:8601`
   - **Username:** the `EASYPLAY_USER` value (default `easyplay`)
   - **Password:** your `EASYPLAY_PASS` value
4. Let TiviMate process live TV, movies, series, and the EPG.

The exact menu labels differ slightly between TiviMate versions. TiviMate 5.x
is the intended client; other Xtream-compatible players have not been formally
qualified.

## Run as a service

The installer renders a user-level systemd service for the current checkout,
creates `.env` with a random password if missing, and enables the unit:

```bash
python3 deploy/install.py
systemctl --user start easyplaytv
systemctl --user status easyplaytv
```

On headless Linux, enable lingering if the service must start without login:

```bash
sudo loginctl enable-linger "$USER"
```

To apply changed environment variables:

```bash
systemctl --user restart easyplaytv
```

The example unit restricts filesystem access and grants writes only to the
runtime data locations. It binds to localhost by default; set
`EASYPLAY_HOST=0.0.0.0` in `.env` for LAN playback.

## Updating the catalogue

The proxy detects updated `channels.json` and `vod.json` automatically. You do
not need to restart it after a successful build.

```bash
python3 build.py --refresh
python3 vod_catalog.py filter
python3 vod_catalog.py verify
```

To deliberately discard candidate/genre/provider caches and rescan the VOD
catalogue:

```bash
python3 vod_catalog.py --rebuild
```

Do not run two catalogue builders concurrently. The filter refuses concurrent
writers; generated files are replaced atomically to protect readers. The VOD
builder retries negative playback resolutions because provider rate limiting
can otherwise silently remove valid titles.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `EASYPLAY_HOST` | `127.0.0.1` | HTTP listen address |
| `EASYPLAY_PORT` | `8601` | HTTP listen port |
| `EASYPLAY_USER` | `easyplay` | Xtream username |
| `EASYPLAY_PASS` | none | Required password; empty is rejected |
| `EASYPLAY_VOD_LANG` | `en` | Preferred on-demand audio language |
| `EASYPLAY_PUBLIC_URL` | request host | Base URL written into playlist/stream links |
| `EASYPLAY_CATALOGUE_PAGES` | `30` | Cinemeta pages per genre |
| `EASYPLAY_CATALOGUE_WORKERS` | `6` | Concurrent catalogue lookups |

Live category selection and EPG matching thresholds are in `build.py`.
PPV, league-pass, VOD-dump, and regional-affiliate groups are excluded by
default to keep the guide manageable.

## HTTP surface

Authenticated routes:

- `player_api.php` and its live/VOD/series category, list, and detail actions
- `get.php?type=m3u` and `playlist.m3u`
- `xmltv.php`
- `live/<username>/<password>/<id>.ts`
- `movie/<username>/<password>/<id>.mp4`
- `series/<username>/<password>/<id>.mp4`

`GET /healthz` is unauthenticated and exposes only aggregate catalogue counts;
it never exposes upstream URLs or credentials.

## Development and verification

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
```

Tests cover idempotent positive/negative caching, failed-lookup handling,
playback retries, episode parsing, stable IDs, authentication on sensitive
routes, Xtream response shapes, and HEAD requests. They use local fixtures and
do not need upstream credentials.

## Project layout

| File | Responsibility |
|---|---|
| `easyplay.py` | Device identity, live lease acquisition, M3U parsing |
| `epg_index.py` | XMLTV retrieval, channel matching, guide trimming |
| `logos.py` | Channel logo selection |
| `build.py` | Live channel, EPG, and logo catalogue builder |
| `vod.py` | Upstream catalogue and playback client |
| `vod_catalog.py` | Resumable VOD collection, filtering, and verification |
| `xtream_proxy.py` | Authenticated Xtream-compatible server and media relay |
| `identifiers.py` | Deterministic content-derived IDs |
| `storage.py` | Atomic file and cache helpers |
| `deploy/` | Portable systemd template and installer |
| `tests/` | Offline regression tests |

## Troubleshooting

**The service exits immediately.** Set a non-empty `EASYPLAY_PASS`. Empty
credentials are deliberately rejected rather than creating an open relay.

**Live TV works but VOD is empty.** Add the server as Xtream Codes, not as an
M3U playlist. Run `vod_catalog.py` and confirm `public/vod.json` exists.

**Titles vanish after a rebuild.** Upstream availability changes. The builder
also removes browser-only embeds and titles that fail repeated direct-stream
verification.

**Playback works on one client but not another.** The proxy relays bytes and
upstream headers instead of expecting the player to send them. Check
`/healthz`, service logs, and LAN firewall rules; do not bypass authentication.

**A provider API or endpoint fails.** This project relies on undocumented
upstream behavior. A 404 for an individual IMDb title is normally cached as
unavailable; repeated transport failures are not cached as definitive
negatives. Broad API failures require a client update.

## Legal and security

See [SECURITY.md](SECURITY.md) for reporting guidance. No licence is granted
for third-party streams, metadata, artwork, or trademarks. Those remain the
property of their respective owners.
