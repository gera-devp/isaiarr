"""Discogs as an album source: physical releases (LP / EP / cassette / CD), with per-track
credits by role. Strong for older Tamil film albums and official label compilations.

- Searching needs a personal access token (DISCOGS_TOKEN, free from discogs.com → Settings →
  Developers). Without it, Discogs can still be used by pasting a release/master URL on the
  album page.
- Cover images are only returned to authenticated requests.
- Release-level credits often carry a `tracks` spec ("A1 to A3, B2") saying which songs they
  apply to; those are expanded so each song gets its own singers / composer.
"""
import re

from ..config import Config
from .http import RateLimiter, SourceError, clean_label, full_date, get_json

API = "https://api.discogs.com"
_limiter = RateLimiter(Config.discogs_rpm)

# "Music By", "Composed By", "Composer", "Music Director", "Music", "Music [Director]", "Score"
MUSIC_ROLE = re.compile(r"(music by|composed by|composer|music director|^music\b|^score\b)", re.I)
VOCAL_ROLE = re.compile(r"\bvocal|\bsinger|playback", re.I)
NOT_LEAD = re.compile(r"backing|chorus|choir", re.I)


def _headers():
    from .. import __version__
    h = {"User-Agent": f"Isaiarr/{__version__} +self-hosted music tagger"}
    if Config.DISCOGS_TOKEN:
        h["Authorization"] = f"Discogs token={Config.DISCOGS_TOKEN}"
    return h


def enabled_search():
    return bool(Config.DISCOGS_TOKEN)


def _get(path, params=None):
    return get_json(f"{API}{path}", params or {}, _limiter, "discogs", headers=_headers(),
                    max_age=30 * 86400)


def _name(a):
    """Display name: the credited variation (anv) if set, without Discogs' '(2)' suffixes."""
    n = (a.get("anv") or a.get("name") or "").strip()
    return re.sub(r"\s*\(\d+\)$", "", n).replace("*", "").strip()


def _join_artists(arts):
    """Discogs gives each artist a `join` ('&', ',', 'Feat.'); rebuild the credit line."""
    arts = arts or []
    out = ""
    for i, a in enumerate(arts):
        out += _name(a)
        if i < len(arts) - 1:
            j = (a.get("join") or ",").strip() or ","
            out += ", " if j == "," else f" {j} "
    return out.strip()


def _seconds(d):
    m = re.match(r"^\s*(?:(\d+):)?(\d{1,2}):(\d{2})\s*$", d or "")
    if not m:
        return 0.0
    h, mi, s = m.groups()
    return float(int(h or 0) * 3600 + int(mi) * 60 + int(s))


def _flat_tracks(tracklist):
    """Tracks in order; headings dropped, index tracks expanded into their sub-tracks."""
    out = []
    for t in tracklist or []:
        kind = t.get("type_", "track")
        if kind == "heading":
            continue
        if kind == "index" and t.get("sub_tracks"):
            for st in t["sub_tracks"]:
                if st.get("type_", "track") == "track":
                    out.append(st)
            continue
        if kind == "track":
            out.append(t)
    return out


def _expand_spec(spec, positions):
    """'A1 to A3, B2' -> {'A1','A2','A3','B2'} using the release's own position order."""
    spec = (spec or "").strip()
    if not spec:
        return None                                  # applies to every track
    out = set()
    for part in re.split(r"\s*[,&]\s*", spec):
        m = re.match(r"^(.+?)\s+to\s+(.+)$", part, re.I) or re.match(r"^([A-Z]?\d+)\s*-\s*([A-Z]?\d+)$", part)
        if m and m.group(1) in positions and m.group(2) in positions:
            i, j = positions.index(m.group(1)), positions.index(m.group(2))
            out.update(positions[min(i, j):max(i, j) + 1])
        elif part:
            out.add(part)
    return out


def _numbers(positions):
    """Track and disc numbers from Discogs positions (A1/B2 sides run on; '2-3' = disc 2 track 3)."""
    discs = [re.match(r"^(?:CD)?(\d+)[-.](\d+)$", p or "", re.I) for p in positions]
    if positions and all(discs):
        nums = [(int(m.group(1)), int(m.group(2))) for m in discs]
        return nums, max(d for d, _ in nums)
    return [(1, i + 1) for i in range(len(positions))], 1


def parse_release(r, kind="release"):
    """Discogs release/master JSON -> (album_summary, tracks)."""
    if not isinstance(r, dict) or not r.get("tracklist"):
        return None, []
    rid = r.get("id")
    tracks_raw = _flat_tracks(r["tracklist"])
    positions = [t.get("position", "") for t in tracks_raw]
    main_artist = _join_artists(r.get("artists"))
    various = main_artist.lower() in ("various", "various artists", "")

    # release-level credits, keyed by which tracks they cover
    rel_music, rel_vocals = [], []          # [(name, set_of_positions or None)]
    for ea in r.get("extraartists") or []:
        role = ea.get("role") or ""
        spec = _expand_spec(ea.get("tracks"), positions)
        if MUSIC_ROLE.search(role):
            rel_music.append((_name(ea), spec))
        elif VOCAL_ROLE.search(role) and not NOT_LEAD.search(role):
            rel_vocals.append((_name(ea), spec))

    whole_album_md = [n for n, spec in rel_music if spec is None]
    md = whole_album_md[0] if whole_album_md else ("" if various else main_artist)

    nums, disc_total = _numbers(positions)
    year = str(r.get("year") or "")[:4]
    if year in ("0", ""):
        year = (r.get("released") or "")[:4]
    text = " ".join([r.get("title") or "", r.get("notes") or "", " ".join(r.get("styles") or []),
                     " ".join(r.get("genres") or [])]).lower()
    genre = "Tamil" if "tamil" in text else ""

    tracks = []
    for t, pos, (disc, num) in zip(tracks_raw, positions, nums):
        t_music = [_name(e) for e in t.get("extraartists") or [] if MUSIC_ROLE.search(e.get("role") or "")]
        t_vocal = [_name(e) for e in t.get("extraartists") or []
                   if VOCAL_ROLE.search(e.get("role") or "") and not NOT_LEAD.search(e.get("role") or "")]
        t_music += [n for n, spec in rel_music if spec is not None and pos in spec]
        t_vocal += [n for n, spec in rel_vocals if spec is None or pos in spec]
        if not t_vocal and t.get("artists"):
            names = _join_artists(t["artists"])
            if names and names != (t_music[0] if t_music else md):
                t_vocal = [names]
        composer = ", ".join(dict.fromkeys(t_music)) or md
        tracks.append({
            "track_id": f"{rid}#{pos}", "title": (t.get("title") or "").strip(),
            "artist": ", ".join(dict.fromkeys(t_vocal)), "composer": composer,
            "album": "", "album_artist": md, "tracknumber": num, "tracktotal": sum(1 for d, _ in nums if d == disc),
            "discnumber": disc, "disctotal": disc_total, "duration": _seconds(t.get("duration")),
            "genre": genre, "date": year, "image": "", "has_lyrics": False, "source": "discogs",
        })

    if not md:
        comps = [t["composer"] for t in tracks if t["composer"]]
        if comps:
            top = max(set(comps), key=comps.count)
            md = top if comps.count(top) >= 0.6 * len(comps) else "Various Artists"
        else:
            md = "Various Artists" if various else ""
        for t in tracks:
            t["album_artist"] = md
            t["composer"] = t["composer"] or (md if md != "Various Artists" else "")

    title = (r.get("title") or "").strip()
    for t in tracks:
        t["album"] = title
    imgs = r.get("images") or []
    primary = next((i for i in imgs if i.get("type") == "primary"), imgs[0] if imgs else {})
    ref_id = f"master-{rid}" if kind == "master" else str(rid)
    summary = {
        "collection_id": ref_id, "ref": f"discogs:{ref_id}", "album": title, "album_artist": md,
        "track_count": len(tracks), "date": year, "genre": genre,
        "artwork": primary.get("uri") or "", "thumb": primary.get("uri150") or primary.get("uri") or "",
        "url": r.get("uri") or f"https://www.discogs.com/{kind}/{rid}",
        "source": "discogs",
        "format": ", ".join(f.get("name", "") for f in r.get("formats") or []),
        "label": next((clean_label(lb.get("name")) for lb in r.get("labels") or [] if clean_label(lb.get("name"))), ""),
        "release_date": full_date(r.get("released")),
        "country": r.get("country") or "",
    }
    return summary, tracks


def lookup_album(ref_id):
    ref_id = str(ref_id)
    if ref_id.startswith("master-"):
        return parse_release(_get(f"/masters/{ref_id[7:]}"), "master")
    return parse_release(_get(f"/releases/{ref_id}"))


def search_albums(term, limit=4):
    """Needs DISCOGS_TOKEN (Discogs only allows authenticated search)."""
    if not enabled_search():
        return []
    term = re.sub(r"\s+", " ", term or "").strip()
    if not term:
        return []
    data = _get("/database/search", {"q": term, "type": "release", "per_page": 15})
    results = [r for r in data.get("results") or [] if r.get("id")]
    # Indian pressings first: they carry the original credits and Tamil track titles
    results.sort(key=lambda r: 0 if (r.get("country") or "").lower() == "india" else 1)
    out, seen_master = [], set()
    for r in results:
        m = r.get("master_id")
        if m and m in seen_master:
            continue                                 # one pressing per master is enough
        if m:
            seen_master.add(m)
        try:
            alb, tracks = lookup_album(r["id"])
        except SourceError:
            continue
        if alb and tracks:
            out.append(alb)
        if len(out) >= limit:
            break
    return out


def search_tracks(term, limit=5):
    return []                                        # Discogs is release-level only


def parse_ref(text):
    m = re.search(r"discogs\.com/(?:[a-z]{2}/)?(release|master)/(\d+)", text or "", re.I)
    if not m:
        return None
    return m.group(2) if m.group(1).lower() == "release" else f"master-{m.group(2)}"
