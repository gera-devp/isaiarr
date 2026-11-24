"""Format-neutral tag read/write/backup for FLAC, MP3 and M4A using mutagen.

Fields are handled through one vocabulary:
  title artist album albumartist composer tracknumber tracktotal
  discnumber disctotal date genre lyrics releasetype label

date is a year ("1986") or a full release date ("1986-11-01"); Plex shows a full one as the
album's Originally Available date (a year alone shows as 1 January).
label is the record label, which Plex shows as Record Label: LABEL in FLAC (as Picard writes it),
TPUB in MP3 (read by Plex since 1.43.5) and the iTunes free-form LABEL in M4A.

releasetype is what Plex uses to put an album under Albums, Singles & EPs, Compilations or
Soundtracks ("album;soundtrack", "album;compilation", "single"). It's written the way MusicBrainz
Picard and Mp3tag do: RELEASETYPE in FLAC, TXXX "MusicBrainz Album Type" in MP3, the iTunes
free-form "MusicBrainz Album Type" in M4A (plus a plain RELEASETYPE alias in the last two).
"""
import base64
import json
import os

from mutagen import File as MFile
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (APIC, ID3, ID3NoHeaderError, TALB, TCMP, TCOM, TCON, TDRC, TIT1, TIT2,
                         TPE1, TPE2, TPOS, TPUB, TRCK, TXXX, USLT, TYER)
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

from .config import Config

FIELDS = ["title", "artist", "album", "albumartist", "composer", "tracknumber",
          "tracktotal", "discnumber", "disctotal", "date", "genre", "grouping", "compilation", "releasetype",
          "label"]

VORBIS = {"title": "TITLE", "artist": "ARTIST", "album": "ALBUM", "albumartist": "ALBUMARTIST",
          "composer": "COMPOSER", "tracknumber": "TRACKNUMBER", "tracktotal": "TRACKTOTAL",
          "discnumber": "DISCNUMBER", "disctotal": "DISCTOTAL", "date": "DATE", "genre": "GENRE",
          "grouping": "GROUPING", "compilation": "COMPILATION", "releasetype": "RELEASETYPE", "label": "LABEL"}
RT_TXXX = ["MusicBrainz Album Type", "RELEASETYPE"]
RT_MP4 = ["----:com.apple.iTunes:MusicBrainz Album Type", "----:com.apple.iTunes:RELEASETYPE"]
MP4KEYS = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "albumartist": "aART",
           "composer": "\xa9wrt", "date": "\xa9day", "genre": "\xa9gen", "grouping": "\xa9grp"}
MP4_LABEL = "----:com.apple.iTunes:LABEL"


def kind_of(path):
    ext = os.path.splitext(path)[1].lower()
    return {".flac": "flac", ".mp3": "mp3", ".m4a": "m4a"}.get(ext)


def _split_num(v):
    """'3/12' -> ('3','12'); '3' -> ('3','')"""
    v = (v or "").strip()
    if "/" in v:
        a, b = v.split("/", 1)
        return a.strip(), b.strip()
    return v, ""


# Other taggers store some fields under different names (foobar2000 "ALBUM ARTIST", Picard
# "ALBUM_ARTIST", TXXX frames in MP3, iTunes free-form atoms). Players such as Plex may read those
# first, so a value we write could look like it never changed. We read them as a fallback and
# remove them whenever we write the field.
VORBIS_ALIASES = {"albumartist": ["ALBUM ARTIST", "ALBUM_ARTIST", "ALBUMARTISTS"],
                  "date": ["YEAR"], "composer": ["COMPOSERS"], "releasetype": ["MUSICBRAINZ_ALBUMTYPE"],
                  "label": ["ORGANIZATION", "PUBLISHER", "RECORDLABEL"]}
ID3_TXXX_ALIASES = {"albumartist": ["ALBUM ARTIST", "ALBUMARTIST", "ALBUM_ARTIST", "BAND"],
                    "date": ["YEAR"], "label": ["LABEL"]}
MP4_ALIASES = {"albumartist": ["----:com.apple.iTunes:ALBUM ARTIST", "----:com.apple.iTunes:ALBUMARTIST",
                               "----:com.apple.iTunes:ALBUM_ARTIST"]}


def _vorbis_get(tags, key):
    """Case-insensitive lookup in a Vorbis comment block."""
    vals = tags.get(key) or []
    if not vals:
        for k2 in tags.keys():
            if k2.upper() == key:
                vals = tags[k2]
                break
    return vals


def read(path):
    """Return {'fields': {...}, 'duration': float, 'has_art': bool, 'has_lyrics': bool}"""
    k = kind_of(path)
    f = {name: "" for name in FIELDS}
    has_art = False
    lyrics = ""
    duration = 0.0
    if k == "flac":
        a = FLAC(path)
        duration = a.info.length
        tags = a.tags or {}
        for name, key in VORBIS.items():
            vals = _vorbis_get(tags, key)
            for alias in VORBIS_ALIASES.get(name, []):
                if vals:
                    break
                vals = _vorbis_get(tags, alias)
            f[name] = "; ".join(vals) if vals else ""
        # some taggers store "3/12" in TRACKNUMBER
        n, t = _split_num(f["tracknumber"])
        f["tracknumber"], f["tracktotal"] = n, f["tracktotal"] or t
        n, t = _split_num(f["discnumber"])
        f["discnumber"], f["disctotal"] = n, f["disctotal"] or t
        if not f["tracktotal"] and tags.get("TOTALTRACKS"):
            f["tracktotal"] = tags["TOTALTRACKS"][0]
        if not f["disctotal"] and tags.get("TOTALDISCS"):
            f["disctotal"] = tags["TOTALDISCS"][0]
        lyrics = ((tags.get("LYRICS") or tags.get("UNSYNCEDLYRICS") or [""])[0])
        has_art = bool(a.pictures)
    elif k == "mp3":
        a = MP3(path)
        duration = a.info.length
        t = a.tags
        if t is not None:
            def g(fid):
                fr = t.getall(fid)
                return "; ".join(str(x) for fr_ in fr for x in fr_.text) if fr else ""
            f["title"], f["artist"], f["album"] = g("TIT2"), g("TPE1"), g("TALB")
            f["albumartist"], f["composer"], f["genre"] = g("TPE2"), g("TCOM"), g("TCON")
            for name, descs in ID3_TXXX_ALIASES.items():
                if not f[name]:
                    for fr in t.getall("TXXX"):
                        if (fr.desc or "").upper() in descs and fr.text:
                            f[name] = "; ".join(str(x) for x in fr.text)
                            break
            for fr in t.getall("TXXX"):
                if (fr.desc or "").upper() in [d.upper() for d in RT_TXXX] and fr.text and not f["releasetype"]:
                    f["releasetype"] = ";".join(str(x) for x in fr.text)
            f["date"] = g("TDRC") or g("TYER")
            f["label"] = g("TPUB")
            if not f["label"]:
                for fr in t.getall("TXXX"):
                    if (fr.desc or "").upper() in ID3_TXXX_ALIASES["label"] and fr.text:
                        f["label"] = "; ".join(str(x) for x in fr.text)
                        break
            f["grouping"], f["compilation"] = g("TIT1"), ("1" if g("TCMP").strip() in ("1", "true") else "")
            f["tracknumber"], f["tracktotal"] = _split_num(g("TRCK"))
            f["discnumber"], f["disctotal"] = _split_num(g("TPOS"))
            uslt = t.getall("USLT")
            lyrics = uslt[0].text if uslt else ""
            has_art = bool(t.getall("APIC"))
    elif k == "m4a":
        a = MP4(path)
        duration = a.info.length
        t = a.tags or {}
        for name, key in MP4KEYS.items():
            vals = t.get(key) or []
            for alias in MP4_ALIASES.get(name, []):
                if vals:
                    break
                vals = t.get(alias) or []
            f[name] = "; ".join((bytes(v).decode("utf-8", "replace") if isinstance(v, MP4FreeForm) else str(v))
                                for v in vals)
        if t.get("trkn"):
            n, tot = t["trkn"][0]
            f["tracknumber"], f["tracktotal"] = str(n or ""), str(tot or "")
        if t.get("disk"):
            n, tot = t["disk"][0]
            f["discnumber"], f["disctotal"] = str(n or ""), str(tot or "")
        if t.get(MP4_LABEL):
            f["label"] = "; ".join(bytes(v).decode("utf-8", "replace") if isinstance(v, MP4FreeForm) else str(v)
                                   for v in t[MP4_LABEL])
        lyrics = (t.get("\xa9lyr") or [""])[0]
        has_art = bool(t.get("covr"))
        f["compilation"] = "1" if t.get("cpil") else ""
        for key in RT_MP4:
            if t.get(key) and not f["releasetype"]:
                f["releasetype"] = ";".join(bytes(v).decode("utf-8", "replace") if isinstance(v, MP4FreeForm) else str(v)
                                            for v in t[key])
    else:
        raise ValueError(f"Unsupported file: {path}")
    # strip leading zeros on numbers for comparison sanity
    for n in ("tracknumber", "tracktotal", "discnumber", "disctotal"):
        if f[n].isdigit():
            f[n] = str(int(f[n]))
    return {"fields": f, "duration": round(duration or 0.0, 2),
            "has_art": has_art, "has_lyrics": bool(lyrics and lyrics.strip())}


def _drop(tags, key):
    if key in tags:
        del tags[key]


def _image_mime(data):
    return "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"


def write(path, fields, artwork=None, lyrics=None):
    """Write only the given fields (dict). artwork: bytes or None. lyrics: str or None."""
    k = kind_of(path)
    if k == "flac":
        a = FLAC(path)
        if a.tags is None:
            a.add_tags()
        for name, val in fields.items():
            key = VORBIS.get(name)
            if not key:
                continue
            for k2 in list(a.tags.keys()):   # same field under another name or case
                if k2.upper() in VORBIS_ALIASES.get(name, []) or (k2.upper() == key and k2 != key):
                    del a.tags[k2]
            if val in (None, ""):
                _drop(a.tags, key)
            else:
                a.tags[key] = [str(val)]
        if "tracknumber" in fields:  # drop conflicting legacy totals
            _drop(a.tags, "TOTALTRACKS")
        if "discnumber" in fields:
            _drop(a.tags, "TOTALDISCS")
        if lyrics is not None:
            a.tags["LYRICS"] = [lyrics]
            _drop(a.tags, "UNSYNCEDLYRICS")
        if artwork:
            a.clear_pictures()
            p = Picture()
            p.type, p.mime, p.desc, p.data = 3, _image_mime(artwork), "Cover", artwork
            a.add_picture(p)
        a.save()
    elif k == "mp3":
        try:
            t = ID3(path)
        except ID3NoHeaderError:
            t = ID3()
        cur = read(path)["fields"]
        merged = {**cur, **fields}
        simple = {"title": TIT2, "artist": TPE1, "album": TALB, "albumartist": TPE2,
                  "composer": TCOM, "genre": TCON, "grouping": TIT1, "compilation": TCMP}
        for name, descs in ID3_TXXX_ALIASES.items():
            if name in fields:
                for key in [k2 for k2, fr in t.items() if k2.startswith("TXXX:") and (fr.desc or "").upper() in descs]:
                    del t[key]
        for name, cls in simple.items():
            if name in fields:
                fid = cls.__name__
                t.delall(fid)
                if fields[name]:
                    t.add(cls(encoding=3, text=[str(fields[name])]))
        if "releasetype" in fields:
            for key in [k2 for k2, fr in t.items() if k2.startswith("TXXX:")
                        and (fr.desc or "").upper() in [d.upper() for d in RT_TXXX]]:
                del t[key]
            if fields["releasetype"]:
                for d in RT_TXXX:
                    t.add(TXXX(encoding=3, desc=d, text=[str(fields["releasetype"])]))
        if "date" in fields:
            t.delall("TDRC"); t.delall("TYER"); t.delall("TDAT")
            if fields["date"]:
                t.add(TDRC(encoding=3, text=[str(fields["date"])]))
        if "label" in fields:
            t.delall("TPUB")
            for key in [k2 for k2, fr in t.items() if k2.startswith("TXXX:")
                        and (fr.desc or "").upper() in ID3_TXXX_ALIASES["label"]]:
                del t[key]
            if fields["label"]:
                t.add(TPUB(encoding=3, text=[str(fields["label"])]))
        if {"tracknumber", "tracktotal"} & fields.keys():
            t.delall("TRCK")
            n, tot = merged.get("tracknumber"), merged.get("tracktotal")
            if n:
                t.add(TRCK(encoding=3, text=[f"{n}/{tot}" if tot else str(n)]))
        if {"discnumber", "disctotal"} & fields.keys():
            t.delall("TPOS")
            n, tot = merged.get("discnumber"), merged.get("disctotal")
            if n:
                t.add(TPOS(encoding=3, text=[f"{n}/{tot}" if tot else str(n)]))
        if lyrics is not None:
            t.delall("USLT")
            t.add(USLT(encoding=3, lang="tam", desc="", text=lyrics))
        if artwork:
            t.delall("APIC")
            t.add(APIC(encoding=3, mime=_image_mime(artwork), type=3, desc="Cover", data=artwork))
        if Config.MP3_ID3_VERSION == 3:
            t.update_to_v23()
            t.save(path, v2_version=3)
        else:
            t.save(path, v2_version=4)
    elif k == "m4a":
        a = MP4(path)
        if a.tags is None:
            a.add_tags()
        cur = read(path)["fields"]
        merged = {**cur, **fields}
        for name, key in MP4KEYS.items():
            if name in fields:
                for alias in MP4_ALIASES.get(name, []):
                    _drop(a.tags, alias)
                if fields[name]:
                    a.tags[key] = [str(fields[name])]
                else:
                    _drop(a.tags, key)

        if "compilation" in fields:
            if fields["compilation"]:
                a.tags["cpil"] = True
            else:
                _drop(a.tags, "cpil")
        if "releasetype" in fields:
            for key in RT_MP4:
                _drop(a.tags, key)
                if fields["releasetype"]:
                    a.tags[key] = [MP4FreeForm(str(fields["releasetype"]).encode("utf-8"))]
        if "label" in fields:
            _drop(a.tags, MP4_LABEL)
            if fields["label"]:
                a.tags[MP4_LABEL] = [MP4FreeForm(str(fields["label"]).encode("utf-8"))]

        def _i(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return 0
        if {"tracknumber", "tracktotal"} & fields.keys():
            a.tags["trkn"] = [(_i(merged.get("tracknumber")), _i(merged.get("tracktotal")))]
        if {"discnumber", "disctotal"} & fields.keys():
            a.tags["disk"] = [(_i(merged.get("discnumber")), _i(merged.get("disctotal")))]
        if lyrics is not None:
            a.tags["\xa9lyr"] = [lyrics]
        if artwork:
            fmt = MP4Cover.FORMAT_PNG if _image_mime(artwork) == "image/png" else MP4Cover.FORMAT_JPEG
            a.tags["covr"] = [MP4Cover(artwork, imageformat=fmt)]
        a.save()
    else:
        raise ValueError(f"Unsupported file: {path}")


# ---------------- backup / restore ----------------

def backup(path, dest_base):
    """Snapshot all tags of `path` into dest_base(.json|.id3). Returns backup file path."""
    k = kind_of(path)
    os.makedirs(os.path.dirname(dest_base), exist_ok=True)
    if k == "mp3":
        dest = dest_base + ".id3"
        try:
            t = ID3(path)
            t.save(dest, v2_version=t.version[1] if t.version[1] in (3, 4) else 4)
        except ID3NoHeaderError:
            with open(dest, "wb"):
                pass  # empty file == "had no tags"
        return dest
    dest = dest_base + ".json"
    if k == "flac":
        a = FLAC(path)
        data = {"kind": "flac",
                "comments": [[kk, vv] for kk, vv in (a.tags or [])],
                "pictures": [base64.b64encode(p.write()).decode() for p in a.pictures]}
    elif k == "m4a":
        a = MP4(path)
        items = {}
        for key, vals in (a.tags or {}).items():
            scalar = not isinstance(vals, list)
            out = []
            for v in ([vals] if scalar else vals):
                if isinstance(v, MP4Cover):
                    out.append({"c": base64.b64encode(bytes(v)).decode(), "f": v.imageformat})
                elif isinstance(v, MP4FreeForm):
                    out.append({"b": base64.b64encode(bytes(v)).decode(), "f": v.dataformat})
                elif isinstance(v, tuple):
                    out.append({"t": list(v)})
                elif isinstance(v, bool):
                    out.append({"bool": v})
                elif isinstance(v, int):
                    out.append({"i": v})
                else:
                    out.append({"s": str(v)})
            # cpil / pgap / pcst etc. are single booleans, not lists - remember that
            items[key] = {"scalar": out[0]} if scalar and out else out
        data = {"kind": "m4a", "items": items}
    else:
        raise ValueError(path)
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    return dest


def restore(path, backup_file):
    k = kind_of(path)
    if k == "mp3":
        try:
            ID3(path).delete(path)
        except ID3NoHeaderError:
            pass
        if os.path.getsize(backup_file) > 0:
            t = ID3(backup_file)
            t.save(path, v2_version=t.version[1] if t.version[1] in (3, 4) else 4)
        return
    with open(backup_file, encoding="utf-8") as fh:
        data = json.load(fh)
    if k == "flac":
        a = FLAC(path)
        if a.tags is None:
            a.add_tags()
        a.tags.clear()
        for kk, vv in data["comments"]:
            a.tags.append((kk, vv))
        a.clear_pictures()
        for b in data["pictures"]:
            a.add_picture(Picture(base64.b64decode(b)))
        a.save()
    elif k == "m4a":
        a = MP4(path)
        if a.tags is None:
            a.add_tags()
        a.tags.clear()
        for key, vals in data["items"].items():
            scalar = isinstance(vals, dict) and "scalar" in vals
            out = []
            for v in ([vals["scalar"]] if scalar else vals):
                if "c" in v:
                    out.append(MP4Cover(base64.b64decode(v["c"]), imageformat=v["f"]))
                elif "b" in v:
                    out.append(MP4FreeForm(base64.b64decode(v["b"]), dataformat=v["f"]))
                elif "t" in v:
                    out.append(tuple(v["t"]))
                elif "bool" in v:
                    out.append(v["bool"])
                elif "i" in v:
                    out.append(v["i"])
                else:
                    out.append(v["s"])
            a.tags[key] = out[0] if scalar else out
        a.save()
