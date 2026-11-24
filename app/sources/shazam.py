"""Identify a song by its sound with Shazam, for files no source found by title.

Uses the shazamio library: it fingerprints a 12-second stretch of the file on this machine (the
audio itself is never uploaded, only the fingerprint) and asks Shazam's own servers what it is.
That API is unofficial and undocumented, so it can break or start refusing requests at any time;
SHAZAM_ENABLED switches it off.

Shazam's answer gives the title, the artist line, the album, the ISRC and the Apple Music song id.
The matcher then looks the song up on iTunes (by that id) and JioSaavn (for the music director and
singers), so tags still come from the usual sources.

Answers are cached per file (path + length): a hit for a year, a miss for two weeks, so a later
re-check doesn't fingerprint the same file again.
"""
import asyncio
import os
import threading
import time

from .. import db
from ..config import Config
from .http import RateLimiter, SourceError

_limiter = RateLimiter(lambda: Config.SHAZAM_RPM)
_lock = threading.Lock()
_blocked = {"until": 0.0}
HIT_DAYS, MISS_DAYS = 365, 14
TIMEOUT = 45


def available():
    try:
        import shazamio  # noqa: F401
        return True
    except ImportError:
        return False


def enabled():
    return bool(Config.SHAZAM_ENABLED) and available()


def _recognize(path):
    """Shazam's raw answer for one file. Raises on network or library errors."""
    from aiohttp_retry import ExponentialRetry
    from shazamio import Shazam
    from shazamio.client import HTTPClient

    async def run():
        # few retries and no retry on 429: Shazam's own default waits up to 20 minutes
        client = HTTPClient(retry_options=ExponentialRetry(attempts=2, max_timeout=10,
                                                           statuses={500, 502, 503, 504}))
        try:
            return await asyncio.wait_for(Shazam(http_client=client).recognize(path), TIMEOUT)
        finally:
            await client.close()
    return asyncio.run(run())


def parse(answer):
    """Shazam's answer -> {title, artist, album, year, label, isrc, apple_id, key, url, image, genre}
    or None when it didn't recognise the song."""
    tr = (answer or {}).get("track") if isinstance(answer, dict) else None
    if not tr or not (tr.get("title") or "").strip():
        return None
    meta = {}
    for sec in tr.get("sections") or []:
        if sec.get("type") == "SONG":
            for m in sec.get("metadata") or []:
                meta[(m.get("title") or "").strip().lower()] = (m.get("text") or "").strip()
    apple = ""
    for a in (tr.get("hub") or {}).get("actions") or []:
        if a.get("type") == "applemusicplay" and a.get("id"):
            apple = str(a["id"])
            break
    imgs = tr.get("images") or {}
    year = meta.get("released", "")[:4]
    return {
        "title": tr["title"].strip(), "artist": (tr.get("subtitle") or "").strip(),
        "album": meta.get("album", ""), "year": year if year.isdigit() else "",
        "label": meta.get("label", ""), "isrc": tr.get("isrc") or "", "apple_id": apple,
        "key": str(tr.get("key") or ""), "url": tr.get("url") or "",
        "image": imgs.get("coverarthq") or imgs.get("coverart") or "",
        "genre": (tr.get("genres") or {}).get("primary") or "",
    }


def _cache_key(path, duration):
    return f"{os.path.normpath(path)}|{round(float(duration or 0), 1)}"


def cached(path, duration=0.0):
    """What Shazam said about this file before, without asking again (None if nothing)."""
    return db.cache_get("shazam:" + _cache_key(path, duration), HIT_DAYS * 86400)


def identify(path, duration=0.0):
    """(hit or None, note). Cached; waits for the rate limit; backs off for 15 minutes when Shazam
    refuses requests."""
    key = _cache_key(path, duration)
    hit = db.cache_get("shazam:" + key, HIT_DAYS * 86400)
    if hit is not None:
        return hit, ""
    if db.cache_get("shazam-miss:" + key, MISS_DAYS * 86400) is not None:
        return None, "Shazam didn't recognise it (checked earlier)"
    if time.time() < _blocked["until"]:
        return None, "Shazam is refusing requests for now; tried again later"
    with _lock:                                     # one fingerprint at a time on the NAS
        _limiter.wait()
        try:
            answer = _recognize(path)
        except Exception as e:  # noqa: BLE001 - network, library or decoding errors
            msg = f"{type(e).__name__}: {e}".strip(": ")
            if type(e).__name__ == "RateLimited" or "429" in msg:
                _blocked["until"] = time.time() + 15 * 60
                msg = "Shazam is refusing requests (too many); paused for 15 minutes"
            raise SourceError(f"Shazam: {msg}") from None
    if not isinstance(answer, dict) or "matches" not in answer:
        raise SourceError("Shazam: unexpected reply (the unofficial API may have changed)")
    found = parse(answer)
    if found:
        db.cache_put("shazam:" + key, found)
        return found, ""
    db.cache_put("shazam-miss:" + key, {"at": time.time()})
    return None, "Shazam didn't recognise it"
