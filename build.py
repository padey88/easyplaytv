#!/usr/bin/env python3
"""Builds the EasyPlayTV playlist + a trimmed EPG from EasyPlay live-TV.

Output (written to ./public):
  playlist.m3u   Malaysia, UK and US channels with tvg-id / tvg-logo / tvg-chno
  epg.xml        XMLTV trimmed to the channels the playlist actually uses
  report.json    per-region match statistics (for the README / debugging)

Run:  python3 build.py [--refresh]
"""
import json
import os
import sys
import time

import easyplay
import epg_index
from logos import Logos, resolve

DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(DIR, "public")

# EasyPlay group-title -> (region, playlist category)
GROUP_MAP = {
    "MALAYSIA": ("MY", "Malaysia"),
    "UK GENERAL": ("UK", "UK | General"),
    "UK ENTERTAIMENT": ("UK", "UK | Entertainment"),
    "UK SPORT": ("UK", "UK | Sport"),
    "UK SPORT 50 FPS": ("UK", "UK | Sport"),
    "UK CINEMA": ("UK", "UK | Movies"),
    "UK DOCUMENTRIES": ("UK", "UK | Documentary"),
    "UK KIDS": ("UK", "UK | Kids"),
    "UK MUSIC": ("UK", "UK | Music"),
    "UK INDIA CHANNELS": ("UK", "UK | India"),
    "USA GENERAL": ("US", "US | General"),
    "USA NEWS": ("US", "US | News"),
    "USA SPORTS": ("US", "US | Sport"),
    "USA ESPN": ("US", "US | Sport"),
    "USA NBC SPORT": ("US", "US | Sport"),
    "USA FOX SPORT": ("US", "US | Sport"),
    "USA ENTERTAINMENT": ("US", "US | Entertainment"),
    "USA CINEMA": ("US", "US | Movies"),
    "USA DOCUMENTARY": ("US", "US | Documentary"),
    "USA KIDS&FAMLIY": ("US", "US | Kids"),
    "USA MUSIC": ("US", "US | Music"),
    "USA ABC": ("US", "US | Networks"),
    "USA CBS": ("US", "US | Networks"),
    "USA NBC": ("US", "US | Networks"),
    "USA FOX": ("US", "US | Networks"),
    "USA CW": ("US", "US | Networks"),
    "USA PBS": ("US", "US | Networks"),
    "USA TELEMUNDO": ("US", "US | Spanish"),
    "USA UNIVISION": ("US", "US | Spanish"),
}

# Deliberately excluded (too large / event-based, would swamp the guide):
#   USA FLO, USA ESPN PLUS, USA CINEMANIA, USA PPV-LIVE EVENTS, USA MAX PPV,
#   USA NBA/NFL/MLB/NHL/NCAA/MLS/OHL/WHL/QMJHHL/BTN/VIX/PEACOCK/SPECTRUM/...
# Re-enable by adding them to GROUP_MAP.
EXCLUDED_NOTE = "PPV, league pass, VOD-dump and regional-affiliate groups are excluded."

EPG_GROUPS = {"MY": ["my1"], "UK": ["uk1", "gb"], "US": ["us2", "us"]}

# Per-region minimum fuzzy score. MY/UK names are near-identical to guide
# names; the US pool is fragmented so it needs a stricter bar to stay honest.
CUTOFF = {"MY": 0.90, "UK": 0.90, "US": 0.92}


def clean_name(raw):
    """'UK| BBC 1 HD' -> 'BBC 1 HD'."""
    n = raw.replace("\xa0", " ")
    for pre in ("MY|", "UK|", "US|"):
        if n.upper().startswith(pre):
            n = n[len(pre):].strip()
    return n.strip()


def build(refresh=False):
    started = time.time()
    print("fetching EasyPlay channel list ...", flush=True)
    entries = easyplay.entries(force=refresh)

    matcher = epg_index.Matcher(EPG_GROUPS)
    logos = {"MY": Logos("MY"), "UK": Logos("UK"), "US": Logos("US")}

    rows, seen = [], set()
    stats = {r: {"total": 0, "epg": 0, "logo": 0} for r in ("MY", "UK", "US")}
    unmatched = []

    for e in entries:
        mapped = GROUP_MAP.get(e["group"].upper())
        if not mapped:
            continue
        region, category = mapped
        name = clean_name(e["name"])
        if not name:
            continue

        # Dedupe: one entry per (region, normalised name) — drops [BK]/[BK1]
        # backups and East/West duplicates of the same channel.
        dkey = (region, epg_index.norm(name))
        if not dkey[1] or dkey in seen:
            continue
        seen.add(dkey)

        stats[region]["total"] += 1
        rec, score = matcher.best(name, region, CUTOFF[region])
        if rec:
            stats[region]["epg"] += 1
        else:
            unmatched.append({"region": region, "name": name,
                              "best": round(score, 2)})

        icon = resolve(logos[region],
                       rec["icon"] if rec else "",
                       e["logo"], name)
        if icon:
            stats[region]["logo"] += 1

        rows.append({
            "name": name,
            "group": category,
            "region": region,
            "url": e["url"],
            "tvg_id": rec["id"] if rec else "",
            "logo": icon,
        })

    # Stable ordering: region order, then category, then source order
    region_order = {"MY": 0, "UK": 1, "US": 2}
    rows.sort(key=lambda r: (region_order[r["region"]], r["group"].lower()))

    os.makedirs(OUT, exist_ok=True)
    epg_url = os.environ.get("EPG_URL", "https://easyplay.kulnem.space/epg.xml")

    lines = ['#EXTM3U url-tvg="%s" x-tvg-url="%s"' % (epg_url, epg_url)]
    chno = 1
    for r in rows:
        bits = ['#EXTINF:-1']
        if r["tvg_id"]:
            bits.append('tvg-id="%s"' % r["tvg_id"])
        if r["logo"]:
            bits.append('tvg-logo="%s"' % r["logo"])
        bits.append('tvg-chno="%d"' % chno)
        bits.append('group-title="%s"' % r["group"])
        lines.append(" ".join(bits) + "," + r["name"])
        lines.append(r["url"])
        chno += 1
    with open(os.path.join(OUT, "playlist.m3u"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    # Structured channel list for the Xtream proxy (stable ids = list order)
    with open(os.path.join(OUT, "channels.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)

    # Trimmed EPG: only the ids actually referenced
    keep = {r["tvg_id"] for r in rows if r["tvg_id"]}
    parts = []
    for label, srcs in EPG_GROUPS.items():
        for s in srcs:
            text, _ = epg_index.load_source(s, refresh=refresh)
            if text:
                parts.append(text)
    merged = epg_index.trim_xmltv("\n".join(parts), keep)
    with open(os.path.join(OUT, "epg.xml"), "w", encoding="utf-8") as f:
        f.write(merged)

    report = {
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "channels": len(rows),
        "by_region": stats,
        "epg_channels_kept": len(keep),
        "epg_bytes": os.path.getsize(os.path.join(OUT, "epg.xml")),
        "excluded": EXCLUDED_NOTE,
        "unmatched_sample": unmatched[:200],
        "unmatched_total": len(unmatched),
        "elapsed_s": round(time.time() - started, 1),
    }
    with open(os.path.join(OUT, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1)

    for region, s in stats.items():
        t = max(s["total"], 1)
        print("%s: %d channels, epg %d (%.0f%%), logo %d (%.0f%%)"
              % (region, s["total"], s["epg"], 100 * s["epg"] / t,
                 s["logo"], 100 * s["logo"] / t))
    print("total %d channels, epg.xml %.1f MB, %.1fs"
          % (len(rows), report["epg_bytes"] / 1e6, report["elapsed_s"]))
    return report


if __name__ == "__main__":
    build(refresh="--refresh" in sys.argv)
