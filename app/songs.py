"""Song index for playlists: one row per audio file in the library.

Built from the files' own tags (the ones Isaiarr writes), so music director, singers, film and
year are reliable. Unchanged files are skipped by modification time, so a re-index is quick.
Plex IDs, ratings and play counts are merged in by `plex_sync`.
"""
import json
import os
import re
import threading
import time

from . import db, tags as T
from .config import Config

_state = {"running": False, "done": 0, "total": 0, "started": None, "finished": None, "error": None,
          "what": ""}
_lock = threading.Lock()

JUNK_RE = re.compile(r"\b(karaoke|instrumental|remix|reprise|unplugged|lofi|lo-fi|8d|slowed|reverb|"
                     r"mashup|cover|ringtone|bgm|theme music|teaser|promo)\b", re.I)


def status():
    n = db.q("SELECT COUNT(*) n, SUM(plex_key IS NOT NULL) plex, SUM(profile IS NOT NULL) prof FROM songs", one=True)
    last = db.state_get("songs_index") or {}
    return {**_state, "songs": n["n"] or 0, "in_plex": n["plex"] or 0, "profiled": n["prof"] or 0,
            "last_index": last.get("at"), "last_plex": (db.state_get("songs_plex") or {}).get("at"),
            "plex_error": (db.state_get("songs_plex") or {}).get("error")}


def _year(v):
    m = re.search(r"(19|20)\d\d", v or "")
    return int(m.group(0)) if m else None


def _row(path, info):
    f = info["fields"]
    film = f.get("grouping") if f.get("compilation") == "1" and f.get("grouping") else f.get("album")
    return {"path": path, "folder": os.path.dirname(path), "title": f.get("title") or os.path.splitext(os.path.basename(path))[0],
            "artist": f.get("artist") or "", "albumartist": f.get("albumartist") or "", "album": f.get("album") or "",
            "film": film or "", "year": _year(f.get("date")), "genre": f.get("genre") or "",
            "duration": info.get("duration") or 0, "has_lyrics": 1 if info.get("has_lyrics") else 0,
            "composer": f.get("composer") or "", "grouping": f.get("grouping") or "",
            "compilation": f.get("compilation") or "", "releasetype": f.get("releasetype") or "",
            "tracknumber": f.get("tracknumber") or "",
            "label": f.get("label") or "", "date": f.get("date") or "",
            "has_art": 1 if info.get("has_art") else 0,
            "tag_score": sum(1 for k in ("title", "artist", "album", "albumartist", "date", "genre", "tracknumber")
                             if (f.get(k) or "").strip()) + (1 if info.get("has_art") else 0) + (1 if info.get("has_lyrics") else 0)}


def _upsert(path, mtime):
    info = T.read(path)
    r = _row(path, info)
    r.update(mtime=mtime, indexed=time.time())
    cols = list(r)
    db.x(f"INSERT INTO songs({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
         f"ON CONFLICT(path) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in cols if c != 'path')}",
         tuple(r.values()))


def _audio_files(folder):
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return [os.path.join(folder, n) for n in names
            if not n.startswith(".") and n.lower().endswith(Config.AUDIO_EXTS) and os.path.isfile(os.path.join(folder, n))]


def index_folder(folder, force=False):
    """Bring one folder's songs up to date (after a write, rename or undo)."""
    known = {r["path"]: r["mtime"] for r in db.q("SELECT path, mtime FROM songs WHERE folder=?", (folder,))}
    seen = set()
    n = 0
    for p in _audio_files(folder):
        seen.add(p)
        try:
            mt = os.path.getmtime(p)
            if force or known.get(p) != mt:
                _upsert(p, mt)
                n += 1
        except Exception:  # noqa: BLE001 - unreadable file: leave it out
            continue
    gone = [p for p in known if p not in seen]
    for p in gone:
        db.x("DELETE FROM songs WHERE path=?", (p,))
    return n


def library_online():
    """The music folder is mounted and not empty (a NAS volume that isn't up yet looks empty)."""
    try:
        return os.path.isdir(Config.MUSIC_DIR) and any(True for _ in os.scandir(Config.MUSIC_DIR))
    except OSError:
        return False


def index_all(force=False):
    """Walk the whole library. Runs in a background thread; progress in status()."""
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, done=0, total=0, started=time.time(), finished=None, error=None,
                      what="Indexing songs")
    try:
        folders = []
        for d, dirs, files in os.walk(Config.MUSIC_DIR):
            dirs[:] = sorted(x for x in dirs if not x.startswith((".", "@")))
            if any(f.lower().endswith(Config.AUDIO_EXTS) for f in files):
                folders.append(d)
        if not folders and not library_online():
            raise RuntimeError(f"The music folder {Config.MUSIC_DIR} is empty or not reachable; the song index was left as it was")
        _state["total"] = len(folders)
        for i, d in enumerate(folders):
            index_folder(d, force=force)
            _state["done"] = i + 1
        live = set(folders)
        from . import plexfix
        live |= set(plexfix.staged().values())       # albums hidden from Plex for a moment, not deleted
        for r in db.q("SELECT DISTINCT folder FROM songs"):
            if r["folder"] not in live:
                db.x("DELETE FROM songs WHERE folder=?", (r["folder"],))
        db.state_set("songs_index", {"at": time.time()})
        db.log(f"Song index: {len(folders)} folders checked")
    except Exception as e:  # noqa: BLE001
        _state["error"] = str(e)
        db.log(f"Song index failed: {e}", "error")
    finally:
        _state.update(running=False, finished=time.time())
    return True


def start_index(force=False):
    if _state["running"]:
        return False
    threading.Thread(target=index_all, kwargs={"force": force}, name="indexer", daemon=True).start()
    return True


# ------------------------------------------------------------------ queries

def get(ids):
    if not ids:
        return []
    rows = db.q(f"SELECT * FROM songs WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))
    by = {r["id"]: dict(r) for r in rows}
    out = []
    for i in ids:
        if i in by:
            r = by[i]
            r["profile"] = json.loads(r["profile"]) if r["profile"] else None
            out.append(r)
    return out


def search(q, limit=20):
    like = f"%{q.strip()}%"
    return [dict(r) for r in db.q(
        "SELECT id, title, artist, albumartist, film, year FROM songs "
        "WHERE title LIKE ? OR film LIKE ? OR artist LIKE ? ORDER BY plays DESC, title LIMIT ?",
        (like, like, like, limit))]


def split_people(s):
    return [p.strip() for p in re.split(r"\s*(?:;|,|&|/|\band\b|\bft\.?|\bfeat\.?)\s*", s or "") if p.strip()]


def directors(limit=400):
    return [(r["albumartist"], r["n"]) for r in db.q(
        "SELECT albumartist, COUNT(*) n FROM songs WHERE albumartist != '' GROUP BY albumartist "
        "ORDER BY n DESC LIMIT ?", (limit,))]


def singers(limit=400):
    counts = {}
    for r in db.q("SELECT artist FROM songs WHERE artist != ''"):
        for p in split_people(r["artist"]):
            counts[p] = counts.get(p, 0) + 1
    return sorted(counts.items(), key=lambda x: -x[1])[:limit]


def genres():
    return [(r["genre"], r["n"]) for r in db.q(
        "SELECT genre, COUNT(*) n FROM songs WHERE genre != '' GROUP BY genre ORDER BY n DESC LIMIT 40")]


def decades():
    return [(r["d"], r["n"]) for r in db.q(
        "SELECT (year/10)*10 d, COUNT(*) n FROM songs WHERE year IS NOT NULL GROUP BY d ORDER BY d")]
