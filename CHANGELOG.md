# Changelog

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
