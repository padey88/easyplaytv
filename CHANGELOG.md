# Changelog

## 1.1.0 - 2026-11-03

### Added
- Automatic live/EPG refresh every 6 hours and daily VOD verification.
- Opaque-token M3U, EPG, and stream routes for public WAF compatibility.
- Public status page and catalogue timestamps in `/healthz`.
- Fire TV/TiviMate automatic-update instructions.

## 1.0.0 - 2026-11-03

### Added
- Xtream Codes-compatible live TV, movie, series, M3U, and XMLTV endpoints.
- Verified direct-stream VOD catalogue with resumable builds.
- Atomic caches for positive and negative provider lookups.
- Stable content-derived movie and episode identifiers.
- Authenticated stream relay with Range forwarding and upstream headers.
- Portable user-systemd installer and offline regression tests.

### Security
- Authentication required for catalogue, EPG, M3U, and stream routes.
- Startup refused when no password is configured.
- Request logging avoids credential-bearing paths.
