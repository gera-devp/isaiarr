"""Single background worker: analyze → (human review) → apply / undo. Plus notifier loop."""
import os
import threading
import time
import traceback

from . import applier, db, dupes, matcher, notify, plex, songs, watcher
from .config import Config

_state = {"current": None}
_batch = {"done": 0, "secs": 0.0, "started": None}     # jobs finished since the queue was last empty


def current():
    return _state["current"]


def batch():
    """Progress of the current run of the queue: jobs done and the time they took."""
    return dict(_batch)


def paused():
    return bool(db.state_get("paused", False))


def set_paused(flag):
    db.state_set("paused", bool(flag))
    db.log("Worker paused" if flag else "Worker resumed")


SKIPPED = "skipped"


def skip(album_id):
    """Skip: the album's tags are already right. It leaves the review queue, nothing is written,
    and scans, the watcher and a library-wide reset pass it by. Re-check (or Send to review,
    Update only…) brings it back. Returns False while the album is being worked on."""
    a = db.album_get(album_id)
    if not a or a["status"] in ("analyzing", "applying"):
        return False
    db.x("DELETE FROM jobs WHERE album_id=? AND status='pending'", (album_id,))
    db.album_update(album_id, status=SKIPPED, auto=0)
    return True


def queue_album(path, origin="manual", ref=None, priority=0, scan_id=None, review=False, sent_from=None, no_auto=False,
                only=None, write=False, force=False):
    """review=True: you sent it back because its tags look wrong, so it lands in the review queue
    even if the files already match what the sources say (marked "Sent back").

    only: update just these parts (matcher.ONLY_PARTS), keeping the album it was matched to
    before. write=True: write straight away when the album passes the auto-approve rules."""
    path = os.path.normpath(path)
    before = db.album_by_path(path) or {}
    if before.get("status") == SKIPPED and priority < db.PRIORITY_USER and not (review or only or ref or force):
        return before["id"]        # you skipped it: only something you ask for on purpose checks it again
    aid = db.album_upsert(path, "queued", origin)
    db.album_update(aid, scan_id=scan_id)
    payload = {}
    if ref:
        payload["ref"] = ref
    only = matcher.clean_only(only)
    if only:
        payload["only"] = only
        prop = before.get("proposal") or {}
        if (before.get("collection_id") and not ref and not prop.get("collection")
                and before.get("status") in ("applied", "uptodate")):
            payload["keep"] = before["collection_id"]      # the album you approved, not a new search
        if write:
            payload["write"] = True
    if priority > 0 or review or no_auto:
        payload["no_auto"] = True     # you asked to look at it: never auto-write behind your back
    if review:
        payload["review"] = True
        db.album_update(aid, sent_back=time.time(), sent_from=(sent_from or "")[:200] or None)
    db.job_add("analyze", aid, payload or None, priority=priority)
    db.album_update(aid, auto=0)
    return aid


def default_apply_options():
    return {"rename": Config.RENAME_ENABLED, "artwork": Config.DEFAULT_ARTWORK, "lyrics": Config.DEFAULT_LYRICS}


def auto_decision(album, prop, payload):
    """(ok, reason). Every rule must pass for an album to be written without review."""
    if payload.get("write"):
        pass        # Library → Update only…, "write straight away": your request stands in for the switch
    elif Config.AUTO_APPROVE != "on":
        return False, "Auto-approve is off"
    elif payload.get("no_auto") or payload.get("ref"):
        return False, "Analyzed on request from the album page"
    elif album.get("origin") != "watch" and Config.AUTO_APPROVE_SCOPE != "all":
        return False, "Manual scans aren't auto-approved (Settings → Auto-approve → Applies to)"
    if prop.get("collection"):
        if not Config.AUTO_APPROVE_COLLECTIONS:
            return False, "Collections need review (can be changed in Settings)"
    elif prop.get("fallback"):
        return False, "No album matched"
    if prop["confidence"] < Config.AUTO_APPROVE_MIN:
        return False, f"Confidence {prop['confidence']:.1f} is below {Config.AUTO_APPROVE_MIN:.1f}"
    files = prop["files"]
    unmatched = [f["file"] for f in files if not f.get("match", "album" if f.get("itunes_track") else None)]
    if Config.AUTO_APPROVE_ALL_MATCHED and unmatched:
        return False, f"{len(unmatched)} file(s) not matched, e.g. {unmatched[0]}"
    weak = [f for f in files if f.get("match") and f["score"] < Config.AUTO_APPROVE_TRACK_MIN]
    if weak:
        w = min(weak, key=lambda f: f["score"])
        return False, f"“{w['file']}” matched at {w['score']:.1f}, below {Config.AUTO_APPROVE_TRACK_MIN:.1f}"
    heard = [f["file"] for f in files if (f.get("source_track") or {}).get("via") == "shazam"]
    if heard:
        return False, f"{len(heard)} song(s) identified by their sound, e.g. {heard[0]}: check them first"
    md = prop.get("md_check") or {}
    if md.get("status") == "differ":
        return False, (f"TMDB lists {', '.join(md.get('composers') or [])} as the music director of "
                       f"{md.get('film')}{' (' + md['year'] + ')' if md.get('year') else ''}")
    return True, "Passed every auto-approve rule"


def queue_folder(path, recursive=True, origin="manual", skip_applied=False, track=False):
    """Queue every album folder under `path`. skip_applied = 'only files with missing or wrong
    tags' (folders already applied, up to date or waiting are left out). track=True records a
    scan for the Scan page's progress panel; returns (count, scan_id) then."""
    dirs = matcher.find_album_dirs(path, recursive)
    todo = []
    for d in dirs:
        a = db.album_by_path(os.path.normpath(d))
        if a and a["status"] == SKIPPED:
            continue                         # you skipped it: scans pass it by
        if skip_applied:
            if a and a["status"] in ("applied", "uptodate", "review", "queued", "analyzing"):
                continue
        todo.append(d)
    scan_id = db.scan_create(path, "changed" if skip_applied else "all", len(todo)) if track else None
    for d in todo:
        queue_album(d, origin, scan_id=scan_id)
    if track and not todo:
        db.x("UPDATE scans SET finished=? WHERE id=?", (time.time(), scan_id))
    return (len(todo), scan_id) if track else len(todo)


def scan_progress(scan_id):
    """Counts for the Scan page: done / total and the three result tiles."""
    sc = db.scan_get(scan_id)
    if not sc:
        return None
    rows = db.q("SELECT status, COUNT(*) n FROM albums WHERE scan_id=? GROUP BY status", (scan_id,))
    by = {r["status"]: r["n"] for r in rows}
    waiting = by.get("queued", 0) + by.get("analyzing", 0)
    total = sc["total"] or sum(by.values())
    done = max(0, total - waiting)
    return {**sc, "done": done, "total": total, "pct": round(100 * done / total) if total else 100,
            "correct": by.get("uptodate", 0) + by.get("applied", 0),
            "review": by.get("review", 0) + by.get("applying", 0),
            "missed": by.get("nomatch", 0) + by.get("error", 0),
            "running": waiting > 0, "paused": paused()}


def _run(job):
    album = db.album_get(job["album_id"])
    if not album:
        return
    name = os.path.basename(album["path"])
    if job["kind"] == "analyze":
        _state["current"] = f"Analyzing {name}"
        db.album_update(album["id"], status="analyzing")
        if not os.path.isdir(album["path"]):
            db.album_update(album["id"], status="error", error="Folder no longer exists")
            return
        try:
            forced = job["payload"].get("ref") or job["payload"].get("collection_id")
            overrides = album.get("overrides") or {}
            if forced and overrides:
                # a different album was picked: its own name/year/genre replace the old edits,
                # but the music director you typed is kept
                overrides = {k: v for k, v in overrides.items() if k == "albumartist"}
                db.album_update(album["id"], overrides=overrides or None)
            only, keep = job["payload"].get("only"), job["payload"].get("keep")
            if keep and not forced:
                try:
                    prop, cands = matcher.analyze(album["path"], keep, overrides=overrides, only=only)
                except Exception as e:  # noqa: BLE001
                    db.log(f"{name}: couldn't look up the album it was matched to ({e}); searching again", "warn")
                    prop, cands = matcher.analyze(album["path"], None, overrides=overrides, only=only)
            else:
                prop, cands = matcher.analyze(album["path"], forced, overrides=overrides, only=only)
        except Exception as e:  # noqa: BLE001
            db.album_update(album["id"], status="error", error=str(e))
            db.log(f"Analyze failed for {name}: {e}", "error")
            return
        a = prop["album"]
        status = "nomatch" if prop["fallback"] and not prop.get("collection") else "review"
        stats = matcher.album_stats(prop)
        if status == "review" and not stats["n_fix"] and not stats["n_fill"] and not job["payload"].get("review"):
            status = "uptodate"            # nothing to write: keep it out of the review queue
        ok, why = auto_decision(album, prop, job["payload"]) if status != "uptodate" else (False, "Already correct")
        prop["auto"] = {"ok": ok, "why": why}
        upd = dict(status=status, confidence=prop["confidence"], collection_id=a["collection_id"],
                   title=a["album"], album_artist=a["album_artist"], proposal=prop, error=None, **stats)
        if status == "uptodate":
            upd["notified"] = 1
        if job["payload"].get("review"):
            upd["notified"] = 1            # you sent it: no "new album to review" notification
        if cands or not forced:
            upd["candidates"] = cands
        if ok:
            upd.update(status="applying", auto=1, notified=1)
        db.album_update(album["id"], **upd)
        if album.get("origin") == "watch":
            _reindex(album["path"])           # new music: index it and look for duplicates now
        if ok:
            db.job_add("apply", album["id"], {**default_apply_options(), "auto": True})
            db.log(f"Auto-approved {name} ({prop['confidence']})")
        else:
            db.log(f"Analyzed {name}: {status} ({prop['confidence']})")
    elif job["kind"] == "apply":
        _state["current"] = f"Applying {name}"
        db.album_update(album["id"], status="applying")
        watcher.suppress(album["path"])
        try:
            n, errors = applier.apply(album["id"], job["payload"])
        except Exception as e:  # noqa: BLE001
            db.album_update(album["id"], status="error", error=f"Apply failed: {e}")
            db.log(f"Apply failed for {name}: {e}\n{traceback.format_exc()}", "error")
            watcher.suppress(album["path"])
            return
        watcher.suppress(album["path"])
        for err in errors:
            db.log(f"{name}: {err}", "error")
        if errors and not n:
            db.album_update(album["id"], status="error",
                            error="Apply failed, nothing was changed:\n" + "\n".join(errors))
            return
        auto = bool(job["payload"].get("auto"))
        db.album_update(album["id"], status="applied", auto=1 if auto else 0,
                        notified=0 if Config.NOTIFY_APPLIED else 1,
                        error=(f"{len(errors)} file(s) could not be updated:\n" + "\n".join(errors))
                        if errors else None)
        _reindex(album["path"])
        try:
            from . import trackcheck
            bad = [r for r in trackcheck.check_album(album["id"]) if r["severity"] >= 2]
            if bad:
                db.log(f"{name}: {len(bad)} track(s) may have another song's title; see Library → Track names", "warn")
        except Exception as e:  # noqa: BLE001
            db.log(f"Track check for {name} failed: {e}", "error")
        if Config.PLEX_RESCAN:
            plex.refresh(album["path"])
        db.log(f"Applied {name}: {n} file(s) updated" + (f", {len(errors)} failed" if errors else ""))
    elif job["kind"] == "setartist":
        new = job["payload"].get("name", "")
        _state["current"] = f"Setting album artist of {name}"
        watcher.suppress(album["path"])
        try:
            n, errors = applier.set_album_artist(album["id"], new, job["payload"].get("composer", False),
                                                 job["payload"].get("only"))
        except Exception as e:  # noqa: BLE001
            db.album_update(album["id"], error=f"Album artist change failed: {e}")
            db.log(f"Album artist change failed for {name}: {e}", "error")
            return
        watcher.suppress(album["path"])
        for err in errors:
            db.log(f"{name}: {err}", "error")
        if errors:
            db.album_update(album["id"], error=f"{len(errors)} file(s) could not be updated:\n" + "\n".join(errors))
        _reindex(album["path"])
        if n and Config.PLEX_RESCAN:
            plex.refresh(album["path"])
        db.log(f"Album artist of {name} set to {new}: {n} file(s) updated")
        if n and album["status"] in ("review", "nomatch", "uptodate"):
            # refresh the proposal so it reflects the files as they are now
            queue_album(album["path"], origin=album["origin"] or "manual")
    elif job["kind"] == "policy":
        _state["current"] = f"Applying the folder policy to {name}"
        watcher.suppress(album["path"])
        try:
            n, errors = applier.apply_policy(album["id"])
        except Exception as e:  # noqa: BLE001
            db.album_update(album["id"], error=f"Policy not applied: {e}")
            db.log(f"Policy for {name} failed: {e}", "error")
            return
        watcher.suppress(album["path"])
        for err in errors:
            db.log(f"{name}: {err}", "error")
        if album["status"] not in ("applied", "uptodate"):
            db.album_update(album["id"], status="applied")
        _reindex(album["path"])
        if n and Config.PLEX_RESCAN:
            plex.refresh(album["path"])
    elif job["kind"] == "albumwide":
        _state["current"] = f"Giving {name}'s unmatched songs the album tags"
        watcher.suppress(album["path"])
        try:
            n, errors = applier.album_wide_fix(album["id"])
        except Exception as e:  # noqa: BLE001
            db.log(f"Album tags for unmatched songs failed for {name}: {e}", "error")
            return
        watcher.suppress(album["path"])
        for err in errors:
            db.log(f"{name}: {err}", "error")
        if n:
            _reindex(album["path"])
            if Config.PLEX_RESCAN:
                plex.refresh(album["path"])
            db.log(f"{name}: album tags written to {n} unmatched song(s)")
    elif job["kind"] == "undo":
        _state["current"] = f"Undoing {name}"
        watcher.suppress(album["path"])
        try:
            n = applier.undo(album["id"])
        except Exception as e:  # noqa: BLE001
            db.log(f"Undo failed for {name}: {e}", "error")
            db.album_update(album["id"], error=f"Undo failed: {e}")
            return
        watcher.suppress(album["path"])
        _reindex(album["path"])
        if Config.PLEX_RESCAN:
            plex.refresh(album["path"])
        db.log(f"Undid {name}: {n} file(s) restored")
        # re-analyze so the queue shows fresh state
        queue_album(album["path"], origin=album["origin"] or "manual", force=True)


def _reindex(folder):
    """Keep the playlist song index in step with what was just written."""
    try:
        songs.index_folder(folder)
        if Config.WRITE_NFO and db.album_by_path(folder):
            from . import nfo
            nfo.write_album(folder)          # the NFO follows the tags just written (before Plex rescans)
        if Config.DUPES_AUTO:
            dupes.start_scan(folders=[folder])
        if Config.AI_PROFILE_NEW:
            from . import playlists
            playlists.start_profiles()
    except Exception as e:  # noqa: BLE001
        db.log(f"Song index update failed for {folder}: {e}", "warn")


def _maintenance_loop():
    """First start: build the song index. Then every night around 03:00: re-index changed files
    and refresh Plex IDs and play counts."""
    time.sleep(20)
    if not db.q("SELECT 1 FROM songs LIMIT 1"):
        songs.index_all()
        _plex_sync()
        if Config.DUPES_AUTO:
            dupes.scan()
    while True:
        time.sleep(600)
        lt = time.localtime()
        last = (db.state_get("songs_index") or {}).get("at") or 0
        if lt.tm_hour == 3 and time.time() - last > 20 * 3600:
            songs.index_all()
            _plex_sync()
            if Config.DUPES_AUTO:
                dupes.scan()
            if Config.DUPES_BIN_DAYS:
                dupes.empty_bin(older_than_days=Config.DUPES_BIN_DAYS)
            if Config.AI_PROFILE_NEW:
                from . import playlists
                playlists.start_profiles()


def _plex_sync():
    if not plex.enabled():
        return
    from . import tasks
    try:
        with tasks.track("plexsync", "Syncing with Plex", detail="Reading every Plex track: IDs, ratings, play counts"):
            m, t = plex.sync_tracks()
        db.log(f"Plex sync: {m} of {t} Plex tracks matched to songs")
        if Config.PLEX_AUTO_MERGE:
            from . import plexfix
            plexfix.merge_duplicates(quiet=True)
        from . import plexmeta
        plexmeta.sync_all()                 # collections, reviews and moods that changed (background)
    except Exception as e:  # noqa: BLE001
        db.state_set("songs_plex", {"at": time.time(), "error": str(e)})
        db.log(f"Plex sync failed: {e}", "warn")


def _loop():
    while True:
        if paused():
            _state["current"] = None
            time.sleep(2)
            continue
        job = db.job_next()
        if not job:
            _state["current"] = None
            _batch.update(done=0, secs=0.0, started=None)
            time.sleep(2)
            continue
        t0 = time.time()
        _batch["started"] = _batch["started"] or t0
        try:
            _run(job)
            db.job_finish(job["id"])
        except Exception as e:  # noqa: BLE001
            db.job_finish(job["id"], str(e))
            db.log(f"Job {job['id']} crashed: {e}", "error")
        finally:
            _state["current"] = None
            _batch["done"] += 1
            _batch["secs"] += time.time() - t0


def _notify_loop():
    while True:
        time.sleep(30)
        try:
            notify.flush()
            _finish_scans()
        except Exception as e:  # noqa: BLE001
            db.log(f"Notifier error: {e}", "warn")


def _finish_scans():
    """Mark manual scans finished; optionally tell Discord when one ended with misses."""
    for sc in db.q("SELECT id FROM scans WHERE finished IS NULL"):
        p = scan_progress(sc["id"])
        if p and not p["running"]:
            db.x("UPDATE scans SET finished=? WHERE id=?", (time.time(), sc["id"]))
            db.log(f"Scan of {p['path']} finished: {p['correct']} correct, {p['review']} for review, "
                   f"{p['missed']} without a match")
            if Config.NOTIFY_SCAN_MISSES and p["missed"]:
                notify.scan_summary(p)


def start():
    db.jobs_recover()
    try:
        from . import plexfix
        plexfix.recover()                 # an album left hidden by an interrupted "Re-add in Plex"
    except Exception as e:  # noqa: BLE001
        db.log(f"Plex re-add recovery failed: {e}", "error")
    from . import plexlive
    plexlive.start()                      # live totals and Plex checks from Plex's notifications
    threading.Thread(target=_loop, name="worker", daemon=True).start()
    # always running: the webhook can be added later in Settings
    threading.Thread(target=_notify_loop, name="notifier", daemon=True).start()
    threading.Thread(target=_maintenance_loop, name="maintenance", daemon=True).start()
