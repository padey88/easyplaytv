#!/usr/bin/env python3
"""EPG sources + channel matching for the EasyPlayTV playlist.

Builds, from public XMLTV feeds, a trimmed guide containing only the channels
our playlist actually uses, plus an index used to attach tvg-id / tvg-logo.

Sources (all free):
  MY  epgshare01 epg_ripper_MY1.xml.gz
  UK  epgshare01 epg_ripper_UK1.xml.gz  +  epg.pw epg_GB.xml
  US  epgshare01 epg_ripper_US2.xml.gz  +  epg.pw epg_US.xml
"""
import difflib
import gzip
import html
import io
import os
import re
import urllib.request

DIR = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(DIR, "epg_cache")

SOURCES = {
    "my1": "https://epgshare01.online/epgshare01/epg_ripper_MY1.xml.gz",
    "uk1": "https://epgshare01.online/epgshare01/epg_ripper_UK1.xml.gz",
    "gb":  "https://epg.pw/xmltv/epg_GB.xml",
    "us2": "https://epgshare01.online/epgshare01/epg_ripper_US2.xml.gz",
    "us":  "https://epg.pw/xmltv/epg_US.xml",
}

# "BBC 1" -> "BBC One" style aliases; keys are already normalised
ALIASES = {
    "bbc1": "bbcone", "bbc2": "bbctwo", "bbc3": "bbcthree", "bbc4": "bbcfour",
    "itv1": "itv1", "itv2": "itv2",
    "channel4": "channel4", "channel5": "channel5",
    "s4c": "s4c",
}

NOISE = r"\b(fhd|uhd|hd|sd|4k|8k|hevc|h265|backup|bk|bk1|bk2|raw|vip|feed|east|west|eastern|western|not 24/7)\b"


def norm(name):
    """Aggressively normalise a channel name for matching."""
    n = (name or "").replace("\xa0", " ")
    n = html.unescape(n).lower()
    n = re.sub(r"^(my|uk|us|gb)\s*\|\s*", "", n)
    n = re.sub(r"\([^)]*\)", " ", n)
    n = re.sub(r"\[[^\]]*\]", " ", n)
    n = n.replace("&", " and ")
    # "+1" timeshift -> "plus1" token, so ITV+1 never collapses onto ITV
    n = re.sub(r"\s*\+\s*", "+", n)
    n = re.sub(r"\+1\b", " plus1 ", n)
    n = re.sub(r"\bplus1\b", " plus1 ", n)
    n = re.sub(NOISE, " ", n)
    # "BBC 4 / CBEEBIES" — timeshare, guide the first name
    n = n.split("/")[0]
    n = re.sub(r"[^a-z0-9+]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def key(name):
    k = norm(name).replace(" ", "")
    return ALIASES.get(k, k)


def _download(url, path, max_age=20 * 3600):
    if os.path.exists(path) and os.path.getsize(path) > 1000 and \
       os.path.getmtime(path) > _now() - max_age:
        return path
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    data = urllib.request.urlopen(req, timeout=300).read()
    with open(path, "wb") as f:
        f.write(data)
    return path


def _now():
    import time
    return time.time()


def _read(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
            return f.read()
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def load_source(name, refresh=False):
    os.makedirs(CACHE, exist_ok=True)
    ext = ".xml.gz" if SOURCES[name].endswith(".gz") else ".xml"
    path = os.path.join(CACHE, name + ext)
    if refresh and os.path.exists(path):
        os.remove(path)
    try:
        _download(SOURCES[name], path)
        return _read(path), path
    except Exception as e:
        print("epg source %s failed: %s" % (name, e), flush=True)
        if os.path.exists(path):
            return _read(path), path
        return "", None


def parse_channels(text):
    """XMLTV text -> [{id, name, icon, norm}] channel records."""
    out = []
    for m in re.finditer(r"<channel\b[^>]*\bid=\"([^\"]*)\"[^>]*>(.*?)</channel>", text, re.S):
        cid, body = m.group(1), m.group(2)
        dn = re.search(r"<display-name[^>]*>(.*?)</display-name>", body, re.S)
        ic = re.search(r"<icon\b[^>]*\bsrc=\"([^\"]*)\"", body)
        if not dn:
            continue
        name = html.unescape(re.sub(r"<[^>]+>", "", dn.group(1))).strip()
        out.append({
            "id": cid,
            "name": name,
            "icon": html.unescape(ic.group(1)) if ic else "",
            "norm": norm(name),
        })
    return out


# tokens that may be added to an EPG name without changing which channel it is
BENIGN = {
    "feed", "network", "channel", "tv", "the", "national", "usa", "us", "uk",
    "east", "west", "pacific", "mountain", "central", "hd", "plus", "one",
}


class Matcher:
    """Matches playlist channel names to XMLTV channels with a confidence score."""

    def __init__(self, groups):
        # groups: {label: [source-name, ...]} e.g. {"MY": ["my1"]}
        self.pools = {}
        for label, srcs in groups.items():
            recs = []
            for s in srcs:
                text, _ = load_source(s)
                recs.extend(parse_channels(text))
            self.pools[label] = recs
        self._by_key = {label: {} for label in self.pools}
        for label, recs in self.pools.items():
            for r in recs:
                self._by_key[label].setdefault(key(r["name"]), r)
        self._by_norm = {label: {} for label in self.pools}
        for label, recs in self.pools.items():
            for r in recs:
                self._by_norm[label].setdefault(r["norm"], r)

    def best(self, name, label, cutoff=0.90):
        """Return (record, score) or (None, 0).

        Matching order: exact normalised key, exact normalised form, then a
        token-subset test (an EPG name may add benign words like "Network" or
        "Feed"), then fuzzy similarity above `cutoff`.
        """
        k, n = key(name), norm(name)
        rec = self._by_key[label].get(k) or self._by_norm[label].get(n)
        if rec:
            return rec, 1.0
        pool = self.pools[label]
        toks = set(n.split())
        if toks:
            for r in pool:
                rt = set(r["norm"].split())
                if not rt:
                    continue
                small, big = (toks, rt) if len(toks) <= len(rt) else (rt, toks)
                if small <= big and (big - small) <= BENIGN:
                    return r, 0.97
        best, score = None, 0.0
        for r in pool:
            s = difflib.SequenceMatcher(None, n, r["norm"]).ratio()
            if s > score:
                best, score = r, s
                if s > 0.99:
                    break
        if score >= cutoff:
            return best, score
        return None, score


def trim_xmltv(text, keep_ids):
    """Return XMLTV text containing only the given channel ids and their programmes."""
    chans, progs = [], []
    for m in re.finditer(r"<channel\b[^>]*\bid=\"([^\"]*)\"[^>]*>.*?</channel>", text, re.S):
        if m.group(1) in keep_ids:
            chans.append(m.group(0))
    for m in re.finditer(r"<programme\b[^>]*\bchannel=\"([^\"]*)\"[^>]*>.*?</programme>", text, re.S):
        if m.group(1) in keep_ids:
            progs.append(m.group(0))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<tv>\n' +
            "\n".join(chans) + "\n" + "\n".join(progs) + "\n</tv>\n")
