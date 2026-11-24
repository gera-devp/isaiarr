"""Everything Isaiarr is doing in the background, in one list.

The sidebar (and Activity → Now) shows this list and refreshes it every couple of seconds, so you
don't have to visit each page, or read the log, to see what's running:

  album queue       analyses and writes (the worker), with a count, the album in hand and time left
  new music         folders the watcher is waiting on while files are still being copied
  song index, Plex sync, duplicates, track names, song descriptions (AI), playlists being made,
  Plex re-adds and checks, Plex collections/moods/reviews, NFO files, Plex refreshes,
  and Plex's own library scan (from its live notifications)

Each long-running module keeps its own progress state; this file only reads them. Short-lived
threads that had no state of their own report through track(). Tasks that just ended stay in the
list for a few minutes (done, or the error) so a finish isn't missed between two refreshes.
"""
import threading
import time
from contextlib import contextmanager

RECENT_SECS = 300           # how long a finished task stays listed
_adhoc = {}                 # key -> task dict, for threads without a module state (see track())
_live = {}                  # key -> last snapshot of each running task
_recent = []                # finished tasks, newest first
_lock = threading.Lock()


@contextmanager
def track(key, label, total=0, url=None, detail=""):
    """with track("nfo", "Writing NFO files", total=n) as t: ... t.step(); t.detail = "…"."""
    t = _Handle(key, label, total, url, detail)
    with _lock:
        _adhoc[key] = t
    try:
        yield t
    except Exception as e:
        t.error = str(e)
        raise
    finally:
        with _lock:
            _adhoc.pop(key, None)
            _errors_adhoc[key] = t.error


_errors_adhoc = {}


class _Handle:
    def __init__(self, key, label, total, url, detail):
        self.key, self.label, self.total, self.url, self.detail = key, label, total, url, detail
        self.done, self.started, self.error = 0, time.time(), None

    def step(self, n=1):
        self.done += n

    def as_task(self):
        return _task(self.key, self.label, self.detail, self.done, self.total, self.url, started=self.started)


def _task(key, label, detail="", done=0, total=0, url=None, sub="", state="running", started=None, eta=None):
    total = max(total or 0, done or 0)
    pct = round(100 * done / total) if total else None
    if eta is None and started and total and done >= 3 and done < total:
        eta = (time.time() - started) / done * (total - done)
    return {"key": key, "label": label, "detail": detail or "", "sub": sub or "", "done": done or 0,
            "total": total, "pct": pct, "url": url, "state": state, "eta": round(eta) if eta else None}


# ------------------------------------------------------------------ what each module is doing

def _queue(u):
    from . import db, worker
    by = {r["kind"]: r["n"] for r in db.q("SELECT kind, COUNT(*) n FROM jobs WHERE status IN ('pending','running') "
                                          "GROUP BY kind")}
    left = sum(by.values())
    cur = worker.current()
    if not left and not cur:
        return None
    b = worker.batch()
    paused = worker.paused()
    words = {"analyze": "to check", "apply": "to write", "undo": "to undo", "setartist": "album artist",
             "policy": "kind of album", "unmatched": "album tags"}
    sub = " · ".join(f"{n} {words.get(k, k)}" for k, n in sorted(by.items(), key=lambda kv: -kv[1]))
    eta = b["secs"] / b["done"] * left if b["done"] >= 3 and not paused else None
    return _task("queue", "Album queue", "Paused: nothing is checked or written until you resume" if paused
                 else (cur or "Starting the next album…"), b["done"], b["done"] + left, u("web.library", tab="queued"), sub,
                 "paused" if paused else "running", eta=eta)


def _watch(u):
    from . import watcher
    n = watcher.status().get("pending") or 0
    if not n:
        return None
    return _task("watch", "New music arriving", f"Waiting for {n} folder(s) to finish copying", url=u("web.scan"))


def _songs(u):
    from . import songs
    s = songs.status()
    if not s.get("running"):
        return None
    return _task("songs", s.get("what") or "Indexing songs", "Reading tags, folder by folder", s["done"], s["total"],
                 u("web.playlists_page"), started=s.get("started"))


def _dupes(u):
    from . import dupes
    s = dupes._state
    if not s.get("running"):
        return None
    return _task("dupes", "Duplicate check", s.get("phase") or "", s.get("done", 0), s.get("total", 0),
                 u("web.duplicates_page"), started=s.get("started"))


def _plexfix(u):
    from . import db, plexfix
    s = plexfix.status()
    out = []
    if s["current"] or s["queued"]:
        a = db.album_get(s["current"]) if s["current"] else None
        out.append(_task("plexreadd", "Re-adding albums in Plex",
                         f"{(a or {}).get('title') or 'Next album'} (a minute or two each)",
                         sub=f"{len(s['queued'])} more waiting" if s["queued"] else "", url=u("web.library", tab="plex")))
    return out              # "Check against Plex" shows as "Syncing with Plex" (worker._plex_sync)


def _plexmeta(u):
    from . import plexmeta
    s = plexmeta._state
    if not s.get("running"):
        return None
    return _task("plexmeta", "Plex " + (s.get("what") or "collections, moods and reviews"), "Album by album",
                 s.get("done", 0), s.get("total", 0), u("web.settings", tab="plex"), started=s.get("started"))


def _trackcheck(u):
    from . import trackcheck as TC
    out = []
    if TC._state.get("running"):
        out.append(_task("trackcheck", "Checking track names", "Looking for songs with another song's title",
                         TC._state.get("done", 0), TC._state.get("total", 0), u("web.tracks_page"),
                         started=TC._state.get("started")))
    if TC._ai.get("running"):
        out.append(_task("trackai", "AI check of track names", "", TC._ai.get("done", 0), TC._ai.get("total", 0),
                         u("web.tracks_page"), started=TC._ai.get("started")))
    return out


def _profiles(u):
    from . import playlists as PL
    s = PL._prof
    if not s.get("running"):
        return None
    return _task("profiles", "Describing songs (AI)", "Moods, type and tempo for each song",
                 s.get("done", 0), s.get("total", 0), u("web.playlists_page"), started=s.get("started"))


def _playlists(u):
    import json
    from . import db
    out = []
    for r in db.q("SELECT id, prompt, mode, progress FROM playlists WHERE status='working' ORDER BY id DESC LIMIT 3"):
        steps = json.loads(r["progress"] or "[]")
        what = (r["prompt"] or "").strip() or {"radio": "Radio", "song": "From a song"}.get(r["mode"], "Playlist")
        out.append(_task(f"playlist{r['id']}", "Making a playlist", steps[-1][1] if steps else "Starting…",
                         sub=f"“{what[:60]}”", url=u("web.playlist_view", pid=r["id"])))
    return out


def _plexscan(u):
    from . import plexlive
    sc = plexlive._state.get("scanning")
    if not sc or time.time() - (sc.get("at") or 0) > 600:     # no word from Plex for 10 minutes: missed its "ended"
        return None
    p = sc.get("progress")
    return _task("plexscan", "Plex: " + (sc.get("title") or "scanning"), sc.get("subtitle") or "",
                 int(p) if isinstance(p, (int, float)) and p >= 0 else 0, 100 if isinstance(p, (int, float)) and p >= 0 else 0)


PROVIDERS = (_queue, _watch, _songs, _dupes, _plexfix, _plexmeta, _trackcheck, _profiles, _playlists, _plexscan)


def _error_of(key):
    """The error a task ended with, if its module kept one."""
    from . import dupes, playlists as PL, plexmeta, songs, trackcheck as TC
    st = {"songs": songs._state, "dupes": dupes._state, "plexmeta": plexmeta._state, "trackcheck": TC._state,
          "trackai": TC._ai, "profiles": PL._prof}.get(key)
    if st is not None:
        return st.get("error")
    if key in _errors_adhoc:
        return _errors_adhoc.get(key)
    if key.startswith("playlist"):
        from . import db
        r = db.q("SELECT status, error FROM playlists WHERE id=?", (int(key[8:]),), one=True)
        return (r["error"] or "failed") if r and r["status"] == "error" else None
    return None


def snapshot(u=None):
    """{"running": [task…], "recent": [finished task…]}. u(endpoint, **args) builds links."""
    u = u or (lambda *a, **k: None)
    running = []
    for p in PROVIDERS:
        try:
            got = p(u)
        except Exception:  # noqa: BLE001  (a status read must never break the page)
            continue
        running += [t for t in (got if isinstance(got, list) else [got]) if t]
    with _lock:
        running += [h.as_task() for h in _adhoc.values()]
        now = time.time()
        keys = {t["key"] for t in running}
        for k, last in list(_live.items()):
            if k not in keys:
                err = None
                try:
                    err = _error_of(k)
                except Exception:  # noqa: BLE001
                    pass
                _recent.insert(0, {**last, "state": "error" if err else "done", "error": err, "finished": now,
                                   "done": last["total"] if not err and last["total"] else last["done"],
                                   "pct": 100 if not err and last["total"] else last["pct"]})
                del _live[k]
        for t in running:
            _live[t["key"]] = t
        _recent[:] = [r for r in _recent if now - r["finished"] < RECENT_SECS
                      and r["key"] not in keys][:6]
        recent = [{**r, "ago": round(now - r["finished"])} for r in _recent]
    return {"running": running, "recent": recent}
