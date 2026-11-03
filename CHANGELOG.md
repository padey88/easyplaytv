# Changelog

All notable changes to EasyPlayTV.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Known issues
- `xtream_proxy.py` and `deploy/install.py` carry uncommitted local edits in the
  working tree; `deploy/easyplaytv-live.timer` exists but is not yet committed.
  Do not deploy from a dirty tree — a rebuild will start from a state that does
  not match any tag.

## [1.1.0] — 2026-11-03

### Added
- **Automatic refresh.** Live TV and EPG rebuild every 6 hours; the VOD catalogue
  re-verifies daily. Both are timer-driven, so a long-running install does not
  serve a stale channel list or a dead stream.
- **Opaque-token routes** for M3U, EPG, and streams. The stock Xtream paths embed
  the username and password in the URL, which some WAFs reject outright — the
  token form passes the same auth without credentials in the path.
- **Public status page** and catalogue timestamps in `/healthz`, so you can tell
  at a glance which build is live and when it last refreshed.
- Fire TV / TiviMate automatic-update setup documented end to end.

### Changed
- The M3U playlist and the Xtream account now serve the same catalogue, so a
  player configured either way sees the same channels, guide, and library.

## [1.0.0] — 2026-11-03

### Added
- **Xtream Codes-compatible API** — live TV, movies, series, M3U, and XMLTV
  behind one login, so TiviMate, Fire TV, and any Xtream client work unmodified.
- **Verified VOD catalogue.** Every title is checked for a directly playable
  stream before it enters the catalogue; browser-only embeds are excluded
  instead of appearing in the library and failing on click.
- **Stable content-derived IDs** for movies and episodes. IDs are a hash of the
  content identity, not a row number, so a catalogue rebuild does not change
  playback IDs and a client's watch history survives it.
- **Resumable catalogue builds.** An interrupted build resumes from the last
  verified title instead of re-verifying the whole library.
- **Atomic caches** for both positive and negative provider lookups — a known
  miss is cached, so a rebuild does not re-query hundreds of dead titles.
- **Byte relay with `Range` forwarding** and upstream header passthrough, so
  seeking works in the player and upstream does not see a mangled request.
- Portable per-user systemd installer, plus offline regression tests that need
  no network and no provider credentials.
- Live categories for Malaysia, UK, and US; XMLTV guide with logo resolution
  matched against the channels actually carried.

### Security
- Authentication is required on catalogue, EPG, M3U, and stream routes alike.
  The server refuses to start with no password configured rather than silently
  serving an open proxy.
- Request logging avoids credential-bearing paths, so tokens do not accumulate
  in journald.
- Intended for a trusted private network. Do **not** expose it to the internet.

[Unreleased]: https://github.com/padey88/easyplaytv/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/padey88/easyplaytv/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/padey88/easyplaytv/releases/tag/v1.0.0
