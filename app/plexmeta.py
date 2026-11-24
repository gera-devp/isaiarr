"""Album details that live only in Plex, set through its API: collections, the review and moods.

  collections  by kind of album (Film soundtracks, Compilations, Singles, Non-film albums) and by
               decade (1980s, 1990s…), worked out from the tags Isaiarr wrote
  review       the soundtrack paragraph of the film's English Wikipedia page, with the credit its
               licence asks for (CC BY-SA)
  moods        the moods most of the album's songs share, from the AI song descriptions
               (AI & playlists → Describe songs)

These are stored in Plex, not in the files, so they're set again after every write, every night
for anything that changed, and on request (Settings → Plex). Isaiarr only manages its own names:
collections and moods you add yourself in Plex stay, and a review you wrote in Plex (or one from
Plex's own agent) is never replaced. The fields are locked so a Plex refresh doesn't clear them,
and Isaiarr's "unlock before refresh" step leaves them alone.

Library → Update only… sets just the parts you pick (say collections and moods) for the albums
you select, now; the others are left as Plex has them.
"""
import hashlib
import json
import re
import threading
import time
from collections import Counter

import requests

from . import db, plex
from .config import Config

KINDS = {"soundtrack": "Film soundtracks", "compilation": "Compilations", "single": "Singles",
         "album": "Non-film albums"}
DECADE_RE = re.compile(r"^(19|20)\d0s$")
PLEX_PARTS = {"collections": "Collections", "moods": "Moods", "review": "Review"}
_state = {"running": False, "done": 0, "total": 0, "error": None, "started": None, "what": ""}
_lock = threading.Lock()


def any_on():
    return any((Config.PLEX_COLLECTIONS_KIND, Config.PLEX_COLLECTIONS_DECADE, Config.PLEX_REVIEW, Config.PLEX_MOODS))


def enabled():
    """Plex is set up. Runs even with every option off, so values Isaiarr set earlier are taken out."""
    return plex.enabled()


def part_on(part):
    """This part is switched on in Settings → Plex."""
    return {"collections": Config.PLEX_COLLECTIONS_KIND or Config.PLEX_COLLECTIONS_DECADE,
            "moods": Config.PLEX_MOODS, "review": Config.PLEX_REVIEW}.get(part, False)


def _managed_collection(name):
    return name in KINDS.values() or bool(DECADE_RE.match(name or ""))


def _managed_mood(name):
    from .playlists import MOODS
    return (name or "").lower() in MOODS


def _common(rows, key):
    c = Counter((r.get(key) or "") for r in rows if r.get(key))
    return c.most_common(1)[0][0] if c else ""


def _kind(releasetype):
    rt = (releasetype or "").lower()
    if "soundtrack" in rt:
        return "soundtrack"
    if "compilation" in rt:
        return "compilation"
    if rt.startswith(("single", "ep")):
        return "single"
    if rt.startswith("album"):
        return "album"
    return ""


def wanted(folder, fetch=True):
    """What Isaiarr wants Plex to show for one album folder:
    {collections: [...], moods: [...], review: {text, title, url} or None}.
    With fetch=False the review is only what an earlier analysis found (no network)."""
    rows = [dict(r) for r in db.q("SELECT album, albumartist, year, releasetype, profile FROM songs WHERE folder=?",
                                  (folder,))]
    out = {"collections": [], "moods": [], "review": None}
    if not rows:
        return out
    album = db.album_by_path(folder) or {}
    prop = album.get("proposal") or {}
    if isinstance(prop, str):
        prop = json.loads(prop)
    rt = _common(rows, "releasetype")
    if not rt:
        from . import policy as P
        pol = P.get(folder)
        rt = P.release_type(pol["policy"], _common(rows, "album")) if pol else ""
    if Config.PLEX_COLLECTIONS_KIND and _kind(rt):
        out["collections"].append(KINDS[_kind(rt)])
    years = Counter(r["year"] for r in rows if r.get("year"))
    if Config.PLEX_COLLECTIONS_DECADE and years and "compilation" not in rt:
        out["collections"].append(f"{years.most_common(1)[0][0] // 10 * 10}s")
    if Config.PLEX_MOODS:
        profiled = [json.loads(r["profile"]) for r in rows if r.get("profile")]
        moods = Counter(m for p in profiled for m in (p.get("moods") or []))
        need = max(1 if len(profiled) <= 2 else 2, 0.3 * len(profiled))
        out["moods"] = [m.capitalize() for m, n in moods.most_common(3) if n >= need]
    if Config.PLEX_REVIEW:
        rv = prop.get("review")
        if isinstance(rv, dict):
            out["review"] = rv
        elif rv is None and fetch and not prop.get("collection"):   # albums checked before 3.7
            out["review"] = find_review(prop.get("album") or {}, rows)
    return out


def find_review(album, rows=()):
    """The Wikipedia review for an album (matched album dict, or the songs' tags)."""
    from .sources import wikipedia
    from .sources.http import SourceError
    name = album.get("album") or _common(rows, "album")
    year = (album.get("date") or str(_common(rows, "year") or ""))[:4]
    md = album.get("album_artist") or _common(rows, "albumartist")
    if md == Config.VARIOUS_ARTISTS or album.get("collection"):
        md = ""
    page = album.get("collection_id") if album.get("source") == "wikipedia" else None
    if not name:
        return None
    try:
        return wikipedia.review(name, year, md, page=page)
    except SourceError as e:
        db.log(f"Wikipedia review for {name} failed: {e}", "warn")
        return None


# ------------------------------------------------------------------ Plex side

def _current(key):
    """{collections, moods, summary} as Plex has them for one album."""
    root = plex._req("GET", f"/library/metadata/{key}")
    d = root.find("Directory") if root is not None else None
    if d is None:
        return None
    return {"collections": [c.get("tag") for c in d.findall("Collection") if c.get("tag")],
            "moods": [m.get("tag") for m in d.findall("Mood") if m.get("tag")],
            "summary": d.get("summary") or "", "title": d.get("title") or ""}


def _hash(s):
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()[:16]


def _tag_edits(field, current, want, managed):
    """Plex tag-list edits: keep tags you added yourself, add ours, drop ours that no longer apply.
    Returns (set_params, remove_params)."""
    keep = [t for t in current if not managed(t) or t in want]
    final = keep + [t for t in want if t not in keep]
    drop = [t for t in current if managed(t) and t not in want]
    sets = {}
    if final != current and [t for t in final if t not in current]:
        sets = {f"{field}[{i}].tag.tag": t for i, t in enumerate(final)}
        sets[f"{field}.locked"] = 1
    rem = {f"{field}[].tag.tag-": ",".join(drop)} if drop else {}
    return sets, rem


def album_keys(folder):
    keys = [r["plex_album_key"] for r in db.q("SELECT DISTINCT plex_album_key FROM songs WHERE folder=? "
                                              "AND plex_album_key IS NOT NULL", (folder,))]
    return keys or plex.album_keys(folder)


def push(folder, keys=None, force=False, only=None):
    """Set collections, review and moods in Plex for one album folder. Returns a short note.
    only: just these PLEX_PARTS; the others are left as Plex has them."""
    if not enabled():
        return "off"
    a = db.album_by_path(folder)
    if not a:
        return "not an album Isaiarr knows"
    last = a.get("plex_meta") or {}
    if isinstance(last, str):
        last = json.loads(last)
    if not any_on() and not (last.get("collections") or last.get("moods") or last.get("summary")):
        return "off"
    parts = set(only or PLEX_PARTS)
    w = wanted(folder, fetch="review" in parts)
    sid = plex.resolve(folder)[0]
    if not sid:
        return "not in a Plex music library"
    keys = keys or album_keys(folder)
    if not keys:
        return "not in Plex yet"
    review = _review_for_plex(w["review"])
    sig = _hash(json.dumps([w["collections"], w["moods"], review], ensure_ascii=False))
    if not force and not only and last.get("sig") == sig and last.get("keys") == sorted(keys) and not last.get("error"):
        return "unchanged"
    done = []
    mark = last.get("summary")          # hash of the review Isaiarr put in Plex (so it knows it's its own)
    try:
        for key in keys:
            cur = _current(key)
            if cur is None:
                continue
            sets, rems = {}, []
            if "collections" in parts and (part_on("collections") or last.get("collections")):
                s, r = _tag_edits("collection", cur["collections"], w["collections"], _managed_collection)
                sets.update(s)
                rems.append(r)
            if "moods" in parts and (Config.PLEX_MOODS or last.get("moods")):
                s, r = _tag_edits("mood", cur["moods"], w["moods"], _managed_mood)
                sets.update(s)
                rems.append(r)
            ours = _hash(cur["summary"]) == last.get("summary") if cur["summary"] else True
            if "review" not in parts:
                pass
            elif review and ours:
                if cur["summary"] != review:
                    sets.update({"summary.value": review, "summary.locked": 1})
                mark = _hash(review)
            elif not review and cur["summary"] and ours and last.get("summary"):
                sets.update({"summary.value": "", "summary.locked": 0})     # review switched off: take ours back
                mark = None
            if sets:
                plex._req("PUT", f"/library/sections/{sid}/all", type=9, id=key, **sets)
            for r in rems:
                if r:
                    plex._req("PUT", f"/library/sections/{sid}/all", type=9, id=key, **r)
            if sets or any(rems):
                done.append(key)
    except (requests.RequestException, ValueError) as e:
        db.album_update(a["id"], plex_meta=json.dumps({**last, "error": str(e), "at": time.time()}))
        return f"failed: {e}"
    db.album_update(a["id"], plex_meta=json.dumps({
        "sig": sig if not only else None,      # a partial update: the next full one checks everything
        "keys": sorted(keys), "at": time.time(), "error": None,
        "collections": w["collections"] if "collections" in parts else last.get("collections") or [],
        "moods": w["moods"] if "moods" in parts else last.get("moods") or [],
        "summary": mark,
        "review_title": (w["review"] or {}).get("title") if "review" in parts else last.get("review_title")},
        ensure_ascii=False))
    return f"updated {len(done)} album(s) in Plex" if done else "already as wanted"


def _review_for_plex(r):
    from .sources.wikipedia import review_text
    return review_text(r) if r else ""


def status():
    return dict(_state)


def sync_all(force=False, folders=None, only=None):
    """Every tagged album, in the background (Settings → Plex, and nightly); or just `folders`,
    and just the `only` parts (Library → Update only…)."""
    what = ", ".join(PLEX_PARTS[p].lower() for p in only) if only else "collections, reviews and moods"
    with _lock:
        if _state["running"] or not enabled():
            return False
        _state.update(running=True, done=0, total=0, error=None, started=time.time(), what=what)

    def run():
        nonlocal folders
        try:
            if folders is None:
                folders = [r["path"] for r in db.q("SELECT path FROM albums WHERE status IN ('applied', 'uptodate', 'skipped')")]
            _state["total"] = len(folders)
            changed = 0
            for i, f in enumerate(folders, 1):
                try:
                    note = push(f, force=force, only=only)
                except Exception as e:  # noqa: BLE001
                    note = f"failed: {e}"
                if note.startswith("updated"):
                    changed += 1
                _state["done"] = i
                if i % 100 == 0:
                    db.log(f"Plex {what}: {i} of {len(folders)} albums checked")
            db.log(f"Plex {what}: {changed} of {len(folders)} album(s) updated")
        except Exception as e:  # noqa: BLE001
            _state["error"] = str(e)
            db.log(f"Plex {what} stopped: {e}", "warn")
        finally:
            _state["running"] = False
    threading.Thread(target=run, name="plexmeta", daemon=True).start()
    return True
