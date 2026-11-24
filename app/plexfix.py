"""Make Plex show what the tags say.

unlock(album_key): fields locked in Plex (from an old match or a hand edit) never follow the tags,
so they're unlocked before Isaiarr asks Plex to refresh an album.

readd(album_id): Plex files an album under the artist it had when it was first added, and a
metadata refresh never moves it. The fix is the "Plex dance": hide the folder from Plex, scan so
Plex sees it gone, empty the trash, put the folder back and scan again, so it's added afresh under
the album artist in the tags. Star ratings and play counts are saved first and put back after;
playlist entries for those tracks can't be kept. One album at a time, in its own thread.
"""
import json
import os
import re
import queue
import shutil
import threading
import time

import requests

from . import db, plex
from .config import Config

ALBUM_FIELDS = ("title", "titleSort", "originallyAvailableAt", "year", "studio", "genre", "thumb")
TRACK_FIELDS = ("title", "titleSort", "originalTitle", "index")
IDENT = "com.plexapp.plugins.library"
STAGE = ".isaiarr-plex"          # dot folder + .plexignore: Plex and Isaiarr's own index both skip it

_q = queue.Queue()
_state = {"current": None, "queued": []}
_lock = threading.Lock()
_started = False


# ------------------------------------------------------------------ unlock

def unlock(sid, album_key):
    """Unlock an album's fields (and its tracks') so a refresh takes the values from the tags."""
    plex._req("PUT", f"/library/sections/{sid}/all", type=9, id=album_key,
              **{f"{f}.locked": 0 for f in ALBUM_FIELDS})
    for t in tracks(album_key):
        plex._req("PUT", f"/library/sections/{sid}/all", type=10, id=t["key"],
                  **{f"{f}.locked": 0 for f in TRACK_FIELDS})


def tracks(album_key):
    root = plex._req("GET", f"/library/metadata/{album_key}/children")
    out = []
    for t in (root.findall("Track") if root is not None else []):
        part = t.find("Media/Part")
        out.append({"key": t.get("ratingKey"), "title": t.get("title"),
                    "artist": t.get("grandparentTitle"), "album": t.get("parentTitle"),
                    "file": (part.get("file") if part is not None else "") or "",
                    "rating": float(t.get("userRating") or 0) or None, "plays": int(t.get("viewCount") or 0)})
    return out


# ------------------------------------------------------------------ re-add

def status():
    return {"current": _state["current"], "queued": list(_state["queued"]), "checking": bool(_state.get("checking")),
            "last": db.state_get("plex_readd") or {}}


def start(album_ids):
    """Queue albums for re-adding in Plex. Returns how many were queued."""
    global _started
    n = 0

    def root(i):
        a = db.album_get(i)
        return album_root(a["path"]) if a else None
    with _lock:
        roots = {root(i) for i in _state["queued"] + ([_state["current"]] if _state["current"] else [])}
        for aid in album_ids:
            r = root(aid)
            if r in roots:              # another disc of an album that's already being re-added
                continue
            roots.add(r)
            if aid != _state["current"] and aid not in _state["queued"]:
                _state["queued"].append(aid)
                _q.put(aid)
                n += 1
        if not _started:
            threading.Thread(target=_loop, name="plex-readd", daemon=True).start()
            _started = True
    return n


def _loop():
    while True:
        aid = _q.get()
        with _lock:
            if aid in _state["queued"]:
                _state["queued"].remove(aid)
            _state["current"] = aid
        try:
            readd(aid)
        except Exception as e:  # noqa: BLE001
            db.log(f"Plex re-add failed: {e}", "error")
        finally:
            _state["current"] = None


def _stage_root():
    d = os.path.join(Config.MUSIC_DIR, STAGE)
    os.makedirs(d, exist_ok=True)
    marker = os.path.join(d, ".plexignore")
    if not os.path.exists(marker):
        with open(marker, "w") as fh:
            fh.write("# Isaiarr: albums being re-added in Plex wait here for a moment\n*\n")
    return d


def staged():
    """{staging folder: original folder} for albums currently hidden from Plex."""
    return (db.state_get("plex_staged") or {}).get("dirs", {})


def _set_staged(d):
    db.state_set("plex_staged", {"dirs": d})


def recover():
    """At start-up: put back any album left hidden by an interrupted re-add."""
    for tmp, orig in list(staged().items()):
        if os.path.isdir(tmp) and not os.path.exists(orig):
            os.makedirs(os.path.dirname(orig), exist_ok=True)
            shutil.move(tmp, orig)
            db.log(f"Plex re-add: put {os.path.basename(orig)} back after an interrupted run", "warn")
        d = staged()
        d.pop(tmp, None)
        _set_staged(d)


def _wait_scan(sid, timeout):
    """Wait until Plex has finished scanning the library (it reports refreshing="1" while busy)."""
    t0 = time.time()
    time.sleep(Config.PLEX_POLL)
    while time.time() - t0 < timeout:
        busy = any(s.get("id") == sid and s.get("refreshing") for s in _sections_raw())
        if not busy:
            return True
        time.sleep(Config.PLEX_POLL)
    return False


def _sections_raw():
    root = plex._req("GET", "/library/sections")
    return [{"id": d.get("key"), "refreshing": d.get("refreshing") == "1"} for d in root.findall("Directory")]


def _gone(keys):
    for k in keys:
        try:
            root = plex._req("GET", f"/library/metadata/{k}")
            if root is not None and (root.findall("Directory") or root.findall("Track")):
                return False
        except requests.HTTPError:
            continue          # 404: gone
    return True


def readd(album_id):
    """Re-add one album in Plex so it's filed under the album artist in its tags."""
    from . import watcher
    a = db.album_get(album_id)
    if not a or not os.path.isdir(a["path"]):
        return _done(album_id, False, "The folder no longer exists")
    disc = a["path"]                    # the folder Isaiarr tagged (may be one disc of several)
    folder = album_root(disc)           # what gets hidden from Plex: the whole album
    name = a.get("title") or os.path.basename(folder)
    sid, ppath, note = plex.resolve(folder)
    if not sid:
        return _done(album_id, False, note)
    keys = plex.album_keys(disc)
    if not keys:
        return _done(album_id, False, "Couldn't find this album in Plex (has Plex scanned it yet?)")
    # 1. remember ratings and play counts, by file name
    saved = {}
    for k in keys:
        for t in tracks(k):
            if t["rating"] or t["plays"]:
                saved[_tail(t["file"])] = (t["rating"], t["plays"])
    db.log(f"Plex re-add: {name} — hiding it from Plex for a moment ({len(saved)} track(s) with ratings or plays saved)")
    tmp = os.path.join(_stage_root(), f"{album_id}-{int(time.time())}")
    parent = os.path.dirname(folder)
    watcher.suppress(folder, 3600)
    watcher.suppress(parent, 3600)
    d = staged()
    d[tmp] = folder
    _set_staged(d)
    shutil.move(folder, tmp)
    moved_back = False
    try:
        # 2. scan the parent so Plex sees the album gone, then empty the trash
        plex._get(f"/library/sections/{sid}/refresh", path=os.path.dirname(ppath))
        _wait_scan(sid, Config.PLEX_SCAN_TIMEOUT)
        plex._req("PUT", f"/library/sections/{sid}/emptyTrash")
        t0 = time.time()
        while not _gone(keys) and time.time() - t0 < Config.PLEX_SCAN_TIMEOUT:
            time.sleep(Config.PLEX_POLL)
        if not _gone(keys):
            raise RuntimeError("Plex didn't let go of the album (is the music library still scanning?)")
    except Exception as e:  # noqa: BLE001
        _move_back(tmp, folder)
        moved_back = True
        plex._get(f"/library/sections/{sid}/refresh", path=ppath)
        return _done(album_id, False, f"{e}. The folder is back where it was; nothing changed in Plex.")
    finally:
        if not moved_back:
            _move_back(tmp, folder)
        watcher.suppress(folder)
        watcher.suppress(parent)
    # 3. scan again: Plex adds it afresh, filed under the album artist in the tags
    plex._get(f"/library/sections/{sid}/refresh", path=ppath)
    _wait_scan(sid, Config.PLEX_SCAN_TIMEOUT)
    new_keys = []
    t0 = time.time()
    while not new_keys and time.time() - t0 < Config.PLEX_SCAN_TIMEOUT:
        new_keys = plex.album_keys(disc)
        if not new_keys:
            time.sleep(Config.PLEX_POLL)
    if not new_keys:
        return _done(album_id, False, "Removed from Plex, but Plex hasn't added it back yet. "
                                      "It will appear after Plex's next scan; ratings couldn't be restored.")
    # 4. put ratings and play counts back
    restored = 0
    for k in new_keys:
        for t in tracks(k):
            r, n = saved.get(_tail(t["file"]), (None, 0))
            if r:
                plex._req("PUT", "/:/rate", key=t["key"], identifier=IDENT, rating=r)
            for _ in range(min(n, Config.PLEX_MAX_REPLAYS)):
                plex._req("GET", "/:/scrobble", key=t["key"], identifier=IDENT)
            restored += 1 if (r or n) else 0
            db.x("UPDATE songs SET plex_key=?, plex_artist=COALESCE(?, plex_artist), plex_album=COALESCE(?, plex_album), "
                 "plex_title=COALESCE(?, plex_title) WHERE (folder=? OR folder LIKE ?) AND LOWER(path) LIKE ?",
                 (t["key"], t["artist"], t["album"], t["title"], folder, folder + "/%", "%/" + _tail(t["file"])))
    if Config.PLEX_AUTO_MERGE:
        try:
            merge_duplicates(quiet=True)       # the re-added album may have started a second artist entry
        except Exception as e:  # noqa: BLE001
            db.log(f"Plex: artist merge after re-add failed: {e}", "warn")
    return _done(album_id, True, f"Re-added in Plex; {restored} track(s) got their ratings and play counts back")


def refresh_many(folders):
    """Unlock + refresh several albums in Plex, in the background (no waiting between them)."""
    from . import tasks

    def run():
        n = 0
        with tasks.track("plexrefresh", "Refreshing albums in Plex", total=len(folders)) as t:
            for f in folders:
                t.detail = os.path.basename(f)
                try:
                    if plex.refresh(f, metadata=False):
                        plex._refresh_albums(f)
                        n += 1
                except Exception as e:  # noqa: BLE001
                    db.log(f"Plex refresh of {os.path.basename(f)} failed: {e}", "warn")
                t.step()
        db.log(f"Plex: refreshed {n} of {len(folders)} album(s)")
    threading.Thread(target=run, name="plex-refresh", daemon=True).start()
    return len(folders)


def check():
    """Re-read every track from Plex (play counts, IDs and what Plex shows), in the background."""
    from . import worker
    if _state.get("checking"):
        return False

    def run():
        _state["checking"] = True
        try:
            worker._plex_sync()
        finally:
            _state["checking"] = False
    threading.Thread(target=run, name="plex-check", daemon=True).start()
    return True


DISC_RE = re.compile(r"^\s*[\[(]?\s*(cd|disc|disk|dvd|vol(ume)?\.?)\s*[-_ ]?\d{1,2}\b", re.I)


def album_root(folder):
    """'…/Bob Marley 3CD/CD 1' -> '…/Bob Marley 3CD' when the album is split into disc folders,
    because Plex treats all the discs as one album and only lets go of it when all are hidden."""
    parent = os.path.dirname(folder)
    if DISC_RE.match(os.path.basename(folder)) and parent.rstrip("/") != Config.MUSIC_DIR.rstrip("/"):
        return parent
    return folder


def _tail(path):
    """'.../CD 1/01 - Song.flac' -> 'cd 1/01 - song.flac': discs can reuse the same file names."""
    parts = path.replace("\\", "/").split("/")
    return "/".join(parts[-2:]).lower()


def merge_duplicates(quiet=False):
    """Merge every group of same-name artists in Plex. Returns how many entries were merged away."""
    n = 0
    for g in plex.duplicate_artists():
        try:
            n += plex.merge_artists(g["keep"], g["merge"])
        except Exception as e:  # noqa: BLE001
            db.log(f"Plex: couldn't merge the “{g['name']}” artists: {e}", "warn")
    if n or not quiet:
        db.log(f"Plex: merged {n} duplicate artist entr{'y' if n == 1 else 'ies'}")
    return n


def _move_back(tmp, folder):
    if os.path.isdir(tmp) and not os.path.exists(folder):
        shutil.move(tmp, folder)
    d = staged()
    d.pop(tmp, None)
    _set_staged(d)


def _done(album_id, ok, msg):
    a = db.album_get(album_id)
    name = (a or {}).get("title") or os.path.basename((a or {}).get("path", "") or "album")
    db.log(f"Plex re-add: {name} — {msg}", "info" if ok else "warn")
    hist = (db.state_get("plex_readd") or {}).get("albums", {})
    hist[str(album_id)] = {"ok": ok, "msg": msg, "at": time.time()}
    db.state_set("plex_readd", {"albums": hist})
    return ok


def last(album_id):
    return ((db.state_get("plex_readd") or {}).get("albums", {}) or {}).get(str(album_id))


def dump():
    return json.dumps(status())
