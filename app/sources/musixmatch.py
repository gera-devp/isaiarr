"""Musixmatch client: per-track metadata, plain lyrics and synced (LRC) lyrics.

Two modes:
  desktop - the token flow used by the Musixmatch desktop app (unofficial, no key needed,
            can hit captcha/rate limits). Optionally pin MUSIXMATCH_USERTOKEN.
  api     - official API with MUSIXMATCH_API_KEY (free tier returns partial lyrics only).
"""
import re
import time

from .. import db
from ..config import Config
from .http import RateLimiter, SourceError, get_json

_limiter = RateLimiter(lambda: Config.MUSIXMATCH_RPM)
DESKTOP = "https://apic-desktop.musixmatch.com/ws/1.1"
OFFICIAL = "https://api.musixmatch.com/ws/1.1"
HEADERS = {"authority": "apic-desktop.musixmatch.com", "cookie": "AWSELBCORS=0; AWSELB=0"}
APP_ID = "web-desktop-app-v1.0"


def enabled():
    if not Config.MUSIXMATCH_ENABLED:
        return False
    if Config.MUSIXMATCH_MODE == "api":
        return bool(Config.MUSIXMATCH_API_KEY)
    return True


def _token(force=False):
    if Config.MUSIXMATCH_USERTOKEN:
        return Config.MUSIXMATCH_USERTOKEN
    if not force:
        hit = db.cache_get("mxm:token", max_age=6 * 3600)
        if hit:
            return hit
    data = get_json(f"{DESKTOP}/token.get", {"app_id": APP_ID, "t": int(time.time())},
                    _limiter, "mxm-token", headers=HEADERS, use_cache=False)
    tok = (data.get("message", {}).get("body") or {}).get("user_token")
    if not tok or tok.startswith("UpgradeOnly"):
        raise SourceError("Musixmatch: could not get a desktop token (captcha / rate limit); "
                          "try again later or set MUSIXMATCH_USERTOKEN")
    db.cache_put("mxm:token", tok)
    return tok


_NOTICE = re.compile(r"\n*\*+\s*This Lyrics is NOT for Commercial use.*$", re.S | re.I)


def _clean_lyrics(s):
    s = _NOTICE.sub("", s or "").strip()
    s = re.sub(r"\(\d{6,}\)\s*$", "", s).strip()  # tracking id on free tier
    return s


def lookup(title, artist="", album="", duration=0):
    """Return dict(title, artist, album, duration, lyrics, synced) or None."""
    if not enabled() or not title:
        return None
    if Config.MUSIXMATCH_MODE == "api":
        return _lookup_api(title, artist, album) or (artist and _lookup_api(title, "", ""))
    # Strict first (artist+album+length), then progressively looser: Tamil songs are often
    # listed under a different artist spelling or without the film name.
    hit = _lookup_desktop(title, artist, album, duration)
    if not hit and (artist or album):
        hit = _lookup_desktop(title, "", album, duration, strict_length=False)
    if not hit and album:
        hit = _lookup_desktop(title, "", "", duration, strict_length=False)
    return hit


def _lookup_desktop(title, artist, album, duration, _retry=True, strict_length=True):
    params = {
        "format": "json", "namespace": "lyrics_richsynched", "subtitle_format": "lrc",
        "app_id": APP_ID, "q_track": title, "q_artist": artist, "q_album": album,
        "usertoken": _token(),
    }
    if duration and strict_length:
        params["f_subtitle_length"] = int(round(duration))
        params["f_subtitle_length_max_deviation"] = 4
    if duration:
        params["q_duration"] = int(round(duration))
    data = get_json(f"{DESKTOP}/macro.subtitles.get", params, _limiter, "mxm", headers=HEADERS)
    header = data.get("message", {}).get("header", {})
    if header.get("status_code") == 401 and _retry and not Config.MUSIXMATCH_USERTOKEN:
        db.cache_put("mxm:token", None)
        _token(force=True)
        return _lookup_desktop(title, artist, album, duration, _retry=False, strict_length=strict_length)
    calls = (data.get("message", {}).get("body") or {}).get("macro_calls") or {}

    def body(name):
        b = (calls.get(name) or {}).get("message", {}).get("body")
        return b if isinstance(b, dict) else {}

    track = body("matcher.track.get").get("track")
    if not track:
        return None
    lyr = body("track.lyrics.get").get("lyrics") or {}
    subs = body("track.subtitles.get").get("subtitle_list") or []
    synced = ""
    if subs:
        synced = (subs[0].get("subtitle") or {}).get("subtitle_body") or ""
    return {
        "title": track.get("track_name") or "",
        "artist": track.get("artist_name") or "",
        "album": track.get("album_name") or "",
        "duration": track.get("track_length") or 0,
        "instrumental": bool(track.get("instrumental")),
        "lyrics": _clean_lyrics(lyr.get("lyrics_body")),
        "synced": synced.strip(),
        "mxm_id": track.get("track_id"),
    }


def _lookup_api(title, artist, album):
    key = Config.MUSIXMATCH_API_KEY
    data = get_json(f"{OFFICIAL}/matcher.track.get",
                    {"q_track": title, "q_artist": artist, "q_album": album, "apikey": key},
                    _limiter, "mxm")
    track = (data.get("message", {}).get("body") or {})
    track = track.get("track") if isinstance(track, dict) else None
    if not track:
        return None
    lyrics = ""
    if track.get("has_lyrics"):
        ld = get_json(f"{OFFICIAL}/track.lyrics.get",
                      {"track_id": track["track_id"], "apikey": key}, _limiter, "mxm")
        b = ld.get("message", {}).get("body")
        if isinstance(b, dict):
            lyrics = _clean_lyrics((b.get("lyrics") or {}).get("lyrics_body"))
    return {
        "title": track.get("track_name") or "",
        "artist": track.get("artist_name") or "",
        "album": track.get("album_name") or "",
        "duration": track.get("track_length") or 0,
        "instrumental": bool(track.get("instrumental")),
        "lyrics": lyrics,
        "synced": "",
        "mxm_id": track.get("track_id"),
    }


def lrc_to_plain(lrc):
    lines = []
    for line in (lrc or "").splitlines():
        t = re.sub(r"^(\[[^\]]*\])+", "", line).strip()
        if t or (lines and lines[-1]):
            lines.append(t)
    return "\n".join(lines).strip()
