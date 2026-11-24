"""Find duplicate songs in the library.

Three levels, strongest first:
  exact      the same audio data (FLAC's built-in audio checksum, or a hash of the audio part of
             an MP3 / M4A, so files that differ only in tags still count as identical)
  recording  the same recording in another file: a FLAC and an MP3 of one song, a re-rip...
             Found by comparing audio fingerprints (Chromaprint `fpcalc`, the AcoustID engine).
  likely     same title, a shared singer or music director and nearly the same length, in
             different folders. Worth a look, not certain.

Duplicates are never deleted outright: "Move to bin" puts the extra files in a hidden folder on
the same share (Plex is told to ignore it), from where they can be restored or emptied for good.
"""
import array
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time

from mutagen import File as MFile

from . import db, songs
from .config import Config

LEVELS = {   # fingerprint similarity, seconds of length difference for "likely", show "likely" at all
    "strict": (0.92, 2.0, False),
    "normal": (0.88, 4.0, True),
    "loose": (0.84, 6.0, True),
}
KIND_LABEL = {"exact": "Identical files", "recording": "Same song, another file", "likely": "Possible duplicates"}
_state = {"running": False, "phase": "", "done": 0, "total": 0, "started": None, "finished": None, "error": None,
          "new": 0}
_lock = threading.Lock()
_pending = set()            # folders that changed while a check was running


def fpcalc_ok():
    return shutil.which("fpcalc") is not None


def status():
    r = db.q("SELECT COUNT(*) n, COALESCE(SUM(saving),0) s, SUM(kind='exact') e, SUM(kind='recording') rc, "
             "SUM(kind='likely') l FROM dupe_groups WHERE ignored=0", one=True)
    ign = db.q("SELECT COUNT(*) n FROM dupe_groups WHERE ignored=1", one=True)["n"]
    binr = db.q("SELECT COUNT(*) n, COALESCE(SUM(size),0) s FROM dupe_moves WHERE restored IS NULL AND deleted IS NULL",
                one=True)
    left = db.q("SELECT COUNT(*) n FROM songs WHERE fp_mtime IS NULL OR fp_mtime != mtime", one=True)["n"]
    return {**_state, "groups": r["n"] or 0, "saving": r["s"] or 0, "exact": r["e"] or 0, "recording": r["rc"] or 0,
            "likely": r["l"] or 0, "ignored": ign, "bin_files": binr["n"], "bin_size": binr["s"],
            "to_analyze": left, "last": (db.state_get("dupes_scan") or {}).get("at"), "fpcalc": fpcalc_ok()}


# ------------------------------------------------------------------ per-file analysis

def _audio_region(path, kind):
    """(start, end) of the audio data, i.e. the file without its tag blocks."""
    size = os.path.getsize(path)
    start, end = 0, size
    with open(path, "rb") as fh:
        if kind == "mp3":
            head = fh.read(10)
            if head[:3] == b"ID3" and len(head) == 10:
                sz = (head[6] & 0x7F) << 21 | (head[7] & 0x7F) << 14 | (head[8] & 0x7F) << 7 | (head[9] & 0x7F)
                start = 10 + sz + (10 if head[5] & 0x10 else 0)
            if size >= 128:
                fh.seek(size - 128)
                if fh.read(3) == b"TAG":
                    end = size - 128
            if end >= 32:
                fh.seek(end - 32)
                ape = fh.read(32)
                if ape[:8] == b"APETAGEX":
                    end -= int.from_bytes(ape[12:16], "little") + 32
        elif kind == "m4a":
            pos = 0
            while pos + 8 <= size:
                fh.seek(pos)
                hdr = fh.read(8)
                n, typ = int.from_bytes(hdr[:4], "big"), hdr[4:8]
                hl = 8
                if n == 1:
                    n, hl = int.from_bytes(fh.read(8), "big"), 16
                elif n == 0:
                    n = size - pos
                if typ == b"mdat":
                    return pos + hl, pos + n
                if n < 8:
                    break
                pos += n
    return start, max(start, end)


def audio_hash(path, kind, info=None):
    """A hash of the audio only, so retagged copies still match."""
    if kind == "flac":
        md5 = getattr(info, "md5_signature", 0) if info is not None else 0
        if md5:
            return "flac:%032x" % md5
    start, end = _audio_region(path, kind)
    length = end - start
    if length <= 0:
        return None
    h = hashlib.sha1(str(length).encode())
    chunk = 1 << 20
    with open(path, "rb") as fh:
        fh.seek(start)
        h.update(fh.read(min(chunk, length)))
        if length > 2 * chunk:
            fh.seek(start + length // 2)
            h.update(fh.read(chunk))
            fh.seek(end - chunk)
            h.update(fh.read(chunk))
    return "a:" + h.hexdigest()


def fingerprint(path):
    """Raw Chromaprint fingerprint of the first two minutes, as an array of 32-bit ints."""
    try:
        r = subprocess.run(["fpcalc", "-raw", "-json", "-length", "120", path],
                           capture_output=True, text=True, timeout=120)
        data = json.loads(r.stdout or "{}")
        fp = data.get("fingerprint") or []
        return array.array("I", [x & 0xFFFFFFFF for x in fp]).tobytes() if fp else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def analyze(row, use_fp=True):
    path = row["path"]
    try:
        size = os.path.getsize(path)
        mf = MFile(path)
        info = mf.info if mf is not None else None
    except Exception:  # noqa: BLE001
        return False
    kind = os.path.splitext(path)[1].lower().lstrip(".")
    kind = {"m4a": "m4a", "mp4": "m4a", "flac": "flac", "mp3": "mp3"}.get(kind, kind)
    codec = getattr(info, "codec", "") or kind
    lossless = 1 if kind == "flac" or codec in ("alac",) else 0
    bits = getattr(info, "bits_per_sample", 0) or 0
    bitrate = int(getattr(info, "bitrate", 0) or 0)
    try:
        ah = audio_hash(path, kind, info)
    except OSError:
        ah = None
    fp = fingerprint(path) if use_fp and fpcalc_ok() else None
    db.x("UPDATE songs SET size=?, bitrate=?, sample_rate=?, bits=?, codec=?, lossless=?, ahash=?, fp=?, fp_mtime=? "
         "WHERE id=?", (size, bitrate, int(getattr(info, "sample_rate", 0) or 0), bits, codec, lossless, ah, fp,
                        row["mtime"], row["id"]))
    return True


# ------------------------------------------------------------------ comparing

def _norm_title(t):
    t = (t or "").lower()
    t = re.sub(r"^\s*(?:cd\s*\d+\s*[-.]?\s*)?\d{1,3}\s*[-._)]\s*", "", t)    # "02 - Song", "1. Song": a track number, not the title
    t = re.sub(r"\((from|film|movie|ost)[^)]*\)|\[(from|film|movie|ost)[^\]]*\]", " ", t)
    t = re.sub(r"\b(original motion picture soundtrack|ost|hq|hd|320kbps|128kbps|mp3|flac)\b", " ", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _fp_array(b):
    a = array.array("I")
    a.frombytes(b)
    return a


def similarity(a, b, max_offset=6):
    """1.0 = identical fingerprints. Tries small time offsets (a few seconds of lead-in)."""
    best = 0.0
    la, lb = len(a), len(b)
    if not la or not lb:
        return 0.0
    for off in range(-max_offset, max_offset + 1):
        i0, i1 = max(0, -off), min(la, lb - off)
        n = i1 - i0
        if n < 40:
            continue
        err = 0
        for i in range(i0, i1):
            err += bin(a[i] ^ b[i + off]).count("1")
        best = max(best, 1 - err / (32.0 * n))
    return best


class _UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _people(s):
    return {p.lower() for p in songs.split_people(s)}


def identity(r):
    """What a "not a duplicate" choice is remembered against: the audio itself when known, so
    renaming or retagging the file doesn't lose it; otherwise the path."""
    return r.get("ahash") or r.get("path")


def exclude(gid, song_id):
    """Take one song out of a group: it's not a duplicate of the others. Remembered for good."""
    g = db.q("SELECT * FROM dupe_groups WHERE id=?", (gid,), one=True)
    if not g:
        return False
    ids = json.loads(g["members"] or "[]")
    if song_id not in ids:
        return False
    rows = {r["id"]: dict(r) for r in db.q(f"SELECT id, path, title, ahash FROM songs WHERE id IN ({','.join('?' * len(ids))})",
                                           tuple(ids))}
    me = rows.get(song_id)
    if not me:
        return False
    tok = hashlib.sha1(f"{gid}:{song_id}:{time.time()}".encode()).hexdigest()[:16]
    for sid, other in rows.items():
        if sid == song_id:
            continue
        x, y = sorted([(identity(me), me), (identity(other), other)], key=lambda t: (t[0], t[1]["path"]))
        db.x("INSERT OR IGNORE INTO dupe_excl(a, b, a_title, a_path, b_title, b_path, created, tok, me_title, me_path) "
             "VALUES(?,?,?,?,?,?,?,?,?,?)", (x[0], y[0], x[1]["title"], x[1]["path"], y[1]["title"], y[1]["path"],
                                             time.time(), tok, me["title"], me["path"]))
    rest = [rows[i] for i in ids if i in rows and i != song_id]
    if len(rest) >= 2:      # the others stay a group for you to review, even if they only matched through this file
        db.x("INSERT INTO dupe_pins(tok, kind, members, created) VALUES(?,?,?,?)",
             (tok, g["kind"], json.dumps([[identity(r), r["path"]] for r in rest]), time.time()))
    rebuild(quiet=True)
    r = db.q("SELECT MIN(id) id FROM dupe_excl WHERE tok=?", (tok,), one=True)
    return {"eid": (r["id"] if r else None), "others": len(ids) - 1, "left": where_now([i for i in ids if i != song_id])}


def where_now(song_ids):
    """The groups these songs are in now: [{id, kind, n, ignored}] (empty: none of them is grouped any more)."""
    want, out = set(song_ids), []
    for g in db.q("SELECT id, kind, members, ignored FROM dupe_groups"):
        m = json.loads(g["members"] or "[]")
        if want & set(m):
            out.append({"id": g["id"], "kind": g["kind"], "n": len(m), "ignored": g["ignored"]})
    return out


def exclusions(limit=200):
    """One entry per "Not a duplicate" click: the file, and how many others it was kept apart from."""
    return [dict(r) for r in db.q(
        "SELECT MIN(id) id, me_title, me_path, COUNT(*) n, MAX(created) created, "
        "GROUP_CONCAT(CASE WHEN a_path = me_path THEN b_title ELSE a_title END, ' · ') others "
        "FROM dupe_excl GROUP BY COALESCE(tok, id) ORDER BY created DESC LIMIT ?", (limit,))]


def unexclude(eid):
    r = db.q("SELECT tok FROM dupe_excl WHERE id=?", (eid,), one=True)
    if r and r["tok"]:
        db.x("DELETE FROM dupe_excl WHERE tok=?", (r["tok"],))
        db.x("DELETE FROM dupe_pins WHERE tok=?", (r["tok"],))
    else:
        db.x("DELETE FROM dupe_excl WHERE id=?", (eid,))
    rebuild(quiet=True)


def find_groups():
    """[(kind, [song ids])] for the whole library."""
    thr, tol, want_likely = LEVELS.get(Config.DUPES_LEVEL, LEVELS["normal"])
    rows = [dict(r) for r in db.q("SELECT id, path, folder, title, artist, albumartist, duration, ahash, fp FROM songs")]
    by_id = {r["id"]: r for r in rows}
    uf, edge = _UF(), {}
    excl, excl_same = set(), set()
    for r in db.q("SELECT a, b, a_path, b_path FROM dupe_excl"):
        if r["a"] != r["b"]:
            excl.add((r["a"], r["b"]))
        else:   # two copies of the very same audio: told apart by the folders they live in
            fa, fb = sorted((os.path.dirname(r["a_path"] or ""), os.path.dirname(r["b_path"] or "")))
            excl_same.add((r["a"], fa, fb))

    def link(a, b, kind):
        ia, ib = identity(by_id[a]), identity(by_id[b])
        if ia != ib and (min(ia, ib), max(ia, ib)) in excl:      # you said these two aren't duplicates
            return
        if ia == ib:
            fa, fb = sorted((by_id[a]["folder"] or "", by_id[b]["folder"] or ""))
            if (ia, fa, fb) in excl_same:
                return
        uf.union(a, b)
        key = (min(a, b), max(a, b))
        rank = {"exact": 0, "recording": 1, "likely": 2}
        if key not in edge or rank[kind] < rank[edge[key]]:
            edge[key] = kind

    # 1. identical audio
    by_hash = {}
    for r in rows:
        if r["ahash"]:
            by_hash.setdefault(r["ahash"], []).append(r["id"])
    for ids in by_hash.values():
        for i, x in enumerate(ids[:60]):        # every pair, so taking one file out doesn't split the rest
            for y in ids[i + 1:60]:
                link(x, y, "exact")

    # 2. same recording: fingerprints. Candidates share several fingerprint "words" (top bits)
    #    and have a similar length; only those pairs are compared in full.
    fps = {r["id"]: _fp_array(r["fp"]) for r in rows if r["fp"]}
    buckets = {}
    for sid, a in fps.items():
        for i in range(8, min(len(a), 200), 6):
            buckets.setdefault(a[i] >> 12, set()).add(sid)
    shared = {}
    for ids in buckets.values():
        if 1 < len(ids) <= 40:
            ids = sorted(ids)
            for i, x in enumerate(ids):
                for y in ids[i + 1:]:
                    shared[(x, y)] = shared.get((x, y), 0) + 1
    for (x, y), n in shared.items():
        if n < 3 or uf.find(x) == uf.find(y):
            continue
        dx, dy = by_id[x]["duration"] or 0, by_id[y]["duration"] or 0
        if dx and dy and abs(dx - dy) > max(6.0, 0.04 * max(dx, dy)):
            continue
        if similarity(fps[x], fps[y]) >= thr:
            link(x, y, "recording")

    # 3. possible: same title, a shared singer or director, nearly the same length, other folder
    if want_likely:
        by_title = {}
        for r in rows:
            t = _norm_title(r["title"])
            if len(t) >= 3:
                by_title.setdefault(t, []).append(r)
        for group in by_title.values():
            if len(group) < 2 or len(group) > 30:
                continue
            for i, a in enumerate(group):
                for b in group[i + 1:]:
                    if a["folder"] == b["folder"] or uf.find(a["id"]) == uf.find(b["id"]):
                        continue
                    da, dbb = a["duration"] or 0, b["duration"] or 0
                    if not da or not dbb or abs(da - dbb) > tol:
                        continue
                    pa, pb = _people(a["artist"]), _people(b["artist"])
                    same_md = a["albumartist"] and a["albumartist"].lower() == (b["albumartist"] or "").lower() \
                        and a["albumartist"] != Config.VARIOUS_ARTISTS
                    if (pa & pb) or same_md or (not pa and not pb):
                        link(a["id"], b["id"], "likely")

    # 4. groups you've already trimmed: the files left in them stay together (found by path, or by
    #    audio hash after a rename). Pairs you split apart stay apart: link() checks that.
    by_path = {r["path"]: r["id"] for r in rows}
    by_ident = {}
    for r in rows:
        by_ident.setdefault(identity(r), []).append(r["id"])
    for p in db.q("SELECT kind, members FROM dupe_pins"):
        got = []
        for ident, path in json.loads(p["members"] or "[]"):
            sid = by_path.get(path) or next((i for i in by_ident.get(ident, []) if i not in got), None)
            if sid and sid not in got:
                got.append(sid)
        kind = p["kind"] if p["kind"] in ("exact", "recording", "likely") else "likely"
        for i, x in enumerate(got[:40]):
            for y in got[i + 1:40]:
                link(x, y, kind)

    groups, kinds = {}, {}
    for (a, b), kind in edge.items():
        root = uf.find(a)
        groups.setdefault(root, set()).update((a, b))
        kinds.setdefault(root, set()).add(kind)
    out = []
    for root, ids in groups.items():
        k = kinds[root]
        kind = "likely" if "likely" in k else "recording" if "recording" in k else "exact"
        out.append((kind, sorted(ids)))
    return out


def quality(r):
    """Higher is better: lossless first, then bit depth / bitrate, then complete tags, then plays."""
    q = (r.get("lossless") or 0) * 10_000_000
    q += (r.get("bits") or 0) * 100_000
    q += min(r.get("bitrate") or 0, 9_999_999) // 10
    q += (r.get("tag_score") or 0) * 1000 + (r.get("plays") or 0) * 10
    p = (r.get("path") or "").lower()
    if re.search(r"(www\.|\.com|collection|hits|songs\)|mp3)", p):   # bundle folders lose ties
        q -= 500
    return q


def _gkey(paths):
    return hashlib.sha1("\n".join(sorted(paths)).encode()).hexdigest()


def rebuild(quiet=False):
    """Recompute every group; keeps 'not duplicates' choices for groups that are still the same.
    quiet: the change came from you (taking a file out), so reshaped groups aren't announced as new."""
    found = find_groups()
    old = {r["gkey"]: dict(r) for r in db.q("SELECT gkey, ignored, notified, found FROM dupe_groups")}
    keep_keys, new = set(), 0
    for kind, ids in found:
        rows = [dict(r) for r in db.q(f"SELECT * FROM songs WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))]
        if len(rows) < 2:
            continue
        rows.sort(key=quality, reverse=True)
        keep = rows[0]
        key = _gkey([r["path"] for r in rows])
        keep_keys.add(key)
        saving = sum(r.get("size") or 0 for r in rows[1:])
        if key in old:
            db.x("UPDATE dupe_groups SET kind=?, members=?, keep_id=?, saving=? WHERE gkey=?",
                 (kind, json.dumps([r["id"] for r in rows]), keep["id"], saving, key))
        else:
            db.x("INSERT INTO dupe_groups(gkey, kind, members, keep_id, saving, found, notified) VALUES(?,?,?,?,?,?,?)",
                 (key, kind, json.dumps([r["id"] for r in rows]), keep["id"], saving, time.time(), 1 if quiet else 0))
            new += 0 if quiet else 1
    for key in old:
        if key not in keep_keys:
            db.x("DELETE FROM dupe_groups WHERE gkey=?", (key,))
    return new


def scan(folders=None, notify_new=True):
    """Analyze songs that changed (all, or only those in `folders`), then regroup the library."""
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, phase="Reading files", done=0, total=0, started=time.time(), finished=None,
                      error=None, new=0)
    try:
        if not folders:
            # a full check starts by looking at the disk again, so folders you added, renamed or
            # deleted outside Isaiarr (File Station, another app) are picked up first
            _state["phase"] = "Looking for new and removed songs"
            songs.index_all()
        if folders:
            marks = ",".join("?" * len(folders))
            todo = db.q(f"SELECT id, path, mtime FROM songs WHERE folder IN ({marks}) AND "
                        "(fp_mtime IS NULL OR fp_mtime != mtime)", tuple(folders))
        else:
            todo = db.q("SELECT id, path, mtime FROM songs WHERE fp_mtime IS NULL OR fp_mtime != mtime")
        _state["total"] = len(todo)
        use_fp = Config.DUPES_FINGERPRINT
        _state["phase"] = "Listening to songs" if use_fp and fpcalc_ok() else "Reading files"
        for i, r in enumerate(todo):
            analyze(dict(r), use_fp)
            _state["done"] = i + 1
        _state["phase"] = "Comparing"
        new = rebuild()
        _state["new"] = new
        db.state_set("dupes_scan", {"at": time.time(), "new": new})
        if new:
            db.log(f"Duplicates: {new} new group(s) found")
        if notify_new and new and Config.DUPES_NOTIFY:
            _notify()
    except Exception as e:  # noqa: BLE001
        _state["error"] = str(e)
        db.log(f"Duplicate check failed: {e}", "error")
    finally:
        _state.update(running=False, finished=time.time(), phase="")
    return True


def prune_missing(groups_rows):
    """Drop songs that are no longer on disk (deleted outside Isaiarr) from the groups being shown.
    Returns True if anything was removed, so the page reloads the groups."""
    if not songs.library_online():
        return False                      # the music folder isn't reachable: don't mistake that for deletions
    from . import plexfix
    hidden = set(plexfix.staged().values())          # being re-added in Plex: back in a moment
    gone_files, gone_dirs = set(), set()
    for g in groups_rows:
        for s in g["songs"]:
            if not os.path.exists(s["path"]) and s["folder"] not in hidden:
                gone_files.add(s["path"])
                if not os.path.isdir(s["folder"]):
                    gone_dirs.add(s["folder"])
    if not gone_files:
        return False
    for d in gone_dirs:
        db.x("DELETE FROM songs WHERE folder=? OR folder LIKE ?", (d, d.rstrip("/") + "/%"))
    for p in gone_files:
        db.x("DELETE FROM songs WHERE path=?", (p,))
    rebuild(quiet=True)
    db.log(f"Duplicates: {len(gone_files)} file(s) no longer on disk were taken out of the list")
    return True


def _run(folders):
    scan(folders=folders)
    while _pending:
        nxt = sorted(_pending)
        _pending.clear()
        scan(folders=nxt)


def start_scan(folders=None):
    if _state["running"]:
        if folders:
            _pending.update(folders)
        return False
    threading.Thread(target=_run, args=(folders,), name="dupes", daemon=True).start()
    return True


def _notify():
    from . import notify
    rows = db.q("SELECT id, kind FROM dupe_groups WHERE notified=0 AND ignored=0")
    if not rows:
        return
    n = len(rows)
    exact = sum(1 for r in rows if r["kind"] == "exact")
    url = (Config.PUBLIC_URL.rstrip("/") + "/duplicates") if Config.PUBLIC_URL else ""
    text = (f"🔁 Isaiarr found {n} new duplicate group{'s' if n != 1 else ''}"
            + (f" ({exact} identical)" if exact else "") + (f". Review: {url}" if url else "."))
    if notify.send(content=text):
        db.x("UPDATE dupe_groups SET notified=1 WHERE notified=0")


# ------------------------------------------------------------------ queries for the page

def groups(kind=None, ignored=False, limit=20, offset=0):
    where, args = ["ignored=?"], [1 if ignored else 0]
    if kind:
        where.append("kind=?")
        args.append(kind)
    rows = [dict(r) for r in db.q(
        f"SELECT * FROM dupe_groups WHERE {' AND '.join(where)} "
        "ORDER BY CASE kind WHEN 'exact' THEN 0 WHEN 'recording' THEN 1 ELSE 2 END, saving DESC, id "
        f"LIMIT ? OFFSET ?", (*args, limit, offset))]
    for g in rows:
        ids = json.loads(g["members"] or "[]")
        got = {r["id"]: dict(r) for r in db.q(
            f"SELECT id, path, folder, title, artist, albumartist, film, year, duration, size, bitrate, sample_rate, "
            f"bits, codec, lossless, tag_score, plays, has_art, has_lyrics FROM songs WHERE id IN ({','.join('?' * len(ids))})",
            tuple(ids))} if ids else {}
        g["songs"] = [got[i] for i in ids if i in got]
    return rows


def count(kind=None, ignored=False):
    where, args = ["ignored=?"], [1 if ignored else 0]
    if kind:
        where.append("kind=?")
        args.append(kind)
    return db.q(f"SELECT COUNT(*) n FROM dupe_groups WHERE {' AND '.join(where)}", tuple(args), one=True)["n"]


# ------------------------------------------------------------------ bin

def bin_dir():
    return Config.DUPES_BIN or os.path.join(Config.MUSIC_DIR, ".isaiarr-bin")


def _ensure_bin():
    d = bin_dir()
    os.makedirs(d, exist_ok=True)
    marker = os.path.join(d, ".plexignore")
    if not os.path.exists(marker):
        with open(marker, "w") as fh:
            fh.write("# Isaiarr duplicates bin: keep Plex out of here\n*\n")
    return d


def move_to_bin(song_id):
    """Move one song (and its .lrc) into the bin. Returns the folder it came from."""
    r = db.q("SELECT * FROM songs WHERE id=?", (song_id,), one=True)
    if not r or not os.path.exists(r["path"]):
        return None
    d = _ensure_bin()
    rel = os.path.relpath(r["path"], Config.MUSIC_DIR)
    if rel.startswith(".."):
        rel = os.path.basename(r["path"])
    dest = os.path.join(d, time.strftime("%Y-%m-%d"), rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    base, ext = os.path.splitext(dest)
    n = 1
    while os.path.exists(dest):
        dest = f"{base} ({n}){ext}"
        n += 1
    shutil.move(r["path"], dest)
    lrc = os.path.splitext(r["path"])[0] + ".lrc"
    if os.path.exists(lrc):
        shutil.move(lrc, os.path.splitext(dest)[0] + ".lrc")
    db.x("INSERT INTO dupe_moves(orig_path, bin_path, size, title, moved) VALUES(?,?,?,?,?)",
         (r["path"], dest, r["size"] or 0, r["title"], time.time()))
    db.x("DELETE FROM songs WHERE id=?", (song_id,))
    return r["folder"]


def resolve(gid, keep_id):
    """Keep one song of a group, move the others to the bin."""
    from . import plex, watcher
    g = db.q("SELECT * FROM dupe_groups WHERE id=?", (gid,), one=True)
    if not g:
        return 0, []
    ids = json.loads(g["members"] or "[]")
    if keep_id not in ids:
        keep_id = g["keep_id"]
    folders, moved = set(), 0
    for sid in ids:
        if sid == keep_id:
            continue
        r = db.q("SELECT folder FROM songs WHERE id=?", (sid,), one=True)
        if r:
            watcher.suppress(r["folder"])
        f = move_to_bin(sid)
        if f:
            folders.add(f)
            moved += 1
    db.x("DELETE FROM dupe_groups WHERE id=?", (gid,))
    for f in folders:
        watcher.suppress(f)
        if Config.PLEX_RESCAN:
            plex.refresh(f)
    if moved:
        db.log(f"Duplicates: moved {moved} file(s) to the bin")
    return moved, sorted(folders)


def bin_items(limit=200):
    return [dict(r) for r in db.q("SELECT * FROM dupe_moves WHERE restored IS NULL AND deleted IS NULL "
                                  "ORDER BY moved DESC LIMIT ?", (limit,))]


def restore(move_id):
    from . import plex, watcher
    m = db.q("SELECT * FROM dupe_moves WHERE id=?", (move_id,), one=True)
    if not m or m["restored"] or m["deleted"] or not os.path.exists(m["bin_path"]):
        return False
    dest = m["orig_path"]
    if os.path.exists(dest):
        base, ext = os.path.splitext(dest)
        dest = f"{base} (restored){ext}"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    watcher.suppress(os.path.dirname(dest))
    shutil.move(m["bin_path"], dest)
    lrc = os.path.splitext(m["bin_path"])[0] + ".lrc"
    if os.path.exists(lrc):
        shutil.move(lrc, os.path.splitext(dest)[0] + ".lrc")
    db.x("UPDATE dupe_moves SET restored=? WHERE id=?", (time.time(), move_id))
    songs.index_folder(os.path.dirname(dest))
    if Config.PLEX_RESCAN:
        plex.refresh(os.path.dirname(dest))
    return True


def empty_bin(older_than_days=None):
    cutoff = time.time() - older_than_days * 86400 if older_than_days else None
    n = 0
    for m in db.q("SELECT * FROM dupe_moves WHERE restored IS NULL AND deleted IS NULL"):
        if cutoff and m["moved"] > cutoff:
            continue
        for p in (m["bin_path"], os.path.splitext(m["bin_path"])[0] + ".lrc"):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        db.x("UPDATE dupe_moves SET deleted=? WHERE id=?", (time.time(), m["id"]))
        n += 1
    # tidy empty dated folders
    d = bin_dir()
    if os.path.isdir(d):
        for root, dirs, files in os.walk(d, topdown=False):
            if root != d and not os.listdir(root):
                try:
                    os.rmdir(root)
                except OSError:
                    pass
    if n:
        db.log(f"Duplicates bin: {n} file(s) deleted for good")
    return n
