"""Apply an approved proposal to disk (with backups) and undo it."""
import json
import os
import time

from . import db, tags as T
from .config import Config
from .matcher import ALBUM_WIDE, OVERRIDE_KEYS, compute_changes, new_filename, only_allows, only_fields, resolve_artist
from .sources import itunes


def _lrc_path(audio_path):
    return os.path.splitext(audio_path)[0] + ".lrc"


def _album_value(album, k):
    return {"album": album.get("album"), "albumartist": album.get("album_artist"),
            "date": album.get("release_date") or album.get("date"), "genre": album.get("genre"),
            "label": album.get("label")}.get(k, "") or ""


def apply(album_id, options):
    """options: {
        files: {idx: {"include": bool, "new": {field: value}}},   # edits from the review page
        album: {field: value},                                     # album-level edits
        rename: bool, artwork: 'missing'|'all'|'none', lyrics: 'missing'|'all'|'none'
    }"""
    album = db.album_get(album_id)
    prop = album["proposal"]
    if not prop:
        raise ValueError("Nothing to apply")
    rename = options.get("rename", Config.RENAME_ENABLED)
    art_mode = options.get("artwork", "missing")
    lyr_mode = options.get("lyrics", "missing")
    only = prop.get("only")                  # Library → Update only…: just these parts
    if only:
        rename = rename and only_allows(only, "rename")
        art_mode = art_mode if only_allows(only, "artwork") else "none"
        lyr_mode = lyr_mode if only_allows(only, "lyrics") else "none"
    of = only_fields(only)
    # only album-level values the user actually changed on the review page count as edits
    album_edits = {k: v for k, v in (options.get("album") or {}).items()
                   if v is not None and str(v) != str(_album_value(prop["album"], k))}
    file_opts = options.get("files") or {}

    batch = int(time.time())
    bdir = os.path.join(Config.DATA_DIR, "backups", str(album_id), str(batch))
    artwork = None
    if art_mode != "none" and prop["album"].get("artwork"):
        needs = art_mode == "all" or any(not f["has_art"] for f in prop["files"])
        if needs:
            artwork = itunes.fetch_artwork(prop["album"]["artwork"])

    wide = album_wide_tags(album)
    done, errors, failed = 0, [], []
    for idx, fp in enumerate(prop["files"]):
        fo = file_opts.get(str(idx), {})
        matched = fp.get("match", "album" if fp.get("itunes_track") else None)
        album_only = False
        if not fo.get("include", matched is not None):
            if matched is not None or not Config.ALBUM_TAGS_UNMATCHED:
                continue                     # a matched file you unticked: left exactly as it is
            album_only = True                # unmatched: only the album-wide tags (album, album artist…)
        path = fp["path"]
        if not os.path.exists(path):
            db.log(f"Skipped missing file {path}", "warn")
            continue
        current = T.read(path)["fields"]
        new = {**wide, **fp["new"]} if matched is None else dict(fp["new"])
        forced = set()
        for k, v in (fo.get("new") or {}).items():
            if v is None:
                continue
            shown = str(fp["new"].get(k) or fp["current"].get(k) or "")
            if str(v) != shown:
                forced.add(k)            # typed in by hand on the review page
            new[k] = v
        for k, v in album_edits.items():  # album-level edits win
            if k in ("album", "albumartist", "date", "genre", "label"):
                new[k] = v
                forced.add(k)
                if k == "albumartist" and Config.WRITE_COMPOSER:
                    if v and v != Config.VARIOUS_ARTISTS:
                        new["composer"] = v
                    else:
                        new.pop("composer", None)
                if (k == "albumartist" and Config.ARTIST_MODE in ("md", "md_first") and "artist" not in forced
                        and fp.get("source_track")):
                    # the music director is part of Artist in these modes: follow the edit
                    new["artist"] = resolve_artist(fp["source_track"]["artist"], v)
                    forced.add("artist")
        if album_only:
            new = {k: v for k, v in new.items() if k in ALBUM_WIDE}
        changes = compute_changes(current, new, forced, only=of)
        fields = {k: new[k] for k in changes}
        if album_only:
            if not fields:
                continue
            try:
                backup_file = T.backup(path, os.path.join(bdir, f"{idx:04d}"))
                T.write(path, fields)
                failed += readback(path, fields)
                db.x("INSERT INTO backups(album_id,batch,old_path,new_path,backup_file,extra_files,created)"
                     " VALUES(?,?,?,?,?,?,?)", (album_id, batch, path, path, backup_file,
                                                json.dumps({"created": [], "renamed": [], "lrc_backup": None}), time.time()))
                done += 1
            except Exception as e:  # noqa: BLE001
                errors.append(f"{fp['file']}: album tags not written ({e})")
            continue

        write_art = artwork if (artwork and (art_mode == "all" or not fp["has_art"])) else None
        lyrics = None
        if lyr_mode != "none" and fp.get("lyrics") and Config.EMBED_LYRICS:
            if lyr_mode == "all" or not fp["has_lyrics"]:
                lyrics = fp["lyrics"]
        lrc_old = _lrc_path(path)
        write_lrc = (Config.WRITE_LRC and lyr_mode != "none" and fp.get("synced")
                     and (lyr_mode == "all" or not os.path.exists(lrc_old)))

        merged = {**current, **fields}
        target_name = new_filename(fp, merged) if rename else fp["file"]
        target = os.path.join(os.path.dirname(path), target_name)
        if target != path and os.path.exists(target):
            target = path  # never clobber another file

        if not fields and not write_art and lyrics is None and not write_lrc and target == path:
            continue

        # One bad file must not stop the album: back up first (nothing changed if that fails),
        # and if a later step fails, put the original tags back and carry on with the next file.
        try:
            backup_file = T.backup(path, os.path.join(bdir, f"{idx:04d}"))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{fp['file']}: could not back up tags ({e}) - left unchanged")
            continue
        extra = {"created": [], "renamed": [], "lrc_backup": None}
        try:
            T.write(path, fields, artwork=write_art, lyrics=lyrics)

            if write_lrc:
                if os.path.exists(lrc_old):
                    bk = os.path.join(bdir, f"{idx:04d}.lrc")
                    with open(lrc_old, "rb") as src, open(bk, "wb") as dst:
                        dst.write(src.read())
                    extra["lrc_backup"] = [lrc_old, bk]
                else:
                    extra["created"].append(lrc_old)
                with open(lrc_old, "w", encoding="utf-8") as fh:
                    fh.write(fp["synced"].rstrip() + "\n")

            if target != path:
                os.rename(path, target)
                if os.path.exists(lrc_old):
                    lrc_new = _lrc_path(target)
                    if not os.path.exists(lrc_new):
                        os.rename(lrc_old, lrc_new)
                        extra["renamed"].append([lrc_old, lrc_new])
                        extra["created"] = [lrc_new if c == lrc_old else c for c in extra["created"]]
        except Exception as e:  # noqa: BLE001
            try:
                T.restore(path if os.path.exists(path) else target, backup_file)
                for c in extra["created"]:
                    if os.path.exists(c):
                        os.remove(c)
                if extra["lrc_backup"]:
                    orig, bk = extra["lrc_backup"]
                    with open(bk, "rb") as src, open(orig, "wb") as dst:
                        dst.write(src.read())
                errors.append(f"{fp['file']}: {e} - original tags restored")
            except Exception as e2:  # noqa: BLE001
                errors.append(f"{fp['file']}: {e}; restoring also failed ({e2}) - backup kept at {backup_file}")
            continue
        db.x("INSERT INTO backups(album_id,batch,old_path,new_path,backup_file,extra_files,created)"
             " VALUES(?,?,?,?,?,?,?)",
             (album_id, batch, path, target, backup_file, json.dumps(extra), time.time()))
        failed += readback(target, fields)
        done += 1
    if album_edits and done:
        _remember_edits(album, prop, album_edits)
    save_check(album_id, done, failed)
    return done, errors


def _remember_edits(album, prop, edits):
    """Keep album-level edits so the album page shows them and a later re-check of the folder
    doesn't propose putting the source's values back."""
    saved = {**(album.get("overrides") or {}), **{k: v for k, v in edits.items() if k in OVERRIDE_KEYS}}
    for k, v in edits.items():
        if k in OVERRIDE_KEYS:
            if k == "date":
                from .sources.http import full_date
                prop["album"]["release_date"] = full_date(v)
                v = str(v)[:4]
            prop["album"][OVERRIDE_KEYS[k]] = v
    prop["overrides"] = saved
    upd = {"overrides": saved, "proposal": prop}
    if "album" in edits:
        upd["title"] = edits["album"]
    if "albumartist" in edits:
        upd["album_artist"] = edits["albumartist"]
    db.album_update(album["id"], **upd)


# ---------------------------------------------------------------- read-back check

def _norm(k, v):
    v = str(v or "").strip()
    if k == "compilation":
        return "1" if v.lower() in ("1", "true", "yes") else ""
    if k in ("tracknumber", "tracktotal", "discnumber", "disctotal") and v.isdigit():
        return str(int(v))
    if k == "date":
        return v[:4]
    return v


def readback(path, fields):
    """Re-read a file after writing and list every tag that isn't what was written."""
    try:
        got = T.read(path)["fields"]
    except Exception as e:  # noqa: BLE001
        return [{"file": os.path.basename(path), "field": "file", "want": "", "got": f"unreadable after writing: {e}"}]
    return [{"file": os.path.basename(path), "field": k, "want": str(v), "got": got.get(k, "")}
            for k, v in fields.items() if k in T.FIELDS and _norm(k, v) != _norm(k, got.get(k))]


def save_check(album_id, written, failed):
    """Remember the last read-back check on the album: shown as ✓ Files or what didn't stick."""
    db.album_update(album_id, verify={"at": time.time(), "files": written, "n_failed": len(failed),
                                      "failed": failed[:50]})
    if failed:
        db.log(f"{len(failed)} tag(s) didn't stick after writing (e.g. {failed[0]['file']}: {failed[0]['field']})", "warn")


def album_wide_tags(album):
    """The album's own tags (album, album artist, genre, year/compilation) for songs no source
    matched, worked out from the album as it is now, so it works for reviews made before 2.11 too."""
    from .matcher import _album_wide
    prop = album.get("proposal") or {}
    if not prop.get("album"):
        return {}
    al = {**prop["album"]}
    if prop.get("fallback") and not prop.get("collection"):
        al["fallback"] = True
    if prop.get("collection"):
        al["collection"] = True
    wide = _album_wide(al)
    if wide:
        for k, v in (album.get("overrides") or {}).items():
            if k in ("album", "albumartist", "genre", "label") and v:
                wide[k] = v
    return wide


def write_folder(album_id, plan, label):
    """The one write path for album-wide changes. plan(path, current_fields) -> {field: value}
    for each audio file in the album's folder (empty: leave it). Backs up, writes, reads every
    file back to check, and records a batch that Undo on the album page reverses.
    Returns (files_changed, errors)."""
    album = db.album_get(album_id)
    folder = album["path"]
    if not os.path.isdir(folder):
        raise ValueError("Folder no longer exists")
    batch = int(time.time())
    bdir = os.path.join(Config.DATA_DIR, "backups", str(album_id), str(batch))
    done, errors, failed = 0, [], []
    for idx, fn in enumerate(sorted(os.listdir(folder))):
        path = os.path.join(folder, fn)
        if fn.startswith(".") or not fn.lower().endswith(Config.AUDIO_EXTS) or not os.path.isfile(path):
            continue
        try:
            cur = T.read(path)["fields"]
        except Exception as e:  # noqa: BLE001
            errors.append(f"{fn}: unreadable ({e})")
            continue
        fields = plan(path, cur) or {}
        fields = {k: v for k, v in fields.items() if k not in Config.PROTECTED_FIELDS}
        if not fields:
            continue
        try:
            backup_file = T.backup(path, os.path.join(bdir, f"{idx:04d}"))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{fn}: could not back up tags ({e}) - left unchanged")
            continue
        try:
            T.write(path, fields)
        except Exception as e:  # noqa: BLE001
            try:
                T.restore(path, backup_file)
                errors.append(f"{fn}: {e} - original tags restored")
            except Exception as e2:  # noqa: BLE001
                errors.append(f"{fn}: {e}; restoring also failed ({e2}) - backup kept at {backup_file}")
            continue
        db.x("INSERT INTO backups(album_id,batch,old_path,new_path,backup_file,extra_files,created)"
             " VALUES(?,?,?,?,?,?,?)",
             (album_id, batch, path, path, backup_file, json.dumps({"created": [], "renamed": []}), time.time()))
        failed += readback(path, fields)
        done += 1
    save_check(album_id, done, failed)
    if done:
        db.log(f"{label}: {done} file(s) in {os.path.basename(folder)}")
    return done, errors


def _changes(cur, want):
    return {k: want[k] for k in compute_changes(cur, want, forced=set(want))}


def album_wide_fix(album_id):
    """For albums tagged before unmatched songs got the album's tags: write album, album artist,
    genre (and year) to the songs no source matched, straight from the stored proposal."""
    album = db.album_get(album_id)
    prop = album.get("proposal") or {}
    wide = album_wide_tags(album)
    if not wide:
        return 0, []
    unmatched = {fp["path"] for fp in prop.get("files", [])
                 if fp.get("match", "album" if fp.get("itunes_track") else None) is None}
    return write_folder(album_id, lambda path, cur: _changes(cur, wide) if path in unmatched else {},
                        "Album tags for unmatched songs")


def apply_policy(album_id):
    """Write the folder's policy (album artist, album, compilation, release type) to every file in
    it, matched or not. Remembered like an edit, so re-checks keep it."""
    from . import policy as P
    album = db.album_get(album_id)
    pol = P.get(album["path"])
    if not pol:
        raise ValueError("No policy for this folder")
    al = ((album.get("proposal") or {}).get("album")) or {"album_artist": album.get("album_artist"),
                                                           "album": album.get("title")}
    want = P.album_tags(pol, al, album["path"])
    if Config.WRITE_COMPOSER and pol["policy"] in ("film", "artist") and want.get("albumartist") \
            and want["albumartist"] != Config.VARIOUS_ARTISTS:
        want["composer"] = want["albumartist"]
    n, errors = write_folder(album_id, lambda path, cur: _changes(cur, want), f"Policy “{P.LABEL[pol['policy']]}”")
    upd = {}
    prop = album.get("proposal")
    if prop:                       # keep the stored plan in step, so a later Approve can't write the old values back
        for fp in prop.get("files", []):
            nw = fp.setdefault("new", {})
            for k, v in want.items():
                if k in nw or v:
                    nw[k] = v
        if want.get("albumartist"):
            prop.setdefault("album", {})["album_artist"] = want["albumartist"]
        if want.get("album"):
            prop.setdefault("album", {})["album"] = want["album"]
        upd["proposal"] = prop
    if want.get("albumartist"):
        upd["album_artist"] = want["albumartist"]
    if want.get("album"):
        upd["title"] = want["album"]
    if upd:
        db.album_update(album_id, **upd)
    return n, errors


def set_album_artist(album_id, name, composer=False, only=None):
    """Batch rename: write only Album Artist (and Composer if asked) to every audio file in the
    album's folder. Nothing else changes, no files are renamed. Undo on the album page puts the
    old values back. only: change just the files whose Album Artist is one of these."""
    name = (name or "").strip()
    if not name:
        raise ValueError("No album artist given")

    def plan(path, cur):
        if only is not None and (cur.get("albumartist") or "") not in only:
            return {}
        fields = {}
        if cur.get("albumartist") != name:
            fields["albumartist"] = name
        if composer:
            want = name if name != Config.VARIOUS_ARTISTS else ""
            if (cur.get("composer") or "") != want:
                fields["composer"] = want
        return fields
    n, errors = write_folder(album_id, plan, f"Album artist set to {name}")
    _remember_after_artist(album_id, name, composer)
    return n, errors


def _remember_after_artist(album_id, name, composer):
    """Remember it like an edit on the album page, so a later re-check keeps it."""
    album = db.album_get(album_id)
    saved = {**(album.get("overrides") or {}), "albumartist": name}
    upd = {"overrides": saved, "album_artist": name}
    prop = album.get("proposal")
    if prop:
        prop["album"]["album_artist"] = name
        prop["overrides"] = saved
        for fp in prop.get("files", []):      # a later Approve must not write the old name back
            if fp.get("new", {}).get("albumartist"):
                fp["new"]["albumartist"] = name
            if composer and fp.get("new", {}).get("composer"):
                fp["new"]["composer"] = name if name != Config.VARIOUS_ARTISTS else ""
        upd["proposal"] = prop
    db.album_update(album_id, **upd)

def undo(album_id):
    row = db.q("SELECT MAX(batch) b FROM backups WHERE album_id=? AND restored=0", (album_id,), one=True)
    if not row or row["b"] is None:
        raise ValueError("No changes to undo")
    batch = row["b"]
    rows = db.q("SELECT * FROM backups WHERE album_id=? AND batch=?", (album_id, batch))
    n = 0
    for r in rows:
        extra = json.loads(r["extra_files"] or "{}")
        cur = r["new_path"]
        if os.path.exists(cur) and cur != r["old_path"] and not os.path.exists(r["old_path"]):
            os.rename(cur, r["old_path"])
            cur = r["old_path"]
        created = set(extra.get("created", []))
        for c in created:  # sidecars we created (at their final name) go away
            if os.path.exists(c):
                os.remove(c)
        for old, new in extra.get("renamed", []):
            if new not in created and os.path.exists(new) and not os.path.exists(old):
                os.rename(new, old)
        if extra.get("lrc_backup"):
            orig, bk = extra["lrc_backup"]
            with open(bk, "rb") as src, open(orig, "wb") as dst:
                dst.write(src.read())
        if os.path.exists(cur):
            T.restore(cur, r["backup_file"])
            n += 1
    db.x("UPDATE backups SET restored=1 WHERE album_id=? AND batch=?", (album_id, batch))
    return n
