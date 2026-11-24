"""Live link to Plex: Plex's own notification websocket (no Plex Pass needed).

Plex pushes a message whenever it adds, updates or deletes an item, and while it scans. Isaiarr
uses that to keep its Plex totals and each album's Plex check current within seconds, instead of
comparing once a night. If the websocket can't be opened, totals are refreshed every few minutes.
"""
import json
import threading
import time

from . import db, plex
from .config import Config

LIB = "com.plexapp.plugins.library"
STATE_NAMES = {0: "created", 1: "progress", 2: "matching", 3: "downloading", 4: "loading", 5: "processed", 9: "deleted"}

_state = {"connected": False, "mode": "off", "since": None, "last_event": None, "last_text": "", "scanning": None,
          "counts": None, "counts_at": None, "error": None}
_lock = threading.Lock()
_dirty = threading.Event()
_items = set()          # Plex item IDs processed since the last refresh
_started = False


def status():
    s = dict(_state)
    if s["counts"] is None:
        cached = db.state_get("plex_counts") or {}
        s["counts"], s["counts_at"] = cached.get("counts"), cached.get("at")
    return s


def start():
    """Start the listener and the refresher (once). Safe to call when Plex isn't set up."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_listen_loop, name="plex-live", daemon=True).start()
    threading.Thread(target=_refresh_loop, name="plex-live-refresh", daemon=True).start()


def _url():
    base = Config.PLEX_URL.rstrip("/")
    ws = "wss://" + base[8:] if base.startswith("https://") else "ws://" + base.split("://", 1)[-1]
    return f"{ws}/:/websockets/notifications?X-Plex-Token={Config.PLEX_TOKEN}"


def _listen_loop():
    delay = 5
    while True:
        if not plex.enabled():
            _state.update(connected=False, mode="off")
            time.sleep(30)
            continue
        try:
            import websocket          # websocket-client
        except ImportError:
            _state.update(connected=False, mode="polling", error="websocket-client isn't installed")
            time.sleep(300)
            continue
        try:
            ws = websocket.create_connection(_url(), timeout=30)
            _state.update(connected=True, mode="live", since=time.time(), error=None)
            db.log("Plex: live link connected")
            delay = 5
            _dirty.set()
            while True:
                try:
                    msg = ws.recv()
                except websocket.WebSocketTimeoutException:
                    ws.ping()
                    continue
                if not msg:
                    break
                handle(msg)
        except Exception as e:  # noqa: BLE001
            if _state.get("connected"):
                db.log(f"Plex: live link dropped ({e}); reconnecting", "warn")
            _state.update(connected=False, mode="polling", error=str(e))
        time.sleep(delay)
        delay = min(delay * 2, 300)


def handle(raw):
    """One message from Plex. Library changes mark the totals for a refresh and remember which
    items changed, so the albums they belong to get a fresh Plex check."""
    try:
        data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except ValueError:
        return
    nc = (data or {}).get("NotificationContainer") or {}
    kind = nc.get("type")
    if kind == "timeline":
        for e in nc.get("TimelineEntry") or []:
            if e.get("identifier") != LIB:
                continue
            st, typ = e.get("state"), e.get("type")
            if st in (5, 9) and typ in (8, 9, 10):
                _state["last_event"] = time.time()
                what = {8: "artist", 9: "album", 10: "track"}[typ]
                _state["last_text"] = f"{'Removed' if st == 9 else 'Updated'} {what}" + (f" “{e['title']}”" if e.get("title") else "")
                if st == 9 and typ == 10 and e.get("itemID"):
                    db.x("UPDATE songs SET plex_key=NULL WHERE plex_key=?", (str(e["itemID"]),))
                elif typ in (9, 10) and e.get("itemID"):
                    _items.add(str(e["itemID"]))
                _dirty.set()
    elif kind == "activity":
        for a in nc.get("ActivityNotification") or []:
            act = a.get("Activity") or {}
            if not str(act.get("type", "")).startswith("library."):
                continue
            if a.get("event") == "ended":
                _state["scanning"] = None
                _dirty.set()
            else:
                _state["scanning"] = {"title": act.get("title") or "Scanning", "subtitle": act.get("subtitle") or "",
                                      "progress": act.get("progress"), "at": time.time()}


def _refresh_loop():
    """Refresh Plex's totals after changes (a few seconds after the last one), or every 5 minutes
    without the live link; and re-check the albums whose tracks Plex just processed."""
    while True:
        triggered = _dirty.wait(timeout=300)
        if triggered:
            time.sleep(Config.PLEX_LIVE_SETTLE)        # let a burst of changes finish
            _dirty.clear()
        if not plex.enabled():
            continue
        try:
            c = plex.counts()
            _state.update(counts=c, counts_at=time.time())
            db.state_set("plex_counts", {"counts": c, "at": time.time()})
        except Exception as e:  # noqa: BLE001
            _state["error"] = str(e)
            continue
        items = list(_items)
        _items.clear()
        if items:
            recheck_items(items)


def recheck_items(item_ids):
    """Plex just processed these items: re-check the albums (folders) they belong to."""
    folders = set()
    for iid in item_ids[:200]:
        try:
            root = plex._req("GET", f"/library/metadata/{iid}")
            if root is not None and not root.findall("Track") and root.findall("Directory"):
                root = plex._req("GET", f"/library/metadata/{iid}/children")      # an album: its tracks
        except Exception:  # noqa: BLE001
            continue
        for t in (root.iter("Track") if root is not None else []):
            part = t.find("Media/Part")
            f = part.get("file") if part is not None else ""
            if f:
                key = plex._key(f, 3)
                for r in db.q("SELECT folder, path FROM songs WHERE path LIKE ?", ("%" + f.rsplit("/", 1)[-1],)):
                    if plex._key(r["path"], 3) == key:
                        folders.add(r["folder"])
    for folder in sorted(folders)[:50]:
        if db.album_by_path(folder):
            try:
                plex.check_folder(folder)
            except Exception:  # noqa: BLE001
                pass
