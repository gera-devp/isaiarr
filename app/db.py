"""SQLite storage: albums (review queue), jobs, backups, http cache, activity log."""
import json
import os
import sqlite3
import threading
import time

from .config import Config

_lock = threading.RLock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS albums (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL,           -- queued|analyzing|review|nomatch|applying|applied|rejected|skipped|error
    confidence REAL,
    collection_id INTEGER,
    title TEXT,
    album_artist TEXT,
    proposal TEXT,                  -- JSON
    candidates TEXT,                -- JSON list of alternative iTunes albums
    error TEXT,
    origin TEXT,                    -- watch|manual
    notified INTEGER DEFAULT 0,
    created REAL,
    updated REAL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,             -- analyze|apply|undo|setartist
    album_id INTEGER,
    payload TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created REAL,
    started REAL,
    finished REAL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS backups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id INTEGER NOT NULL,
    batch INTEGER NOT NULL,
    old_path TEXT NOT NULL,
    new_path TEXT NOT NULL,
    backup_file TEXT NOT NULL,
    extra_files TEXT,               -- JSON list of sidecars created/renamed
    created REAL,
    restored INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS http_cache (
    key TEXT PRIMARY KEY,
    value TEXT,
    created REAL
);
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    level TEXT,
    message TEXT
);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    mode TEXT,                      -- changed|all
    total INTEGER DEFAULT 0,
    started REAL,
    finished REAL,
    notified INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS songs (           -- one row per audio file, for playlists
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    folder TEXT,
    title TEXT, artist TEXT, albumartist TEXT, album TEXT, film TEXT,
    year INTEGER, genre TEXT, duration REAL, has_lyrics INTEGER DEFAULT 0,
    mtime REAL, indexed REAL,
    plex_key TEXT, rating REAL, plays INTEGER DEFAULT 0, last_played REAL,
    profile TEXT                            -- JSON {moods, kind, tempo} from the AI
);
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL, mode TEXT, prompt TEXT, params TEXT, title TEXT,
    status TEXT,                            -- working|ready|error
    items TEXT,                             -- JSON [{id, why}]
    notes TEXT, error TEXT,
    provider TEXT, model TEXT, in_tokens INTEGER DEFAULT 0, out_tokens INTEGER DEFAULT 0, cost REAL DEFAULT 0,
    plex_id TEXT, saved REAL,
    progress TEXT                           -- JSON [[tag, text], ...] shown while it's being made
);
CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, provider TEXT, model TEXT, purpose TEXT,
    in_tokens INTEGER, out_tokens INTEGER, cost REAL
);
CREATE TABLE IF NOT EXISTS dupe_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gkey TEXT UNIQUE,                       -- stable id: hash of the member paths
    kind TEXT,                              -- exact | recording | likely
    members TEXT,                           -- JSON [song ids]
    keep_id INTEGER, saving INTEGER,        -- suggested file to keep; bytes freed by moving the rest
    found REAL, ignored INTEGER DEFAULT 0, notified INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS dupe_excl (           -- "not a duplicate of these": never group the two again
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    a TEXT, b TEXT,                         -- song identities (audio hash, else path), a < b
    a_title TEXT, a_path TEXT, b_title TEXT, b_path TEXT, created REAL,
    tok TEXT, me_title TEXT, me_path TEXT,    -- one "Not a duplicate" click; the file it was about
    UNIQUE(a, b, a_path, b_path)
);
CREATE TABLE IF NOT EXISTS dupe_pins (           -- files left in a group after you took one out: kept together
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tok TEXT, kind TEXT, members TEXT,       -- members: [[identity, path], ...]
    created REAL
);
CREATE TABLE IF NOT EXISTS folder_policy (      -- your decision for a folder; wins over every re-check
    folder TEXT PRIMARY KEY,
    policy TEXT NOT NULL,                    -- film|various|collection|single|artist|keep
    albumartist TEXT, album TEXT,            -- empty: from the match (film/artist) or the rule
    reason TEXT, created REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS dupe_moves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    orig_path TEXT, bin_path TEXT, size INTEGER, title TEXT,
    moved REAL, restored REAL, deleted REAL
);
CREATE TABLE IF NOT EXISTS track_check (        -- tracks whose title may be another song's (see trackcheck.py)
    path TEXT PRIMARY KEY, album_id INTEGER, folder TEXT, title TEXT, orig_name TEXT,
    file_dur REAL, src_dur REAL, src_title TEXT, reasons TEXT, severity INTEGER,
    ai TEXT, dismissed INTEGER DEFAULT 0, at REAL
);
CREATE INDEX IF NOT EXISTS idx_songs_folder ON songs(folder);
CREATE INDEX IF NOT EXISTS idx_songs_aa ON songs(albumartist);
CREATE INDEX IF NOT EXISTS idx_albums_status ON albums(status);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""


def conn():
    global _conn
    with _lock:
        if _conn is None:
            os.makedirs(Config.DATA_DIR, exist_ok=True)
            _conn = sqlite3.connect(os.path.join(Config.DATA_DIR, "isaiarr.db"),
                                    check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.executescript(SCHEMA)
            _migrate(_conn)
        return _conn


def _migrate(c):
    """Additive schema changes for databases created by older versions."""
    cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
    if "priority" not in cols:
        c.execute("ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
    cols = {r[1] for r in c.execute("PRAGMA table_info(albums)")}
    if "auto" not in cols:   # 1 = written by auto-approve
        c.execute("ALTER TABLE albums ADD COLUMN auto INTEGER NOT NULL DEFAULT 0")
    scols = {r[1] for r in c.execute("PRAGMA table_info(songs)")}
    for col, decl in (("size", "INTEGER"), ("bitrate", "INTEGER"), ("sample_rate", "INTEGER"), ("bits", "INTEGER"),
                      ("codec", "TEXT"), ("lossless", "INTEGER DEFAULT 0"), ("ahash", "TEXT"), ("fp", "BLOB"),
                      ("fp_mtime", "REAL"), ("tag_score", "INTEGER"), ("has_art", "INTEGER DEFAULT 0"),
                      ("plex_artist", "TEXT"), ("plex_album", "TEXT"), ("plex_title", "TEXT"),
                      ("composer", "TEXT"), ("grouping", "TEXT"), ("compilation", "TEXT"), ("releasetype", "TEXT"),
                      ("tracknumber", "TEXT"), ("label", "TEXT"), ("date", "TEXT"), ("plex_album_key", "TEXT")):
        if scols and col not in scols:
            c.execute(f"ALTER TABLE songs ADD COLUMN {col} {decl}")
            if col in ("composer", "label"):
                c.execute("UPDATE songs SET mtime=NULL")      # re-read every file once to fill the new columns
    pcols = {r[1] for r in c.execute("PRAGMA table_info(playlists)")}
    if pcols and "progress" not in pcols:
        c.execute("ALTER TABLE playlists ADD COLUMN progress TEXT")
    for col, decl in (("n_fix", "INTEGER"), ("n_fill", "INTEGER"), ("various", "INTEGER DEFAULT 0"),
                      ("n_files", "INTEGER"), ("scan_id", "INTEGER"), ("overrides", "TEXT"),
                      ("sent_back", "REAL"), ("sent_from", "TEXT"), ("verify", "TEXT"),
                      ("plex_check", "TEXT"), ("plex_meta", "TEXT")):
        if col not in cols:
            c.execute(f"ALTER TABLE albums ADD COLUMN {col} {decl}")


def q(sql, args=(), one=False):
    with _lock:
        cur = conn().execute(sql, args)
        rows = cur.fetchall()
    if one:
        return rows[0] if rows else None
    return rows


def x(sql, args=()):
    with _lock:
        cur = conn().execute(sql, args)
        return cur.lastrowid


def reset_for_tests():
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None


# ---------- albums ----------

def album_upsert(path, status, origin="watch"):
    now = time.time()
    row = q("SELECT * FROM albums WHERE path=?", (path,), one=True)
    if row:
        x("UPDATE albums SET status=?, origin=?, updated=?, error=NULL, notified=0 WHERE id=?",
          (status, origin, now, row["id"]))
        return row["id"]
    return x("INSERT INTO albums(path,status,origin,created,updated) VALUES(?,?,?,?,?)",
             (path, status, origin, now, now))


def album_get(album_id):
    row = q("SELECT * FROM albums WHERE id=?", (album_id,), one=True)
    return _album_dict(row) if row else None


def album_by_path(path):
    row = q("SELECT * FROM albums WHERE path=?", (path,), one=True)
    return _album_dict(row) if row else None


def _album_dict(row):
    d = dict(row)
    for k in ("proposal", "candidates", "overrides", "verify", "plex_check", "plex_meta"):
        d[k] = json.loads(d[k]) if d.get(k) else None
    return d


def album_update(album_id, **fields):
    if not fields:
        return
    for k in ("proposal", "candidates", "overrides", "verify"):
        if k in fields and fields[k] is not None and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
    fields["updated"] = time.time()
    if fields.get("status") in ("applying", "applied", "rejected", "skipped") and "sent_back" not in fields:
        fields.update(sent_back=None, sent_from=None)     # you've dealt with an album you sent back
    cols = ", ".join(f"{k}=?" for k in fields)
    x(f"UPDATE albums SET {cols} WHERE id=?", (*fields.values(), album_id))


# Whitelisted sort columns (user input never reaches SQL directly). Text sorts ignore case;
# rows with no value sort last in either direction; ties break on id for a stable order.
ALBUM_SORTS = {
    "album": "COALESCE(NULLIF(title,''), path) COLLATE NOCASE",
    "artist": "NULLIF(album_artist,'') COLLATE NOCASE",
    "folder": "path COLLATE NOCASE",
    "confidence": "confidence",
    "status": "status",
    "updated": "updated",
    "tracks": "n_files",
}


def order_by(expr_map, key, direction, default):
    expr = expr_map.get(key) or expr_map[default]
    d = "ASC" if direction == "asc" else "DESC"
    return f" ORDER BY ({expr}) IS NULL, {expr} {d}, id {d}"


# Review-queue filters (see web.QUEUE_FILTERS); confidence is 0-10 internally
FILTERS = {
    "fixes": ("n_fix > 0", ()),
    "fill": ("COALESCE(n_fix,0) = 0 AND n_fill > 0", ()),
    "various": ("various = 1", ()),
    "low": ("COALESCE(confidence,0) < ?", ("LOW",)),
    "sentback": ("sent_back IS NOT NULL", ()),
    "tmdb": ("json_extract(proposal, '$.md_check.status') = 'differ'", ()),
    "sound": ("proposal LIKE '%\"via\": \"shazam\"%'", ()),
}


def _where(statuses, search, flt, extra=None):
    where, args = [], []
    if statuses:
        where.append("status IN (%s)" % ",".join("?" * len(statuses)))
        args += list(statuses)
    if search:
        where.append("(path LIKE ? OR title LIKE ? OR album_artist LIKE ? OR proposal LIKE ?)")
        args += [f"%{search}%"] * 4
    if flt in FILTERS:
        cond, fargs = FILTERS[flt]
        where.append(cond)
        args += [Config.LOW_CONFIDENCE if a == "LOW" else a for a in fargs]
    if extra:
        where.append(extra[0])
        args += list(extra[1])
    return (" WHERE " + " AND ".join(where)) if where else "", args


def albums_list(statuses=None, limit=500, offset=0, search=None, sort="updated", direction="desc",
                flt=None, extra=None):
    sql = "SELECT * FROM albums"
    w, args = _where(statuses, search, flt, extra)
    sql += w + order_by(ALBUM_SORTS, sort, direction, "updated") + " LIMIT ? OFFSET ?"
    args += [limit, offset]
    return [_album_dict(r) for r in q(sql, args)]


def albums_count(statuses=None, search=None, flt=None, extra=None):
    w, args = _where(statuses, search, flt, extra)
    return q("SELECT COUNT(*) n FROM albums" + w, args, one=True)["n"]


# ---------- small key/value state (connection status, pause flag) ----------

def state_get(key, default=None):
    r = q("SELECT value FROM state WHERE key=?", (key,), one=True)
    return json.loads(r["value"]) if r else default


def state_set(key, value):
    x("INSERT OR REPLACE INTO state(key,value) VALUES(?,?)", (key, json.dumps(value)))


# ---------- scans ----------

def scan_create(path, mode, total):
    return x("INSERT INTO scans(path,mode,total,started) VALUES(?,?,?,?)", (path, mode, total, time.time()))


def scan_get(scan_id):
    r = q("SELECT * FROM scans WHERE id=?", (scan_id,), one=True)
    return dict(r) if r else None


def scan_latest():
    r = q("SELECT * FROM scans ORDER BY id DESC LIMIT 1", one=True)
    return dict(r) if r else None


def job_position(album_id):
    """How many jobs will run before this album's pending job (None if it has none)."""
    j = q("SELECT id, priority, kind FROM jobs WHERE album_id=? AND status='pending' ORDER BY id LIMIT 1",
          (album_id,), one=True)
    if not j:
        return None
    r = q("SELECT COUNT(*) n FROM jobs WHERE status='pending' AND (priority > ? OR (priority = ? AND id < ?))",
          (j["priority"], j["priority"], j["id"]), one=True)
    return r["n"]


def status_counts():
    return {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) n FROM albums GROUP BY status")}


# ---------- jobs ----------

PRIORITY_USER = 10   # actions clicked in the UI jump ahead of bulk scans


def job_add(kind, album_id, payload=None, priority=0):
    existing = q("SELECT id, priority FROM jobs WHERE kind=? AND album_id=? AND status='pending'",
                 (kind, album_id), one=True)
    if existing:
        # a newer request (e.g. "Use this album") replaces the queued one's instructions
        x("UPDATE jobs SET payload=?, priority=? WHERE id=?",
          (json.dumps(payload or {}), max(priority, existing["priority"]), existing["id"]))
        return existing["id"]
    return x("INSERT INTO jobs(kind,album_id,payload,priority,created) VALUES(?,?,?,?,?)",
             (kind, album_id, json.dumps(payload or {}), priority, time.time()))


def job_next():
    with _lock:
        row = q("SELECT * FROM jobs WHERE status='pending' "
                "ORDER BY priority DESC, CASE kind WHEN 'apply' THEN 0 WHEN 'undo' THEN 0 WHEN 'setartist' THEN 0 WHEN 'albumwide' THEN 0 WHEN 'policy' THEN 0 ELSE 1 END, id "
                "LIMIT 1", one=True)
        if not row:
            return None
        x("UPDATE jobs SET status='running', started=? WHERE id=?", (time.time(), row["id"]))
    d = dict(row)
    d["payload"] = json.loads(d["payload"] or "{}")
    return d


def job_finish(job_id, error=None):
    x("UPDATE jobs SET status=?, finished=?, error=? WHERE id=?",
      ("error" if error else "done", time.time(), error, job_id))


def jobs_pending_count():
    r = q("SELECT COUNT(*) n FROM jobs WHERE status IN ('pending','running')", one=True)
    return r["n"]


def jobs_recover():
    """Requeue jobs left running by a crash/restart."""
    x("UPDATE jobs SET status='pending' WHERE status='running'")
    x("UPDATE albums SET status='queued' WHERE status='analyzing'")
    x("UPDATE albums SET status='review' WHERE status='applying'")
    # a playlist being made when the app stopped will never finish: say so instead of "working" forever
    x("UPDATE playlists SET status='error', error='Stopped by a restart: make it again' WHERE status='working'")


# ---------- cache ----------

def cache_get(key, max_age=7 * 86400):
    r = q("SELECT value, created FROM http_cache WHERE key=?", (key,), one=True)
    if r and time.time() - r["created"] < max_age:
        return json.loads(r["value"])
    return None


def cache_put(key, value):
    x("INSERT OR REPLACE INTO http_cache(key,value,created) VALUES(?,?,?)",
      (key, json.dumps(value, ensure_ascii=False), time.time()))


# ---------- log ----------

def log(message, level="info"):
    x("INSERT INTO log(ts,level,message) VALUES(?,?,?)", (time.time(), level, message))
    x("DELETE FROM log WHERE id < (SELECT MAX(id) - 2000 FROM log)")


LOG_SORTS = {"time": "ts", "level": "level", "message": "message COLLATE NOCASE"}


def log_recent(n=200, offset=0, sort="time", direction="desc"):
    return [dict(r) for r in q("SELECT * FROM log" + order_by(LOG_SORTS, sort, direction, "time")
                               + " LIMIT ? OFFSET ?", (n, offset))]


def log_count():
    return q("SELECT COUNT(*) n FROM log", one=True)["n"]
