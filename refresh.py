#!/usr/bin/env python3
"""Refresh EasyPlayTV data without ever replacing good data with an empty build.

Usage:
  python3 refresh.py live     Acquire channels, match EPG, publish atomically.
  python3 refresh.py vod      Check new provider availability and verify VOD.

Intended for a systemd timer. Exit nonzero on failure so the previous published
catalogue remains available and the failure is visible to monitoring.
"""
import argparse
import os
import sys
import time

import build
import vod_catalog
from storage import read_json

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")


def live():
    before = read_json(os.path.join(PUBLIC, "channels.json"), [])
    report = build.build(refresh=True)
    if report["channels"] < 100:
        raise RuntimeError("refusing to publish only %d channels" % report["channels"])
    if before and report["channels"] < len(before) * 0.5:
        raise RuntimeError("channel count dropped unexpectedly: %d -> %d"
                           % (len(before), report["channels"]))
    print("live refresh published: %d channels" % report["channels"], flush=True)


def vod():
    # Incremental candidates: only genres not previously completed are fetched.
    # Provider availability is revalidated for supported titles; negative
    # answers are retained so the daily job does not hammer unavailable IDs.
    vod_catalog.stage_candidates()
    vod_catalog.stage_filter()
    vod_catalog.stage_verify()
    data = read_json(os.path.join(PUBLIC, "vod.json"), {})
    if not data.get("movies") and not data.get("series"):
        raise RuntimeError("refusing to publish an empty VOD catalogue")
    print("VOD refresh published: %d movies, %d series"
          % (len(data.get("movies") or []), len(data.get("series") or [])), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=("live", "vod", "all"))
    args = parser.parse_args()
    started = time.time()
    if args.target in ("live", "all"):
        live()
    if args.target in ("vod", "all"):
        vod()
    print("refresh complete in %.0fs" % (time.time() - started), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("REFRESH FAILED: %s" % exc, file=sys.stderr, flush=True)
        raise
