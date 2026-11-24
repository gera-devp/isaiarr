"""Folders view: browse the music library like a file manager, one level at a time, with each
folder's albums, Album Artists, decisions and problems added up over everything inside it.

Built from the song index (only folders with music show up), so it stays quick on big libraries.
"""
import os
from collections import Counter

from . import db, policy as POL
from .config import Config


def root():
    return os.path.normpath(Config.MUSIC_DIR)


def abs_path(rel):
    """'Ilaiyaraaja/Hits' -> '/music/Ilaiyaraaja/Hits', refusing anything outside the library."""
    p = os.path.normpath(os.path.join(root(), (rel or "").strip("/")))
    if p != root() and not p.startswith(root() + os.sep):
        return root()
    return p


def rel(path):
    return "" if path == root() else os.path.relpath(path, root())


def _album_folders():
    """{folder: {"n": songs, "aa": Counter(album artist), "rt": Counter(release type)}}"""
    out = {}
    for r in db.q("SELECT folder, albumartist, releasetype FROM songs"):
        d = out.setdefault(r["folder"], {"n": 0, "aa": Counter(), "rt": Counter()})
        d["n"] += 1
        d["aa"][(r["albumartist"] or "").strip()] += 1
        d["rt"][(r["releasetype"] or "").strip()] += 1
    return out


def _inside(folder, base):
    return folder == base or folder.startswith(base.rstrip(os.sep) + os.sep)


def expand(folders, recursive=True):
    """The album folders (folders with songs) that the chosen folders stand for."""
    albums = set(_album_folders())
    out = set()
    for f in folders:
        f = os.path.normpath(f)
        if f in albums:
            out.add(f)
        if recursive:
            out |= {a for a in albums if _inside(a, f)}
    return sorted(out)


def browse(path, q=None):
    """Children of `path` (or every matching folder below it when searching), each with totals."""
    path = os.path.normpath(path)
    albums = _album_folders()
    decided = POL.all_policies()
    suggested = {d["folder"]: d for d in POL.suggestions()}
    known = {r["path"]: (r["id"], r["status"]) for r in db.q("SELECT id, path, status FROM albums")}
    try:
        from . import plex
        diffs = plex.differences() if plex.enabled() else {}
    except Exception:  # noqa: BLE001
        diffs = {}

    def totals(base, only_self=False):
        t = {"albums": 0, "songs": 0, "aa": Counter(), "decided": 0, "suggest": 0, "unchecked": 0, "plex": 0,
             "policies": Counter()}
        for f, d in albums.items():
            if not (f == base if only_self else _inside(f, base)):
                continue
            t["albums"] += 1
            t["songs"] += d["n"]
            t["aa"].update(d["aa"])
            if f in decided:
                t["decided"] += 1
                t["policies"][decided[f]["policy"]] += 1
            if f in suggested:
                t["suggest"] += 1
            if f not in known:
                t["unchecked"] += 1
            if f in diffs:
                t["plex"] += 1
        return t

    def row(f, t):
        aa = [(n or "(no Album Artist)", c) for n, c in t["aa"].most_common(3)]
        r = {"path": f, "rel": rel(f), "name": os.path.basename(f) or "Music", "is_album": f in albums,
             "has_children": any(a != f and _inside(a, f) for a in albums), **t, "top_aa": aa,
             "n_aa": len(t["aa"]), "policy": decided.get(f), "suggestion": suggested.get(f),
             "album": known.get(f)}
        if f in albums:
            r["releasetype"] = albums[f]["rt"].most_common(1)[0][0]
        return r

    if q:
        ql = q.lower()
        hits = sorted({f for f in albums if _inside(f, path) and ql in rel(f).lower()})[:300]
        children = [row(f, totals(f, only_self=True)) for f in hits]
    else:
        names = set()
        for f in albums:
            if f != path and _inside(f, path):
                names.add(os.path.relpath(f, path).split(os.sep)[0])
        children = [row(os.path.join(path, n), totals(os.path.join(path, n))) for n in sorted(names, key=str.lower)]
    here = row(path, totals(path, only_self=True)) if path in albums else None
    crumbs = [("Music", "")]
    acc = ""
    for part in [p for p in rel(path).split(os.sep) if p]:
        acc = os.path.join(acc, part)
        crumbs.append((part, acc))
    return {"path": path, "rel": rel(path), "crumbs": crumbs, "children": children, "here": here,
            "total": totals(path)}
