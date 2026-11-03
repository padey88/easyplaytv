"""Identifiers for the subset of the Xtream Codes protocol we implement."""
import hashlib


LIVE_BASE = 1
MOVIE_BASE = 1_000_000
EPISODE_BASE = 100_000_000
ID_SPACE = 100_000_000


def _numeric_id(value):
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    # Reserve zero, keep IDs comfortably inside a signed 32-bit integer.
    return int.from_bytes(digest[:4], "big") % (ID_SPACE - 1) + 1


def movie_id(imdb_id):
    """A stable movie ID, independent of catalogue ordering."""
    return MOVIE_BASE + _numeric_id("movie:" + imdb_id)


def episode_id(imdb_id, season, episode):
    """A stable episode ID which contains no catalogue-position component."""
    value = "episode:%s:%s:%s" % (imdb_id, int(season), int(episode))
    return EPISODE_BASE + _numeric_id(value)


def split_episode_id(eid):
    """Kept only to reject the obsolete position-based IDs clearly."""
    raise ValueError("episode IDs are resolved from the generated catalogue")


def category_id(name):
    """Stable across processes, unlike Python's salted hash()."""
    return _numeric_id("category:" + name)
