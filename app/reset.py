"""Start the tag analysis over: Isaiarr forgets what it worked out and what you decided, and
re-checks the folders from the tags the files have now. Files are never touched.

Kept: the album rows' ids and their backups (so Undo on an album still works), the song index,
duplicates, playlists, settings and the lookup cache.
Forgotten: proposals and matches, your album edits, folder decisions, "Leave as is" marks,
track-name checks and "It's fine" marks, Plex/read-back check results.
Every folder is then queued for a fresh analysis that never writes on its own: albums with
something to change wait in the review queue.

Albums you skipped are left out of a library-wide reset (they stay skipped); resetting chosen
folders includes them.
"""
import os

from . import db, folders as FO
from .config import Config

BUSY = ("analyzing", "applying")
CLEAR = {"proposal": None, "candidates": None, "error": None, "confidence": None, "collection_id": None,
         "overrides": None, "verify": None, "plex_check": None, "sent_back": None, "sent_from": None,
         "n_fix": None, "n_fill": None, "n_files": None, "various": 0, "auto": 0, "notified": 0}


def preview(folders=None):
    """What a reset would cover: {albums, folders, decisions, edits, busy}."""
    targets = _targets(folders)
    rows = _album_rows(targets)
    return {"folders": len(targets), "albums": len(rows),
            "decisions": sum(1 for f in targets if db.q("SELECT 1 FROM folder_policy WHERE folder=?", (f,), one=True)),
            "edits": sum(1 for r in rows if r["overrides"] and r["overrides"] not in ("{}", "null")),
            "busy": sum(1 for r in rows if r["status"] in BUSY)}


def _targets(folders):
    if folders is None:
        return sorted(set(FO._album_folders()) | {r["path"] for r in db.q("SELECT path FROM albums")})
    return sorted({os.path.normpath(f) for f in folders})


def _album_rows(targets):
    t = set(targets)
    return [dict(r) for r in db.q("SELECT id, path, status, overrides FROM albums") if r["path"] in t]


def reset(folders=None):
    """folders=None: the whole library. Returns {"reset": n, "queued": n, "skipped": n}."""
    from . import worker
    targets = _targets(folders)
    rows = {r["path"]: r for r in _album_rows(targets)}
    whole = folders is None
    skipped = queued = n = 0
    cols = {r[1] for r in db.q("PRAGMA table_info(albums)")}
    clear = {k: v for k, v in CLEAR.items() if k in cols}
    sets = ", ".join(f"{k}=?" for k in clear)
    kept = 0
    for f in targets:
        r = rows.get(f)
        if whole and r and r["status"] == "skipped":
            kept += 1
            continue
        if r and r["status"] in BUSY:
            skipped += 1                     # being worked on right now: leave it alone
            continue
        if r:
            db.x("DELETE FROM jobs WHERE album_id=? AND status='pending'", (r["id"],))
            db.x(f"UPDATE albums SET {sets} WHERE id=?", (*clear.values(), r["id"]))
            n += 1
        db.x("DELETE FROM folder_policy WHERE folder=?", (f,))
        db.x("DELETE FROM track_check WHERE folder=?", (f,))
        if os.path.isdir(f):
            worker.queue_album(f, origin="manual", no_auto=True, force=True)
            queued += 1
        elif r:
            db.album_update(r["id"], status="error", error="Folder not found when the analysis was reset")
    if whole:
        db.state_set("policy_dismissed", {"folders": []})
        db.state_set("artist_ignored", {"keys": []})
        db.state_set("trackcheck", None)
    else:
        keep = set(targets)
        d = (db.state_get("policy_dismissed") or {}).get("folders", [])
        db.state_set("policy_dismissed", {"folders": [x for x in d if x not in keep]})
    what = "the whole library" if whole else f"{len(targets)} folder(s)"
    db.log(f"Analysis reset for {what}: {n} album(s) forgotten, {queued} queued for a fresh check"
           + (f", {skipped} skipped (being worked on)" if skipped else "")
           + (f", {kept} album(s) you skipped left alone" if kept else "") + ". Files weren't changed; Undo history kept.")
    return {"reset": n, "queued": queued, "skipped": skipped, "kept": kept}
