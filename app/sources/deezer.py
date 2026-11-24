"""Deezer as an album source (official public API, no key).

Good for: complete track lists with track and disc numbers, lengths, release dates and
1000 px covers from the label feeds (Sony, Think, Saregama, Lahari, Divo...).
Weak for: credits. Deezer only marks artists as main or featured, so it can't tell the music
director from the singers. When a Deezer album wins, each matched song's artists are fetched
and the album artist (the music director) is taken out; what's left are the singers. A song
whose only artist is the music director gets no singers from Deezer, so the file's own Artist
tag is kept.

Deezer answers some errors (quota, "no data") with HTTP 200 and an `error` object; those are
never cached.
"""
import re

from ..config import Config
from .http import RateLimiter, SourceError, clean_label, full_date, get_json

API = "https://api.deezer.com"
_limiter = RateLimiter(lambda: Config.DEEZER_RPM)


def _check(data):
    err = data.get("error") if isinstance(data, dict) else None
    if not err:
        return None
    code = err.get("code") if isinstance(err, dict) else None
    msg = (err.get("message") if isinstance(err, dict) else str(err)) or "error"
    if code == 4:                                   # "Quota limit exceeded": wait and retry
        return ("retry", f"quota exceeded ({msg})")
    return ("nocache", msg)


def _get(path, params=None):
    data = get_json(f"{API}{path}", params or {}, _limiter, "deezer", max_age=30 * 86400, check=_check)
    if not isinstance(data, dict):
        raise SourceError("deezer: unexpected reply")
    if data.get("error"):
        err = data["error"]
        if isinstance(err, dict) and err.get("code") == 800:      # no data: not found
            return None
        raise SourceError(f"deezer: {err.get('message') if isinstance(err, dict) else err}")
    return data


def _year(d):
    return str(d or "")[:4] if str(d or "")[:4].isdigit() and str(d)[:4] != "0000" else ""


def _genre(album):
    names = " ".join(g.get("name", "") for g in ((album.get("genres") or {}).get("data") or []))
    return "Tamil" if "tamil" in (names + " " + (album.get("title") or "")).lower() else ""


def _summary(a):
    aid = a.get("id")
    return {
        "collection_id": aid,
        "ref": f"deezer:{aid}",
        "album": (a.get("title") or "").strip(),
        "album_artist": ((a.get("artist") or {}).get("name") or "").strip(),
        "track_count": a.get("nb_tracks") or 0,
        "date": a.get("release_date") or "",
        "genre": _genre(a),
        "artwork": a.get("cover_xl") or a.get("cover_big") or "",
        "thumb": a.get("cover_medium") or a.get("cover") or "",
        "url": a.get("link") or (f"https://www.deezer.com/album/{aid}" if aid else ""),
        "source": "deezer",
        "record_type": a.get("record_type") or "",
        "label": clean_label(a.get("label")),
        "release_date": full_date(a.get("release_date")),
    }


def _track(t, album, index, disc_counts, disc_total, album_artist):
    disc = t.get("disk_number") or 1
    artist = ((t.get("artist") or {}).get("name") or "").strip()
    return {
        "track_id": t.get("id"),
        "title": (t.get("title") or "").strip(),
        # the track's main artist is often the music director; then it says nothing about singers
        "artist": "" if same_name(artist, album_artist) else artist,
        "composer": "",
        "album": (album.get("title") or "").strip(),
        "album_artist": album_artist,
        "tracknumber": t.get("track_position") or index,
        "tracktotal": disc_counts.get(disc, 0),
        "discnumber": disc,
        "disctotal": disc_total,
        "duration": float(t.get("duration") or 0),
        "genre": _genre(album),
        "date": _year(album.get("release_date")),
        "image": album.get("cover_xl") or "",
        "isrc": t.get("isrc") or "",
        "has_lyrics": False,
        "source": "deezer",
    }


def same_name(a, b):
    """Same person, ignoring dots, spaces and Tamil transliteration differences."""
    from ..artists import sound_key
    sa, sb = re.sub(r"[^a-z]", "", (a or "").lower()), re.sub(r"[^a-z]", "", (b or "").lower())
    if not sa or not sb:
        return False
    return sa == sb or (len(sa) >= 4 and sound_key(a) == sound_key(b))


def lookup_album(album_id):
    """Return (album_summary, [tracks]) for a Deezer album id."""
    album = _get(f"/album/{album_id}")
    if not album or not album.get("id"):
        return None, []
    data = _get(f"/album/{album_id}/tracks", {"limit": 300}) or {}
    raw = data.get("data") or ((album.get("tracks") or {}).get("data") or [])
    raw = [t for t in raw if isinstance(t, dict) and t.get("title")]
    if not raw:
        return None, []
    disc_counts = {}
    for t in raw:
        d = t.get("disk_number") or 1
        disc_counts[d] = disc_counts.get(d, 0) + 1
    disc_total = max(disc_counts) if disc_counts else 1
    summary = _summary(album)
    summary["date"] = _year(album.get("release_date"))
    summary["track_count"] = len(raw)
    tracks, seen = [], {}
    for t in raw:
        d = t.get("disk_number") or 1
        seen[d] = seen.get(d, 0) + 1
        tracks.append(_track(t, album, seen[d], disc_counts, disc_total, summary["album_artist"]))
    tracks.sort(key=lambda x: (x["discnumber"], x["tracknumber"]))
    return summary, tracks


def add_singers(tracks, album_artist):
    """Fetch each track's artists and keep everyone but the music director as its singers.
    Only called for the album that won, for the songs that matched."""
    for t in tracks:
        if t.get("source") != "deezer" or t.get("_singers_checked") or not t.get("track_id"):
            continue
        t["_singers_checked"] = True
        try:
            d = _get(f"/track/{t['track_id']}")
        except SourceError:
            continue
        names = []
        for c in (d or {}).get("contributors") or []:
            n = (c.get("name") or "").strip()
            if n and not same_name(n, album_artist) and n.lower() not in ("various artists", "various") \
                    and n not in names:
                names.append(n)
        if names:
            t["artist"] = ", ".join(names)
        if (d or {}).get("isrc"):
            t["isrc"] = d["isrc"]


def search_albums(term, limit=8):
    term = re.sub(r"\s+", " ", term or "").strip()
    if not term:
        return []
    data = _get("/search/album", {"q": term, "limit": limit}) or {}
    return [_summary(a) for a in data.get("data") or [] if isinstance(a, dict) and a.get("id")]


def search_tracks(term, limit=10):
    return []        # not used song by song: Deezer can't tell the singers from the music director


def track_by_isrc(isrc):
    """One track by its ISRC (as Shazam reports it), with its album id; None when unknown."""
    if not isrc:
        return None
    d = _get(f"/track/isrc:{isrc}")
    if not d or not d.get("id"):
        return None
    return d


def parse_ref(text):
    """Accept a deezer.com album URL; return the album id or None."""
    m = re.search(r"deezer\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?album/(\d+)", text or "", re.I)
    return m.group(1) if m else None
