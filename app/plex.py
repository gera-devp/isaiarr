"""Trigger a partial Plex scan for a changed album folder."""
import collections
import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET

import requests

from . import db
from .config import Config

_section_cache = {}


def enabled():
    return bool(Config.PLEX_URL and Config.PLEX_TOKEN)


def _map(prefix, local_path):
    root = Config.MUSIC_DIR.rstrip("/")
    if prefix and (local_path == root or local_path.startswith(root + "/")):
        return prefix.rstrip("/") + local_path[len(root):]
    return local_path


def detected_prefix():
    """The music folder as Plex sees it, learned from Plex's own track paths (see learn_prefix)."""
    return (db.state_get("plex_prefix") or {}).get("prefix")


def plex_path(local_path):
    """This app's path -> the same file as Plex sees it. Settings first, then what was learned from Plex."""
    return _map(Config.PLEX_MUSIC_PATH or detected_prefix(), local_path)


def _prefix_from(plex_file, local_file):
    """'/volume2/Multimedia/Audio Songs/A/B/c.flac' + '/music/A/B/c.flac' -> '/volume2/Multimedia/Audio Songs'."""
    root = Config.MUSIC_DIR.rstrip("/")
    if not local_file.startswith(root + "/"):
        return None
    rel = local_file[len(root):]                 # '/A/B/c.flac'
    pf = plex_file.replace("\\", "/")
    if pf.lower().endswith(rel.lower()):
        return pf[:len(pf) - len(rel)] or "/"
    return None


def _remember_prefix(votes):
    if not votes:
        return None
    prefix, n = votes.most_common(1)[0]
    db.state_set("plex_prefix", {"prefix": prefix, "local": Config.MUSIC_DIR, "votes": n, "at": time.time()})
    return prefix


def learn_prefix(sample=200):
    """Ask Plex for a few tracks and compare their paths with the song index to learn where Plex
    sees the music folder (a Synology Plex package sees /volume2/..., this container sees /music)."""
    index = {}
    for r in db.q("SELECT path FROM songs"):
        index.setdefault(_key(r["path"], 3), r["path"])
    if not index:
        return None
    votes = collections.Counter()
    for s in sections():
        if s["type"] != "artist":
            continue
        root = _req("GET", f"/library/sections/{s['id']}/all", type=10,
                    **{"X-Plex-Container-Start": 0, "X-Plex-Container-Size": sample})
        for t in root.findall("Track"):
            part = t.find("Media/Part")
            f = part.get("file") if part is not None else None
            if f and _key(f, 3) in index:
                p = _prefix_from(f, index[_key(f, 3)])
                if p:
                    votes[p] += 1
    return _remember_prefix(votes)


def _inside(path, locs):
    return any(path == l.rstrip("/") or path.startswith(l.rstrip("/") + "/") for l in locs)


def resolve(local_dir):
    """(section id, path as Plex sees it, note). Checks the path really is inside a Plex music
    library; if the configured "Music folder as Plex sees it" is wrong, uses the learned one."""
    music = [s for s in sections() if s["type"] == "artist"]
    tried = []
    for prefix in (Config.PLEX_MUSIC_PATH, detected_prefix(), None, "learn"):
        if prefix == "learn":
            try:
                prefix = learn_prefix()
            except Exception:  # noqa: BLE001
                prefix = None
            if not prefix:
                continue
        p = _map(prefix, local_dir) if prefix else local_dir
        if p in tried:
            continue
        tried.append(p)
        for s in music:
            if (not Config.PLEX_SECTION_ID or s["id"] == Config.PLEX_SECTION_ID) and _inside(p, s["locations"]):
                note = None
                if Config.PLEX_MUSIC_PATH and prefix != Config.PLEX_MUSIC_PATH:
                    note = (f"“Music folder as Plex sees it” is set to {Config.PLEX_MUSIC_PATH}, which isn't in any "
                            f"Plex music library; used {prefix or local_dir} instead. Fix it or clear it in Settings → Plex.")
                return s["id"], p, note
    locs = "; ".join(f"{s['title']}: {', '.join(s['locations'])}" for s in music) or "no music libraries"
    return None, tried[0] if tried else local_dir, (f"Plex has no music library containing {tried[0] if tried else local_dir} "
                                                    f"(Plex libraries: {locs}). Set “Music folder as Plex sees it” in Settings → Plex.")


def _get(endpoint, **params):
    params["X-Plex-Token"] = Config.PLEX_TOKEN
    r = requests.get(Config.PLEX_URL + endpoint, params=params, timeout=15,
                     headers={"Accept": "application/xml"})
    r.raise_for_status()
    return r


def sections():
    try:
        root = ET.fromstring(_get("/library/sections").content)
    except Exception as e:
        db.state_set("plex", {"ok": False, "at": time.time(), "error": str(e)})
        raise
    db.state_set("plex", {"ok": True, "at": time.time(), "error": None})
    out = []
    for d in root.findall("Directory"):
        out.append({"id": d.get("key"), "title": d.get("title"), "type": d.get("type"),
                    "agent": d.get("agent"),
                    "locations": [l.get("path") for l in d.findall("Location")]})
    return out


def section_for(path):
    if Config.PLEX_SECTION_ID:
        return Config.PLEX_SECTION_ID
    for s in sections():
        if s["type"] != "artist":
            continue
        for loc in s["locations"]:
            if path == loc or path.startswith(loc.rstrip("/") + "/"):
                return s["id"]
    return None


def refresh(local_dir, metadata=True):
    """Partial scan of one folder, then (a little later) a metadata refresh of its albums so Plex
    re-reads the tags that changed. A scan alone often leaves the old album artist or title."""
    if not enabled():
        return False
    try:
        sid, p, note = resolve(local_dir)
        if note:
            db.log(f"Plex: {note}", "warn")
        if not sid:
            db.state_set("plex", {"ok": False, "at": time.time(), "error": note})
            return False
        _get(f"/library/sections/{sid}/refresh", path=p)
        db.state_set("plex", {"ok": True, "at": time.time(), "error": None, "last_path": p})
        if metadata and Config.PLEX_REFRESH_META:
            _later(Config.PLEX_REFRESH_DELAY, _refresh_albums, local_dir)
        else:
            _later(Config.PLEX_REFRESH_DELAY, _push_meta, local_dir, None)
        return True
    except (requests.RequestException, ET.ParseError) as e:
        db.log(f"Plex refresh failed: {e}", "warn")
        db.state_set("plex", {"ok": False, "at": time.time(), "error": str(e)})
        return False


def _later(seconds, fn, *args):
    t = threading.Timer(max(0, seconds), _safe, args=(fn, *args))
    t.daemon = True
    t.start()


def _safe(fn, *args):
    try:
        fn(*args)
    except Exception as e:  # noqa: BLE001
        db.log(f"Plex metadata refresh failed: {e}", "warn")


def album_keys(local_dir):
    """Plex album IDs for the tracks in one folder: found by asking Plex for the folder's tracks."""
    sid, p, _ = resolve(local_dir)
    if not sid:
        return []
    keys = set()
    want = _key(p, 1)                                    # the folder, as Plex sees it
    # after a rename Plex has new track IDs, so look the tracks up by title and match the folder
    for r in db.q("SELECT title, path FROM songs WHERE folder=? ORDER BY path LIMIT 4", (local_dir,)):
        try:
            root = _req("GET", f"/library/sections/{sid}/all", type=10, title=r["title"])
        except requests.RequestException:
            continue
        for t in root.findall("Track"):
            part = t.find("Media/Part")
            f = (part.get("file") if part is not None else "") or ""
            if t.get("parentRatingKey") and _key(os.path.dirname(f), 1) == want and \
                    _key(f, 2) == _key(r["path"], 2):
                keys.add(t.get("parentRatingKey"))
    return sorted(keys)


def _refresh_albums(local_dir):
    from . import plexfix
    keys = album_keys(local_dir)
    sid = resolve(local_dir)[0] if keys else None
    for k in keys:
        if Config.PLEX_UNLOCK and sid:
            try:
                plexfix.unlock(sid, k)
            except requests.RequestException as e:
                db.log(f"Plex: couldn't unlock fields of album {k}: {e}", "warn")
        _req("PUT", f"/library/metadata/{k}/refresh", force=1)
    if keys:
        db.log(f"Plex: refreshed {len(keys)} album(s) for {os.path.basename(local_dir)}")
        _push_meta(local_dir, keys)
    _later(Config.PLEX_REFRESH_DELAY, _safe_check, local_dir)      # then: does Plex show the tags now?


def _push_meta(local_dir, keys):
    """Collections, review and moods (plexmeta): after the album's refresh, since they're set
    through Plex and locked there."""
    from . import plexmeta
    if not plexmeta.enabled():
        return
    try:
        note = plexmeta.push(local_dir, keys, force=plexmeta.any_on())
        if note.startswith("failed"):
            db.log(f"Plex collections/review/moods for {os.path.basename(local_dir)}: {note}", "warn")
    except Exception as e:  # noqa: BLE001
        db.log(f"Plex collections/review/moods for {os.path.basename(local_dir)} failed: {e}", "warn")


def _safe_check(local_dir):
    try:
        res = check_folder(local_dir)
        if res.get("diffs") or res.get("missing"):
            db.log(f"Plex check: {os.path.basename(local_dir)} — {len(res['diffs'])} difference(s), "
                   f"{res['missing']} track(s) not found in Plex", "warn")
    except Exception as e:  # noqa: BLE001
        db.log(f"Plex check failed for {os.path.basename(local_dir)}: {e}", "warn")


def diagnose():
    """Lines for Settings → Plex → Test connection: libraries, where Plex sees the music, and the
    library settings that decide whether Plex uses the tags Isaiarr writes."""
    out = []
    music = [s for s in sections() if s["type"] == "artist"]
    if not music:
        return ["Connected, but Plex has no music library."]
    out.append("Connected. Music libraries: " + "; ".join(
        f"#{s['id']} {s['title']} ({', '.join(s['locations'])})" for s in music))
    try:
        learned = learn_prefix()
    except Exception as e:  # noqa: BLE001
        learned, _ = None, out.append(f"Couldn't read tracks from Plex: {e}")
    if learned:
        out.append(f"Plex sees this app's {Config.MUSIC_DIR} as {learned}.")
    if Config.PLEX_MUSIC_PATH and learned and Config.PLEX_MUSIC_PATH.rstrip("/") != learned.rstrip("/"):
        out.append(f"⚠ “Music folder as Plex sees it” is {Config.PLEX_MUSIC_PATH}, but Plex uses {learned}. "
                   "Clear it (it's detected now) or set it to that path.")
    sid, p, note = resolve(Config.MUSIC_DIR)
    out.append(f"Rescans go to {p} in library #{sid}." if sid else f"⚠ {note}")
    for s in music:
        try:
            root = _req("GET", f"/library/sections/{s['id']}/prefs")
        except requests.RequestException:
            continue
        for st in root.iter("Setting"):
            label = (st.get("label") or st.get("id") or "")
            if any(w in label.lower() for w in ("local", "embedded")):
                v = st.get("value")
                on = {"true": "on", "1": "on", "false": "off", "0": "off"}.get(str(v).lower(), v)
                out.append(f"{s['title']} · {label}: {on}" + ("  ⚠ turn this on so Plex uses the tags Isaiarr writes"
                                                              if on == "off" and "local" in label.lower() else ""))
    return out


# ------------------------------------------------------------------ tracks and playlists

def _req(method, endpoint, **params):
    params["X-Plex-Token"] = Config.PLEX_TOKEN
    r = requests.request(method, Config.PLEX_URL + endpoint, params=params, timeout=30,
                         headers={"Accept": "application/xml"})
    r.raise_for_status()
    return ET.fromstring(r.content) if r.content.strip() else None


def machine_id():
    root = _req("GET", "/identity")
    return root.get("machineIdentifier")


def _key(path, n):
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    return "/".join(parts[-n:]).lower()


def sync_tracks(page=500):
    """Match every Plex music track to a song in the index by its file path, and copy over its
    Plex ID, star rating and play count. Paths are matched on their last folders + file name,
    so it works whatever path Plex uses for the share."""
    songs = db.q("SELECT id, path FROM songs")
    by3 = {_key(r["path"], 3): r["id"] for r in songs}
    by2 = {}
    for r in songs:
        by2.setdefault(_key(r["path"], 2), []).append(r["id"])
    matched, total = 0, 0
    found, votes = {}, collections.Counter()
    by_path = {_key(r["path"], 3): r["path"] for r in songs}
    for s in sections():
        if s["type"] != "artist":
            continue
        start = 0
        while True:
            root = _req("GET", f"/library/sections/{s['id']}/all", type=10,
                        **{"X-Plex-Container-Start": start, "X-Plex-Container-Size": page})
            tracks = root.findall("Track")
            for t in tracks:
                total += 1
                part = t.find("Media/Part")
                if part is None or not part.get("file"):
                    continue
                f = part.get("file")
                sid = by3.get(_key(f, 3))
                if sid is None:
                    c = by2.get(_key(f, 2)) or []
                    sid = c[0] if len(c) == 1 else None
                if sid is None:
                    continue
                matched += 1
                if len(votes) < 5 and sum(votes.values()) < 300 and _key(f, 3) in by_path:
                    pre = _prefix_from(f, by_path[_key(f, 3)])
                    if pre:
                        votes[pre] += 1
                found[sid] = (t.get("ratingKey"), float(t.get("userRating") or 0) or None,
                              int(t.get("viewCount") or 0), float(t.get("lastViewedAt") or 0) or None,
                              t.get("grandparentTitle"), t.get("parentTitle"), t.get("title"),
                              t.get("parentRatingKey"))
            size = int(root.get("totalSize") or root.get("size") or 0)
            start += len(tracks)
            if not tracks or start >= size:
                break
    db.x("UPDATE songs SET plex_key=NULL, plex_artist=NULL, plex_album=NULL, plex_title=NULL, plex_album_key=NULL")
    for sid, (key, rating, plays, last, part, album, title, akey) in found.items():
        db.x("UPDATE songs SET plex_key=?, rating=?, plays=?, last_played=?, plex_artist=?, plex_album=?, plex_title=?, "
             "plex_album_key=? WHERE id=?", (key, rating, plays, last, part, album, title, akey, sid))
    _remember_prefix(votes)
    db.state_set("songs_plex", {"at": time.time(), "matched": matched, "tracks": total, "error": None})
    db.state_set("plex", {"ok": True, "at": time.time(), "error": None})
    return matched, total


def _norm(s):
    return " ".join(str(s or "").lower().replace("’", "'").split())


def _same_artist(plex_artist, tag):
    """Plex files an album under the first album artist when the tag lists several."""
    a, t = _norm(plex_artist), _norm(tag)
    if a == t:
        return True
    first = _norm(re.split(r"\s*(?:;|/|,|&|\bfeat\.?)\s*", tag or "")[0])
    return a == first


def differences():
    """Albums whose tags (what Isaiarr wrote) don't match what Plex shows, from the last Plex sync.
    {folder: {"kind": "artist"|"details", "plex_artist", "plex_album", "tag_artist", "tag_album", "n", "of"}}
    artist: Plex files the album under another artist (needs Re-add in Plex);
    details: album name or a track title is out of date (Refresh in Plex fixes it)."""
    out = {}
    for r in db.q("SELECT folder, title, artist, albumartist, album, plex_artist, plex_album, plex_title "
                  "FROM songs WHERE plex_key IS NOT NULL AND plex_artist IS NOT NULL"):
        tag_artist = r["albumartist"] or r["artist"]
        d = out.setdefault(r["folder"], {"kind": None, "n": 0, "of": 0, "plex_artist": r["plex_artist"],
                                         "plex_album": r["plex_album"], "tag_artist": tag_artist, "tag_album": r["album"]})
        d["of"] += 1
        if tag_artist and not _same_artist(r["plex_artist"], tag_artist):
            kind = "artist"
        elif (r["album"] and _norm(r["plex_album"]) != _norm(r["album"])) or \
                (r["title"] and r["plex_title"] and _norm(r["plex_title"]) != _norm(r["title"])):
            kind = "details"
        else:
            continue
        d["n"] += 1
        if d["kind"] != "artist":
            d["kind"] = kind
            d.update(plex_artist=r["plex_artist"], plex_album=r["plex_album"], tag_artist=tag_artist, tag_album=r["album"])
    return {f: d for f, d in out.items() if d["kind"]}


_HIDDEN = re.compile("[\u200b-\u200f\u2060\ufeff\u00ad]")


def artist_key(name):
    """How two Plex artist names compare: same letters, ignoring case, spaces, hidden characters
    and look-alike punctuation (so 'A.R. Rahman' and 'A.R.  Rahman ' are the same)."""
    import unicodedata
    s = unicodedata.normalize("NFKC", _HIDDEN.sub("", name or "")).casefold()
    return " ".join(s.split())


def plex_artists(page=1000):
    """Every artist in Plex's music libraries: [{key, title, guid, albums, section}]."""
    out = []
    for s in sections():
        if s["type"] != "artist":
            continue
        start = 0
        while True:
            root = _req("GET", f"/library/sections/{s['id']}/all", type=8,
                        **{"X-Plex-Container-Start": start, "X-Plex-Container-Size": page})
            items = root.findall("Directory")
            for d in items:
                out.append({"key": d.get("ratingKey"), "title": d.get("title") or "", "guid": d.get("guid") or "",
                            "albums": int(d.get("childCount") or 0), "section": s["id"],
                            "thumb": bool(d.get("thumb"))})
            size = int(root.get("totalSize") or root.get("size") or 0)
            start += len(items)
            if not items or start >= size:
                break
    return out


def duplicate_artists():
    """Plex artists that share a name: [{name, keep, merge: [..], artists: [...]}], biggest first.
    The one with the most albums (then a photo) is kept; the others are merged into it."""
    groups = collections.defaultdict(list)
    for a in plex_artists():
        if a["title"].strip():
            groups[(a["section"], artist_key(a["title"]))].append(a)
    out = []
    for (_sid, _k), items in groups.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda a: (-a["albums"], not a["thumb"], int(a["key"] or 0)))
        hidden = len({a["title"] for a in items}) > 1
        out.append({"name": items[0]["title"], "keep": items[0]["key"], "merge": [a["key"] for a in items[1:]],
                    "artists": items, "albums": sum(a["albums"] for a in items), "hidden_diff": hidden})
    out = sorted(out, key=lambda g: (-len(g["artists"]), -g["albums"]))
    db.state_set("plex_dupe_artists", {"n": len(out), "at": time.time()})
    return out


def merge_artists(keep, others):
    """Plex's own Merge (as in Plex Web: select artists → Merge): the others' albums move to `keep`."""
    others = [k for k in others if k and k != keep]
    if not others:
        return 0
    _req("PUT", f"/library/metadata/{keep}/merge", ids=",".join(others))
    return len(others)


def counts():
    """Plex's own totals across its music libraries: {artists, albums, tracks}."""
    out = {"artists": 0, "albums": 0, "tracks": 0}
    for s in sections():
        if s["type"] != "artist":
            continue
        for key, t in (("artists", 8), ("albums", 9), ("tracks", 10)):
            root = _req("GET", f"/library/sections/{s['id']}/all", type=t,
                        **{"X-Plex-Container-Start": 0, "X-Plex-Container-Size": 0})
            out[key] += int(root.get("totalSize") or root.get("size") or 0)
    return out


def local_counts():
    """The same totals worked out from the files' tags, the way Plex groups them: one artist per
    Album Artist (the singers when it's empty), one album per Album Artist + album name."""
    artists, albums, tracks = set(), set(), 0
    for r in db.q("SELECT albumartist, artist, album FROM songs"):
        tracks += 1
        aa = artist_key((r["albumartist"] or "").strip() or (r["artist"] or "").strip())
        if aa:
            artists.add(aa)
        albums.add((aa, _norm(r["album"])))
    return {"artists": len(artists), "albums": len(albums), "tracks": tracks,
            "folders": db.q("SELECT COUNT(DISTINCT folder) n FROM songs", one=True)["n"] or 0}


def check_folder(local_dir):
    """Step 5 of a write: does Plex now show what the files say? Looks the folder's tracks up in
    Plex and compares album artist and album. Saves the result on the album:
    {"ok": bool, "at", "checked": n, "diffs": [{file, field, plex, tags}], "missing": n}"""
    sid, p, note = resolve(local_dir)
    if not sid:
        res = {"ok": False, "at": time.time(), "checked": 0, "diffs": [], "missing": 0, "error": note}
        _save_check(local_dir, res)
        return res
    want = _key(p, 1)
    rows = [dict(r) for r in db.q("SELECT id, path, title, album, albumartist, artist FROM songs WHERE folder=? "
                                  "ORDER BY path LIMIT 12", (local_dir,))]
    diffs, checked, missing = [], 0, 0
    for r in rows:
        try:
            root = _req("GET", f"/library/sections/{sid}/all", type=10, title=r["title"])
        except requests.RequestException:
            continue
        hit = None
        for t in root.findall("Track"):
            part = t.find("Media/Part")
            f = (part.get("file") if part is not None else "") or ""
            if _key(os.path.dirname(f), 1) == want and _key(f, 2) == _key(r["path"], 2):
                hit = t
                break
        if hit is None:
            missing += 1
            continue
        checked += 1
        tag_aa = (r["albumartist"] or "").strip() or (r["artist"] or "").strip()
        db.x("UPDATE songs SET plex_key=?, plex_artist=?, plex_album=?, plex_title=?, plex_album_key=? WHERE id=?",
             (hit.get("ratingKey"), hit.get("grandparentTitle"), hit.get("parentTitle"), hit.get("title"),
              hit.get("parentRatingKey"), r["id"]))
        if tag_aa and not _same_artist(hit.get("grandparentTitle"), tag_aa):
            diffs.append({"file": os.path.basename(r["path"]), "field": "album artist",
                          "plex": hit.get("grandparentTitle") or "", "tags": tag_aa})
        elif r["album"] and _norm(hit.get("parentTitle")) != _norm(r["album"]):
            diffs.append({"file": os.path.basename(r["path"]), "field": "album",
                          "plex": hit.get("parentTitle") or "", "tags": r["album"]})
    res = {"ok": bool(checked) and not diffs and not missing, "at": time.time(), "checked": checked,
           "diffs": diffs[:20], "missing": missing}
    _save_check(local_dir, res)
    return res


def _save_check(local_dir, res):
    a = db.album_by_path(local_dir)
    if a:
        db.album_update(a["id"], plex_check=json.dumps(res))


def playlists():
    root = _req("GET", "/playlists", playlistType="audio")
    return [{"id": p.get("ratingKey"), "title": p.get("title"), "count": int(p.get("leafCount") or 0),
             "smart": p.get("smart") == "1"} for p in root.findall("Playlist")]


def _uri(mid, keys):
    return f"server://{mid}/com.plexapp.plugins.library/library/metadata/{','.join(keys)}"


def playlist_items(pid):
    root = _req("GET", f"/playlists/{pid}/items")
    return [t.get("ratingKey") for t in root.findall("Track")]


def save_playlist(title, keys, mode="new", target=None, chunk=100):
    """mode: new | replace (target id) | append (target id, duplicates skipped). Returns (id, added)."""
    if not keys:
        raise ValueError("No songs to save")
    mid = machine_id()
    if mode == "append" and target:
        have = set(playlist_items(target))
        keys = [k for k in keys if k not in have]
        for i in range(0, len(keys), chunk):
            _req("PUT", f"/playlists/{target}/items", uri=_uri(mid, keys[i:i + chunk]))
        return target, len(keys)
    if mode == "replace" and target:
        _req("DELETE", f"/playlists/{target}")
    root = _req("POST", "/playlists", type="audio", title=title, smart=0, uri=_uri(mid, keys[:chunk]))
    pl = root.find("Playlist")
    pid = pl.get("ratingKey") if pl is not None else None
    for i in range(chunk, len(keys), chunk):
        _req("PUT", f"/playlists/{pid}/items", uri=_uri(mid, keys[i:i + chunk]))
    return pid, len(keys)
