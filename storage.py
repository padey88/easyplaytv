"""Small, safe persistence helpers used by the catalogue builders."""
import hashlib
import json
import os


def atomic_write(path, content, mode="w", encoding="utf-8"):
    """Write a file atomically in its own directory."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        if "b" in mode:
            with open(tmp, mode) as f:
                f.write(content)
        else:
            with open(tmp, mode, encoding=encoding) as f:
                f.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return default


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False))


def read_jsonl(path):
    """Return a list of valid records and whether a malformed line was seen."""
    records = []
    malformed = False
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except (ValueError, TypeError):
                    malformed = True
    except FileNotFoundError:
        pass
    return records, malformed


def cache_key(value):
    """A stable key which changes whenever a candidate's source data changes."""
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
