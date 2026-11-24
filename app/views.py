"""Turn stored proposals into what the review-queue screens show (design: docs/design)."""
import hashlib
from collections import Counter

from .config import Config
from .matcher import SOURCE_LABEL

# Tinted tiles for albums without cover art (from the design's sample tones)
TONES = ["#3B2A4A", "#4A2A22", "#22384A", "#2A4A36", "#3A3A3A", "#4A3A22", "#2A3A4A", "#4A2238"]

ALBUM_FIELDS = [("album", "Album"), ("albumartist", "Album artist"), ("composer", "Composer"),
                ("date", "Release date"), ("genre", "Genre"), ("label", "Record label"), ("grouping", "Grouping"),
                ("compilation", "Compilation")]


def tone(name):
    h = int(hashlib.md5((name or "?").encode("utf-8")).hexdigest(), 16)
    return TONES[h % len(TONES)]


def initial(name):
    for ch in (name or "").strip():
        if ch.isalnum():
            return ch.upper()
    return "♪"


def pct(conf):
    """Internal confidence is 0-10; the UI shows a percentage."""
    return None if conf is None else int(round(float(conf) * 10))


def conf_class(p):
    if p is None:
        return "c-none"
    return "c-ok" if p >= 95 else "c-warn" if p >= 80 else "c-bad"


def source_text(prop):
    if not prop:
        return ""
    a = prop.get("album") or {}
    src = a.get("source") or ("itunes" if a.get("collection_id") else "")
    parts = [SOURCE_LABEL.get(src, src)] if src else []
    if prop.get("collection"):
        parts = ["Collection"]
    elif prop.get("fallback"):
        parts = ["Song by song"]
    lyr = {f.get("lyrics_source") for f in prop.get("files", []) if f.get("lyrics_source")}
    for s in sorted(lyr):
        if SOURCE_LABEL.get(s, s) not in parts:
            parts.append(SOURCE_LABEL.get(s, s))
    return " + ".join(parts) or "No source"


def cover_url(prop):
    a = (prop or {}).get("album") or {}
    u = a.get("thumb") or a.get("artwork") or ""
    return u.replace("100x100bb", "400x400bb").replace("150x150", "500x500")


def card(a):
    """One album card in the review-queue list."""
    p = a.get("proposal") or {}
    al = p.get("album") or {}
    year = al.get("date") or ""
    files = len(p.get("files", [])) or a.get("n_files") or 0
    name = a.get("title") or al.get("album") or a["path"].rstrip("/").split("/")[-1]
    c = pct(a.get("confidence"))
    return {"id": a["id"], "album": name, "director": a.get("album_artist") or al.get("album_artist") or "Unknown",
            "year": year or "—", "tracks": files, "source": source_text(p), "conf": c, "conf_class": conf_class(c),
            "status": a["status"], "approved": a["status"] in ("applying", "applied"),
            "writing": a["status"] == "applying", "cover": cover_url(p), "tone": tone(name), "initial": initial(name),
            "sent_back": bool(a.get("sent_back"))}


def _common(values):
    vals = [v for v in values if v not in (None, "")]
    if not vals:
        return "", 0
    c = Counter(vals)
    top, _ = c.most_common(1)[0]
    return top, len(c)


def detail(a):
    """Album header, album-tag rows and track rows for the detail panel."""
    p = a.get("proposal") or {}
    files = p.get("files", [])
    al = p.get("album") or {}
    rows = []
    for key, label in ALBUM_FIELDS:
        cur, n_cur = _common(str(f["current"].get(key) or "") for f in files)
        proposed, _ = _common(str((f["new"].get(key) if key in f.get("changes", {}) else f["current"].get(key)) or "")
                              for f in files)
        kinds = [f["changes"][key] for f in files if key in f.get("changes", {})]
        action = "fix" if "fix" in kinds else "fill" if kinds else "ok"
        if not cur and not proposed:
            continue
        if key == "compilation":
            cur, proposed = ("Yes" if cur else ""), ("Yes" if proposed else "")
        rows.append({"label": label, "current": cur, "mixed": n_cur > 1, "proposed": proposed or cur,
                     "action": action, "count": len(kinds), "of": len(files)})
    # cover art
    have_art = sum(1 for f in files if f.get("has_art"))
    art_src = SOURCE_LABEL.get(al.get("source") or "itunes", "") if al.get("artwork") else ""
    if files:
        will_add = bool(al.get("artwork")) and have_art < len(files) and Config.DEFAULT_ARTWORK != "none"
        rows.append({"label": "Cover art",
                     "current": ("Embedded" if have_art == len(files) else f"In {have_art} of {len(files)} files")
                     if have_art else "",
                     "mixed": 0 < have_art < len(files),
                     "proposed": (f"{Config.ARTWORK_SIZE} × {Config.ARTWORK_SIZE} from {art_src}"
                                  if art_src == "iTunes" else f"From {art_src}") if will_add
                     else ("Embedded" if have_art else "None found"),
                     "action": "fill" if will_add else "ok", "count": len(files) - have_art, "of": len(files)})
        synced = sum(1 for f in files if f.get("synced") and not f.get("has_lyrics"))
        plain = sum(1 for f in files if f.get("lyrics") and not f.get("synced") and not f.get("has_lyrics"))
        had = sum(1 for f in files if f.get("has_lyrics"))
        if synced or plain or had:
            parts = [f"{synced} synced" if synced else "", f"{plain} plain" if plain else ""]
            rows.append({"label": "Lyrics", "current": f"In {had} of {len(files)} files" if had else "",
                         "mixed": False, "proposed": " · ".join(x for x in parts if x) or "No new lyrics",
                         "action": "fill" if (synced or plain) and Config.DEFAULT_LYRICS != "none" else "ok",
                         "count": synced + plain, "of": len(files)})

    tracks = []
    for i, f in enumerate(files):
        kind = f.get("match", "album" if f.get("itunes_track") else None)
        new, cur = f.get("new") or {}, f.get("current") or {}
        no = new.get("tracknumber") or cur.get("tracknumber") or str(i + 1)
        lyr = ("Synced" if f.get("synced") else "Plain" if f.get("lyrics")
               else "In file" if f.get("has_lyrics") else "Instrumental" if f.get("lyrics_note") == "instrumental"
               else "None found")
        tracks.append({"no": f"{int(no):02d}" if str(no).isdigit() else no, "old": f["file"],
                       "new": f.get("rename_to") or "", "title": new.get("title") or cur.get("title") or f["file"],
                       "singers": new.get("artist") or cur.get("artist") or "—", "lyrics": lyr,
                       "matched": bool(kind), "song": kind == "track",
                       "sound": (f.get("source_track") or {}).get("via") == "shazam",
                       "from": (f.get("source_track") or {}).get("album") if kind == "track" else "",
                       "n_changes": len(f.get("changes") or {})})
    matched = sum(1 for t in tracks if t["matched"])
    name = a.get("title") or al.get("album") or a["path"].rstrip("/").split("/")[-1]
    c = pct(a.get("confidence"))
    return {"id": a["id"], "album": name, "path": a["path"], "source": source_text(p).upper(), "conf": c,
            "conf_class": conf_class(c), "fields": rows, "tracks": tracks, "matched": matched,
            "cover": cover_url(p), "tone": tone(name), "initial": initial(name), "status": a["status"],
            "approved": a["status"] in ("applying", "applied"), "collection": bool(p.get("collection")),
            "fallback": bool(p.get("fallback")), "notes": p.get("notes") or [], "error": a.get("error"),
            "md_check": p.get("md_check"), "n_sound": sum(1 for t in tracks if t["sound"]),
            "auto_why": (p.get("auto") or {}).get("why") if Config.AUTO_APPROVE == "on" else None,
            "rename": Config.RENAME_ENABLED and any(t["new"] for t in tracks),
            "plex": bool(Config.PLEX_URL and Config.PLEX_TOKEN and Config.PLEX_RESCAN),
            "discord": bool(Config.DISCORD_WEBHOOK and Config.NOTIFY_APPLIED),
            "sent_back": a.get("sent_back"), "sent_from": a.get("sent_from"),
            "nothing_found": not any(t.get("n_changes") for t in tracks) and not any(r["action"] != "ok" for r in rows)}


def scan_log(scan_id, limit=40):
    """Lines for the Scan page's live log, newest first: (status, text)."""
    from . import db
    rows = db.q("SELECT * FROM albums WHERE scan_id=? AND status NOT IN ('queued','analyzing') "
                "ORDER BY updated DESC LIMIT ?", (scan_id, limit))
    out = []
    for r in rows:
        a = db._album_dict(r)
        p = a.get("proposal") or {}
        name = a.get("title") or a["path"].rstrip("/").split("/")[-1]
        if a["status"] in ("nomatch", "error"):
            why = a.get("error") or "no candidate on " + " or ".join(SOURCE_LABEL.get(s, s) for s in Config.SOURCES)
            out.append(("MISS", f"{_rel(a['path'])}  ·  {why}"))
            continue
        if a["status"] == "uptodate":
            out.append(("SKIP", f"{name} / tags already correct"))
            continue
        for row in detail(a)["fields"][:6]:
            if row["action"] == "fix" and row["current"]:
                out.append(("FIX", f"{name} / {row['label'].lower()} “{row['current']}” → “{row['proposed']}”"))
            elif row["action"] == "fill" and row["label"] not in ("Cover art", "Lyrics"):
                out.append(("FILL", f"{name} / {row['label'].lower()} = {row['proposed']}"))
        out.append(("MATCH", f"{name}  ←  {source_text(p)} · {pct(a.get('confidence'))}%"))
    return out[:limit]


def _rel(path):
    root = Config.MUSIC_DIR
    return path[len(root):].lstrip("/") if path.startswith(root) else path
