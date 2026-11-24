"""JioSaavn client (the website's own JSON endpoints - unofficial, no key needed).

Why it's useful for Tamil music: every song carries the music director ("music") separately
from the singers, the catalogue covers old film albums iTunes lacks, and many songs have lyrics.
Responses are normalised to the same shapes the iTunes client returns.
"""
import html
import re

from ..config import Config
from .http import RateLimiter, clean_label, full_date, get_json

BASE = "https://www.jiosaavn.com/api.php"
COMMON = {"_format": "json", "_marker": "0", "api_version": "4", "ctx": "web6dot0"}
HEADERS = {"Accept": "application/json", "Referer": "https://www.jiosaavn.com/"}
_limiter = RateLimiter(lambda: Config.JIOSAAVN_RPM)


def _call(params):
    return get_json(BASE, {**COMMON, **params}, _limiter, "saavn", headers=HEADERS)


def _u(s):
    return html.unescape(str(s or "")).strip()


def _names(lst):
    out = []
    for a in lst or []:
        n = _u(a.get("name") if isinstance(a, dict) else a)
        if n and n not in out:
            out.append(n)
    return out


def hires(url):
    return re.sub(r"(\d+)x(\d+)(?=\.(jpg|jpeg|png|webp))", "500x500", url or "")


def _song(d, index=0, total=0):
    """Normalise a song object (api v4 or the older flat format)."""
    mi = d.get("more_info") or {}
    am = mi.get("artistMap") or {}
    singers = [a for a in (am.get("artists") or []) if isinstance(a, dict) and a.get("role") == "singer"]
    singers = _names(singers) or _names(am.get("primary_artists"))
    if not singers:
        singers = [s.strip() for s in _u(d.get("singers") or d.get("primary_artists")).split(",") if s.strip()]
    music = _u(mi.get("music") or d.get("music"))
    if not music:
        music = ", ".join(_names([a for a in (am.get("artists") or [])
                                  if isinstance(a, dict) and a.get("role") == "music"]))
    try:
        duration = float(mi.get("duration") or d.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    has_lyrics = str(mi.get("has_lyrics") or d.get("has_lyrics") or "").lower() == "true"
    return {
        "track_id": d.get("id"),
        "title": _u(d.get("title") or d.get("song")),
        "artist": ", ".join(singers),
        "composer": music,
        "album": _u(mi.get("album") or d.get("album")),
        "album_id": mi.get("album_id") or d.get("albumid") or "",
        "tracknumber": index,
        "tracktotal": total,
        "discnumber": 1,
        "disctotal": 1,
        "duration": duration,
        "genre": _u(d.get("language")).title(),
        "date": str(d.get("year") or "")[:4],
        "has_lyrics": has_lyrics,
        "image": hires(d.get("image")),
        "url": d.get("perma_url") or "",
        "label": clean_label(mi.get("label") or d.get("label")),
        "release_date": full_date(mi.get("release_date") or d.get("release_date")),
        "source": "jiosaavn",
    }


def _album_artist(album_obj, songs):
    """Music director: the composer every song agrees on, else the album's primary artist."""
    comps = [s["composer"] for s in songs if s["composer"]]
    if comps:
        most = max(set(comps), key=comps.count)
        if comps.count(most) >= max(1, len(comps) * 0.6):
            return most
        return "Various Artists"
    mi = album_obj.get("more_info") or {}
    am = mi.get("artistMap") or {}
    names = _names(am.get("primary_artists")) or [_u(album_obj.get("primary_artists") or album_obj.get("subtitle"))]
    return ", ".join(n for n in names if n)


def _summary(a, songs=None):
    mi = a.get("more_info") or {}
    try:
        count = int(mi.get("song_count") or a.get("list_count") or len(songs or []) or 0)
    except (TypeError, ValueError):
        count = len(songs or [])
    artist = _album_artist(a, songs) if songs else (_u(mi.get("music")) or _u(a.get("subtitle")))
    aid = a.get("id") or a.get("albumid")
    labels = [s["label"] for s in songs or [] if s.get("label")]
    dates = [s["release_date"] for s in songs or [] if s.get("release_date")]
    return {
        "collection_id": aid,
        "ref": f"jiosaavn:{aid}",
        "album": _u(a.get("title") or a.get("name")),
        "album_artist": artist,
        "track_count": count,
        "date": str(a.get("year") or "")[:4],
        "genre": _u(a.get("language")).title(),
        "artwork": hires(a.get("image")),
        "thumb": a.get("image") or "",
        "url": a.get("perma_url") or "",
        "label": clean_label(mi.get("label") or a.get("label")) or (max(set(labels), key=labels.count) if labels else ""),
        "release_date": full_date(mi.get("release_date") or a.get("release_date"))
        or (max(set(dates), key=dates.count) if dates else ""),
        "source": "jiosaavn",
    }


def search_albums(term, limit=10):
    data = _call({"__call": "search.getAlbumResults", "q": term, "p": "1", "n": str(limit)})
    return [_summary(a) for a in (data.get("results") or []) if isinstance(a, dict) and a.get("id")]


def lookup_album(album_id):
    """album_id: numeric JioSaavn id, or the token at the end of a jiosaavn.com/album/... URL."""
    album_id = str(album_id)
    if album_id.isdigit():
        data = _call({"__call": "content.getAlbumDetails", "albumid": album_id})
    else:
        data = _call({"__call": "webapi.get", "token": album_id, "type": "album", "includeMetaTags": "0"})
    if not isinstance(data, dict) or not (data.get("id") or data.get("albumid")):
        return None, []
    raw = data.get("list") or data.get("songs") or []
    songs = [_song(s, i + 1, len(raw)) for i, s in enumerate(raw) if isinstance(s, dict)]
    return _summary(data, songs), songs


def search_tracks(term, limit=10):
    data = _call({"__call": "search.getResults", "q": term, "p": "1", "n": str(limit)})
    return [_song(s) for s in (data.get("results") or []) if isinstance(s, dict) and s.get("id")]


def lyrics(track_id):
    data = _call({"__call": "lyrics.getLyrics", "lyrics_id": track_id})
    text = data.get("lyrics") if isinstance(data, dict) else ""
    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    return _u(re.sub(r"<[^>]+>", "", text)).strip()


def parse_ref(text):
    """Accept a jiosaavn.com album URL or id; return the id/token or None."""
    m = re.search(r"jiosaavn\.com/album/[^/]+/([A-Za-z0-9_,\-]+)", text or "")
    return m.group(1) if m else None
