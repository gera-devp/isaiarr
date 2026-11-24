"""Track check: were the right titles written to the right files?

After a bulk approve, a song can end up with another song's title (tracks shifted within an
album, a wrong song-by-song match). Isaiarr already keeps everything needed to spot that, so the
check needs no new lookups:

  * length   the file's length vs the length of the song it was matched to
  * name     the file's original name (from the backups) vs the title now in the file
  * swapped  the original name or the length fits another track of the same album better

Flagged tracks get a severity (1 low, 2 likely, 3 almost certain). The AI can give a second
opinion on them (it's asked only about the flagged ones, in small batches).
"""
import json
import os
import re
import threading
import time

from rapidfuzz import fuzz

from . import db

_state = {"running": False, "done": 0, "total": 0, "finished": None, "error": None}
_lock = threading.Lock()
_ai = {"running": False, "done": 0, "total": 0, "error": None}

GENERIC = re.compile(r"^(track|audio ?track|song|untitled|unknown)?\s*\d*$", re.I)
NOISE = re.compile(r"\b(from|feat|ft|remix|version|original|motion|picture|soundtrack|ost|kbps|flac|mp3|"
                   r"www|com|tamil|hindi|telugu|movie|film|songs?|hq|hd|lyrics?|video|audio|full|karaoke)\b", re.I)


def clean(s):
    s = os.path.splitext(os.path.basename(s or ""))[0] if ("/" in (s or "") or "." in (s or "")[-5:]) else (s or "")
    s = s.lower()
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)                 # (From "Film"), [320kbps]
    s = re.sub(r"^\s*(cd\s*\d+\s*[-.]?\s*)?\d{1,3}\s*[-._)]?\s*", " ", s)   # leading track number
    s = re.sub(r"[_\-.]+", " ", s)
    s = NOISE.sub(" ", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return " ".join(s.split())


def skeleton(s):
    """Spelling-proof form for Tamil/Hindi words in English letters: no vowels, no doubled letters
    ('Kannalanae', 'Kannalane' and 'Kanalane' all become 'knln')."""
    s = re.sub(r"[aeiouy\s]", "", s)
    s = s.replace("zh", "l").replace("th", "t").replace("dh", "d").replace("sh", "s").replace("ph", "f")
    return re.sub(r"(.)\1+", r"\1", s)


def similar(a, b):
    """0–100: how much a file name and a title look like the same song."""
    a, b = clean(a), clean(b)
    if not a or not b:
        return 100
    plain = max(fuzz.token_set_ratio(a, b), fuzz.partial_ratio(a, b) if min(len(a), len(b)) >= 5 else 0)
    ska, skb = skeleton(a), skeleton(b)
    sk = max(fuzz.ratio(ska, skb), fuzz.partial_ratio(ska, skb) if min(len(ska), len(skb)) >= 4 else 0) if ska and skb else 0
    return max(plain, sk)


def _origins(album_id):
    """current path -> the file's name before Isaiarr first renamed it (from the backups)."""
    origin = {}
    for r in db.q("SELECT old_path, new_path FROM backups WHERE album_id=? AND restored=0 ORDER BY batch, id", (album_id,)):
        if r["old_path"] and r["new_path"]:
            origin[r["new_path"]] = origin.get(r["old_path"], r["old_path"])
    return origin


def check_album(album_id):
    """Check one album's tracks; replaces its rows in track_check. Returns the flagged rows."""
    a = db.album_get(album_id)
    db.x("DELETE FROM track_check WHERE album_id=? AND dismissed=0", (album_id,))
    if not a or a["status"] not in ("applied", "uptodate") or not a.get("proposal"):
        return []
    prop = a["proposal"]
    origin = _origins(album_id)
    renamed = {}
    for o, n in [(r["old_path"], r["new_path"]) for r in db.q(
            "SELECT old_path, new_path FROM backups WHERE album_id=? AND restored=0 ORDER BY batch, id", (album_id,))]:
        renamed[o] = n
    songs = {r["path"]: dict(r) for r in db.q("SELECT path, title, duration FROM songs WHERE folder=?", (a["path"],))}
    rows = []
    for f in prop.get("files", []):
        st = f.get("source_track") or {}
        path = f["path"]
        while path in renamed and renamed[path] != path:
            path = renamed[path]
        if path not in songs:
            alt = os.path.join(a["path"], f.get("rename_to") or f["file"])
            path = alt if alt in songs else path
        cur = songs.get(path)
        if not cur:
            continue
        rows.append({"path": path, "title": cur["title"] or "", "dur": cur["duration"] or f.get("duration") or 0,
                     "src_title": st.get("title") or "", "src_dur": st.get("duration") or 0,
                     "orig": os.path.basename(origin.get(path) or f["path"])})
    titles = [r["title"] for r in rows]
    flagged = []
    dismissed = {r["path"] for r in db.q("SELECT path FROM track_check WHERE album_id=? AND dismissed=1", (album_id,))}
    for r in rows:
        if r["path"] in dismissed:
            continue
        reasons, sev = [], 0
        d, sd = r["dur"], r["src_dur"]
        if d and sd:
            off = abs(d - sd)
            if off > max(15, 0.12 * sd):
                reasons.append(f"the file is {int(d // 60)}:{int(d % 60):02d} long, the matched song {int(sd // 60)}:{int(sd % 60):02d}")
                sev = max(sev, 2)
            elif off > max(8, 0.06 * sd):
                reasons.append(f"lengths differ by {int(off)} s")
                sev = max(sev, 1)
            # does the length fit another track of this album much better?
            better = [o for o in rows if o is not r and o["src_dur"] and abs(d - o["src_dur"]) <= 2 < off]
            if better:
                reasons.append(f"its length fits “{better[0]['title']}” instead")
                sev = max(sev, 2 if off > 8 else 1)
        name = clean(r["orig"])
        if name and not GENERIC.match(name) and len(re.sub(r"\W", "", name)) >= 4:
            own = similar(r["orig"], r["title"])
            others = [(similar(r["orig"], t), t) for t in titles if t and t != r["title"]]
            best = max(others) if others else (0, "")
            if best[0] >= 80 and own < 60:
                reasons.append(f"the original file name “{r['orig']}” looks like “{best[1]}”")
                sev = max(sev, 3)
            elif own < 45:
                reasons.append(f"the original file name “{r['orig']}” doesn't look like the title")
                sev = max(sev, 2 if own < 30 else 1)
        if reasons:
            if len(reasons) >= 2:
                sev = min(3, sev + 1)
            row = {"path": r["path"], "album_id": album_id, "folder": a["path"], "title": r["title"],
                   "orig_name": r["orig"], "file_dur": r["dur"], "src_dur": r["src_dur"], "src_title": r["src_title"],
                   "reasons": json.dumps(reasons), "severity": sev, "at": time.time()}
            db.x("INSERT OR REPLACE INTO track_check(path, album_id, folder, title, orig_name, file_dur, src_dur, src_title, "
                 "reasons, severity, at, dismissed) VALUES(:path,:album_id,:folder,:title,:orig_name,:file_dur,:src_dur,"
                 ":src_title,:reasons,:severity,:at,0)", row)
            flagged.append(row)
    return flagged


def run_all():
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, done=0, total=0, error=None, started=time.time())
    try:
        ids = [r["id"] for r in db.q("SELECT id FROM albums WHERE status IN ('applied', 'uptodate')")]
        _state["total"] = len(ids)
        n = 0
        for i, aid in enumerate(ids):
            n += len(check_album(aid))
            _state["done"] = i + 1
        db.state_set("trackcheck", {"at": time.time(), "albums": len(ids), "flagged": n})
        db.log(f"Track check: {n} track(s) in {len(ids)} album(s) look like they have the wrong title")
    except Exception as e:  # noqa: BLE001
        _state["error"] = str(e)
        db.log(f"Track check failed: {e}", "error")
    finally:
        _state.update(running=False, finished=time.time())
    return True


def start():
    if _state["running"]:
        return False
    threading.Thread(target=run_all, name="trackcheck", daemon=True).start()
    return True


def status():
    return {**_state, "ai": dict(_ai), "last": db.state_get("trackcheck") or {}}


def flagged(min_sev=1, limit=1000):
    return [dict(r) | {"reasons": json.loads(r["reasons"] or "[]"), "ai": json.loads(r["ai"]) if r["ai"] else None}
            for r in db.q("SELECT * FROM track_check WHERE dismissed=0 AND severity>=? "
                          "ORDER BY severity DESC, folder, path LIMIT ?", (min_sev, limit))]


def count(min_sev=2):
    return db.q("SELECT COUNT(*) n FROM track_check WHERE dismissed=0 AND (severity>=? OR json_extract(ai, '$.verdict')='wrong')",
                (min_sev,), one=True)["n"]


def dismiss(paths):
    for p in paths:
        db.x("UPDATE track_check SET dismissed=1 WHERE path=?", (p,))


# ---------------------------------------------------------------- AI second opinion

AI_SYSTEM = (
    "You check music tags for Indian film songs. For each track you get the film/album, the file's original "
    "name (before it was renamed), the title now written in the file, the file's length and the length of the "
    "song it was matched to. Decide whether the written title is the right song for that file. Tamil, Hindi, "
    "Telugu and Malayalam titles are written in English letters and spelt in many ways; treat spelling "
    "variants as the same song. Answer only JSON: {\"tracks\": [{\"id\": <id>, \"verdict\": \"ok\"|\"wrong\"|\"unsure\", "
    "\"reason\": \"<short>\", \"title\": \"<the right title if wrong, else empty>\"}]}")


def ask_ai(paths):
    """Second opinion on flagged tracks, in batches of 25. Returns how many got a verdict."""
    from . import ai
    rows = [dict(r) for r in db.q(f"SELECT t.*, a.title album FROM track_check t LEFT JOIN albums a ON a.id=t.album_id "
                                  f"WHERE t.path IN ({','.join('?' * len(paths))})", tuple(paths))] if paths else []
    done = 0
    for i in range(0, len(rows), 25):
        batch = rows[i:i + 25]
        items = [{"id": j, "album": r["album"] or os.path.basename(r["folder"]), "original_file_name": r["orig_name"],
                  "title_now": r["title"], "file_length_s": round(r["file_dur"] or 0),
                  "matched_song_length_s": round(r["src_dur"] or 0)} for j, r in enumerate(batch)]
        data, _u = ai.complete(AI_SYSTEM, json.dumps({"tracks": items}, ensure_ascii=False), purpose="trackcheck",
                               temperature=0.1)
        for v in (data or {}).get("tracks", []):
            try:
                r = batch[int(v.get("id"))]
            except (TypeError, ValueError, IndexError):
                continue
            verdict = v.get("verdict") if v.get("verdict") in ("ok", "wrong", "unsure") else "unsure"
            db.x("UPDATE track_check SET ai=? WHERE path=?",
                 (json.dumps({"verdict": verdict, "reason": (v.get("reason") or "")[:200],
                              "title": (v.get("title") or "")[:200], "at": time.time()}), r["path"]))
            done += 1
        _ai["done"] = min(len(rows), i + len(batch))
    return done


def start_ai(paths):
    """ask_ai in the background (the AI can take a minute for a few hundred tracks)."""
    if _ai["running"] or not paths:
        return False
    _ai.update(running=True, done=0, total=len(paths), error=None, started=time.time())

    def run():
        try:
            n = ask_ai(paths)
            wrong = db.q("SELECT COUNT(*) n FROM track_check WHERE json_extract(ai, '$.verdict')='wrong' AND dismissed=0",
                         one=True)["n"]
            db.log(f"Track check: the AI looked at {n} track(s); {wrong} flagged track(s) are wrong in its view")
        except Exception as e:  # noqa: BLE001
            _ai["error"] = str(e)
            db.log(f"Track check: asking the AI failed: {e}", "error")
        finally:
            _ai["running"] = False
    threading.Thread(target=run, name="trackcheck-ai", daemon=True).start()
    return True
