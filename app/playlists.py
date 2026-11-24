"""Playlists from the songs you own.

Three ways in:
  radio     no AI. Scores songs by shared music director, singers, era, film and mood.
  describe  AI reads your request -> filters; the library is narrowed here; AI picks from that list.
  song      start from one song; AI (or radio) picks songs like it along the qualities you choose.

The AI only ever sees songs from your library, each with a short numeric ID, and must answer
with those IDs, so it can't suggest a song you don't have or be confused by spellings.
"""
import json
import random
import re
import threading
import time

from rapidfuzz import fuzz, process

from . import ai, db, songs
from .config import Config

MOODS = ["romantic", "happy", "sad", "melancholic", "energetic", "peaceful", "devotional", "nostalgic",
         "motivational", "playful", "heartbreak", "longing", "patriotic", "philosophical"]
KINDS = ["melody", "kuthu", "duet", "folk", "classical", "devotional", "lullaby", "celebration", "wedding",
         "love", "breakup", "friendship", "mother", "rap", "item", "theme", "dance", "comedy"]
TEMPOS = ["slow", "medium", "fast"]
DIMENSIONS = [("director", "Music director"), ("singers", "Singers"), ("era", "Era"),
              ("mood", "Mood"), ("sound", "Sound and style")]
EXAMPLES = ["Rainy evening Ilaiyaraaja melodies", "90s Rahman duets", "SPB and Janaki hits",
            "Kuthu songs for a road trip", "Soft songs for a long night drive", "Yuvan's sad songs",
            "Devotional songs for the morning", "2010s Anirudh party songs"]


# ------------------------------------------------------------------ filtering

def _like_any(col, values):
    return "(" + " OR ".join(f"{col} LIKE ?" for _ in values) + ")", [f"%{v}%" for v in values]


def candidates(f, limit=None):
    """Songs matching the filters (dict). Keys: directors, singers, decades, genres, films, moods,
    kinds, year_from, year_to, min_rating, lyrics_only, in_plex, skip_junk, exclude."""
    where, args = ["1=1"], []
    if f.get("directors"):
        where.append("(" + " OR ".join("albumartist = ? COLLATE NOCASE" for _ in f["directors"]) + ")")
        args += f["directors"]
    if f.get("singers"):
        w, a = _like_any("artist", f["singers"])
        where.append(w); args += a
    if f.get("films"):
        w, a = _like_any("film", f["films"])
        where.append(w); args += a
    if f.get("genres"):
        where.append("(" + " OR ".join("genre = ? COLLATE NOCASE" for _ in f["genres"]) + ")")
        args += f["genres"]
    if f.get("decades"):
        where.append("(" + " OR ".join("(year >= ? AND year < ?)" for _ in f["decades"]) + ")")
        for d in f["decades"]:
            args += [int(d), int(d) + 10]
    if f.get("year_from"):
        where.append("year >= ?"); args.append(int(f["year_from"]))
    if f.get("year_to"):
        where.append("year <= ?"); args.append(int(f["year_to"]))
    if f.get("min_rating"):
        where.append("rating >= ?"); args.append(float(f["min_rating"]))
    if f.get("lyrics_only"):
        where.append("has_lyrics = 1")
    if f.get("in_plex"):
        where.append("plex_key IS NOT NULL")
    if f.get("moods"):
        w, a = _like_any("profile", [f'"{m}"' for m in f["moods"]])
        where.append(w); args += a
    if f.get("kinds"):
        w, a = _like_any("profile", [f'"{k}"' for k in f["kinds"]])
        where.append(w); args += a
    if f.get("exclude"):
        ex = [int(x) for x in f["exclude"]]
        where.append(f"id NOT IN ({','.join('?' * len(ex))})"); args += ex
    sql = f"SELECT * FROM songs WHERE {' AND '.join(where)}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = [dict(r) for r in db.q(sql, tuple(args))]
    if f.get("skip_junk", Config.PLAYLIST_SKIP_JUNK):
        rows = [r for r in rows if not songs.JUNK_RE.search(f"{r['title']} {r['album']}")]
    for r in rows:
        r["profile"] = json.loads(r["profile"]) if r.get("profile") else None
    return rows


def count(f):
    return len(candidates(f))


def _diversify(rows, n, cap=None):
    cap = cap or Config.PLAYLIST_MAX_PER_FILM
    out, per = [], {}
    for r in rows:
        k = (r.get("film") or r.get("folder") or "").lower()
        if per.get(k, 0) >= cap:
            continue
        per[k] = per.get(k, 0) + 1
        out.append(r)
        if len(out) >= n:
            break
    return out


def _weight(r):
    return 1.0 + (r.get("plays") or 0) ** 0.5 * 0.3 + (r.get("rating") or 0) * 0.2


def _sample(rows, n):
    """Pick n rows at random, favouring songs you play and rate, without repeats."""
    if len(rows) <= n:
        return list(rows)
    keyed = sorted(rows, key=lambda r: random.random() ** (1.0 / _weight(r)), reverse=True)
    return keyed[:n]


# ------------------------------------------------------------------ radio (no AI)

def _people(s):
    return {p.lower() for p in songs.split_people(s)}


def similarity(seed, r, dims=None):
    dims = set(dims or [d for d, _ in DIMENSIONS])
    s = 0.0
    if "director" in dims and seed["albumartist"] and r["albumartist"].lower() == seed["albumartist"].lower() \
            and seed["albumartist"] != Config.VARIOUS_ARTISTS:
        s += 3
    if "singers" in dims:
        s += 2 * len(_people(seed["artist"]) & _people(r["artist"]))
    if "era" in dims and seed.get("year") and r.get("year"):
        gap = abs(seed["year"] - r["year"])
        s += 2 if gap <= 3 else 1 if gap <= 8 else 0
    sp, rp = seed.get("profile") or {}, r.get("profile") or {}
    if "mood" in dims and sp and rp:
        s += 1.5 * len(set(sp.get("moods", [])) & set(rp.get("moods", [])))
        s += 1 if sp.get("tempo") and sp.get("tempo") == rp.get("tempo") else 0
    if "sound" in dims and sp and rp:
        s += 1.5 * len(set(sp.get("kind", [])) & set(rp.get("kind", [])))
    if seed.get("genre") and r.get("genre") and seed["genre"].lower() == r["genre"].lower():
        s += 0.3
    return s


def radio(seed=None, f=None, n=None, dims=None):
    n = n or Config.PLAYLIST_SIZE
    f = dict(f or {})
    if seed:
        f["exclude"] = list(f.get("exclude") or []) + [seed["id"]]
    rows = candidates(f)
    if seed:
        scored = [(similarity(seed, r, dims) + random.random() * 1.2, r) for r in rows]
        scored = [x for x in scored if x[0] > 1.2]
        scored.sort(key=lambda x: -x[0])
        picked = _diversify([r for _, r in scored], n)
        why = lambda r: _why(seed, r)  # noqa: E731
    else:
        picked = _diversify(_sample(rows, max(n * 4, 200)), n)
        why = lambda r: ""  # noqa: E731
    return [{"id": r["id"], "why": why(r)} for r in picked], {"pool": len(rows)}


def _why(seed, r):
    bits = []
    if r["albumartist"] and r["albumartist"].lower() == (seed["albumartist"] or "").lower():
        bits.append(r["albumartist"])
    common = _people(seed["artist"]) & _people(r["artist"])
    if common:
        bits.append(", ".join(sorted(p.title() for p in common)))
    if seed.get("year") and r.get("year") and abs(seed["year"] - r["year"]) <= 3:
        bits.append(f"around {seed['year']}")
    return "Same " + " · ".join(bits) if bits else ""


# ------------------------------------------------------------------ AI helpers

def _line(r):
    p = r.get("profile") or {}
    mood = "/".join((p.get("moods") or [])[:2] + (p.get("kind") or [])[:1])
    return f"{r['id']}|{r['title']}|{r['artist']}|{r['albumartist']}|{r['film']}|{r['year'] or ''}|{mood}"


def _map_names(names, pool, cutoff=86):
    """Map names the AI wrote to the exact spellings in the library (Ilayaraja -> Ilaiyaraaja)."""
    out = []
    choices = [p for p, _ in pool]
    for n in names or []:
        if not isinstance(n, str) or not n.strip():
            continue
        hit = process.extractOne(n, choices, scorer=fuzz.WRatio, score_cutoff=cutoff)
        if hit and hit[0] not in out:
            out.append(hit[0])
    return out


SYSTEM_ANALYZE = """You help build playlists from a personal Tamil music library (mostly Tamil film songs).
Turn the user's request into filters. Use ONLY names from the lists provided; leave a list empty when
the request doesn't limit it. Album artist = the film's music director; artist = the singers.
Answer JSON: {"title": short playlist name, "directors": [], "singers": [], "decades": [1980, ...],
"year_from": null, "year_to": null, "genres": [], "moods": [], "kinds": [], "include_junk": false,
"brief": one sentence on what songs fit}.
moods must come from: %s. kinds from: %s.
Set include_junk true only if the user wants karaoke, instrumentals, BGM or remixes."""

SYSTEM_PICK = """You are a Tamil film music expert building a playlist from the user's own library.
Choose ONLY from the numbered songs given (format: id|title|singers|music director|film|year|mood).
Use your knowledge of these songs (mood, tempo, singers, situation in the film) to fit the request.
Pick exactly %d songs if enough fit, at most %d from the same film, and order them so the playlist
flows. Answer JSON: {"title": short playlist name, "description": one sentence,
"tracks": [{"id": number, "why": very short reason}]}."""


def _prompt_lists(have_profiles):
    k = 250 if ai.max_songs() > 300 else 120          # small-context / free tiers get shorter lists
    d = ", ".join(f"{n} ({c})" for n, c in songs.directors(k))
    s = ", ".join(n for n, _ in songs.singers(k))
    dec = ", ".join(f"{d}s ({c})" for d, c in songs.decades() if d)
    g = ", ".join(n for n, _ in songs.genres())
    return (f"Music directors in the library (song count): {d}\nSingers: {s}\nDecades: {dec}\nGenres: {g}\n"
            + ("Songs carry mood labels.\n" if have_profiles else "Songs have no mood labels yet, so leave moods and kinds empty.\n"))


def _int_year(v):
    try:
        y = int(str(v)[:4])
    except (TypeError, ValueError):
        return None
    return y if 1930 <= y <= 2030 else None


def analyze_request(prompt):
    have_profiles = (songs.status()["profiled"] or 0) > 0
    system = SYSTEM_ANALYZE % (", ".join(MOODS), ", ".join(KINDS))
    user = _prompt_lists(have_profiles) + f"\nRequest: {prompt}"
    data, usage = ai.complete(system, user, purpose="analyze", temperature=0.2)
    f = {
        "directors": _map_names(data.get("directors"), songs.directors(2000)),
        "singers": _map_names(data.get("singers"), songs.singers(3000), cutoff=88),
        "decades": [int(d) for d in (str(x).rstrip("s").strip("'") for x in (data.get("decades") or []))
                    if d.isdigit() and 1930 <= int(d) <= 2030],
        "genres": _map_names(data.get("genres"), songs.genres(), cutoff=90),
        "moods": [m for m in (data.get("moods") or []) if m in MOODS] if have_profiles else [],
        "kinds": [k for k in (data.get("kinds") or []) if k in KINDS] if have_profiles else [],
        "year_from": _int_year(data.get("year_from")),
        "year_to": _int_year(data.get("year_to")),
        "skip_junk": not data.get("include_junk"),
    }
    return f, data.get("title") or "", data.get("brief") or "", usage


RELAX_ORDER = ["kinds", "moods", "genres", "singers", "year_from", "year_to", "decades", "directors"]


def _relaxed(f, need):
    """Loosen the AI's filters one at a time until enough songs match."""
    f = dict(f)
    dropped = []
    rows = candidates(f)
    for k in RELAX_ORDER:
        if len(rows) >= need:
            break
        if f.get(k):
            f[k] = [] if isinstance(f[k], list) else None
            dropped.append(k)
            rows = candidates(f)
    return rows, f, dropped


def ai_pick(request_text, pool, n, extra=""):
    cap = ai.max_songs()
    sample = _sample(pool, cap)
    by = {r["id"]: r for r in sample}
    lines = "\n".join(_line(r) for r in sample)
    system = SYSTEM_PICK % (n, Config.PLAYLIST_MAX_PER_FILM)
    user = f"Request: {request_text}\n{extra}\nSongs ({len(sample)}):\n{lines}"
    data, usage = ai.complete(system, user, purpose="playlist", temperature=0.7)
    picked, seen = [], set()
    for t in data.get("tracks") or []:
        try:
            i = int(t.get("id") if isinstance(t, dict) else t)
        except (TypeError, ValueError):
            continue
        if i in by and i not in seen:
            seen.add(i)
            picked.append({"id": i, "why": (t.get("why") if isinstance(t, dict) else "") or ""})
    rows = _diversify([dict(by[p["id"]], why=p["why"]) for p in picked], n)
    items = [{"id": r["id"], "why": r["why"]} for r in rows]
    if len(items) < n:                         # top up from the same pool if the model came up short
        rest = [r for r in _sample(pool, len(pool)) if r["id"] not in {x["id"] for x in items}]
        for r in _diversify(rest, n - len(items)):
            items.append({"id": r["id"], "why": "", "filled": True})
    return items, data.get("title") or "", data.get("description") or "", usage, len(sample)


# ------------------------------------------------------------------ jobs

def _add_usage(total, u):
    for k in ("in", "out", "cost"):
        total[k] = round(total.get(k, 0) + (u.get(k) or 0), 5)
    total["provider"], total["model"] = u.get("provider"), u.get("model")


def _fmt_filters(f):
    names = {"directors": "", "singers": "", "decades": "", "genres": "", "moods": "", "kinds": "", "films": "",
             "year_from": "from ", "year_to": "until ", "min_rating": "rated ", "lyrics_only": "with lyrics"}
    out = []
    for k, pre in names.items():
        v = f.get(k)
        if not v:
            continue
        if k == "decades":
            out.append(", ".join(f"{d}s" for d in v))
        elif k == "lyrics_only":
            out.append(pre)
        elif isinstance(v, list):
            out.append(", ".join(map(str, v)))
        else:
            out.append(f"{pre}{v}")
    return " · ".join(out) or "whole library"


def run(pid):
    """Build playlist `pid` (row already created with mode + params)."""
    row = db.q("SELECT * FROM playlists WHERE id=?", (pid,), one=True)
    steps = []

    def step(tag, text):
        steps.append([tag, text])
        db.x("UPDATE playlists SET progress=? WHERE id=?", (json.dumps(steps), pid))
    params = json.loads(row["params"] or "{}")
    mode, n = row["mode"], int(params.get("size") or Config.PLAYLIST_SIZE)
    base = params.get("filters") or {}
    usage, notes = {}, []
    title = params.get("title") or ""
    try:
        if mode == "radio":
            seed = songs.get([int(params["seed"])])[0] if params.get("seed") else None
            step("FILTER", f"Library: {_fmt_filters(base)}")
            items, info = radio(seed, base, n, params.get("dims"))
            if not title:
                title = (f"{seed['title']} radio" if seed else "Library radio")
            notes.append(f"Picked from {info['pool']:,} songs, no AI")
            step("PICK", f"Scored {info['pool']:,} songs" + (f" against “{seed['title']}”" if seed else "") + ", no AI")
        elif mode == "song":
            seed = songs.get([int(params["seed"])])[0]
            dims = params.get("dims") or [d for d, _ in DIMENSIONS]
            step("SEED", f"{seed['title']} · {seed['artist']} · {seed['film']} {seed['year'] or ''}")
            f = dict(base, exclude=[seed["id"]])
            scored = sorted(((similarity(seed, r, dims), r) for r in candidates(f)), key=lambda x: -x[0])
            pool = [r for s, r in scored if s > 0][: ai.max_songs()] or [r for _, r in scored][: ai.max_songs()]
            if ai.enabled():
                want = ", ".join("same " + l.lower() for d, l in DIMENSIONS if d in dims)
                sp = seed.get("profile") or {}
                feel = ", ".join((sp.get("moods") or []) + (sp.get("kind") or []) + ([sp["tempo"]] if sp.get("tempo") else []))
                req = (f"Songs like “{seed['title']}” ({seed['artist']}, music by {seed['albumartist']}, "
                       f"{seed['film']} {seed['year'] or ''}{'; feels ' + feel if feel else ''}). Keep: {want}.")
                step("FILTER", f"{len(pool):,} songs share something with it")
                step("ASK", f"{ai.label()} is choosing {n}")
                items, t2, desc, u, sent = ai_pick(req, pool, n)
                _add_usage(usage, u)
                title = title or t2 or f"Like {seed['title']}"
                notes.append(f"AI chose from {sent:,} similar songs")
                if desc:
                    notes.append(desc)
            else:
                items, info = radio(seed, base, n, dims)
                title = title or f"Like {seed['title']}"
                notes.append("AI is off: picked by shared director, singers and era")
        else:                                   # describe
            prompt = row["prompt"] or ""
            step("ASK", f"{ai.label()} is reading your request")
            f, t1, brief, u = analyze_request(prompt)
            _add_usage(usage, u)
            for k, v in base.items():           # filters you set yourself win over the AI's
                if v:
                    f[k] = v
            rows, f, dropped = _relaxed(f, n)       # loosen only when fewer songs match than asked for
            if not rows:
                raise ValueError("No songs in the library match this request")
            step("FILTER", f"{len(rows):,} songs match: {_fmt_filters(f)}")
            if dropped:
                notes.append("Not enough exact matches, so these limits were loosened: " + ", ".join(dropped))
                step("LOOSEN", "Too few exact matches, loosened: " + ", ".join(dropped))
            step("PICK", f"Choosing {n} from {min(len(rows), ai.max_songs()):,} songs")
            shown = {k: v for k, v in f.items() if v and k not in ("skip_junk",)}
            if shown:
                notes.append("Filters: " + "; ".join(f"{k} {', '.join(map(str, v)) if isinstance(v, list) else v}"
                                                       for k, v in shown.items()))
            items, t2, desc, u, sent = ai_pick(prompt + (f"\n({brief})" if brief else ""), rows, n)
            _add_usage(usage, u)
            title = title or t2 or t1 or prompt[:60]
            notes.append(f"AI chose from {sent:,} of {len(rows):,} matching songs")
            if desc:
                notes.insert(0, desc)
        step("DONE", f"{len(items)} songs" + (f" · {usage.get('in', 0) + usage.get('out', 0):,} tokens" if usage else ""))
        db.x("UPDATE playlists SET status='ready', items=?, title=?, notes=?, provider=?, model=?, "
             "in_tokens=?, out_tokens=?, cost=? WHERE id=?",
             (json.dumps(items), title, json.dumps(notes), usage.get("provider") or "", usage.get("model") or "",
              usage.get("in", 0), usage.get("out", 0), usage.get("cost", 0), pid))
    except Exception as e:  # noqa: BLE001
        step("MISS", str(e))
        db.x("UPDATE playlists SET status='error', error=?, provider=?, model=?, in_tokens=?, out_tokens=?, cost=? "
             "WHERE id=?", (str(e), usage.get("provider") or "", usage.get("model") or "",
                            usage.get("in", 0), usage.get("out", 0), usage.get("cost", 0), pid))
        db.log(f"Playlist {pid} failed: {e}", "warn")


def create(mode, prompt="", params=None, background=True):
    pid = db.x("INSERT INTO playlists(created, mode, prompt, params, status) VALUES(?,?,?,?, 'working')",
               (time.time(), mode, prompt, json.dumps(params or {})))
    if background:
        threading.Thread(target=run, args=(pid,), name=f"playlist-{pid}", daemon=True).start()
    else:
        run(pid)
    return pid


def get(pid):
    r = db.q("SELECT * FROM playlists WHERE id=?", (pid,), one=True)
    if not r:
        return None
    d = dict(r)
    d["items"] = json.loads(d["items"] or "[]")
    d["notes"] = json.loads(d["notes"] or "[]")
    d["params"] = json.loads(d["params"] or "{}")
    d["progress"] = json.loads(d.get("progress") or "[]")
    return d


def history(limit=30):
    return [dict(r) for r in db.q("SELECT id, created, mode, prompt, title, status, cost, in_tokens, out_tokens, "
                                  "plex_id, saved, items FROM playlists ORDER BY id DESC LIMIT ?", (limit,))]


# ------------------------------------------------------------------ song profiles (mood labels)

SYSTEM_PROFILE = """You label songs from a Tamil music library. For each song give up to 3 moods, up to 2 kinds
and a tempo, using ONLY these words.
moods: %s
kinds: %s
tempo: slow, medium, fast
Use what you know about the song; if unsure, judge from title, singers, film and era.
Answer JSON: {"songs": [{"id": number, "moods": [], "kind": [], "tempo": ""}]} with one entry per song."""

_prof = {"running": False, "stop": False, "done": 0, "total": 0, "error": None, "started": None}


def profile_status():
    left = db.q("SELECT COUNT(*) n FROM songs WHERE profile IS NULL", one=True)["n"]
    return {**_prof, "left": left}


def profile_batch(rows):
    lines = "\n".join(f"{r['id']}|{r['title']}|{r['artist']}|{r['albumartist']}|{r['film']}|{r['year'] or ''}"
                      for r in rows)
    data, u = ai.complete(SYSTEM_PROFILE % (", ".join(MOODS), ", ".join(KINDS)),
                          f"Songs (id|title|singers|music director|film|year):\n{lines}",
                          purpose="profile", temperature=0.2)
    ids = {r["id"] for r in rows}
    n = 0
    for s in data.get("songs") or []:
        try:
            i = int(s.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        if i not in ids:
            continue
        prof = {"moods": [m for m in (s.get("moods") or []) if m in MOODS][:3],
                "kind": [k for k in (s.get("kind") or []) if k in KINDS][:2],
                "tempo": s.get("tempo") if s.get("tempo") in TEMPOS else ""}
        db.x("UPDATE songs SET profile=? WHERE id=?", (json.dumps(prof), i))
        n += 1
    return n, u


def _profile_loop(batch):
    try:
        while not _prof["stop"]:
            rows = [dict(r) for r in db.q("SELECT id, title, artist, albumartist, film, year FROM songs "
                                          "WHERE profile IS NULL ORDER BY plays DESC, id LIMIT ?", (batch,))]
            if not rows:
                break
            n, _u = profile_batch(rows)
            if n == 0:                          # the model skipped them all: mark to avoid looping forever
                for r in rows:
                    db.x("UPDATE songs SET profile=? WHERE id=?", (json.dumps({"moods": [], "kind": [], "tempo": ""}), r["id"]))
            _prof["done"] += len(rows)
    except Exception as e:  # noqa: BLE001
        _prof["error"] = str(e)
        db.log(f"Song descriptions stopped: {e}", "warn")
    finally:
        _prof["running"] = False


def start_profiles(batch=None):
    if _prof["running"] or not ai.enabled():
        return False
    # Groq's free tier allows ~8,000 tokens a minute; the batch has to fit the second service too
    batch = batch or (60 if "groq" in (ai.provider(), ai.fallback()) else 120)
    left = profile_status()["left"]
    _prof.update(running=True, stop=False, done=0, total=left, error=None, started=time.time())
    threading.Thread(target=_profile_loop, args=(batch,), name="profiler", daemon=True).start()
    return True


def stop_profiles():
    _prof["stop"] = True
