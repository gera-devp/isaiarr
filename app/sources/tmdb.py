"""TMDB (The Movie Database) as a check on the music director, not as an album source.

After a film album is matched, the film is looked up on TMDB and its music credits (jobs such as
"Original Music Composer" or "Music Director") are compared with the Album Artist the sources
gave. TMDB is independent of Wikipedia, so agreement is real confirmation, and a different name
is worth a look before writing.

- Needs a free API key or read access token (TMDB_API_KEY, themoviedb.org → Settings → API).
- Free for non-commercial use. TMDB asks apps to say they use TMDB and aren't endorsed by it;
  that line is on the Settings page next to the key.
- Same-name films (Vikram 1986 and 2022) are both checked before calling a name different, since
  streaming re-issues often carry the wrong year.
"""
import re

from rapidfuzz import fuzz

from ..config import Config
from .http import RateLimiter, SourceError, get_json

API = "https://api.themoviedb.org/3"
SITE = "https://www.themoviedb.org/movie"
ATTRIBUTION = "This product uses the TMDB API but is not endorsed or certified by TMDB."
_limiter = RateLimiter(lambda: Config.TMDB_RPM)

# Crew jobs that name who composed a film's songs or score
MUSIC_JOBS = {"original music composer", "music", "music director", "composer", "songs", "music composer",
              "original music", "music by"}
LANG = {"tamil": "ta", "telugu": "te", "malayalam": "ml", "kannada": "kn", "hindi": "hi", "bengali": "bn",
        "marathi": "mr", "punjabi": "pa"}
SPLIT = re.compile(r"\s*(?:,|;|&|/|\band\b|–|—|\s-\s)\s*", re.I)


def enabled():
    return bool(Config.TMDB_CHECK and Config.TMDB_API_KEY)


def _auth():
    key = (Config.TMDB_API_KEY or "").strip()
    if key.startswith("eyJ"):                       # v4 read access token (a JWT)
        return {}, {"Authorization": f"Bearer {key}"}
    return {"api_key": key}, {}


def _get(path, params=None):
    p, h = _auth()
    try:
        return get_json(f"{API}{path}", {**(params or {}), **p}, _limiter, "tmdb", headers=h,
                        max_age=30 * 86400)
    except SourceError as e:
        key = Config.TMDB_API_KEY or ""
        raise SourceError(str(e).replace(key, "***") if key else str(e)) from None


# ------------------------------------------------------------------ names

def _squash(s):
    return re.sub(r"[^a-z]", "", (s or "").lower())


def _people(s):
    s = re.sub(r"\s*\([^)]*\)", "", s or "")             # "Ilaiyaraaja (songs)" -> "Ilaiyaraaja"
    return [p.strip() for p in SPLIT.split(s) if p.strip()]


def same_person(a, b):
    """'A.R. Rahman' = 'A. R. Rahman'; 'Ilayaraja' = 'Ilaiyaraaja'; 'Harris Jeyaraj' = 'Harris Jayaraj'."""
    from ..artists import sound_key
    sa, sb = _squash(a), _squash(b)
    if not sa or not sb:
        return False
    if sa == sb or fuzz.ratio(sa, sb) >= 88:
        return True
    if min(len(sa), len(sb)) >= 5 and sound_key(a) == sound_key(b):
        return True
    # a duo credited by surname ("Viswanathan–Ramamoorthy") against one member ("M. S. Viswanathan")
    la, lb = (a.split() or [""])[-1], (b.split() or [""])[-1]
    return len(_squash(la)) >= 6 and len(_squash(lb)) >= 6 and sound_key(la) == sound_key(lb) and (
        len(a.split()) == 1 or len(b.split()) == 1)


def matches_any(album_artist, composers):
    return any(same_person(p, c) for p in _people(album_artist) for c in composers)


# ------------------------------------------------------------------ films

def _norm(s):
    return re.sub(r"[^\w]+", " ", (s or "").casefold()).strip()


def _year(s):
    m = re.match(r"^(\d{4})", str(s or ""))
    return m.group(1) if m else ""


def films(title, language=""):
    """Films whose title is the album name, most likely first: [{id, title, year, lang}]."""
    q = _norm(title)
    if not q:
        return []
    data = _get("/search/movie", {"query": title, "include_adult": "false", "language": "en-US"})
    want = LANG.get((language or "").lower(), "")
    out = []
    for r in (data or {}).get("results") or []:
        names = [_norm(r.get("title")), _norm(r.get("original_title"))]
        sim = max(100 if n == q else fuzz.token_sort_ratio(n, q) for n in names if n) if any(names) else 0
        if sim < 90:
            continue
        out.append({"id": r.get("id"), "title": r.get("title") or r.get("original_title") or "",
                    "year": _year(r.get("release_date")), "lang": r.get("original_language") or "",
                    "sim": sim, "lang_ok": not want or r.get("original_language") == want,
                    "popularity": r.get("popularity") or 0})
    out.sort(key=lambda f: (not f["lang_ok"], -f["sim"], -f["popularity"]))
    return out


def composers(movie_id):
    data = _get(f"/movie/{movie_id}/credits")
    names = []
    for c in (data or {}).get("crew") or []:
        if (c.get("job") or "").strip().lower() in MUSIC_JOBS:
            n = (c.get("name") or "").strip()
            if n and n not in names:
                names.append(n)
    return names


def check(album_name, year, album_artist, language=""):
    """Compare an album's music director with TMDB's credits for the film.
    Returns {status: agree|differ|nofilm|nocredits, film, year, url, composers, checked}."""
    from ..matcher import clean_album
    title = clean_album(album_name)
    out = {"source": "tmdb", "checked": album_artist, "film": "", "year": "", "url": "", "composers": []}
    cands = films(title, language)
    if not cands:
        return {**out, "status": "nofilm"}
    # same title, plausible language; the closest year first
    pool = [f for f in cands if f["lang_ok"]] or cands
    if year and year.isdigit():
        pool.sort(key=lambda f: abs(int(f["year"]) - int(year)) if f["year"] else 99)
        near = [f for f in pool if f["year"] and abs(int(f["year"]) - int(year)) <= 1]
    else:
        near = pool[:1] if len(pool) == 1 else []
    first = (near or pool)[0]
    tried = []
    for f in [first] + [x for x in pool[:4] if x is not first]:
        names = composers(f["id"])
        tried.append((f, names))
        if names and matches_any(album_artist, names):
            return {**out, "status": "agree", "film": f["title"], "year": f["year"],
                    "url": f"{SITE}/{f['id']}", "composers": names}
    # no film of that name credits this music director. Only call it different when we're sure
    # which film the album is: a year that fits, or the only film with that title.
    f, names = tried[0]
    sure = bool(near) or len(pool) == 1
    if not names:
        return {**out, "status": "nocredits", "film": f["title"], "year": f["year"], "url": f"{SITE}/{f['id']}"}
    return {**out, "status": "differ" if sure else "unsure", "film": f["title"], "year": f["year"],
            "url": f"{SITE}/{f['id']}", "composers": names}
