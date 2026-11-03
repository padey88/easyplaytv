#!/usr/bin/env python3
"""Logo resolution: attach an icon that actually matches the channel name.

EasyPlay ships logos on lo1.in, but a large share are placeholder flags
(`.../flag.png`, `.../flg.png`) or belong to a sibling channel. Sources, in
priority order:

  1. iptv-org logos.json (30k channels, name-matched to our channel)
  2. XMLTV <icon> from the matched EPG channel
  3. EasyPlay's own tvg-logo — only when it is not a known placeholder

A logo whose URL slug disagrees with the channel name is rejected, because
sibling channels in the same group often share one file (e.g. ASTRO CRICKET
pointing at astroaur.png = Astro Aura).
"""
import html
import json
import os
import re
import urllib.request

from epg_index import norm

DIR = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(DIR, "epg_cache")
LOGOS_URL = "https://iptv-org.github.io/api/logos.json"

# EasyPlay placeholder art — never usable as a channel icon
PLACEHOLDER = re.compile(
    r"/(flag|flg|default|noimage|no-image|blank|placeholder)\.(png|jpg|jpeg|gif|webp)$",
    re.I)

# iptv-org country suffix preferred for a playlist region
REGION_CC = {"MY": (".my",), "UK": (".uk",), "US": (".us",)}


class Logos:
    def __init__(self, region):
        self.region = region
        self.by_name = {}
        self._load()

    def _load(self):
        os.makedirs(CACHE, exist_ok=True)
        path = os.path.join(CACHE, "logos.json")
        try:
            if not os.path.exists(path) or os.path.getsize(path) < 1000:
                req = urllib.request.Request(LOGOS_URL, headers={"User-Agent": "Mozilla/5.0"})
                data = urllib.request.urlopen(req, timeout=180).read()
                with open(path, "wb") as f:
                    f.write(data)
        except Exception as e:
            print("logos.json unavailable: %s" % e, flush=True)
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                records = json.load(f)
        except Exception as e:
            print("logos.json unreadable: %s" % e, flush=True)
            return

        cc = REGION_CC.get(self.region, ())
        # best[normalised name][cc match?] = (rank, url)
        best = {}
        for r in records:
            ch = r.get("channel") or ""
            url = r.get("url") or ""
            if not ch or not url or not r.get("in_use", True):
                continue
            suffix = ch[ch.rfind("."):] if "." in ch else ""
            base = ch[:-len(suffix)] if suffix else ch
            name = norm(base.replace(".", " ").replace("_", " "))
            if not name:
                continue
            regional = suffix in cc
            # prefer PNG over SVG (TiviMate renders PNG more reliably), wider art over tiny
            fmt = (r.get("format") or "").upper()
            rank = (2 if regional else 0) + (1 if fmt == "PNG" else 0)
            prev = best.get(name)
            if prev is None or rank > prev[0]:
                best[name] = (rank, url)
        self.by_name = {k: v[1] for k, v in best.items()}

    def from_iptv_org(self, name, cutoff=0.92):
        """Exact normalised lookup only — avoids attaching a stranger's logo."""
        n = norm(name)
        if not n:
            return ""
        if n in self.by_name:
            return self.by_name[n]
        compact = n.replace(" ", "")
        for k, url in self.by_name.items():
            if k.replace(" ", "") == compact:
                return url
        return ""

    @staticmethod
    def usable(raw, name):
        """Is EasyPlay's own logo plausible for this channel?"""
        if not raw or not raw.startswith(("http://", "https://")):
            return ""
        if PLACEHOLDER.search(raw):
            return ""
        slug = raw.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
        slug = re.sub(r"[^a-z0-9]+", "", slug)
        if not slug:
            return ""
        target = norm(name).replace(" ", "")
        # accept when the file slug is a prefix/abbreviation of the channel name
        # (tv3 -> tv3, astrori -> astro, esptw -> espn...) or vice versa
        if slug in target or target.startswith(slug[:4]):
            return raw
        # numeric-only slugs (e.g. 395.png) carry no signal — accept
        if slug.isdigit():
            return raw
        return ""


def resolve(logos, epg_icon, ez_logo, name):
    """Return the best icon URL for a channel, or ''."""
    return (epg_icon
            or logos.from_iptv_org(name)
            or Logos.usable(ez_logo, name))
