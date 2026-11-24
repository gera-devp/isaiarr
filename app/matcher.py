"""Turn an album folder into a proposal: matched album (iTunes, JioSaavn, Wikipedia, Discogs, Deezer),
per-file new tags, diff and renames."""
import os
import re
from collections import Counter

from rapidfuzz import fuzz

from . import tags as T
from .config import Config
from .sources import deezer, discogs, itunes, jiosaavn, musixmatch, shazam, tmdb, wikipedia
from .sources.http import SourceError, full_date

SUFFIX_RE = re.compile(
    r"\s*[\(\[]\s*(original\s+(motion\s+picture\s+)?soundtrack|o\.?s\.?t\.?|from\s+[^\)\]]*|"
    r"tamil|telugu|hindi|malayalam|kannada|\d{4}|deluxe[^\)\]]*|remaster[^\)\]]*)\s*[\)\]]",
    re.I)
NOISE_RE = re.compile(r"\b(ost|original soundtrack|soundtrack|- ep|- single)\b", re.I)
TRACKPREFIX_RE = re.compile(r"^\s*(\d{1,2}[-.\s]+)?\d{1,3}\s*[-._)\s]+\s*")


def clean_album(name):
    s = name or ""
    for _ in range(3):
        s = SUFFIX_RE.sub("", s)
    s = NOISE_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip(" -_.")


def clean_title(name):
    s = re.sub(r"\s*[\(\[](from|feat\.?|ft\.?)[^\)\]]*[\)\]]", "", name or "", flags=re.I)
    return re.sub(r"\s+", " ", s).strip()


def stem_title(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = TRACKPREFIX_RE.sub("", stem)
    return stem.replace("_", " ").strip()


def norm(s):
    return re.sub(r"[^\w]+", " ", (s or "").casefold()).strip()


# ---------------------------------------------------------------- folder scanning

def find_album_dirs(root, recursive=True):
    out = []
    if recursive:
        for d, _dirs, files in os.walk(root):
            _dirs[:] = sorted(x for x in _dirs if not x.startswith((".", "@")))
            if any(f.lower().endswith(Config.AUDIO_EXTS) and not f.startswith(".") for f in files):
                out.append(d)
    else:
        if any(f.lower().endswith(Config.AUDIO_EXTS) for f in os.listdir(root)):
            out.append(root)
    return out


def read_album(album_dir, skipped=None):
    """Read every audio file's tags. Unreadable files are listed in `skipped` instead of
    failing the whole album."""
    files = []
    for name in sorted(os.listdir(album_dir)):
        p = os.path.join(album_dir, name)
        if name.startswith(".") or not name.lower().endswith(Config.AUDIO_EXTS) or not os.path.isfile(p):
            continue
        try:
            info = T.read(p)
        except Exception as e:  # noqa: BLE001 - corrupt/partial files, wrong extension
            if skipped is not None:
                skipped.append(f"{name} ({type(e).__name__}: {e})")
            continue
        files.append({"path": p, "file": name, "kind": T.kind_of(p), **info})
    return files


FOLDER_JUNK_RE = re.compile(
    r"\s*[\(\[\{][^\)\]\}]*(mp3|songs?|movies?|hits|collection|kbps|vbr|flac|320|128|tamil|www\.|\.com)"
    r"[^\)\]\}]*[\)\]\}]|\s+-\s+\d+\s+(songs|movies).*$|\b(www\.)?\w+\.(com|in|net|org)\b", re.I)


def clean_folder(name):
    """'Ilayaraja (2800 Tamil Mp3 Songs - from 585 Movies)' -> 'Ilayaraja'."""
    s = FOLDER_JUNK_RE.sub("", name or "")
    s = re.sub(r"[_]+", " ", s)
    return re.sub(r"\s+", " ", s).strip(" -_.")


def guess_query(album_dir, files):
    albums = Counter(f["fields"]["album"] for f in files if f["fields"]["album"])
    aas = Counter(f["fields"]["albumartist"] for f in files if f["fields"]["albumartist"])
    folder = clean_folder(os.path.basename(album_dir.rstrip("/")))
    parent = clean_folder(os.path.basename(os.path.dirname(album_dir.rstrip("/"))))
    album = albums.most_common(1)[0][0] if albums else folder
    artist = aas.most_common(1)[0][0] if aas else parent
    if os.path.normpath(os.path.dirname(album_dir)) == os.path.normpath(Config.MUSIC_DIR):
        artist = aas.most_common(1)[0][0] if aas else ""
    return {"album": album, "artist": artist, "folder": folder}


# ---------------------------------------------------------------- scoring

def album_score(cand, query, n_files):
    qa, ca = norm(clean_album(query["album"])), norm(clean_album(cand["album"]))
    qf = norm(clean_album(query["folder"]))
    name = max(fuzz.token_sort_ratio(qa, ca), fuzz.token_sort_ratio(qf, ca)) / 100.0
    if ca and (ca == qa or ca == qf):
        name = 1.0
    art = 0.5
    if query["artist"]:
        art = fuzz.token_set_ratio(norm(query["artist"]), norm(cand["album_artist"])) / 100.0
    tc = cand.get("track_count") or 0
    count = 1.0 if tc == n_files else max(0.0, 1 - abs(tc - n_files) / max(tc, n_files, 1))
    genre = 1.0 if "tamil" in (cand.get("genre") or "").lower() else 0.4
    return round(10 * (0.55 * name + 0.2 * art + 0.15 * count + 0.10 * genre), 2)


def file_title(f):
    """The title to match a file by: the one Shazam recognised (second pass), else its tag, else
    its file name."""
    return f.get("match_title") or f["fields"]["title"] or stem_title(f["path"])


def track_pair_score(f, t):
    cur_title = file_title(f)
    title = max(fuzz.token_sort_ratio(norm(clean_title(cur_title)), norm(clean_title(t["title"]))),
                fuzz.partial_ratio(norm(clean_title(cur_title)), norm(clean_title(t["title"])))) / 100.0
    if f["duration"] and t["duration"]:
        diff = abs(f["duration"] - t["duration"])
        dur = 1.0 if diff <= 2 else 0.8 if diff <= 5 else 0.4 if diff <= 12 else 0.0
    else:
        dur = 0.5
    num = 0.5
    fn = f["fields"]["tracknumber"]
    if fn.isdigit():
        same_disc = str(t["discnumber"]) == (f["fields"]["discnumber"] or "1")
        num = 1.0 if int(fn) == t["tracknumber"] and same_disc else 0.0
    return round(10 * (0.55 * title + 0.3 * dur + 0.15 * num), 2)


def assign_tracks(files, tracks):
    """Greedy best-pair assignment. Returns list of (track or None, score) aligned to files."""
    pairs = []
    for i, f in enumerate(files):
        for j, t in enumerate(tracks):
            pairs.append((track_pair_score(f, t), i, j))
    pairs.sort(reverse=True)
    used_f, used_t = set(), set()
    result = [(None, 0.0)] * len(files)
    for s, i, j in pairs:
        if i in used_f or j in used_t or s < 4.0:
            continue
        used_f.add(i); used_t.add(j)
        result[i] = (tracks[j], s)
    return result


# ---------------------------------------------------------------- tag rules

MULTI_SPLIT = re.compile(r"\s*(?:,|&| and |;| x |/)\s*", re.I)


def resolve_album_artist(itunes_artist):
    a = (itunes_artist or "").strip()
    if not a or a.lower() in ("various artists", "various"):
        return Config.VARIOUS_ARTISTS
    if len([p for p in MULTI_SPLIT.split(a) if p]) > 1:
        return Config.VARIOUS_ARTISTS
    return a


def resolve_artist(track_artist, album_artist):
    mode = Config.ARTIST_MODE
    md = album_artist if album_artist != Config.VARIOUS_ARTISTS else ""
    if mode == "md" and md:
        return md
    if mode == "md_first" and md:
        parts = [p for p in MULTI_SPLIT.split(track_artist or "") if p and norm(p) != norm(md)]
        return "; ".join([md] + parts)
    return track_artist or md


def safe_filename(s):
    s = re.sub(r'[\\/:*?"<>|]+', "-", s or "")
    s = re.sub(r"\s+", " ", s).strip(" .-")
    return s[:180] or "track"


def new_filename(file, fields):
    ext = os.path.splitext(file["file"])[1].lower()
    try:
        n = int(fields.get("tracknumber") or 0)
        d = int(fields.get("discnumber") or 1)
        dt = int(fields.get("disctotal") or 1)
    except ValueError:
        return file["file"]
    if not n or not fields.get("title"):
        return file["file"]
    pat = Config.RENAME_PATTERN_MULTIDISC if dt > 1 else Config.RENAME_PATTERN
    try:
        name = pat.format(track=n, disc=d, title=fields["title"], artist=fields.get("artist", ""),
                          album=fields.get("album", ""), year=fields.get("date", ""))
    except (KeyError, ValueError, IndexError):
        name = f"{n:02d} - {fields['title']}"
    return safe_filename(name) + ext


CLEARABLE = ("compilation", "releasetype")

# What you can pick to update (Library → Update only…): label and the tags each one covers.
# artwork, lyrics and rename aren't tags; they gate those steps of a write.
ONLY_PARTS = {
    "title": ("Song titles", ["title"]),
    "artist": ("Singers", ["artist"]),
    "album": ("Album name", ["album"]),
    "albumartist": ("Album artist", ["albumartist"]),
    "composer": ("Composer", ["composer"]),
    "tracknumber": ("Track and disc numbers", ["tracknumber", "tracktotal", "discnumber", "disctotal"]),
    "date": ("Year and release date", ["date"]),
    "genre": ("Genre", ["genre"]),
    "label": ("Record label", ["label"]),
    "kind": ("Kind of album (release type, compilation)", ["releasetype", "compilation", "grouping"]),
    "artwork": ("Artwork", []),
    "lyrics": ("Lyrics", []),
    "rename": ("File names", []),
}


def clean_only(parts):
    """The known parts, in display order; None when nothing (or everything) was picked."""
    got = [k for k in ONLY_PARTS if k in (parts or ())]
    return got if got and len(got) < len(ONLY_PARTS) else None


def only_fields(only):
    """Tag fields covered by a list of parts; None = no restriction."""
    if not only:
        return None
    return {f for k in only for f in ONLY_PARTS.get(k, ("", []))[1]}


def only_allows(only, part):
    return not only or part in only


def compute_changes(current, new, forced=(), only=None):
    """{field: 'fill'|'fix'} for values that would change. In 'fill' overwrite mode existing
    values are left alone unless the field is in `forced` (edited by hand on the review page).
    only: a set of tag fields; others are left alone (unless forced)."""
    changes = {}
    fill_only = getattr(Config, "OVERWRITE_MODE", "fix") == "fill"
    for k in T.FIELDS:
        if k in Config.PROTECTED_FIELDS and k not in forced:
            continue
        if only is not None and k not in only and k not in forced:
            continue
        nv = str(new.get(k) or "").strip()
        cv = str(current.get(k) or "").strip()
        if k == "date" and k not in forced and nv and cv and cv[:4] == nv[:4]:
            if len(nv) == 4 and len(cv) > 4:
                continue                     # the file already has the full date of that year: keep it
            if len(cv) == 4 and len(nv) == 10:
                changes[k] = "fill"          # a year becomes the full release date: more detail, not a fix
                continue
        if not nv:
            if k in CLEARABLE and k in new and new[k] == "" and cv and not (fill_only and k not in forced):
                changes[k] = "fix"           # a policy says this flag must go (e.g. not a compilation)
            continue
        if nv.isdigit() and cv.isdigit() and int(nv) == int(cv):
            continue
        if nv == cv:
            continue
        if cv and fill_only and k not in forced:
            continue
        changes[k] = "fill" if not cv else "fix"
    return changes


def album_stats(prop):
    """Counts used by the review-queue filters and the scan summary."""
    n_fix = n_fill = 0
    for f in prop.get("files", []):
        for kind in (f.get("changes") or {}).values():
            if kind == "fix":
                n_fix += 1
            else:
                n_fill += 1
        if f.get("rename_to"):
            n_fix += 1
        if (f.get("lyrics") or f.get("synced")) and not f.get("has_lyrics"):
            n_fill += 1
        if prop.get("album", {}).get("artwork") and not f.get("has_art") and only_allows(prop.get("only"), "artwork"):
            n_fill += 1
    a = prop.get("album") or {}
    return {"n_fix": n_fix, "n_fill": n_fill, "n_files": len(prop.get("files", [])),
            "various": 1 if a.get("album_artist") == Config.VARIOUS_ARTISTS else 0}


# ---------------------------------------------------------------- sources

SOURCE_MODS = {"itunes": itunes, "jiosaavn": jiosaavn, "wikipedia": wikipedia, "discogs": discogs,
               "deezer": deezer}
SOURCE_LABEL = {"itunes": "iTunes", "jiosaavn": "JioSaavn", "wikipedia": "Wikipedia", "discogs": "Discogs",
                "deezer": "Deezer", "musixmatch": "Musixmatch", "shazam": "Shazam", "tmdb": "TMDB"}


def parse_ref(text):
    """'source:id' | Apple Music / JioSaavn / Wikipedia / Discogs / Deezer URL | bare iTunes id -> ref or None."""
    text = str(text or "").strip()
    if not text:
        return None
    m = re.match(r"^(itunes|jiosaavn|wikipedia|discogs|deezer):(.+)$", text)
    if m:
        return f"{m.group(1)}:{m.group(2)}"
    z = deezer.parse_ref(text)
    if z:
        return f"deezer:{z}"
    d = discogs.parse_ref(text)
    if d:
        return f"discogs:{d}"
    w = wikipedia.parse_ref(text)
    if w:
        return f"wikipedia:{w}"
    j = jiosaavn.parse_ref(text)
    if j:
        return f"jiosaavn:{j}"
    i = itunes.parse_ref(text)
    if i:
        return f"itunes:{i}"
    return None


def lookup_ref(ref):
    src, _, rid = ref.partition(":")
    mod = SOURCE_MODS.get(src)
    if not mod:
        raise SourceError(f"Unknown source in {ref}")
    return mod.lookup_album(rid)


def _search_candidates(mod, query, n_files):
    seen = {}
    terms = []
    for t in (f"{clean_album(query['album'])} {query['artist']}".strip(),
              clean_album(query["album"]), clean_album(query["folder"])):
        if t and t not in terms:
            terms.append(t)
    for term in terms:
        for c in mod.search_albums(term):
            if c["ref"] not in seen:
                c["score"] = album_score(c, query, n_files)
                seen[c["ref"]] = c
        if seen and max(c["score"] for c in seen.values()) >= 8.5:
            break
    return sorted(seen.values(), key=lambda c: c["score"], reverse=True)


def _evaluate(alb, tracks, files, a_score):
    assigned = assign_tracks(files, tracks)
    matched = [s for t, s in assigned if t]
    tscore = sum(s for _, s in assigned) / len(files)
    return {"album": alb, "tracks": tracks, "assigned": assigned, "a_score": a_score,
            "total": round(0.4 * a_score + 0.6 * tscore, 2),
            "coverage": len(matched) / len(files)}


def _good_enough(best):
    return best and best["total"] >= Config.FALLBACK_BELOW and best["coverage"] >= Config.FALLBACK_COVERAGE


# ---------------------------------------------------------------- per-track fallback

OTHER_LANGS = {"telugu", "malayalam", "kannada", "hindi", "bengali", "marathi", "punjabi"}


def _track_parts(f, t, album_hint):
    """(title_sim 0-1, duration_diff_seconds or None, album_sim 0-1 or None)"""
    cur_title = clean_title(file_title(f))
    a, b = norm(cur_title), norm(clean_title(t["title"]))
    ts = fuzz.token_sort_ratio(a, b) / 100.0
    # partial matches only count when the shorter title is most of the longer one
    if a and b and min(len(a), len(b)) >= 5 and min(len(a), len(b)) / max(len(a), len(b)) >= 0.6:
        ts = max(ts, fuzz.partial_ratio(a, b) / 100.0 * 0.95)
    diff = abs(f["duration"] - t["duration"]) if (f["duration"] and t["duration"]) else None
    # Shortened file titles ("Kanmani" for "Kanmani Anbodu"): every word of the shorter title
    # appears in the longer one AND the length matches to the second -> treat as a strong match.
    wa, wb = a.split(), b.split()
    short, long_ = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    if (short and len(" ".join(short)) >= 5 and set(short) <= set(long_)
            and diff is not None and diff <= 3):
        ts = max(ts, 0.9)
    alb = None
    if album_hint and t.get("album"):
        alb = fuzz.token_set_ratio(norm(clean_album(album_hint)), norm(clean_album(t["album"]))) / 100.0
    return ts, diff, alb


def _track_score(f, t, album_hint):
    ts, diff, alb = _track_parts(f, t, album_hint)
    dur = 0.5 if diff is None else 1.0 if diff <= 3 else 0.7 if diff <= 8 else 0.2 if diff <= 20 else 0.0
    return round(10 * (0.6 * ts + 0.3 * dur + 0.1 * (0.5 if alb is None else alb)), 2)


def _track_acceptable(f, t, album_hint, language=""):
    """Hard gates so a song-by-song search can't grab a different song with a similar name."""
    ts, diff, alb = _track_parts(f, t, album_hint)
    lang = (t.get("genre") or "").lower()
    if language and language.lower() == "tamil" and lang in OTHER_LANGS:
        return False                       # dubbed / other-language version
    if diff is not None and diff > 8:
        return False                       # different recording
    if ts < 0.8:
        return False
    if alb is not None and alb < 0.5:      # from another album: demand a near-exact song
        return ts >= 0.92 and (diff is None or diff <= 4)
    return True


def find_track(f, album_hint="", language="", real_album=True):
    """Search sources song-by-song for a file the album match didn't cover.
    JioSaavn first (better Tamil coverage, carries the music director), then iTunes."""
    title = clean_title(file_title(f))
    if not title:
        return None, 0.0
    order = [s for s in ("jiosaavn", "itunes") if s in Config.SOURCES]
    terms = []
    for t in (f"{title} {clean_album(album_hint)}".strip(), title):
        if t not in terms:
            terms.append(t)
    best, best_s = None, 0.0
    for src in order:
        mod = SOURCE_MODS[src]
        for term in terms:
            try:
                hits = mod.search_tracks(term)
            except SourceError:
                break
            gate_hint = album_hint if real_album else ""   # a folder name isn't an album to compare against
            for h in hits:
                if not _track_acceptable(f, h, gate_hint, language):
                    continue
                s = _track_score(f, h, gate_hint)
                if s > best_s:
                    best, best_s = h, s
            if best_s >= 8.5:
                return best, best_s
        if best_s >= 7.5:
            return best, best_s
    return (best, best_s) if best_s >= 7.5 else (None, best_s)


# ---------------------------------------------------------------- identify by sound (Shazam)

class SoundState:
    """Per-album bookkeeping for Shazam: how many files it was asked about, the limit, errors."""
    def __init__(self):
        self.asked = self.found = self.missed = self.skipped = 0
        self.error = ""
        self.limit = Config.SHAZAM_MAX_PER_ALBUM or 10 ** 9

    def notes(self):
        out = []
        if self.found:
            out.append(f"{self.found} song(s) identified by their sound (Shazam), then looked up as usual")
        if self.missed:
            out.append(f"Shazam didn't recognise {self.missed} song(s)")
        if self.skipped:
            out.append(f"{self.skipped} more song(s) not sent to Shazam (limit of {self.limit} per album, "
                       "Settings → Metadata sources)")
        if self.error:
            out.append(self.error)
        return out


def _same_song(a, b, min_sim=0.85):
    a, b = norm(clean_title(a)), norm(clean_title(b))
    if not a or not b:
        return False
    sim = fuzz.token_sort_ratio(a, b) / 100.0
    if min(len(a), len(b)) >= 5 and min(len(a), len(b)) / max(len(a), len(b)) >= 0.6:
        sim = max(sim, fuzz.partial_ratio(a, b) / 100.0)
    return sim >= min_sim


def _dur_diff(f, t):
    return abs(f["duration"] - t["duration"]) if (f.get("duration") and t.get("duration")) else None


def _sound_catalogue(hit, f, language):
    """The song Shazam named, as a catalogue track: JioSaavn (music director and singers) when it
    has the same recording, else iTunes by the Apple Music id. (track, score) or (None, 0)."""
    best, best_s = None, 0.0
    if "jiosaavn" in Config.SOURCES:
        term = f"{clean_title(hit['title'])} {clean_album(hit.get('album') or '')}".strip()
        try:
            hits = jiosaavn.search_tracks(term)
        except SourceError:
            hits = []
        for h in hits:
            lang = (h.get("genre") or "").lower()
            if language and language.lower() == "tamil" and lang in OTHER_LANGS:
                continue
            diff = _dur_diff(f, h)
            if not _same_song(hit["title"], h["title"]) or (diff is not None and diff > 8):
                continue
            s = 8.0 if diff is not None and diff <= 3 else 7.5
            if hit.get("album") and h.get("album") and fuzz.token_set_ratio(
                    norm(clean_album(hit["album"])), norm(clean_album(h["album"]))) >= 80:
                s += 0.3
            if s > best_s:
                best, best_s = h, s
    if not best and hit.get("apple_id") and "itunes" in Config.SOURCES:
        try:
            t = itunes.lookup_track(hit["apple_id"])
        except SourceError:
            t = None
        diff = _dur_diff(f, t) if t else None
        if t and (diff is None or diff <= 8):
            best, best_s = t, (8.0 if diff is not None and diff <= 3 else 7.5)
    return best, best_s


def by_sound(f, state, album_tracks=None, used=None, language=""):
    """Ask Shazam about a file no title search found. Returns (track, score, kind) where kind is
    'album' (a song of the matched album that wasn't assigned yet), 'track', or None."""
    if not shazam.enabled() or state.error:
        return None, 0.0, None
    if state.asked >= state.limit:
        state.skipped += 1
        return None, 0.0, None
    state.asked += 1
    try:
        hit, _note = shazam.identify(f["path"], f.get("duration"))
    except SourceError as e:
        state.error = f"{e} (no more songs sent to Shazam for this album)"
        return None, 0.0, None
    if not hit:
        state.missed += 1
        return None, 0.0, None
    f["sound"] = hit
    # 1) a song of the matched album that no file took yet
    for t in album_tracks or []:
        if used is not None and id(t) in used:
            continue
        diff = _dur_diff(f, t)
        if _same_song(hit["title"], t["title"], 0.85) and (diff is None or diff <= 5):
            if used is not None:
                used.add(id(t))
            state.found += 1
            return {**t, "via": "shazam"}, (8.5 if diff is not None and diff <= 3 else 8.0), "album"
    # 2) the same song in a catalogue
    t, s = _sound_catalogue(hit, f, language)
    if t:
        state.found += 1
        return {**t, "via": "shazam"}, s, "track"
    # 3) Shazam's own details: title and album only; singers stay as they are in the file
    state.found += 1
    return ({"track_id": hit.get("key"), "title": hit["title"], "artist": "", "keep_artist": True,
             "composer": "", "album": hit.get("album") or "", "album_artist": "", "tracknumber": 0,
             "tracktotal": 0, "discnumber": 1, "disctotal": 1, "duration": 0.0, "genre": "",
             "date": hit.get("year") or "", "image": hit.get("image") or "", "has_lyrics": False,
             "source": "shazam", "via": "shazam"}, 6.5, "track")


# ---------------------------------------------------------------- main entry

# album-level tags the user can edit on the album page -> key in proposal["album"]
OVERRIDE_KEYS = {"album": "album", "albumartist": "album_artist", "date": "date", "genre": "genre", "label": "label"}


def analyze(album_dir, forced_ref=None, fetch_lyrics=True, overrides=None, _sound_album=None, only=None):
    """Build the full proposal for one album folder. `overrides` are album-level values the user
    typed on the album page before (album, albumartist, date, genre); they win over the source so
    a later re-check doesn't propose changing them back.

    `only`: parts of ONLY_PARTS to update (Library → Update only…); everything else stays as the
    files have it. Lyrics aren't looked up unless asked for.

    `_sound_album`: second pass for a folder whose songs Shazam identified as one album: the album
    is searched by that name and files are matched by the titles Shazam heard."""
    unreadable = []
    files = read_album(album_dir, unreadable)
    if not files:
        raise ValueError("No readable audio files in folder" +
                         (": " + "; ".join(unreadable) if unreadable else ""))
    query = guess_query(album_dir, files)
    if _sound_album:
        query = {**query, "album": _sound_album, "artist": ""}
        for f in files:
            hit = shazam.cached(f["path"], f.get("duration"))
            if hit:
                f["match_title"], f["sound"] = hit["title"], hit
    n = len(files)
    candidates, best, notes = [], None, [f"Skipped unreadable file: {u}" for u in unreadable]

    if forced_ref:
        ref = parse_ref(forced_ref)
        if not ref:
            raise SourceError(f"Not a recognised album id or URL: {forced_ref}")
        alb, trk = lookup_ref(ref)
        if not alb or not trk:
            raise SourceError(f"Album {ref} not found")
        best = _evaluate(alb, trk, files, album_score(alb, query, n))
    else:
        for i, src in enumerate(Config.SOURCES):
            mod = SOURCE_MODS.get(src)
            if not mod or (i > 0 and _good_enough(best)):
                continue
            if src == "discogs" and not discogs.enabled_search():
                notes.append("Discogs not searched (set DISCOGS_TOKEN; you can still paste a Discogs link)")
                continue
            try:
                cands = _search_candidates(mod, query, n)
            except SourceError as e:
                notes.append(f"{SOURCE_LABEL.get(src, src)} search failed: {e}")
                continue
            candidates += cands[:8]
            for c in cands[:3]:
                try:
                    alb, trk = mod.lookup_album(c["collection_id"])
                except SourceError as e:
                    notes.append(str(e))
                    continue
                if not alb or not trk:
                    continue
                ev = _evaluate(alb, trk, files, c["score"])
                # prefer the more complete match when totals are close
                if (not best or ev["total"] > best["total"] + 0.3
                        or (ev["total"] >= best["total"] - 0.3 and ev["coverage"] > best["coverage"])):
                    best = ev
        candidates.sort(key=lambda c: c["score"], reverse=True)
        # An album that holds hardly any of this folder's songs is not this folder's album
        # (typical for collections): drop it and match the songs one by one instead.
        if best and (best["coverage"] < Config.MIN_ALBUM_COVERAGE or best["total"] < 4.0):
            notes.append(f"Closest album “{best['album']['album']}” "
                         f"({SOURCE_LABEL.get(best['album']['source'], '')}) holds only "
                         f"{round(best['coverage'] * n)} of {n} songs, so songs were matched one by one")
            best = None

    proposal = {"query": query, "files": [], "album": None, "art": {}, "fallback": False,
                "notes": notes}
    sound = SoundState()
    if _sound_album:
        notes.append(f"Album found from songs identified by their sound: “{_sound_album}”")

    if best and best["album"].get("source") == "deezer":
        _deezer_singers(best, notes)

    if best:
        alb = best["album"]
        album_artist = resolve_album_artist(alb["album_artist"])
        album = {
            "collection_id": alb["ref"], "source": alb["source"], "album": alb["album"],
            "source_artist": alb["album_artist"], "album_artist": album_artist,
            "date": (alb.get("date") or "")[:4], "genre": alb["genre"],
            "artwork": alb["artwork"], "thumb": alb.get("thumb") or alb["artwork"],
            "url": alb.get("url", ""), "track_count": alb["track_count"],
        }
        # Re-issues on streaming services carry the upload year (e.g. 2022 for a 1988 film);
        # use the earliest year any source gives for the same album.
        # same title AND same music director, so a remake of the same name (Vikram 1986 vs 2022) can't leak in
        same = [c for c in candidates if c.get("date")
                and fuzz.token_sort_ratio(norm(clean_album(c["album"])), norm(clean_album(album["album"]))) >= 90
                and fuzz.token_set_ratio(norm(c.get("album_artist")), norm(album["source_artist"])) >= 80]
        years = [y for y in [album["date"]] + [(c["date"] or "")[:4] for c in same] if y and y.isdigit()]
        if years and min(years) != album["date"]:
            src_of = next((c["source"] for c in same if (c["date"] or "")[:4] == min(years)), "")
            notes.append(f"Year {min(years)} from {SOURCE_LABEL.get(src_of, src_of)} "
                         f"(earliest release; {SOURCE_LABEL.get(album['source'], '')} says {album['date']})")
            album["date"] = min(years)
        _release_and_label(album, alb, same, best["tracks"], notes)
        if not album["artwork"]:
            for c in candidates:
                if (c.get("artwork") and c["source"] != album["source"]
                        and fuzz.token_sort_ratio(norm(clean_album(c["album"])),
                                                  norm(clean_album(album["album"]))) >= 90):
                    album["artwork"], album["thumb"] = c["artwork"], c.get("thumb") or c["artwork"]
                    notes.append(f"Artwork from {SOURCE_LABEL.get(c['source'], c['source'])}")
                    break
        matches = []
        used = {id(t) for t, _ in best["assigned"] if t}
        for f, (t, s) in zip(files, best["assigned"]):
            if t:
                matches.append((t, s, "album"))
                continue
            t2, s2 = find_track(f, album["album"], album["genre"]) if Config.TRACK_FALLBACK else (None, 0.0)
            if t2:
                matches.append((t2, s2, "track"))
                continue
            t3, s3, k3 = by_sound(f, sound, best["tracks"], used, album["genre"])
            matches.append((t3, s3, k3) if t3 else (None, 0.0, None))
        scores = [s if kind == "album" else s * 0.8 for _, s, kind in matches]
        confidence = round(0.4 * best["a_score"] + 0.6 * sum(scores) / n, 2)
    else:
        proposal["fallback"] = True
        matches = []
        for f in files:
            t, s = (find_track(f, query["album"], "Tamil", real_album=False) if Config.TRACK_FALLBACK
                    else (None, 0.0))
            if not t:
                t, s, _k = by_sound(f, sound, language="Tamil")
            matches.append((t, s, "track") if t else (None, 0.0, None))
        hits = [t for t, _, _ in matches if t]
        names = Counter(t["album"] for t in hits if t.get("album"))
        album_name = query["album"]
        if names and names.most_common(1)[0][1] >= max(2, 0.6 * len(hits)):
            album_name = names.most_common(1)[0][0]
        comps = Counter(t["composer"] for t in hits if t.get("composer"))
        if comps and comps.most_common(1)[0][1] >= 0.6 * len(hits):
            album_artist = resolve_album_artist(comps.most_common(1)[0][0])
        elif len(comps) > 1:
            album_artist = Config.VARIOUS_ARTISTS
        else:
            album_artist = query["artist"] or ""
        years = Counter(t["date"][:4] for t in hits if t.get("date"))
        genres = Counter(t["genre"] for t in hits if t.get("genre"))
        labels = Counter(t["label"] for t in hits if t.get("label"))
        art = next((t["image"] for t in hits if t.get("image") and t.get("album") == album_name), "")
        album = {"collection_id": None, "source": "", "album": album_name, "source_artist": "",
                 "album_artist": album_artist,
                 "date": years.most_common(1)[0][0] if years else "",
                 "genre": genres.most_common(1)[0][0] if genres else "",
                 "artwork": art, "thumb": art, "url": "", "track_count": n, "fallback": True}
        if labels and labels.most_common(1)[0][1] >= max(2, 0.6 * len(hits)):
            album["label"] = labels.most_common(1)[0][0]
        dates = Counter(t["release_date"] for t in hits if (t.get("release_date") or "")[:4] == album["date"]
                        and t.get("album") == album_name)
        if album["date"] and dates and dates.most_common(1)[0][1] >= max(2, 0.6 * len(hits)):
            album["release_date"] = dates.most_common(1)[0][0]
        films = {norm(clean_album(t["album"])) for t in hits if t.get("album")}
        if (Config.COLLECTION_MODE and len(hits) >= 3
                and len(films) >= max(Config.COLLECTION_MIN_FILMS, 0.5 * len(hits))):
            # A collection ("Ilaiyaraaja Hits", a downloaded bundle): songs from many films.
            # Keep it together as one compilation album named after the folder; each song keeps
            # its own year, composer and singers, and its film goes into the Grouping tag.
            top = comps.most_common(1)[0] if comps else None
            album.update({
                "album": clean_folder(query["album"]) or query["folder"],
                "album_artist": resolve_album_artist(top[0]) if top and top[1] >= 0.8 * len(hits)
                else Config.VARIOUS_ARTISTS,
                "date": "", "release_date": "", "label": "", "artwork": "", "thumb": "", "collection": True,
                "films": len(films),
            })
            proposal["collection"] = True
        if proposal.get("collection"):
            # every song was identified on its own: confidence = how well the songs matched
            confidence = round(sum(s for _, s, _ in matches) / n, 2)
        else:
            confidence = round(0.6 * sum(s * 0.8 for _, s, _ in matches) / n, 2)
            # Junk file and folder names: Shazam heard that most songs belong to one album, which
            # no title search found. Search that album by name and match the files again.
            if (sound.found and not forced_ref and not _sound_album and album_name != query["album"]
                    and norm(clean_album(album_name)) != norm(clean_album(query["folder"]))):
                return analyze(album_dir, None, fetch_lyrics, overrides, _sound_album=album_name, only=only)

    for k, v in (overrides or {}).items():
        if k in OVERRIDE_KEYS and v not in (None, ""):
            if k == "date":
                v = str(v).strip()
                album["release_date"] = full_date(v)     # a year you typed means just the year
                v = v[:4]
            album[OVERRIDE_KEYS[k]] = v
            if k == "albumartist":
                album["album_artist_edited"] = True
    if overrides:
        proposal["overrides"] = {k: v for k, v in overrides.items() if k in OVERRIDE_KEYS}
    _apply_policy(album_dir, album, proposal)
    _md_check(album, proposal, notes)
    _review(album, proposal)
    notes += sound.notes()
    proposal["album"] = album
    only = clean_only(only)
    if only:
        proposal["only"] = only
        notes.append("Updating only: " + ", ".join(ONLY_PARTS[k][0] for k in only))
        fetch_lyrics = fetch_lyrics and "lyrics" in only
    for f, (t, s, kind) in zip(files, matches):
        proposal["files"].append(_file_proposal(f, t, s, kind, album, fetch_lyrics, only))
    proposal["confidence"] = confidence
    proposal["art"] = {"current": sum(1 for f in files if f["has_art"]), "total": n}
    proposal["matched"] = sum(1 for _, _, k in matches if k)
    return proposal, candidates


def _release_and_label(album, alb, same, tracks, notes):
    """The full release date and the record label: from the matched album, else from the same
    album on another source. A date counts only when its year is the album's year (streaming
    re-issues carry their upload date)."""
    pool = [alb] + [c for c in same if c is not alb]
    year = album.get("date") or ""
    dates = [c.get("release_date") for c in pool if (c.get("release_date") or "")[:4] == year and year]
    if not dates:   # songs of the album may carry it (JioSaavn)
        d = Counter(t.get("release_date") for t in tracks if (t.get("release_date") or "")[:4] == year and year)
        dates = [d.most_common(1)[0][0]] if d else []
    album["release_date"] = dates[0] if dates else ""
    labels = [c.get("label") for c in pool if c.get("label")]
    if not labels:
        lc = Counter(t.get("label") for t in tracks if t.get("label"))
        labels = [lc.most_common(1)[0][0]] if lc and lc.most_common(1)[0][1] >= 0.6 * len(tracks) else []
    album["label"] = labels[0] if labels else ""
    if album["release_date"] and alb.get("release_date") != album["release_date"]:
        src = next((c.get("source") for c in pool if c.get("release_date") == album["release_date"]), "")
        if src:
            notes.append(f"Release date {album['release_date']} from {SOURCE_LABEL.get(src, src)}")


def _md_check(album, proposal, notes):
    """Compare the music director with TMDB's credits for the film (Settings → Metadata sources).
    Only for a matched film album whose Album Artist came from the sources, not one you typed or
    decided, and not for Various Artists, collections or singles."""
    if not tmdb.enabled():
        return
    aa = album.get("album_artist") or ""
    if (not aa or aa == Config.VARIOUS_ARTISTS or album.get("album_artist_edited") or album.get("keep")
            or proposal.get("fallback") or proposal.get("collection") or not album.get("album")
            or album.get("policy") in ("various", "single", "collection", "artist")):
        return
    try:
        proposal["md_check"] = tmdb.check(album["album"], album.get("date") or "", aa, album.get("genre") or "")
    except SourceError as e:
        notes.append(f"TMDB check failed: {e}")


def _review(album, proposal):
    """The album's review for Plex (and album.nfo): Wikipedia's soundtrack paragraph, shown on the
    album page before it's written. False when there's none, so it isn't looked up again."""
    from . import plex, plexmeta
    if not Config.PLEX_REVIEW or not (plex.enabled() or Config.WRITE_NFO):
        return
    if proposal.get("collection") or (proposal.get("fallback") and not album.get("policy")):
        return
    proposal["review"] = plexmeta.find_review(album) or False


def _deezer_singers(best, notes):
    """Deezer can't tell singers from the music director: fetch each matched song's artists and
    keep everyone but the album artist."""
    matched = [t for t, _ in best["assigned"] if t]
    try:
        deezer.add_singers(matched, best["album"]["album_artist"])
    except SourceError as e:
        notes.append(f"Deezer song credits failed: {e}")
    blank = sum(1 for t in matched if not t.get("artist"))
    if blank:
        notes.append(f"Deezer names no singers for {blank} of {len(matched)} songs; "
                     "their Artist tag is left as it is")


def _composer_for(t, album):
    if not Config.WRITE_COMPOSER:
        return ""
    if album.get("album_artist_edited"):      # the music director you typed is the composer
        return album["album_artist"] if album["album_artist"] != Config.VARIOUS_ARTISTS else ""
    if t.get("composer"):
        c = resolve_album_artist(t["composer"])
        return t["composer"] if c == Config.VARIOUS_ARTISTS else c
    return album["album_artist"] if album["album_artist"] != Config.VARIOUS_ARTISTS else ""


ALBUM_WIDE = ("album", "albumartist", "date", "genre", "compilation", "composer", "releasetype", "label")
POLICY_KEYS = ("album", "albumartist", "compilation", "releasetype")


def _apply_policy(album_dir, album, proposal):
    """Your folder policy wins over whatever the sources say (album artist, album name, kind)."""
    from . import policy as P
    pol = P.get(album_dir)
    if not pol:
        if Config.WRITE_RELEASETYPE:
            if proposal.get("collection"):
                album["releasetype"] = "album;compilation"
            elif not proposal.get("fallback") and P.SOUNDTRACK_RE.search(album.get("album") or ""):
                album["releasetype"] = "album;soundtrack"
        return
    proposal["policy"] = {k: pol.get(k) for k in ("policy", "albumartist", "album")}
    if pol["policy"] == "keep":
        album["keep"] = True
        return
    tags = P.album_tags(pol, album, album_dir)
    if tags.get("albumartist"):
        album["album_artist"] = tags["albumartist"]
        album["album_artist_edited"] = True
    if tags.get("album"):
        album["album"] = tags["album"]
    if pol["policy"] == "collection":
        album["collection"] = True
        proposal["collection"] = True
    album["compilation"] = tags.get("compilation", "")
    if "releasetype" in tags:
        album["releasetype"] = tags["releasetype"]
    album["policy"] = pol["policy"]


def _album_wide(album):
    """Tags that belong to the whole folder, for files no source could match: without them Plex
    files those songs under their singers and splits the album. Only when the album itself is
    known (a matched album or a collection), not for a best guess."""
    if not Config.ALBUM_TAGS_UNMATCHED or (album.get("fallback") and not album.get("collection")
                                           and not album.get("policy")):
        return {}
    new = {"album": album.get("album"), "albumartist": album.get("album_artist"), "genre": album.get("genre")}
    if album.get("collection"):
        new["compilation"] = "1"
    else:
        new["date"] = album.get("release_date") or album.get("date")
        new["label"] = album.get("label")
        if (Config.WRITE_COMPOSER and album.get("album_artist") and album["album_artist"] != Config.VARIOUS_ARTISTS
                and album.get("policy") not in ("various", "single", "collection")):
            new["composer"] = album["album_artist"]
    return _album_extras({k: v for k, v in new.items() if v}, album)


def _new_fields(t, kind, album):
    if not t:
        return _album_wide(album)
    new = {
        "title": t["title"],
        # a Deezer song whose only artist is the music director: keep the file's singers
        "artist": ("" if t.get("keep_artist") or (t.get("source") == "deezer" and not t.get("artist"))
                   else resolve_artist(t["artist"], album["album_artist"])),
        "album": album["album"],
        "albumartist": album["album_artist"],
        "date": album.get("release_date") or album["date"] or (t.get("date") or "")[:4],
        "genre": album["genre"] or t.get("genre", ""),
        "composer": _composer_for(t, album),
        "label": album.get("label") or "",
    }
    if kind == "album":
        new.update({"tracknumber": str(t["tracknumber"]), "tracktotal": str(t["tracktotal"]),
                    "discnumber": str(t["discnumber"]), "disctotal": str(t["disctotal"])})
    if album.get("collection"):
        new["grouping"] = t.get("album") or ""     # the song's own film
        new["compilation"] = "1"
    out = {k: v for k, v in new.items() if v}
    return _album_extras(out, album)


def _album_extras(new, album):
    if album.get("releasetype"):
        new["releasetype"] = album["releasetype"]
    if "compilation" in album and album.get("policy"):
        new["compilation"] = album["compilation"]
    if album.get("keep"):
        new = {k: v for k, v in new.items() if k not in POLICY_KEYS}
    return new


def _title_ok(a, b):
    a, b = norm(clean_title(a)), norm(clean_title(b))
    if not a or not b:
        return False
    return max(fuzz.token_sort_ratio(a, b), fuzz.partial_ratio(a, b) if min(len(a), len(b)) >= 4 else 0) >= 75


def _lyrics(f, t, album, fetch_lyrics):
    """Returns (plain, synced, source, note, mxm_info)."""
    if not fetch_lyrics:
        return "", "", "", "", None
    title = (t or {}).get("title") or f["fields"]["title"] or stem_title(f["path"])
    artist = (t or {}).get("artist") or f["fields"]["artist"]
    note, mxm_info = "", None
    if musixmatch.enabled():
        try:
            mxm = musixmatch.lookup(clean_title(title), artist, album["album"] or f["fields"]["album"],
                                    f["duration"])
        except SourceError as e:
            mxm, note = None, str(e)
            mxm_info = {"error": str(e)}
        if mxm:
            dur_ok = not (mxm["duration"] and f["duration"]) or abs(mxm["duration"] - f["duration"]) <= 8
            ok = _title_ok(title, mxm["title"]) and dur_ok
            mxm_info = {k: mxm.get(k) for k in ("title", "artist", "album", "instrumental")}
            mxm_info["ok"] = ok
            if ok and (mxm.get("lyrics") or mxm.get("synced")):
                synced = mxm.get("synced") or ""
                return (mxm.get("lyrics") or musixmatch.lrc_to_plain(synced), synced,
                        "musixmatch", "", mxm_info)
            if ok and mxm.get("instrumental"):
                return "", "", "", "instrumental", mxm_info
            note = (f"Musixmatch matched “{mxm['title']}”, rejected (title/length differ)"
                    if not ok else "Musixmatch has no lyrics for this song")
        elif not note:
            note = "Not found on Musixmatch"
    if Config.JIOSAAVN_LYRICS and "jiosaavn" in Config.SOURCES:
        try:
            src = t if (t and t.get("source") == "jiosaavn") else None
            if not src:
                hits = jiosaavn.search_tracks(f"{clean_title(title)} {clean_album(album['album'])}".strip())
                scored = sorted(((_track_score(f, h, album["album"]), h) for h in hits),
                                key=lambda x: x[0], reverse=True)
                src = scored[0][1] if scored and scored[0][0] >= 7.0 else None
            if src and src.get("has_lyrics"):
                text = jiosaavn.lyrics(src["track_id"])
                if text:
                    return text, "", "jiosaavn", "", mxm_info
        except SourceError as e:
            note = (note + "; " if note else "") + f"JioSaavn: {e}"
    return "", "", "", note or "No lyrics found", mxm_info


def _file_proposal(f, t, score, kind, album, fetch_lyrics, only=None):
    cur = f["fields"]
    new = _new_fields(t, kind, album)
    lyrics_plain, synced, lyr_src, lyr_note, mxm = _lyrics(f, t, album, fetch_lyrics)
    if (t and mxm and mxm.get("ok") and Config.TRACK_FIELDS_SOURCE == "musixmatch"):
        new["title"] = mxm["title"] or new.get("title", "")
        new["artist"] = resolve_artist(mxm["artist"] or t["artist"], album["album_artist"])
    of = only_fields(only)
    changes = compute_changes(cur, new, only=of)
    if of is not None:
        new = {k: v for k, v in new.items() if k in of}       # the rest stays as the file has it
    if getattr(Config, "OVERWRITE_MODE", "fix") == "fill":
        # show what will really be written: existing values stay
        new = {k: v for k, v in new.items() if k in changes or not str(cur.get(k) or "").strip()}
    merged = {**cur, **{k: new[k] for k in changes}}
    target = new_filename(f, merged) if Config.RENAME_ENABLED and only_allows(only, "rename") else f["file"]
    return {
        "path": f["path"], "file": f["file"], "kind": f["kind"], "duration": f["duration"],
        "current": cur, "new": new, "changes": changes, "score": round(score, 2),
        "match": kind,                                 # album | track | None
        "source_track": ({"title": t["title"], "artist": t["artist"], "album": t.get("album", ""),
                          "duration": t["duration"], "number": t.get("tracknumber") or "",
                          "source": t.get("source", ""), "composer": t.get("composer", ""),
                          "via": t.get("via") or ("shazam" if f.get("match_title") else "")}
                         if t else None),
        "sound": ({k: f["sound"].get(k) for k in ("title", "artist", "album", "year", "url")}
                  if f.get("sound") else None),
        "mxm": mxm, "lyrics": lyrics_plain, "synced": synced,
        "lyrics_source": lyr_src, "lyrics_note": lyr_note,
        "has_lyrics": f["has_lyrics"], "has_art": f["has_art"],
        "rename_to": target if target != f["file"] else "",
    }
