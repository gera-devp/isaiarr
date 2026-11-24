"""iTunes Search API client (album search, album track lookup, song search)."""
import re

import requests

from ..config import Config
from .http import RateLimiter, clean_label, full_date, get_json

BASE = "https://itunes.apple.com"
_limiter = RateLimiter(lambda: Config.ITUNES_RPM)


def search_albums(term, limit=15):
    data = get_json(f"{BASE}/search",
                    {"term": term, "entity": "album", "media": "music",
                     "country": Config.ITUNES_COUNTRY, "limit": limit},
                    _limiter, "itunes")
    return [album_summary(r) for r in data.get("results", []) if r.get("wrapperType") == "collection"]


def album_summary(r):
    url100 = r.get("artworkUrl100") or ""
    return {
        "collection_id": r.get("collectionId"),
        "ref": f"itunes:{r.get('collectionId')}",
        "album": r.get("collectionName") or "",
        "album_artist": r.get("artistName") or "",
        "track_count": r.get("trackCount") or 0,
        "date": (r.get("releaseDate") or "")[:10],
        "release_date": full_date(r.get("releaseDate")),
        "label": clean_label(r.get("copyright")),
        "genre": r.get("primaryGenreName") or "",
        "artwork": artwork_url(url100),
        "thumb": url100,
        "url": r.get("collectionViewUrl") or "",
        "source": "itunes",
    }


def _track(r):
    return {
        "track_id": r.get("trackId"),
        "title": r.get("trackName") or "",
        "artist": r.get("artistName") or "",
        "composer": "",
        "album": r.get("collectionName") or "",
        "album_artist": r.get("collectionArtistName") or "",
        "tracknumber": r.get("trackNumber") or 0,
        "tracktotal": r.get("trackCount") or 0,
        "discnumber": r.get("discNumber") or 1,
        "disctotal": r.get("discCount") or 1,
        "duration": (r.get("trackTimeMillis") or 0) / 1000.0,
        "genre": r.get("primaryGenreName") or "",
        "date": (r.get("releaseDate") or "")[:10],
        "image": artwork_url(r.get("artworkUrl100") or ""),
        "has_lyrics": False,
        "source": "itunes",
    }


def lookup_album(collection_id):
    """Return (album_summary, [tracks]) for a collection id."""
    data = get_json(f"{BASE}/lookup",
                    {"id": collection_id, "entity": "song", "country": Config.ITUNES_COUNTRY,
                     "limit": 200},
                    _limiter, "itunes")
    album, tracks = None, []
    for r in data.get("results", []):
        if r.get("wrapperType") == "collection":
            album = album_summary(r)
        elif r.get("wrapperType") == "track" and r.get("kind", "song") == "song":
            tracks.append(_track(r))
    tracks.sort(key=lambda t: (t["discnumber"], t["tracknumber"]))
    return album, tracks


def lookup_track(song_id):
    """One song by its Apple Music / iTunes id (as Shazam reports it), or None. Tries the store
    country first, then the US store, which carries most Indian releases too."""
    for country in dict.fromkeys([Config.ITUNES_COUNTRY, "us"]):
        data = get_json(f"{BASE}/lookup", {"id": song_id, "entity": "song", "country": country},
                        _limiter, "itunes")
        for r in data.get("results", []):
            if r.get("wrapperType") == "track" and str(r.get("trackId")) == str(song_id):
                return _track(r)
    return None


def search_tracks(term, limit=10):
    data = get_json(f"{BASE}/search",
                    {"term": term, "entity": "song", "media": "music",
                     "country": Config.ITUNES_COUNTRY, "limit": limit},
                    _limiter, "itunes")
    return [_track(r) for r in data.get("results", []) if r.get("wrapperType") == "track"]


def artwork_url(url100, size=None):
    size = size or Config.ARTWORK_SIZE
    return re.sub(r"/\d+x\d+bb\.(jpg|png)$", f"/{size}x{size}bb.jpg", url100 or "")


def parse_ref(text):
    """Accept an Apple Music / iTunes album URL or a bare id."""
    text = (text or "").strip()
    if text.isdigit():
        return text
    if "apple.com" in text:
        # .../album/name/id123 (old iTunes) or .../album/name/123?i=456 (Apple Music)
        m = re.search(r"/id(\d+)", text) or re.search(r"/album/(?:[^/?#]+/)?(\d+)(?:[/?#]|$)", text)
        return m.group(1) if m else None
    return None


def _is_image(b):
    return b[:3] == b"\xff\xd8\xff" or b[:8] == b"\x89PNG\r\n\x1a\n"


def fetch_artwork(url):
    """Download album art (iTunes or JioSaavn URL). Falls back to a smaller iTunes size."""
    if not url:
        return None
    urls = [url]
    if "mzstatic" in url:
        urls = [artwork_url(url, Config.ARTWORK_SIZE), artwork_url(url, 600)]
    for u in urls:
        try:
            r = requests.get(u, timeout=30)
            if r.ok and _is_image(r.content):
                return r.content
        except requests.RequestException:
            pass
    return None
