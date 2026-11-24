"""Detect new music in MUSIC_DIR and queue album folders once copying has finished.

poll mode (default): walks the library every POLL_INTERVAL seconds comparing file names per
folder against a saved snapshot - reliable on Synology bind mounts and for SMB copies.
inotify mode: uses watchdog events (faster, but inotify can miss events on some NAS mounts).
A folder is queued only after SETTLE_SECONDS with no new files and unchanged file sizes.
"""
import json
import os
import threading
import time

from . import db
from .config import Config

STATE_FILE = lambda: os.path.join(Config.DATA_DIR, "watch_state.json")  # noqa: E731

_lock = threading.Lock()
_pending = {}        # dir -> {"last": ts, "sizes": {...}}
_suppress = {}       # dir -> ts until which our own changes are ignored
_snapshot = {}       # dir -> sorted list of audio file names
RECHECK = 15  # seconds between size checks once a folder looks quiet
_status = {"mode": None, "last_poll": None, "running": False}


def status():
    with _lock:
        return {**_status, "pending": len(_pending), "folders_known": len(_snapshot)}


def _audio_names(d):
    try:
        return sorted(f for f in os.listdir(d)
                      if f.lower().endswith(Config.AUDIO_EXTS) and not f.startswith("."))
    except OSError:
        return []


def _sizes(d):
    out = {}
    for f in _audio_names(d):
        try:
            out[f] = os.path.getsize(os.path.join(d, f))
        except OSError:
            pass
    return out


def suppress(d, seconds=None):
    """Called after we write/rename files so our own changes don't requeue the album."""
    with _lock:
        _suppress[d] = time.time() + (seconds or Config.SETTLE_SECONDS + 120)
        _snapshot[d] = _audio_names(d)
        _pending.pop(d, None)
    _save_state()


def mark(d):
    with _lock:
        if _suppress.get(d, 0) > time.time():
            return
        _pending[d] = {"last": time.time(), "sizes": None}


def _walk_snapshot():
    snap = {}
    for d, dirs, files in os.walk(Config.MUSIC_DIR):
        dirs[:] = [x for x in dirs if not x.startswith((".", "@"))]
        names = sorted(f for f in files if f.lower().endswith(Config.AUDIO_EXTS) and not f.startswith("."))
        if names:
            snap[d] = names
    return snap


def _save_state():
    try:
        with _lock:
            data = dict(_snapshot)
        tmp = STATE_FILE() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, STATE_FILE())
    except OSError as e:
        db.log(f"Watcher state save failed: {e}", "warn")


def _load_state():
    try:
        with open(STATE_FILE(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _diff(old, new):
    changed = []
    for d, names in new.items():
        prev = set(old.get(d, []))
        if set(names) - prev:
            changed.append(d)
    return changed


def poll_once():
    new = _walk_snapshot()
    with _lock:
        old = dict(_snapshot)
    for d in _diff(old, new):
        mark(d)
    with _lock:
        _snapshot.clear()
        _snapshot.update(new)
        _status["last_poll"] = time.time()
    _save_state()


def settle_once():
    """Queue folders that have been quiet for SETTLE_SECONDS with stable sizes."""
    from .worker import queue_album
    now = time.time()
    ready = []
    with _lock:
        items = list(_pending.items())
    for d, st in items:
        if now - st["last"] < Config.SETTLE_SECONDS:
            continue
        sizes = _sizes(d)
        if not sizes:
            with _lock:
                _pending.pop(d, None)
            continue
        if st["sizes"] != sizes:  # still growing (or first check) - wait one more round
            with _lock:
                if d in _pending:
                    _pending[d] = {"last": now - Config.SETTLE_SECONDS + RECHECK, "sizes": sizes}
            continue
        ready.append(d)
    for d in ready:
        with _lock:
            _pending.pop(d, None)
        a = db.album_by_path(os.path.normpath(d))
        if a and a["status"] == "skipped":
            db.log(f"Files changed in {d}, which you skipped: left alone (Re-check it to look at it again)")
            continue
        queue_album(d, origin="watch")
        db.log(f"New music detected: {d}")


def _start_inotify():
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    class H(FileSystemEventHandler):
        def on_any_event(self, ev):
            if ev.is_directory or ev.event_type not in ("created", "moved", "modified", "closed"):
                return
            p = getattr(ev, "dest_path", None) or ev.src_path
            if p.lower().endswith(Config.AUDIO_EXTS):
                mark(os.path.dirname(p))

    obs = Observer()
    obs.schedule(H(), Config.MUSIC_DIR, recursive=True)
    obs.daemon = True
    obs.start()
    return obs


def start():
    """The loop always runs; the Watching switch (Scan page) takes effect within seconds."""
    _status["running"] = True

    def baseline():
        prev = _load_state()
        new = _walk_snapshot()
        with _lock:
            _snapshot.update(new)
        if prev is not None:  # catch files added while the container was down / watching was off
            for d in _diff(prev, new):
                mark(d)
        _save_state()
        db.log(f"Watcher ready ({Config.WATCH_MODE}): {len(new)} album folders in library")

    def loop():
        ready, last_poll, first = False, 0.0, True
        while True:
            if not first:
                time.sleep(10)
            first = False
            try:
                if not Config.WATCH_ENABLED:
                    _status["mode"] = "off"
                    continue
                if not ready:
                    baseline()
                    if Config.WATCH_MODE == "inotify":
                        try:
                            _start_inotify()
                        except Exception as e:  # noqa: BLE001
                            db.log(f"inotify unavailable ({e}); falling back to polling", "warn")
                            Config.WATCH_MODE = "poll"
                    ready, last_poll = True, time.time()
                _status["mode"] = Config.WATCH_MODE
                if Config.WATCH_MODE == "poll" and time.time() - last_poll >= Config.POLL_INTERVAL:
                    poll_once()
                    last_poll = time.time()
                settle_once()
            except Exception as e:  # noqa: BLE001
                db.log(f"Watcher error: {e}", "error")

    threading.Thread(target=loop, name="watcher", daemon=True).start()
