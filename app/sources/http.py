"""Rate-limited, cached HTTP GET for metadata sources."""
import hashlib
import json
import re
import threading
import time

import requests

from .. import db

UA = "Isaiarr/1.0 (+self-hosted music tagger)"


class RateLimiter:
    """per_minute may be a number or a function returning one, so a limit changed in
    Settings applies to the very next request."""
    def __init__(self, per_minute):
        self.per_minute = per_minute
        self.lock = threading.Lock()
        self.next_at = 0.0

    @property
    def interval(self):
        rpm = self.per_minute() if callable(self.per_minute) else self.per_minute
        return 60.0 / max(1, rpm or 1)

    def wait(self):
        with self.lock:
            now = time.monotonic()
            if now < self.next_at:
                time.sleep(self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval


class SourceError(Exception):
    pass


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                       "nov", "dec"], 1)}


def full_date(s):
    """A full release date 'YYYY-MM-DD' from '2021-06-18T07:00:00Z', '18 June 2021', 'June 18, 2021',
    or '' when it isn't a real full date (a year alone, 1970-01-01 placeholders, 0000)."""
    s = str(s or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m1 = re.search(r"\b(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b", s)
        m2 = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})\b", s)
        if m1 and m1.group(2)[:3].lower() in _MONTHS:
            d, mo, y = int(m1.group(1)), _MONTHS[m1.group(2)[:3].lower()], int(m1.group(3))
        elif m2 and m2.group(1)[:3].lower() in _MONTHS:
            mo, d, y = _MONTHS[m2.group(1)[:3].lower()], int(m2.group(2)), int(m2.group(3))
        else:
            return ""
    if not (1900 < y < 2100 and 1 <= mo <= 12 and 1 <= d <= 31) or (y, mo, d) == (1970, 1, 1):
        return ""
    return f"{y:04d}-{mo:02d}-{d:02d}"


LABEL_JUNK = re.compile(r"^\s*(this\s+compilation\s+)?(\(p\)|\(c\)|℗|©)?\s*"
                        r"((19|20)\d\d(\s*[-,/&]\s*(19|20)\d\d)*)?\s*", re.I)


def clean_label(s):
    """'℗ 2021 Sony Music Entertainment India Pvt. Ltd.' -> 'Sony Music Entertainment India Pvt. Ltd.'"""
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    for _ in range(2):
        s = LABEL_JUNK.sub("", s, count=1).strip(" ,-") if s else s
    s = re.sub(r"\s*\(\d+\)$", "", s)          # Discogs "EMI (2)"
    return s if len(s) >= 2 and not s.isdigit() else ""


def get_json(url, params, limiter, cache_prefix, headers=None, max_age=7 * 86400,
             retries=3, use_cache=True, check=None):
    """`check(data)` lets a source look at a reply before it's cached: return None to accept it,
    ("retry", message) for a temporary error sent as HTTP 200 (Deezer's quota reply), or
    ("nocache", message) to return it without caching (e.g. "not found")."""
    key = cache_prefix + ":" + hashlib.sha1(
        (url + json.dumps(params, sort_keys=True)).encode()).hexdigest()
    if use_cache:
        hit = db.cache_get(key, max_age)
        if hit is not None:
            return hit
    h = {"User-Agent": UA}
    h.update(headers or {})
    last = None
    for attempt in range(retries):
        limiter.wait()
        try:
            r = requests.get(url, params=params, headers=h, timeout=20)
            if r.status_code == 401:
                raise SourceError(f"{cache_prefix}: the key or token was refused (HTTP 401)")
            if r.status_code in (403, 429, 503):
                last = SourceError(f"{cache_prefix} HTTP {r.status_code} (rate limited)")
                time.sleep(15 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            verdict = check(data) if check else None
            if verdict and verdict[0] == "retry":
                last = SourceError(f"{cache_prefix}: {verdict[1]}")
                time.sleep(5 * (attempt + 1))
                continue
            if use_cache and not verdict:
                db.cache_put(key, data)
            return data
        except (requests.RequestException, ValueError) as e:
            last = SourceError(f"{cache_prefix}: {e}")
            time.sleep(3 * (attempt + 1))
    raise last
