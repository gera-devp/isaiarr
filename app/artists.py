"""Album artists as Plex will see them: one Plex artist per distinct Album Artist tag (or the
track's Artist when Album Artist is empty). Finds what makes the list longer than it should be:
the same name spelled differently, files without an Album Artist, and several names in one tag."""
import collections
import os
import re

from rapidfuzz import fuzz

from . import db
from .config import Config

MULTI_RE = re.compile(r"\s*(?:,|;|&|/|\band\b|\bfeat\.?|\bft\.?)\s*", re.I)


def plex_name(albumartist, artist):
    return (albumartist or "").strip() or (artist or "").strip() or ""


def _key(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


_SOUND = (("zh", "l"), ("th", "t"), ("dh", "t"), ("sh", "s"), ("ch", "s"), ("ph", "f"), ("kh", "k"),
          ("d", "t"), ("g", "k"), ("b", "p"), ("w", "v"))


def sound_key(name):
    """How a Tamil name in English letters sounds, ignoring spelling: no vowels, no doubled letters,
    and the letter pairs Tamil writes with one letter made equal (t/d, k/g, p/b).
    "Ilayaraja" = "Ilaiyaraaja" = "Ilaiyaraja"; "Bharathwaj" = "Bharadwaj"."""
    s = re.sub(r"[^a-z]", "", (name or "").lower())
    for a, b in _SOUND:
        s = s.replace(a, b)
    s = re.sub(r"[aeiouy]", "", s)
    return re.sub(r"(.)\1+", r"\1", s)


def _rows():
    return [dict(r) for r in db.q("SELECT folder, albumartist, artist FROM songs")]


def _checked():
    """folder -> album id/status for folders Isaiarr has analyzed."""
    return {r["path"]: (r["id"], r["status"]) for r in db.q("SELECT id, path, status FROM albums")}


def summary(rows=None):
    rows = rows if rows is not None else _rows()
    names = {plex_name(r["albumartist"], r["artist"]) for r in rows}
    names.discard("")
    return {"songs": len(rows), "artists": len(names),
            "missing_songs": sum(1 for r in rows if not (r["albumartist"] or "").strip())}


def everyone(rows=None, find=None):
    """Every name Plex will list as an artist: [{name, songs, folders, from_singers}], biggest first."""
    rows = rows if rows is not None else _rows()
    by = {}
    for r in rows:
        aa = (r["albumartist"] or "").strip()
        name = plex_name(r["albumartist"], r["artist"])
        if not name:
            continue
        d = by.setdefault(name, {"name": name, "songs": 0, "folders": set(), "from_singers": False})
        d["songs"] += 1
        d["folders"].add(r["folder"])
        if not aa:
            d["from_singers"] = True
    out = [{**d, "folders": len(d["folders"])} for d in by.values()]
    if find:
        f = find.lower()
        out = [d for d in out if f in d["name"].lower()]
    return sorted(out, key=lambda d: (-d["songs"], d["name"].lower()))


def variant_groups(rows=None):
    """Names that are probably the same artist written differently, biggest first.
    [{key, names: [{name, songs, folders, unchecked}], suggested}]"""
    rows = rows if rows is not None else _rows()
    checked = _checked()
    stats = {}
    for r in rows:
        aa = (r["albumartist"] or "").strip()
        if not aa:
            continue
        s = stats.setdefault(aa, {"name": aa, "songs": 0, "folders": set(), "unchecked": set()})
        s["songs"] += 1
        s["folders"].add(r["folder"])
        if r["folder"] not in checked:
            s["unchecked"].add(r["folder"])
    ignored = set((db.state_get("artist_ignored") or {}).get("keys", []))
    # 1. same letters once spaces and punctuation are gone ("A.R. Rahman" = "A R Rahman")
    by_key = collections.defaultdict(list)
    for n in stats:
        by_key[_key(n)].append(n)
    keys = [k for k in by_key if len(k) >= 4]
    # 2. near-identical spellings ("Shankar-Ganesh" ~ "Sankar-Ganesh"), compared only within the
    #    same first letter so it stays quick on big libraries
    parent = {k: k for k in keys}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k
    by_first = collections.defaultdict(list)
    for k in keys:
        by_first[k[0]].append(k)
    for ks in by_first.values():
        ks.sort()
        for i, a in enumerate(ks):
            for b in ks[i + 1:i + 60]:
                if abs(len(a) - len(b)) <= 2 and fuzz.ratio(a, b) >= 92:
                    parent[find(a)] = find(b)
    # 3. same sound, different transliteration ("Ilayaraja" ~ "Ilaiyaraaja", "Bharathwaj" ~ "Bharadwaj"):
    #    same consonants and the same number of words, and still broadly alike when spelt out
    by_sound = collections.defaultdict(list)
    for k in keys:
        n = by_key[k][0]
        sk = sound_key(n)
        if len(sk) >= 3:
            by_sound[(sk, len(n.split()))].append(k)
    for ks in by_sound.values():
        for a in ks[1:]:
            if fuzz.ratio(ks[0], a) >= 75:
                parent[find(a)] = find(ks[0])
    groups = collections.defaultdict(list)
    for k in keys:
        groups[find(k)].extend(by_key[k])
    out = []
    for names in groups.values():
        if len(names) < 2:
            continue
        gkey = "|".join(sorted(_key(n) for n in names))
        if gkey in ignored:
            continue
        items = sorted(({"name": n, "songs": stats[n]["songs"], "folders": len(stats[n]["folders"]),
                         "unchecked": len(stats[n]["unchecked"])} for n in names), key=lambda x: -x["songs"])
        # the spelling Plex already uses for these songs (its own artist name) wins; else the commonest
        pl = collections.Counter(r["plex_artist"] for r in db.q(
            f"SELECT plex_artist FROM songs WHERE albumartist IN ({','.join('?' * len(names))}) AND plex_artist IS NOT NULL",
            tuple(names)) if r["plex_artist"] in names)
        best = pl.most_common(1)[0][0] if pl else items[0]["name"]
        for i in items:
            i["plex"] = i["name"] == best and bool(pl)
        out.append({"key": gkey, "names": items, "suggested": best,
                    "songs": sum(i["songs"] for i in items)})
    return sorted(out, key=lambda g: -g["songs"])


def missing(rows=None):
    """Folders with songs that have no Album Artist (Plex then makes an artist from the singers)."""
    rows = rows if rows is not None else _rows()
    checked = _checked()
    by = {}
    for r in rows:
        if (r["albumartist"] or "").strip():
            continue
        d = by.setdefault(r["folder"], {"folder": r["folder"], "songs": 0, "artists": set()})
        d["songs"] += 1
        if r["artist"]:
            d["artists"].add(r["artist"])
    out = []
    for f, d in by.items():
        aid, st = checked.get(f, (None, None))
        out.append({**d, "artists": sorted(d["artists"])[:3], "n_artists": len(d["artists"]),
                    "album_id": aid, "status": st})
    return sorted(out, key=lambda d: (-d["n_artists"], d["folder"]))


def multi(rows=None):
    """Album Artist tags holding several names (often the singers instead of the music director)."""
    rows = rows if rows is not None else _rows()
    checked = _checked()
    by = {}
    for r in rows:
        aa = (r["albumartist"] or "").strip()
        if not aa or aa == Config.VARIOUS_ARTISTS or len([p for p in MULTI_RE.split(aa) if p]) < 2:
            continue
        d = by.setdefault(aa, {"name": aa, "songs": 0, "folders": set()})
        d["songs"] += 1
        d["folders"].add(r["folder"])
    out = []
    for d in by.values():
        fs = sorted(d["folders"])
        out.append({"name": d["name"], "songs": d["songs"], "folders": fs,
                    "album_ids": [checked[f][0] for f in fs if f in checked]})
    return sorted(out, key=lambda d: (-d["songs"], d["name"]))


def merge(names, target):
    """Write `target` as Album Artist to every file whose Album Artist is one of `names`.
    Only folders Isaiarr has analyzed (it needs their album entry for backups and Undo).
    Returns (albums queued, folders skipped because they haven't been checked yet)."""
    names = [n for n in names if n and n != target] + [target]
    checked = _checked()
    marks = ",".join("?" * len(names))
    folders = sorted({r["folder"] for r in db.q(f"SELECT DISTINCT folder FROM songs WHERE albumartist IN ({marks})",
                                                 tuple(names))})
    queued, skipped = 0, []
    for f in folders:
        if f not in checked:
            skipped.append(f)
            continue
        db.job_add("setartist", checked[f][0], {"name": target, "composer": False, "only": names},
                   priority=db.PRIORITY_USER)
        queued += 1
    return queued, skipped


def ignore(key):
    st = db.state_get("artist_ignored") or {}
    keys = set(st.get("keys", []))
    keys.add(key)
    db.state_set("artist_ignored", {"keys": sorted(keys)})


def folders_rel(folders):
    root = Config.MUSIC_DIR.rstrip("/") + "/"
    return [f[len(root):] if f.startswith(root) else f for f in folders]


def exists(folder):
    return os.path.isdir(folder)
