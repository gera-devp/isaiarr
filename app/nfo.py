"""album.nfo / artist.nfo sidecar files for music libraries.

Plex Media Server 1.43.5 reads artist and album metadata from NFO files in music libraries; Kodi,
Jellyfin and Emby read the same files. They're written in Kodi's music NFO format, from the tags
Isaiarr wrote into the files (via the song index), so the NFO always agrees with the files:

  <album folder>/album.nfo    title, album artist(s), year and release date, genre, label, release type,
                              compilation, review and moods (as sent to Plex), tracks
  <artist folder>/artist.nfo  only when every album folder in it has the same Album Artist

NFO files Isaiarr didn't write are never overwritten.
"""
import os
import re
import xml.etree.ElementTree as ET
from collections import Counter

from . import db
from .config import Config

MARK = "Written by Isaiarr"
SPLIT = re.compile(r"\s*;\s*")


def _ours(path):
    """True if the file doesn't exist yet or Isaiarr wrote it (safe to replace)."""
    if not os.path.exists(path):
        return True
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return MARK in fh.read(4096)
    except OSError:
        return False


def _write(path, root):
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    tmp = path + ".isaiarr-tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n')
        fh.write(f"<!-- {MARK}; edits here are replaced the next time the album is written -->\n")
        fh.write(body + "\n")
    os.replace(tmp, path)


def _sub(parent, tag, text):
    if text not in (None, ""):
        ET.SubElement(parent, tag).text = str(text)


def _mmss(sec):
    sec = int(round(sec or 0))
    return f"{sec // 60}:{sec % 60:02d}"


def album_xml(folder):
    """<album> element for a folder, from its songs' tags; None when the folder has no songs."""
    rows = [dict(r) for r in db.q(
        "SELECT title, artist, albumartist, album, year, genre, releasetype, compilation, tracknumber, duration, composer, "
        "label, date "
        "FROM songs WHERE folder=? ORDER BY CAST(tracknumber AS INTEGER), path", (folder,))]
    if not rows:
        return None
    common = lambda k: (Counter((r.get(k) or "") for r in rows).most_common(1)[0][0])  # noqa: E731
    album, aa = common("album"), common("albumartist")
    root = ET.Element("album")
    _sub(root, "title", album or os.path.basename(folder))
    for name in [n for n in SPLIT.split(aa or "") if n]:
        cred = ET.SubElement(root, "albumArtistCredits")
        _sub(cred, "artist", name)
    _sub(root, "artistdesc", aa)
    genre = common("genre")
    for g in [x for x in SPLIT.split(genre or "") if x]:
        _sub(root, "genre", g)
    years = Counter(r["year"] for r in rows if r.get("year"))
    if years:
        _sub(root, "year", years.most_common(1)[0][0])
    dates = Counter(r["date"] for r in rows if len(r.get("date") or "") == 10)
    if dates:
        _sub(root, "releasedate", dates.most_common(1)[0][0])
    _sub(root, "label", common("label"))
    rt = common("releasetype")
    if rt:
        primary = rt.split(";")[0].strip()
        _sub(root, "releasetype", primary)                     # album | single | ep
        kind = [x.strip() for x in rt.split(";")[1:] if x.strip()]
        if kind:
            _sub(root, "type", kind[0].capitalize())          # Soundtrack | Compilation
    comp = common("compilation") == "1" or (aa or "") == Config.VARIOUS_ARTISTS
    _sub(root, "compilation", "true" if comp else "false")
    from . import plexmeta
    from .sources.wikipedia import review_text
    extra = plexmeta.wanted(folder, fetch=False)
    _sub(root, "review", review_text(extra["review"]))
    for m in extra["moods"]:
        _sub(root, "mood", m)
    for r in rows:
        t = ET.SubElement(root, "track")
        _sub(t, "position", (r.get("tracknumber") or "").split("/")[0])
        _sub(t, "title", r.get("title"))
        _sub(t, "duration", _mmss(r.get("duration")))
    return root


def artist_xml(name):
    root = ET.Element("artist")
    _sub(root, "name", name)
    _sub(root, "sortname", name)
    if name == Config.VARIOUS_ARTISTS:
        _sub(root, "type", "Various")
    return root


def write_album(folder):
    """Write album.nfo (and artist.nfo one level up when that folder is one artist's).
    Returns a short note of what happened."""
    folder = os.path.normpath(folder)
    if not os.path.isdir(folder):
        return "folder missing"
    root = album_xml(folder)
    if root is None:
        return "no songs"
    path = os.path.join(folder, "album.nfo")
    if not _ours(path):
        return "album.nfo exists and wasn't written by Isaiarr: left alone"
    _write(path, root)
    note = "album.nfo written"
    parent = os.path.dirname(folder)
    if parent.rstrip(os.sep) != os.path.normpath(Config.MUSIC_DIR).rstrip(os.sep):
        names = {(r["albumartist"] or "").strip() for r in db.q(
            "SELECT albumartist FROM songs WHERE folder=? OR folder LIKE ?", (parent, parent.rstrip(os.sep) + os.sep + "%"))}
        names.discard("")
        if len(names) == 1:
            name = names.pop()
            ap = os.path.join(parent, "artist.nfo")
            if name != Config.VARIOUS_ARTISTS and _ours(ap):
                _write(ap, artist_xml(name))
                note += ", artist.nfo written"
    return note


def write_many(folders):
    """For the "write NFO files for all tagged albums" button. Returns (written, skipped)."""
    written = skipped = 0
    for f in folders:
        try:
            n = write_album(f)
        except Exception as e:  # noqa: BLE001
            n = f"failed: {e}"
        if n.startswith("album.nfo written"):
            written += 1
        else:
            skipped += 1
    return written, skipped
