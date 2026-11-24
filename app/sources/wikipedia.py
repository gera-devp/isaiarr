"""English Wikipedia as an album source for Indian film soundtracks.

Film articles usually carry a "Soundtrack" / "Music" section with either a {{Track listing}}
template (newer films) or a plain wikitable (older films) listing each song with its singers,
lyricist and length, plus the music director in the infobox. That makes Wikipedia strong
exactly where streaming catalogues are weak: older albums and per-song credits.

No artwork (Wikipedia covers are non-free) and no lyrics.
"""
import re
from collections import Counter

import mwparserfromhell as mwp

from ..config import Config
from .http import RateLimiter, clean_label, full_date, get_json

API = "https://en.wikipedia.org/w/api.php"
_limiter = RateLimiter(lambda: Config.WIKIPEDIA_RPM)


def _version():
    from .. import __version__
    return __version__


def _headers():
    # Wikimedia asks every client for a descriptive User-Agent with contact details.
    ua = f"Isaiarr/{_version()} (self-hosted music tagger"
    if Config.WIKIPEDIA_CONTACT:
        ua += f"; {Config.WIKIPEDIA_CONTACT}"
    return {"User-Agent": ua + ")", "Api-User-Agent": ua + ")"}


def _api(params):
    return get_json(API, {"format": "json", "formatversion": "2", **params}, _limiter, "wiki",
                    headers=_headers(), max_age=30 * 86400)


# ------------------------------------------------------------------ wikitext → text

_FLAT = {"hlist", "flatlist", "plainlist", "ubl", "unbulleted list", "bulleted list", "flat list"}
_FIRST = {"nowrap", "small", "big", "nobold", "nobr", "ill", "interlanguage link", "lang-ta",
          "transl", "tooltip", "abbr", "sic", "noitalic", "resize", "nbsp"}


def text(wikitext):
    """Wikitext fragment → plain text. Lists/<br> become ', '."""
    if not wikitext:
        return ""
    raw = re.sub(r"<br\s*/?>", ", ", str(wikitext), flags=re.I)
    raw = re.sub(r"\n\s*[*#]+\s*", ", ", raw)          # bullet lists → comma list
    code = mwp.parse(raw)
    for tpl in code.filter_templates(recursive=False):
        name = str(tpl.name).strip().lower()
        try:
            if name in _FLAT:
                repl = ", ".join(text(p.value) for p in tpl.params if not p.showkey)
            elif name == "sortname":
                repl = " ".join(text(p.value) for p in tpl.params[:2] if not p.showkey)
            elif name == "lang":
                repl = text(tpl.params[1].value) if len(tpl.params) > 1 else ""
            elif name == "nbsp":
                repl = " "
            elif name in ("transl",) or name.startswith("lang-"):
                pos = [p for p in tpl.params if not p.showkey]
                repl = text(pos[-1].value) if pos else ""
            elif name in _FIRST:
                pos = [p for p in tpl.params if not p.showkey]
                repl = text(pos[0].value) if pos else ""
            else:
                repl = ""
            code.replace(tpl, repl)
        except (ValueError, IndexError):
            code.replace(tpl, "")
    s = code.strip_code(normalize=True, collapse=True)
    s = re.sub(r"<br\s*/?>", ", ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\n\s*[*#]\s*", ", ", s)
    s = s.replace("\n", " ").replace(" ", " ")
    s = re.sub(r"\s*,\s*(,\s*)*", ", ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" ,;\"'")


def _strip_refs(wt):
    wt = re.sub(r"<ref[^>]*/>", "", wt, flags=re.I)
    wt = re.sub(r"<ref[^>]*>.*?</ref>", "", wt, flags=re.I | re.S)
    return re.sub(r"<!--.*?-->", "", wt, flags=re.S)


def _seconds(s):
    m = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", s or "")
    if not m:
        return 0.0
    a, b, c = m.groups()
    return float(int(a) * 3600 + int(b) * 60 + int(c)) if c else float(int(a) * 60 + int(b))


def _clean_song_title(t):
    t = text(t)
    t = re.sub(r'^[\"“”]+|[\"“”]+$', "", t).strip()
    return t


# ------------------------------------------------------------------ page parsing

def _infobox(code):
    for tpl in code.filter_templates(recursive=False):
        name = str(tpl.name).strip().lower()
        if name.startswith("infobox"):
            return tpl, name
    return None, ""


def _param(tpl, *names):
    for n in names:
        if tpl is not None and tpl.has(n):
            v = text(tpl.get(n).value)
            if v:
                return v
    return ""


def _year(s):
    m = re.search(r"\b(19[3-9]\d|20[0-4]\d)\b", s or "")
    return m.group(1) if m else ""


DATE_TPL = re.compile(r"\{\{\s*(?:film date|start date|release date|start date and age|dts)\s*\|"
                      r"(?:[^|}]*=[^|}]*\|)*\s*(\d{4})\s*\|\s*(\d{1,2})\s*\|\s*(\d{1,2})", re.I)


def _raw_param(tpl, *names):
    for n in names:
        if tpl is not None and tpl.has(n):
            return str(tpl.get(n).value)
    return ""


def _release_date(tpl):
    """The first full release date in the infobox: {{Film date|2022|06|03}}, or text like
    '1 November 1986'."""
    raw = _raw_param(tpl, "released", "release_date", "release")
    m = DATE_TPL.search(raw)
    if m:
        return full_date(f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}")
    return full_date(text(raw))


def _track_listings(code):
    """Yield (headline, rows) for every {{Track listing}} on the page."""
    for tpl in code.filter_templates(recursive=True):
        if str(tpl.name).strip().lower() not in ("track listing", "tracklist", "track list"):
            continue
        extra_col = _param(tpl, "extra_column").lower()
        all_music = _param(tpl, "all_music")
        rows = []
        for n in range(1, 80):
            title = _clean_song_title(tpl.get(f"title{n}").value) if tpl.has(f"title{n}") else ""
            if not title:
                if n > 1 and not tpl.has(f"title{n + 1}"):
                    break
                continue
            extra = _param(tpl, f"extra{n}")
            singers = extra if (not extra_col or "sing" in extra_col or "artist" in extra_col
                                or "vocal" in extra_col or "perform" in extra_col) else ""
            rows.append({
                "title": title,
                "singers": singers,
                "music": _param(tpl, f"music{n}") or all_music,
                "length": _seconds(_param(tpl, f"length{n}")),
                "note": _param(tpl, f"note{n}"),
            })
        if rows:
            yield _param(tpl, "headline"), rows


def _wikitables(wikitext):
    """Yield (caption, rows) for wikitables that look like song lists (Song + Singer columns)."""
    for m in re.finditer(r"\{\|(.*?)\n\|\}", wikitext, flags=re.S):
        body = m.group(1)
        caption = ""
        cm = re.search(r"\n\|\+\s*(.*)", body)
        if cm:
            caption = text(cm.group(1))
        chunks = re.split(r"\n\|-[^\n]*", body)
        header, rows = None, []
        for ch in chunks:
            lines = [ln for ln in ch.split("\n") if ln.strip() and not ln.startswith("|+")]
            cells, is_header = [], False
            for ln in lines:
                if ln.startswith("!"):
                    is_header = True
                    cells += [c for c in re.split(r"!!|\|\|", ln[1:])]
                elif ln.startswith("|"):
                    cells += [c for c in ln[1:].split("||")]
                elif cells:
                    cells[-1] += "\n" + ln
            # drop cell attributes:  style="..." | value
            cells = [re.split(r"(?<!\[)\|(?!\])", c, maxsplit=1)[-1] if re.match(r'^\s*[a-z\-]+\s*=\s*"', c) else c
                     for c in cells]
            if not cells:
                continue
            if is_header and header is None:
                header = [text(c).lower() for c in cells]
                continue
            if header:
                rows.append(cells)
        if not header:
            continue

        def col(*keys):
            for i, h in enumerate(header):
                if any(k in h for k in keys):
                    return i
            return None
        ti = col("song", "title", "track")
        si = col("singer", "artist", "vocal", "performer")
        if ti is None or si is None:
            continue
        li, mi = col("length", "duration", "time"), col("composer", "music")
        out = []
        for r in rows:
            if len(r) <= max(ti, si):
                continue
            t = _clean_song_title(r[ti])
            if not t or t.lower().startswith("total"):
                continue
            out.append({"title": t, "singers": text(r[si]),
                        "music": text(r[mi]) if mi is not None and mi < len(r) else "",
                        "length": _seconds(text(r[li])) if li is not None and li < len(r) else 0.0,
                        "note": ""})
        if out:
            yield caption, out


def _pick_listing(listings, title):
    """Prefer the Tamil (original) listing over dubbed versions on multi-language pages."""
    if not listings:
        return None
    other = re.compile(r"telugu|hindi|malayalam|kannada|dubbed|bonus|remix", re.I)
    for headline, rows in listings:
        if re.search(r"tamil", headline or "", re.I):
            return rows
    for headline, rows in listings:
        if not other.search(headline or ""):
            return rows
    return listings[0][1]


def parse_page(title, wikitext, categories=()):
    """Return (album_summary, tracks) or (None, [])."""
    wt = _strip_refs(wikitext or "")
    code = mwp.parse(wt)
    box, boxname = _infobox(code)
    listings = list(_track_listings(code)) or list(_wikitables(wt))
    rows = _pick_listing(listings, title)
    if not rows:
        return None, []
    is_album_box = "album" in boxname
    md = (_param(box, "artist") if is_album_box else "") or _param(box, "music", "music_director", "composer")
    md = re.sub(r"\s*\(.*?\)\s*", " ", md).strip()  # "Ilaiyaraaja (songs)" → "Ilaiyaraaja"
    released = _param(box, "released", "release_date", "release")
    year = _year(released) or _year(title)
    lang = _param(box, "language").lower()
    cats = " ".join(categories).lower()
    genre = "Tamil" if ("tamil" in lang or "tamil-language" in cats or "tamil film soundtracks" in cats) else (
        lang.split(",")[0].strip().title() if lang else "")
    comps = Counter(r["music"] for r in rows if r["music"])
    if not md and comps:
        md = comps.most_common(1)[0][0]
    album_name = re.sub(r"\s*\((?:\d{4} )?(?:tamil )?(?:film|soundtrack|album)\)\s*$", "", title, flags=re.I).strip()
    n = len(rows)
    tracks = []
    for i, r in enumerate(rows, 1):
        tracks.append({
            "track_id": f"{title}#{i}", "title": r["title"], "artist": r["singers"],
            "composer": r["music"] or md, "album": album_name, "album_artist": md,
            "tracknumber": i, "tracktotal": n, "discnumber": 1, "disctotal": 1,
            "duration": r["length"], "genre": genre, "date": year, "image": "",
            "has_lyrics": False, "source": "wikipedia",
        })
    page = title.replace(" ", "_")
    rd = _release_date(box)
    summary = {
        "collection_id": page, "ref": f"wikipedia:{page}", "album": album_name,
        "album_artist": md, "track_count": n, "date": year, "genre": genre,
        "artwork": "", "thumb": "", "url": f"https://en.wikipedia.org/wiki/{page}",
        "release_date": rd if rd[:4] == year else "",
        "label": clean_label(_param(box, "label")) if is_album_box else "",
        "source": "wikipedia",
    }
    return summary, tracks


# ------------------------------------------------------------------ public API (same shape as other sources)

def _fetch(title):
    data = _api({"action": "query", "prop": "revisions|categories", "rvprop": "content", "rvslots": "main",
                 "cllimit": "50", "titles": title, "redirects": "1"})
    pages = (data.get("query") or {}).get("pages") or []
    if not pages or pages[0].get("missing"):
        return None, "", []
    p = pages[0]
    revs = p.get("revisions") or []
    wt = ((revs[0].get("slots") or {}).get("main") or {}).get("content", "") if revs else ""
    cats = [c.get("title", "") for c in p.get("categories") or []]
    return p.get("title") or title, wt, cats


def lookup_album(page):
    title, wt, cats = _fetch(str(page).replace("_", " "))
    if not title:
        return None, []
    return parse_page(title, wt, cats)


def _search_titles(term, limit=6):
    data = _api({"action": "query", "list": "search", "srsearch": term, "srlimit": str(limit),
                 "srnamespace": "0", "srprop": ""})
    return [r["title"] for r in (data.get("query") or {}).get("search") or []]


def search_albums(term, limit=4):
    """Search film/soundtrack articles and parse the best few into album candidates."""
    base = re.sub(r"\s+", " ", term).strip()
    if not base:
        return []
    titles = []
    for q in (f"{base} Tamil film soundtrack", f"{base} soundtrack", base):
        for t in _search_titles(q):
            if t not in titles:
                titles.append(t)
        if len(titles) >= limit * 2:
            break
    key = re.sub(r"[^\w]+", " ", base.lower()).split()
    ranked = sorted(titles, key=lambda t: -sum(1 for k in key if k in t.lower()))
    out = []
    for t in ranked[:limit]:
        alb, tracks = lookup_album(t)
        if alb:
            out.append(alb)
    return out


def search_tracks(term, limit=5):
    return []  # Wikipedia is album-level only


def parse_ref(text_):
    m = re.search(r"en\.(?:m\.)?wikipedia\.org/wiki/([^?#]+)", text_ or "")
    if not m:
        return None
    from urllib.parse import unquote
    return unquote(m.group(1))


# ------------------------------------------------------------------ album review (for Plex)

SECTION_RE = re.compile(r"^(={2,3})\s*(soundtrack|soundtracks|music|music and soundtrack|soundtrack and music|songs|"
                        r"music and songs|soundtrack album)\s*\1\s*$", re.I | re.M)
HEADING_RE = re.compile(r"^(={2,6})[^=\n].*?\1\s*$", re.M)
BLOCK_TPL = ("infobox", "track listing", "tracklist", "track list", "main", "see also", "further", "quote box",
             "multiple image", "listen", "audio", "efn", "refn", "sfn", "cn", "citation needed", "clarify", "when",
             "who", "by whom", "music ratings", "album ratings", "external media", "reflist", "portal", "use dmy",
             "use indian english", "short description", "redirect", "for", "about", "unreferenced", "more citations")
REVIEW_MAX = 1100


def _section(wikitext, rx):
    """The wikitext of the first section whose heading matches, up to the next heading of the same
    or a higher level."""
    m = rx.search(wikitext)
    if not m:
        return ""
    level = len(m.group(1))
    rest = wikitext[m.end():]
    for h in HEADING_RE.finditer(rest):
        if len(h.group(1)) <= level:
            return rest[:h.start()]
    return rest


def _lead(wikitext):
    h = HEADING_RE.search(wikitext)
    return wikitext[:h.start()] if h else wikitext


def prose(wikitext, limit=REVIEW_MAX):
    """Plain paragraphs from a stretch of wikitext: no infoboxes, track lists, tables, images or
    references; cut at a sentence end near `limit` characters."""
    wt = _strip_refs(wikitext or "")
    wt = re.sub(r"\{\|.*?\n\|\}", "", wt, flags=re.S)               # tables
    code = mwp.parse(wt)
    for tpl in code.filter_templates(recursive=False):
        name = str(tpl.name).strip().lower()
        if "\n" in str(tpl) or name.startswith(BLOCK_TPL):
            try:
                code.remove(tpl)
            except ValueError:
                pass
    for link in code.filter_wikilinks(recursive=False):
        if str(link.title).strip().lower().startswith(("file:", "image:", "category:")):
            try:
                code.remove(link)
            except ValueError:
                pass
    paras = []
    for block in re.split(r"\n\s*\n", str(code)):
        lines = [ln for ln in block.split("\n") if ln.strip() and not ln.lstrip().startswith(("*", "#", ":", ";", "|", "!", "{", "}"))]
        t = text("\n".join(lines).replace("\n", " "))
        t = re.sub(r"\s+([,.;:])", r"\1", t)
        t = re.sub(r"\(\s*[,;]?\s*\)", "", t)          # "( )" left by removed templates
        t = re.sub(r"\s{2,}", " ", t).strip()
        if len(t) >= 80 and re.search(r"[.!?]$", t):
            paras.append(t)
    out = ""
    for p in paras:
        if len(out) + len(p) + 2 <= limit:
            out = (out + "\n\n" + p).strip()
            continue
        if not out:                                       # first paragraph alone is too long: cut it
            cut = p[:limit]
            end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
            out = cut[:end + 1] if end > 80 else cut.rsplit(" ", 1)[0] + "…"
        break
    return out


def _base_title(title):
    return re.sub(r"\s*\([^)]*\)\s*$", "", title or "").strip()


def review(album_name, year="", music_director="", page=None):
    """{text, title, url} for an album's review: the lead of the film's soundtrack article, else
    the Music / Soundtrack section of the film's article. Only from a page whose title is the album
    name and whose year and music director agree with the album. None when nothing fits.
    `page` is a Wikipedia title already known to be the album (when Wikipedia was the match)."""
    from ..matcher import clean_album, norm
    from .tmdb import matches_any
    want = norm(clean_album(album_name))
    if not want:
        return None
    titles = [page] if page else []
    if not page:
        for q in (f"{clean_album(album_name)} {year} film soundtrack".strip(), f"{clean_album(album_name)} film"):
            for t in _search_titles(q, limit=8):
                if t not in titles and norm(_base_title(t)) == want:
                    titles.append(t)
            if titles:
                break
    titles.sort(key=lambda t: 0 if "soundtrack" in t.lower() else 1)      # a soundtrack article first
    for t in titles[:4]:
        title, wt, cats = _fetch(str(t).replace("_", " "))
        if not title or not wt:
            continue
        code = mwp.parse(_strip_refs(wt))
        box, boxname = _infobox(code)
        page_year = ((_release_date(box) or "")[:4] or _year(_param(box, "released", "release_date", "release"))
                     or _year(_raw_param(box, "released", "release_date", "release")) or _year(title))
        if year and page_year and abs(int(page_year) - int(year)) > 1:
            continue
        md = ((_param(box, "artist") if "album" in boxname else "") or _param(box, "music", "music_director", "composer"))
        if music_director and md and not matches_any(music_director, [re.sub(r"\s*\(.*?\)\s*", " ", md).strip()]):
            continue
        if not music_director and not page and not (year and page_year):
            continue                                       # nothing to tell same-name films apart
        if "soundtrack" in title.lower() or "album" in boxname:
            body = prose(_lead(wt))
        else:
            body = prose(_section(wt, SECTION_RE))
        if body:
            return {"text": body, "title": title, "url": f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}"}
    return None


def review_text(r):
    """The review as Plex shows it, with the credit Wikipedia's licence asks for."""
    if not r:
        return ""
    return f"{r['text']}\n\nFrom Wikipedia, “{r['title']}” (CC BY-SA 4.0): {r['url']}"
