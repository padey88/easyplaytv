# EasyPlayTV

Live TV + on-demand from [EasyPlay](https://easyplaytv.sh) (easyplaytv.sh), packaged
for **TiviMate** via an **Xtream Codes** account: Malaysia, UK and US live channels,
plus a verified on-demand movie and series library.

TiviMate only reads VOD from an Xtream Codes login, not from an M3U file — hence
the small proxy in this repo, which speaks enough of the XC protocol and maps
every call onto EasyPlay's own APIs.

## What you get

| | |
|---|---|
| Live channels | ~1,950 — Malaysia (99), UK (359), US (1,488) |
| EPG | trimmed XMLTV for the channels actually carried |
| VOD | movies + series, only titles verified to return a direct playable MP4 |
| Player | TiviMate 5.x (Android / Fire TV) |

## Quick start

    python3 xtream_proxy.py          # serves :8601

In TiviMate: *Add playlist → Xtream Codes login*

    Server:  http://<host>:8601
    Username: easyplay
    Password: (whatever EASYPLAY_PASS is set to; empty = no auth)

Set `EASYPLAY_USER` / `EASYPLAY_PASS` before starting to require credentials.
Leaving both empty disables authentication — do not expose it that way.

## Rebuilding the catalogue

    python3 build.py            # live channels + EPG + channels.json
    python3 vod_catalog.py      # VOD candidates -> verified direct streams

`build.py` writes `public/{playlist.m3u,epg.xml,channels.json,report.json}`.
`vod_catalog.py` writes `public/vod.json` and takes a while: every candidate title
is resolved against EasyPlay to confirm a direct MP4 actually exists.

## Layout

    easyplay.py      lease + live channel list (m3u_plus) from EasyPlay
    epg_index.py     XMLTV sources, channel-name matching, EPG trimming
    logos.py         channel icon resolution
    build.py         live playlist + trimmed EPG builder
    vod.py           EasyPlay catalog / episode / playback API client
    vod_catalog.py   VOD catalogue builder (verified streams only)
    xtream_proxy.py  Xtream Codes server -> TiviMate

## How it works

Live TV: `POST /live-tv/api/v1/acquire` on easyapi.sh returns a lease holding an
XUI.one `get.php` playlist URL (~24.8k channels, tagged by country). The device
identity is generated once and cached in `.easyplay_device.json` (mode 0600) so
runs reuse one pseudo-device instead of minting new ones.

VOD: `/playback/v1/resolve` with `preferred_provider=moviebox` returns a signed
MP4 plus `Referer`/`User-Agent` headers. The `vidsrc` provider only returns a web
player embed, which TiviMate cannot play, so those titles are dropped rather than
shipped broken. Signed MP4 URLs expire, so the proxy resolves a fresh one per
request.

Channel icons prefer the EPG's own `<icon>`, then iptv-org, and only fall back to
EasyPlay's art when it is not a placeholder flag or a sibling channel's logo.

## Notes

- Live streams 302 through two edge hops; play URLs outlive the lease, so only
  the playlist needs periodic re-acquisition.
- `USA FLO`, league passes, PPV and regional-affiliate groups are excluded —
  they would swamp the guide. Add them to `GROUP_MAP` in `build.py` to include.
- Personal use. Streams belong to their respective broadcasters.
