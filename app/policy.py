"""Folder policies: one decision per album folder, written to every file in it.

A policy says what kind of album a folder is, which sets its Album Artist, album name and
release type (the tag Plex uses for Albums / Singles & EPs / Compilations / Soundtracks):

  film        a film soundtrack by one music director     -> Album Artist = the music director
  various     one album, several music directors          -> Various Artists
  collection  songs from many films ("Hits of …")        -> Various Artists (or the shared composer),
                                                             album named after the folder
  single      a one-track release                          -> Various Artists
  artist      a non-film album by an artist                -> Album Artist = that artist
  keep        leave this folder alone

Rules only *suggest* a policy; nothing is written until you accept it. Once accepted, the policy
is stored per folder and every later re-check respects it.
"""
import os
import re
import time
from collections import Counter

from . import db
from .config import Config

POLICIES = [
    ("film", "Film soundtrack", "Album Artist = the music director"),
    ("various", "Various Artists", "Several music directors on one album"),
    ("collection", "Collection", "Songs from many films, named after the folder"),
    ("single", "Single", "A one-track release, under Various Artists"),
    ("artist", "Artist album", "A non-film album by one artist"),
    ("keep", "Keep as is", "Isaiarr leaves this folder's album tags alone"),
]
LABEL = {k: l for k, l, _ in POLICIES}

SOUNDTRACK_RE = re.compile(r"(original motion picture|soundtrack|\bost\b|\(from |film)", re.I)
SPLIT_RE = re.compile(r"\s*(?:;|,|&|/|\band\b)\s*", re.I)
DISC_RE = re.compile(r"^\s*[\[(]?\s*(cd|disc|disk)\s*[-_ ]?\d{1,2}\b", re.I)


def _people(s):
    return {p.strip().lower().replace(".", "").replace(" ", "") for p in SPLIT_RE.split(s or "") if p.strip()}


def clean_folder_name(folder):
    name = os.path.basename(folder.rstrip("/"))
    name = re.sub(r"\s*[\[(](www\.|[^)\]]*(\.com|\.in|kbps|flac|mp3|rip)\b)[^)\]]*[)\]]", "", name, flags=re.I)
    name = re.sub(r"\s*-\s*(www\.\S+|\S+\.com)\s*$", "", name, flags=re.I)
    return re.sub(r"\s+", " ", name).strip(" -_") or os.path.basename(folder)


# ---------------------------------------------------------------- stored decisions

def get(folder):
    r = db.q("SELECT * FROM folder_policy WHERE folder=?", (os.path.normpath(folder),), one=True)
    return dict(r) if r else None


def all_policies():
    return {r["folder"]: dict(r) for r in db.q("SELECT * FROM folder_policy")}


def set_policy(folder, policy, albumartist=None, album=None, reason=None):
    if policy not in LABEL:
        raise ValueError(f"Unknown policy {policy}")
    folder, now = os.path.normpath(folder), time.time()
    db.x("INSERT INTO folder_policy(folder, policy, albumartist, album, reason, created, updated) VALUES(?,?,?,?,?,?,?) "
         "ON CONFLICT(folder) DO UPDATE SET policy=excluded.policy, albumartist=excluded.albumartist, "
         "album=excluded.album, reason=excluded.reason, updated=excluded.updated",
         (folder, policy, (albumartist or "").strip() or None, (album or "").strip() or None, reason, now, now))


def clear(folder):
    db.x("DELETE FROM folder_policy WHERE folder=?", (os.path.normpath(folder),))


def dismissed():
    return set((db.state_get("policy_dismissed") or {}).get("folders", []))


def dismiss(folders):
    d = dismissed() | {os.path.normpath(f) for f in folders}
    db.state_set("policy_dismissed", {"folders": sorted(d)})


# ---------------------------------------------------------------- rules

def _facts(folder, songs, album=None):
    """What the rules look at, from the song index (and the album's last match when there is one)."""
    prop = (album or {}).get("proposal") or {}
    al = prop.get("album") or {}
    composers, display = Counter(), {}
    for s in songs:
        for raw in SPLIT_RE.split(s.get("composer") or ""):
            p = raw.strip().lower().replace(".", "").replace(" ", "")
            if p:
                composers[p] += 1
                display.setdefault(p, raw.strip())
    src_comps = Counter()
    for f in prop.get("files", []):
        for raw in SPLIT_RE.split(((f.get("source_track") or {}).get("composer") or "")):
            p = raw.strip().lower().replace(".", "").replace(" ", "")
            if p:
                src_comps[p] += 1
                display.setdefault(p, raw.strip())
    comps = src_comps or composers
    films = {(s.get("grouping") or s.get("album") or "").strip().lower() for s in songs} - {""}
    return {
        "n": len(songs), "name": os.path.basename(folder.rstrip("/")),
        "composers": comps, "films": films, "display": display,
        "current_aa": Counter((s.get("albumartist") or "").strip() for s in songs),
        "md": al.get("album_artist") or "", "album": al.get("album") or "",
        "collection": bool(prop.get("collection")), "fallback": bool(prop.get("fallback")),
        "source_tracks": al.get("track_count") or 0,
        "single_title": bool(re.search(r"\b(single)\b", al.get("album") or "", re.I)),
        "tmdb": (prop.get("md_check") or {}).get("status") or "",
    }


_words = {"src": None, "re": None}


def collection_re():
    """Folder-name words that mark a collection (Settings → Kinds of album), as one regex."""
    src = Config.POLICY_COLLECTION_WORDS or ""
    if _words["src"] != src:
        words = [w.strip() for w in src.split(",") if w.strip()]
        pat = "|".join(re.escape(w).replace("\\ ", r"\s+") for w in sorted(words, key=len, reverse=True))
        _words.update(src=src, re=re.compile(rf"\b({pat})\b|\b\d{{3,}}\s*(tamil|hindi|telugu|malayalam)?\s*(mp3\s*)?songs\b", re.I)
                      if pat else None)
    return _words["re"]


def suggest(folder, songs, album=None):
    """(policy, albumartist, album, [reasons]) or None when the rules have nothing to say."""
    f = _facts(folder, songs, album)
    va = Config.VARIOUS_ARTISTS
    if f["n"] == 0:
        return None
    if DISC_RE.match(f["name"]):
        return None                      # one disc of a bigger album: decided at the album folder
    cre = collection_re()
    named = bool(cre and cre.search(f["name"]))
    if Config.POLICY_SUGGEST_COLLECTION and (f["collection"] or named or
                                             (f["n"] >= 4 and len(f["films"]) >= max(3, 0.5 * f["n"]))):
        why = []
        if named:
            why.append(f"the folder is named “{f['name']}”")
        if len(f["films"]) >= 3:
            why.append(f"its songs come from {len(f['films'])} different films")
        if f["collection"]:
            why.append("its songs were matched one by one from different films")
        top = f["composers"].most_common(1)
        aa = va
        if top and f["n"] and top[0][1] >= 0.8 * f["n"] and len(f["composers"]) <= 2:
            aa = f["display"].get(top[0][0]) or va     # one composer for (almost) everything: keep him
            why.append(f"{aa} composed almost every song")
        return "collection", aa, clean_folder_name(folder), why or ["it holds songs from many albums"]
    if Config.POLICY_SUGGEST_SINGLE and (f["n"] == 1 or f["single_title"] or f["source_tracks"] == 1):
        return "single", va, None, ["it's a one-track release" if f["n"] == 1 else "it was released as a single"]
    if Config.POLICY_SUGGEST_MULTI and len(f["composers"]) >= 2:
        names = ", ".join(f["display"].get(n, n) for n, _ in f["composers"].most_common(3))
        return "various", va, None, [f"its songs have {len(f['composers'])} different composers ({names})"]
    if len(f["composers"]) == 1 or f["md"]:
        why = ["one music director for every song"]
        if f["tmdb"] == "agree":
            why.append("TMDB confirms the music director")
        return "film", None, None, why
    return None


def needs_attention(folder, songs, album=None):
    """A suggestion worth showing: the rule differs from what the files say now."""
    s = suggest(folder, songs, album)
    if not s:
        return None
    pol, aa, alb, why = s
    cur = {k for k in (x.get("albumartist") or "" for x in songs)}
    rt = {x.get("releasetype") or "" for x in songs}
    if pol in ("film", "artist"):
        want_aa = (album or {}).get("album_artist") or ((album or {}).get("proposal") or {}).get("album", {}).get("album_artist")
        if not want_aa or cur == {want_aa}:
            return None
        aa = want_aa
    elif aa and cur == {aa} and rt == {release_type(pol, alb or "")} and (pol != "collection" or
                                                                          {x.get("album") for x in songs} == {alb}):
        return None
    return {"policy": pol, "albumartist": aa, "album": alb, "why": why,
            "current": sorted(x for x in cur if x) or ["(no Album Artist)"]}


# ---------------------------------------------------------------- what a policy writes

def release_type(policy, album_name):
    if policy == "film":
        return "album;soundtrack"
    if policy == "various":
        return "album;soundtrack" if SOUNDTRACK_RE.search(album_name or "") else "album;compilation"
    if policy == "collection":
        return "album;compilation"
    if policy == "single":
        return "single"
    if policy == "artist":
        return "album"
    return ""


def album_tags(pol, matched_album=None, folder=""):
    """Album-wide tags a stored policy writes to every file of the folder. `matched_album` is the
    album from the last match (for the music director / album name of film and artist albums)."""
    if not pol or pol["policy"] == "keep":
        return {}
    al = matched_album or {}
    p = pol["policy"]
    va = Config.VARIOUS_ARTISTS
    out = {}
    if p in ("film", "artist"):
        out["albumartist"] = pol.get("albumartist") or al.get("album_artist") or ""
        out["album"] = pol.get("album") or al.get("album") or ""
        out["compilation"] = ""
    elif p == "collection":
        out["albumartist"] = pol.get("albumartist") or va
        out["album"] = pol.get("album") or clean_folder_name(folder)
        out["compilation"] = "1"
    else:  # various / single
        out["albumartist"] = pol.get("albumartist") or va
        if pol.get("album") or al.get("album"):
            out["album"] = pol.get("album") or al.get("album")
        out["compilation"] = "1" if p == "various" else ""
    if Config.WRITE_RELEASETYPE:
        out["releasetype"] = release_type(p, out.get("album") or al.get("album") or "")
    return {k: v for k, v in out.items() if v is not None and (v != "" or k == "compilation")}


_cache = {"key": None, "items": None}


def _data_key():
    r = db.q("SELECT (SELECT COUNT(*) FROM songs) a, (SELECT MAX(indexed) FROM songs) b, "
             "(SELECT COUNT(*) FROM folder_policy) c, (SELECT MAX(updated) FROM folder_policy) d, "
             "(SELECT MAX(updated) FROM albums) e", one=True)
    return (tuple(r), len(dismissed()), Config.POLICY_COLLECTION_WORDS, Config.POLICY_SUGGEST_MULTI,
            Config.POLICY_SUGGEST_SINGLE, Config.POLICY_SUGGEST_COLLECTION, Config.VARIOUS_ARTISTS)


def suggestions(limit=None):
    """Folders where a rule suggests something different from what the files say now, and you
    haven't decided yet. [{folder, album_id, status, n, policy, albumartist, album, why, current}]
    Recomputed only when songs, albums or decisions changed."""
    key = _data_key()
    if _cache["key"] != key:
        _cache.update(key=key, items=_suggestions())
    items = _cache["items"]
    return items[:limit] if limit else list(items)


def _suggestions():
    songs = {}
    for r in db.q("SELECT folder, albumartist, album, grouping, composer, releasetype FROM songs"):
        songs.setdefault(r["folder"], []).append(dict(r))
    albums = {r["path"]: dict(r) for r in db.q(
        "SELECT id, path, status, album_artist, title, json_extract(proposal, '$.collection') coll, "
        "json_extract(proposal, '$.fallback') fb, json_extract(proposal, '$.album.album_artist') md, "
        "json_extract(proposal, '$.album.track_count') tc, json_extract(proposal, '$.album.album') alb, "
        "json_extract(proposal, '$.md_check.status') mds FROM albums")}
    decided, gone = all_policies(), dismissed()
    out = []
    for folder, rows in songs.items():
        if folder in decided or folder in gone:
            continue
        a = albums.get(folder)
        light = None
        if a:
            light = {"album_artist": a["album_artist"], "proposal": {
                "collection": bool(a["coll"]), "fallback": bool(a["fb"]),
                "md_check": {"status": a["mds"]} if a["mds"] else None,
                "album": {"album_artist": a["md"] or a["album_artist"] or "", "track_count": a["tc"] or 0,
                          "album": a["alb"] or a["title"] or ""}}}
        s = needs_attention(folder, rows, light)
        if s:
            out.append({"folder": folder, "album_id": a["id"] if a else None, "status": a["status"] if a else None,
                        "n": len(rows), **s})
    order = {"collection": 0, "various": 1, "single": 2, "film": 3, "artist": 4}
    out.sort(key=lambda d: (order.get(d["policy"], 9), d["folder"]))
    return out


def accept(folder, pol, albumartist=None, album=None, reason=None):
    """Store a decision and queue writing it to every file of the folder."""
    from . import worker
    folder = os.path.normpath(folder)
    set_policy(folder, pol, albumartist, album, reason)
    a = db.album_by_path(folder)
    aid = a["id"] if a else db.album_upsert(folder, "applied", "manual")
    if pol != "keep":
        db.job_add("policy", aid, {}, priority=db.PRIORITY_USER)
    return aid
