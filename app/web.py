"""Flask routes. Screens follow docs/design: Review queue (/), Library, Scan, Activity, Settings,
plus the full album editor (/album/<id>)."""
import json
import os
import time

import threading

from flask import (Blueprint, Response, abort, flash, g, jsonify, redirect, render_template, request,
                   url_for)

from . import plexmeta as PM
from . import __version__, ai, applier, artists as AR, db, plexlive, folders as FO, policy as POL, dupes, matcher, notify, playlists as PL, plex, plexfix, settings as S, reset as RS, songs, trackcheck as TC, views, watcher, worker
from .config import Config
from .sources import itunes
from .sources.http import SourceError

bp = Blueprint("web", __name__)

# Library tabs (all albums by status)
TABS = {
    "review": ["review"],
    "nomatch": ["nomatch"],
    "uptodate": ["uptodate"],
    "queued": ["queued", "analyzing", "applying"],
    "applied": ["applied"],
    "rejected": ["rejected"],
    "skipped": ["skipped"],
    "error": ["error"],
    "all": None,
}
TAB_LABELS = [("review", "Needs review"), ("nomatch", "No match"), ("uptodate", "Up to date"),
              ("queued", "In progress"), ("applied", "Applied"), ("skipped", "Skipped"), ("rejected", "Rejected"),
              ("error", "Errors"), ("all", "All")]

LIB_VIEWS = ("web.library", "web.folders_page", "web.artists_page", "web.decisions_page", "web.tracks_page")

# Review queue: what still needs a decision, and its filter chips
QUEUE_STATUSES = ["review", "nomatch", "error"]
QUEUE_FILTERS = [("all", "All"), ("fixes", "Fixes"), ("fill", "Fill blanks"),
                 ("various", "Various Artists"), ("low", "Low confidence"), ("tmdb", "Music director?"),
                 ("sound", "Found by sound"), ("sentback", "Sent back")]
RECENT_APPROVED_SECONDS = 15 * 60


def _safe_path(p):
    """Resolve a user-entered path (absolute, or relative to MUSIC_DIR) inside MUSIC_DIR."""
    p = (p or "").strip()
    if not p:
        return Config.MUSIC_DIR
    if not os.path.isabs(p):
        p = os.path.join(Config.MUSIC_DIR, p)
    real = os.path.realpath(p)
    root = os.path.realpath(Config.MUSIC_DIR)
    if real != root and not real.startswith(root + os.sep):
        return None
    return real


@bp.app_template_filter("rel")
def rel(path):
    root = Config.MUSIC_DIR
    if path and path.startswith(root):
        return path[len(root):].lstrip("/") or "/"
    return path


@bp.app_template_filter("ago")
def ago(ts):
    if not ts:
        return ""
    s = int(time.time() - ts)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit} ago"
    return "just now"


@bp.app_template_filter("srclabel")
def srclabel(src):
    return matcher.SOURCE_LABEL.get(src or "", src or "")


@bp.app_template_filter("mmss")
def mmss(sec):
    sec = int(round(sec or 0))
    return f"{sec // 60}:{sec % 60:02d}"


PER_PAGE = (25, 50, 100, 200)
# columns where the most useful first click is "biggest / newest first"
DESC_FIRST = {"confidence", "updated", "when", "files", "time"}


def _sort(allowed, default, default_dir="desc"):
    """Read ?sort=&dir= from the URL, falling back to the page default."""
    key = request.args.get("sort", default)
    if key not in allowed:
        key = default
    d = request.args.get("dir", default_dir if key == default else ("desc" if key in DESC_FIRST else "asc"))
    return {"key": key, "dir": "asc" if d == "asc" else "desc"}


def _per_page(default=50):
    try:
        per = int(request.args.get("per", default))
    except ValueError:
        per = default
    return per if per in PER_PAGE else default


def pager(total, per):
    """Page maths for list views: current page, page count, window of page numbers (None = gap)."""
    pages = max(1, -(-total // per))
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    page = min(max(1, page), pages)
    window = sorted({1, 2, pages - 1, pages, *range(page - 2, page + 3)})
    nums, last = [], 0
    for n in window:
        if 1 <= n <= pages:
            if n - last > 1:
                nums.append(None)
            nums.append(n)
            last = n
    return {"page": page, "pages": pages, "per": per, "total": total, "nums": nums,
            "offset": (page - 1) * per, "start": (page - 1) * per + 1 if total else 0,
            "end": min(page * per, total), "per_options": PER_PAGE}


def page_url(n=None, **changes):
    args = request.args.to_dict()
    args.pop("theme", None)
    if n is not None:
        args["page"] = n
    args.update({k: v for k, v in changes.items()})
    return url_for(request.endpoint, **request.view_args, **args)


@bp.route("/theme/<mode>")
def set_theme(mode):
    """Kept so old links keep working; the interface is dark-only since 2.0."""
    return redirect(request.referrer or url_for("web.index"))


def _conn_state(name, configured):
    if not configured:
        return "off"
    st = db.state_get(name) or {}
    return "ok" if st.get("ok") else "error" if st else "untested"


@bp.app_context_processor
def ctx():
    return {"counts": db.status_counts(), "cfg": Config, "busy": worker.current(), "tasks": _tasks(),
            "pending_jobs": db.jobs_pending_count(), "page_url": page_url,
            "queue_count": db.albums_count(QUEUE_STATUSES),
            "watch_state": _watch_state(),
            "plex_state": _conn_state("plex", Config.PLEX_URL and Config.PLEX_TOKEN),
            "discord_state": _conn_state("discord", Config.DISCORD_WEBHOOK),
            "accent": Config.ACCENT, "pct": views.pct,
            "dupe_count": db.q("SELECT COUNT(*) n FROM dupe_groups WHERE ignored=0", one=True)["n"],
            "plex_on": plex.enabled(),
            "lib_decisions": len(POL.suggestions()) if request.endpoint in LIB_VIEWS else 0,
            "lib_tracks": TC.count() if request.endpoint in LIB_VIEWS else 0}


# ------------------------------------------------------------------ queue

@bp.route("/")
def home():
    """Home: the library's health at a glance. Old review-queue links (/?a=12) still open the queue."""
    if set(request.args) & {"a", "f", "q", "page", "sort", "dir", "per"}:
        return index()
    return render_template("home.html", h=home_data())


def home_data():
    local = plex.local_counts()
    live = plexlive.status()
    todo = []

    def add(n, text, url, tone="warn"):
        if n:
            todo.append({"n": n, "text": text, "url": url, "tone": tone})
    add(db.albums_count(QUEUE_STATUSES), "albums waiting in the review queue", url_for("web.index"))
    add(len(POL.suggestions()), "folders need a decision (Various Artists, collections, singles…)", url_for("web.decisions_page"))
    add(db.albums_count(QUEUE_STATUSES, flt="tmdb"), "albums where TMDB names a different music director",
        url_for("web.index", f="tmdb"))
    add(AR.summary()["missing_songs"], "songs have no Album Artist", url_for("web.artists_page") + "#missing", "bad")
    add(len(_unchecked_folders()), "music folders haven't been checked yet", url_for("web.library"), "src")
    add(TC.count(), "tracks may have another song's title (check after bulk approvals)", url_for("web.tracks_page"), "bad")
    fails = db.q("SELECT COUNT(*) n FROM albums WHERE json_extract(verify, '$.n_failed') > 0", one=True)["n"]
    add(fails, "albums had tags that didn't stick after writing", url_for("web.library"), "bad")
    if plex.enabled():
        try:
            diffs = plex.differences()
        except Exception:  # noqa: BLE001
            diffs = {}
        add(sum(1 for d in diffs.values() if d["kind"] == "artist"), "albums are under the wrong artist in Plex",
            url_for("web.library", tab="plex", kind="artist"), "bad")
        add(sum(1 for d in diffs.values() if d["kind"] == "details"), "albums show old details in Plex",
            url_for("web.library", tab="plex", kind="details"))
        pc = db.q("SELECT COUNT(*) n FROM albums WHERE json_extract(plex_check, '$.ok') = 0", one=True)["n"]
        add(pc, "albums don't match Plex after their last write", url_for("web.library", tab="plex"))
        add((db.state_get("plex_dupe_artists") or {}).get("n", 0), "artists appear more than once in Plex",
            url_for("web.artists_page") + "#plexdupes")
    add(db.q("SELECT COUNT(*) n FROM dupe_groups WHERE ignored=0", one=True)["n"], "groups of duplicate songs",
        url_for("web.duplicates_page"), "src")
    recent = [dict(r) for r in db.q("SELECT ts, level, message FROM log ORDER BY id DESC LIMIT 8")]
    return {"local": local, "plex": live.get("counts"), "plex_at": live.get("counts_at"), "live": live,
            "plex_on": plex.enabled(), "todo": todo, "recent": recent, "watch": watcher.status()}


@bp.route("/api/home")
def api_home():
    h = home_data()
    return jsonify({"local": h["local"], "plex": h["plex"], "plex_at": h["plex_at"],
                    "live": {k: h["live"].get(k) for k in ("connected", "mode", "last_event", "last_text", "scanning", "error")},
                    "todo": h["todo"]})


@bp.route("/review")
def index():
    """Review queue: album cards on the left, the selected album's proposed changes on the right."""
    flt = request.args.get("f", "all")
    if flt not in dict(QUEUE_FILTERS):
        flt = "all"
    search = request.args.get("q", "").strip() or None
    srt = _sort(db.ALBUM_SORTS, "confidence")
    counts = {k: db.albums_count(QUEUE_STATUSES, search, None if k == "all" else k) for k, _ in QUEUE_FILTERS}
    # the list also keeps albums you approved in the last few minutes (shown faded, "Approved")
    recent = time.time() - RECENT_APPROVED_SECONDS
    extra = ("(status IN ('review','nomatch','error') OR (status IN ('applying','applied') AND updated > ? "
             "AND auto = 0 AND COALESCE(n_fix,0) + COALESCE(n_fill,0) > 0))", [recent])
    total = db.albums_count(None, search, None if flt == "all" else flt, extra)
    pg = pager(total, _per_page(50))
    rows = db.albums_list(None, limit=pg["per"], offset=pg["offset"], search=search,
                          sort=srt["key"], direction=srt["dir"], flt=None if flt == "all" else flt, extra=extra)
    cards = [views.card(a) for a in rows]
    sel_id = request.args.get("a", type=int)
    sel = db.album_get(sel_id) if sel_id else None
    if not sel and rows:
        sel = next((r for r in rows if r["status"] in QUEUE_STATUSES), rows[0])
    det = views.detail(sel) if sel and sel.get("proposal") else (
        {"id": sel["id"], "album": sel.get("title") or sel["path"], "path": sel["path"], "fields": [],
         "tracks": [], "status": sel["status"], "error": sel.get("error"), "empty": True} if sel else None)
    approve_pct = int(round(Config.APPROVE_ALL_MIN * 10))
    review_confs = [round((r["confidence"] or 0) * 10) for r in db.q("SELECT confidence FROM albums WHERE status='review'")]
    return render_template("queue.html", cards=cards, sel=det, sel_id=sel["id"] if sel else None, flt=flt,
                           approve_pct=approve_pct, review_confs=review_confs,
                           filters=QUEUE_FILTERS, counts=counts, q=search or "", pg=pg, srt=srt,
                           watch=watcher.status())


@bp.route("/library")
def library():
    tab = request.args.get("tab", "all")
    if tab not in TABS and not (tab == "plex" and plex.enabled()):
        tab = "all"
    search = request.args.get("q", "").strip() or None
    per = _per_page()
    srt = _sort(db.ALBUM_SORTS, "updated")
    diffs, kind, extra, statuses = {}, request.args.get("kind", "all"), None, TABS.get(tab)
    if plex.enabled():
        diffs = plex.differences()
    if tab == "plex":
        # albums whose tags don't match what Plex shows (from the last Plex sync)
        if kind not in ("artist", "details"):
            kind = "all"
        folders = [f for f, d in diffs.items() if kind == "all" or d["kind"] == kind]
        extra = (f"path IN ({','.join('?' * len(folders))})", folders) if folders else ("0", [])
    total = db.albums_count(statuses, search, None, extra)
    pg = pager(total, per)
    albums = db.albums_list(statuses, limit=per, offset=pg["offset"], search=search,
                            sort=srt["key"], direction=srt["dir"], extra=extra)
    artists = [r["album_artist"] for r in db.q(
        "SELECT album_artist, COUNT(*) n FROM albums WHERE COALESCE(album_artist,'') != '' "
        "GROUP BY album_artist ORDER BY n DESC LIMIT 500")]
    unchecked = _unchecked_folders()
    known = set(r["path"] for r in db.q("SELECT path FROM albums"))
    diffs = {f: d for f, d in diffs.items() if f in known}
    plex_counts = {"all": len(diffs), "artist": sum(1 for d in diffs.values() if d["kind"] == "artist"),
                   "details": sum(1 for d in diffs.values() if d["kind"] == "details")}
    return render_template("library.html", albums=albums, tab=tab, tabs=TAB_LABELS, q=search or "",
                           diffs=diffs, kind=kind, plex_counts=plex_counts, plex_st=plexfix.status(),
                           pols=POL.all_policies(), pol_label=POL.LABEL, policies=POL.POLICIES,
                           plex_synced=(db.state_get("songs_plex") or {}),
                           only_parts=matcher.ONLY_PARTS, plex_parts=PM.PLEX_PARTS,
                           plex_part_on={k: PM.part_on(k) for k in PM.PLEX_PARTS},
                           pg=pg, srt=srt, watch=watcher.status(), artists=artists, unchecked=len(unchecked),
                           folders=db.q("SELECT COUNT(DISTINCT folder) n FROM songs", one=True)["n"] or 0)


def _unchecked_folders():
    """Folders with music in the song index that Isaiarr has never analyzed (not in the album list)."""
    return [r["folder"] for r in db.q(
        "SELECT DISTINCT folder FROM songs WHERE folder NOT IN (SELECT path FROM albums) ORDER BY folder")]


@bp.route("/artists")
def artists_page():
    rows = AR._rows()
    pdupes, perr = [], None
    if plex.enabled():
        try:
            pdupes = plex.duplicate_artists()
        except Exception as e:  # noqa: BLE001
            perr = str(e)
    find = request.args.get("find", "").strip()
    return render_template("artists.html", sm=AR.summary(rows), groups=AR.variant_groups(rows),
                           everyone=AR.everyone(rows, find or None), find=find, policies=POL.POLICIES,
                           pdupes=pdupes, perr=perr,
                           missing=AR.missing(rows), multi=AR.multi(rows), unchecked=len(_unchecked_folders()))


@bp.route("/artists/merge", methods=["POST"])
def artists_merge():
    names = request.form.getlist("names")
    target = (request.form.get("target") or "").strip()
    if request.form.get("custom", "").strip():
        target = request.form["custom"].strip()
    if not target or not names:
        flash("Pick the spelling to keep")
        return redirect(url_for("web.artists_page"))
    n, skipped = AR.merge(names, target)
    msg = f"Writing “{target}” as Album Artist in {n} album(s), only on files spelled another way. Undo is on each album's page."
    if skipped:
        msg += (f" {len(skipped)} folder(s) Isaiarr hasn't checked yet were left alone: "
                "use Check them in the Library first.")
    flash(msg)
    return redirect(url_for("web.artists_page") + "#variants")


@bp.route("/artists/albumwide", methods=["POST"])
def artists_albumwide():
    """Albums tagged before 2.11: give their unmatched songs the album's tags (no new lookups)."""
    want = set(request.form.getlist("folders"))
    rows = [d for d in AR.missing() if d["album_id"] and d["status"] in ("applied", "uptodate")
            and (request.form.get("all") == "1" or d["folder"] in want)]
    for d in rows:
        db.job_add("albumwide", d["album_id"], {}, priority=db.PRIORITY_USER)
    flash(f"Writing the album's tags to the unmatched songs of {len(rows)} album(s). Only album, album artist, "
          "genre and year; each song's title and singers stay. Undo is on each album's page.")
    return redirect(url_for("web.artists_page") + "#missing")


@bp.route("/artists/plex-merge", methods=["POST"])
def artists_plex_merge():
    """Merge Plex artists that share a name: one group, or all of them."""
    try:
        groups = plex.duplicate_artists()
    except Exception as e:  # noqa: BLE001
        flash(f"Couldn't read artists from Plex: {e}")
        return redirect(url_for("web.artists_page") + "#plexdupes")
    want = request.form.get("keep")
    n = g_n = 0
    for g in groups:
        if want and g["keep"] != want:
            continue
        try:
            n += plex.merge_artists(g["keep"], g["merge"])
            g_n += 1
        except Exception as e:  # noqa: BLE001
            db.log(f"Plex: couldn't merge the “{g['name']}” artists: {e}", "warn")
    db.log(f"Plex: merged {n} duplicate artist entr{'y' if n == 1 else 'ies'} into {g_n} artist(s)")
    flash(f"Merged {n} duplicate artist entr{'y' if n == 1 else 'ies'} in Plex into {g_n} artist(s). "
          "Play counts and ratings stay with the songs.")
    return redirect(url_for("web.artists_page") + "#plexdupes")


@bp.route("/artists/ignore", methods=["POST"])
def artists_ignore():
    AR.ignore(request.form.get("key", ""))
    flash("Kept apart: these won't be suggested again")
    return redirect(url_for("web.artists_page") + "#variants")


@bp.route("/artists/review", methods=["POST"])
def artists_review():
    """Send the chosen folders to the review queue (analyzed again, kept there until you decide)."""
    folders = _chosen_folders(request.form)
    folders = sorted(set(folders))
    for f in folders:
        send_back(f, "Album artists")
    flash(f"GO:{url_for('web.index', f='sentback')}:Sent {len(folders)} folder(s) to the review queue. "
          "They're analyzed first, then wait under Review queue → Sent back.")
    return redirect(request.referrer or url_for("web.artists_page"))


@bp.route("/library/decisions")
def decisions_page():
    """Folders where the rules suggest a policy (Various Artists, Collection, Single…) that
    differs from the files, waiting for you to accept, change or dismiss."""
    items = POL.suggestions()
    kind = request.args.get("kind", "all")
    counts = {"all": len(items)}
    for d in items:
        counts[d["policy"]] = counts.get(d["policy"], 0) + 1
    if kind != "all":
        items = [d for d in items if d["policy"] == kind]
    return render_template("decisions.html", items=items[:500], total=len(items), kind=kind, counts=counts,
                           policies=POL.POLICIES, label=POL.LABEL)


@bp.route("/library/tracks")
def tracks_page():
    """Tracks whose title may belong to another song: length, original file name, swapped tracks."""
    level = request.args.get("level", "2")
    min_sev = {"1": 1, "2": 2, "3": 3, "ai": 1}.get(level, 2)
    rows = TC.flagged(min_sev)
    if level == "ai":
        rows = [r for r in rows if (r["ai"] or {}).get("verdict") == "wrong"]
    counts = {k: len(TC.flagged(v)) for k, v in (("1", 1), ("2", 2), ("3", 3))}
    counts["ai"] = sum(1 for r in TC.flagged(1) if (r["ai"] or {}).get("verdict") == "wrong")
    groups = {}
    for r in rows:
        groups.setdefault(r["album_id"], {"album_id": r["album_id"], "folder": r["folder"], "tracks": []})["tracks"].append(r)
    for gr in groups.values():
        a = db.album_get(gr["album_id"]) or {}
        gr["name"] = a.get("title") or os.path.basename(gr["folder"])
    return render_template("tracks.html", groups=list(groups.values())[:300], total=len(rows), level=level,
                           counts=counts, st=TC.status(), ai_on=ai.enabled())


@bp.route("/tracks/check", methods=["POST"])
def tracks_check():
    if TC.start():
        flash("Checking the titles of every tagged album; the list fills in as it goes (see Activity)")
    else:
        flash("A track check is already running")
    return redirect(url_for("web.tracks_page"))


@bp.route("/tracks/act", methods=["POST"])
def tracks_act():
    """Ticked tracks: send their albums to review, ask the AI, or mark them as fine."""
    paths = request.form.getlist("paths")
    action = request.form.get("action")
    back = request.referrer or url_for("web.tracks_page")
    if not paths:
        flash("Tick one or more tracks first")
        return redirect(back)
    if action == "dismiss":
        TC.dismiss(paths)
        flash(f"Marked {len(paths)} track(s) as fine; they won't be flagged again")
    elif action == "review":
        folders = sorted({r["folder"] for r in db.q(
            f"SELECT folder FROM track_check WHERE path IN ({','.join('?' * len(paths))})", tuple(paths))})
        for f in folders:
            if os.path.isdir(f):
                send_back(f, "Track names")
        flash(f"GO:{url_for('web.index', f='sentback')}:Sent {len(folders)} album(s) to the review queue. They're "
              "matched again, then wait under Review queue → Sent back with each song's proposed title.")
    elif action == "ai":
        if not ai.enabled():
            flash("Set up an AI first (Settings → AI)")
        elif TC.start_ai(paths):
            flash(f"Asking the AI about {len(paths)} track(s); its verdict appears next to each one")
        else:
            flash("The AI is still working on the last request")
    return redirect(back)


@bp.route("/api/tracks/status")
def api_tracks_status():
    return jsonify(TC.status())


@bp.route("/library/folders")
def folders_page():
    """Browse the library folder by folder and decide whole folders (and everything inside) at once."""
    path = FO.abs_path(request.args.get("path", ""))
    q = request.args.get("q", "").strip() or None
    return render_template("folders.html", b=FO.browse(path, q), q=q or "", policies=POL.POLICIES, label=POL.LABEL)


def _chosen_folders(form):
    """Ticked folders, safely inside the library; with "include subfolders" every album folder below them."""
    root = os.path.realpath(Config.MUSIC_DIR)
    picked = [x for x in form.getlist("folders") if os.path.isdir(x) and os.path.realpath(x).startswith(root)]
    names = set(form.getlist("names"))
    if names:                       # "several names in one tag": the folders using those names
        picked += [f for d in AR.multi() if d["name"] in names for f in d["folders"]]
    return FO.expand(picked, recursive=form.get("recursive") == "on")   # only folders that hold songs


@bp.route("/folders/policy", methods=["POST"])
def folders_policy():
    """Accept suggestions (each folder its own suggested policy) or set one policy for the
    folders you ticked. Writes to every file of each folder, with Undo per album."""
    f = request.form
    folders = _chosen_folders(f)
    pol = f.get("policy") or ""
    aa, alb = f.get("albumartist", "").strip() or None, f.get("album", "").strip() or None
    if alb and len(folders) > 1:
        alb = None                        # one album name for many folders would merge them in Plex
    sug = {d["folder"]: d for d in POL.suggestions()} if pol in ("", "suggested") else {}
    n = 0
    for folder in folders:
        if pol in ("", "suggested"):
            d = sug.get(folder)
            if not d:
                continue
            POL.accept(folder, d["policy"], d["albumartist"], d["album"], "; ".join(d["why"]))
        else:
            if pol not in POL.LABEL:
                continue
            POL.accept(folder, pol, aa, alb, "set by you")
        n += 1
    flash(f"Decided {n} folder(s): writing their album tags now (album artist, album, release type) to every file. "
          "Undo is on each album's page.")
    back = request.form.get("back") or ""
    if back.startswith("/") and not back.startswith("//"):
        return redirect(back)
    return redirect(request.referrer or url_for("web.decisions_page"))


@bp.route("/folders/dismiss", methods=["POST"])
def folders_dismiss():
    POL.dismiss(request.form.getlist("folders"))
    flash("Left as they are: these won't be suggested again")
    return redirect(request.referrer or url_for("web.decisions_page"))


@bp.route("/folders/clear", methods=["POST"])
def folders_clear():
    for x in request.form.getlist("folders"):
        POL.clear(x)
    flash("Policy removed: the next check suggests one again; your files weren't changed")
    return redirect(request.referrer or url_for("web.library"))


@bp.route("/analysis/reset", methods=["POST"])
def analysis_reset():
    """Start the analysis over (whole library, or the ticked folders). Files are never changed."""
    if request.form.get("scope") == "all":
        if request.form.get("confirm", "").strip().upper() != "RESET":
            flash("Type RESET in the box to confirm starting the whole library over")
            return redirect(url_for("web.settings", tab="matching") + "#reset")
        folders = None
    else:
        folders = _chosen_folders(request.form)
        if not folders:
            flash("Tick one or more folders first")
            return redirect(request.referrer or url_for("web.folders_page"))
    r = RS.reset(folders)
    flash(f"GO:{url_for('web.index')}:Started over: {r['queued']} folder(s) are being checked again from the tags "
          "they have now; albums with changes wait in the review queue. Nothing is written until you approve."
          + (f" {r['skipped']} album(s) were busy and were left alone." if r["skipped"] else ""))
    return redirect(url_for("web.index") if folders is None else (request.referrer or url_for("web.folders_page")))


@bp.route("/plex/meta/all", methods=["POST"])
def plex_meta_all():
    """Set collections, reviews and moods in Plex for every tagged album, in the background."""
    from . import plexmeta
    if not plexmeta.enabled():
        flash("Set up Plex first (Settings → Plex → Connection)")
    elif plexmeta.sync_all(force=True):
        flash(f"GO:{url_for('web.log')}:Updating collections, reviews and moods in Plex for every tagged album "
              "in the background. Progress shows in the sidebar.")
    else:
        flash("Already updating Plex; progress shows in the sidebar")
    return redirect(url_for("web.settings", tab="plex"))


@bp.route("/nfo/all", methods=["POST"])
def nfo_all():
    """Write NFO files for every tagged album, in the background, then let Plex pick them up."""
    from . import nfo
    from . import tasks
    folders = [r["path"] for r in db.q("SELECT path FROM albums WHERE status IN ('applied', 'uptodate')")]
    link = url_for("web.settings", tab="plex")

    def run():
        db.log(f"NFO files: writing for {len(folders)} tagged album(s)…")
        w = s = 0
        with tasks.track("nfo", "Writing NFO files", total=len(folders), url=link, detail="album.nfo next to each album") as t:
            for i in range(0, len(folders), 100):
                a, b = nfo.write_many(folders[i:i + 100])
                w, s = w + a, s + b
                t.step(len(folders[i:i + 100]))
                if i + 100 < len(folders):
                    db.log(f"NFO files: {i + 100} of {len(folders)} albums done")
        db.log(f"NFO files: written for {w} album(s), {s} skipped")
        if plex.enabled() and w:
            try:
                for sec in plex.sections():
                    if sec["type"] == "artist":
                        plex._get(f"/library/sections/{sec['id']}/refresh")
            except Exception as e:  # noqa: BLE001
                db.log(f"Plex rescan after NFO files failed: {e}", "warn")
    threading.Thread(target=run, name="nfo", daemon=True).start()
    flash(f"Writing NFO files for {len(folders)} tagged album(s) in the background; Plex rescans the music library after. "
          "Progress shows in the sidebar.")
    return redirect(url_for("web.settings", tab="plex"))


@bp.route("/library/plex-check", methods=["POST"])
def library_plex_check():
    if plexfix.check():
        flash("Reading every track from Plex to compare with the tags (a minute or two for a big library). "
              "Refresh this page in a moment.")
    else:
        flash("A check against Plex is already running")
    return redirect(url_for("web.library", tab="plex"))


@bp.route("/library/check-unchecked", methods=["POST"])
def library_check_unchecked():
    todo = [f for f in _unchecked_folders() if os.path.isdir(f)]
    for f in todo:
        worker.queue_album(f, origin="manual")
    flash(f"GO:{url_for('web.index')}:Checking {len(todo)} folder(s) Isaiarr hadn't looked at yet. "
          "Albums with changes to make appear in the review queue as they're done.")
    return redirect(url_for("web.library"))


@bp.route("/bulk", methods=["POST"])
def bulk():
    action = request.form.get("action")
    ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()]
    if action == "approve_confident":
        try:
            thr = float(request.form.get("threshold") or Config.APPROVE_ALL_MIN)
        except ValueError:
            thr = Config.APPROVE_ALL_MIN
        thr = min(max(thr, 5.0), 10.0)                 # never below 50 %
        want = round(thr * 10)                          # compare as the cards show it: whole percent
        confident = [a for a in db.albums_list(["review"], limit=100000) if round((a["confidence"] or 0) * 10) >= want]
        # a music director TMDB disagrees with, or songs found only by their sound, wait for a look
        held = [a for a in confident if ((a.get("proposal") or {}).get("md_check") or {}).get("status") == "differ"
                or any((f.get("source_track") or {}).get("via") == "shazam" for f in (a.get("proposal") or {}).get("files", []))]
        ids = [a["id"] for a in confident if a not in held]
        if held:
            flash(f"Kept {len(held)} album(s) back for a look: TMDB names a different music director, or songs "
                  "were identified by their sound (filters “Music director?” and “Found by sound”)")
    elif request.form.get("all_matching") == "on":
        # "select every album matching this search", not just the ones on this page
        tab = request.form.get("tab", "all")
        extra = None
        if tab == "plex":          # only the albums listed on the Differs in Plex tab
            kind = request.form.get("kind", "all")
            folders = [f for f, d in plex.differences().items() if kind not in ("artist", "details") or d["kind"] == kind]
            extra = (f"path IN ({','.join('?' * len(folders))})", folders) if folders else ("0", [])
        ids = [a["id"] for a in db.albums_list(TABS.get(tab, TABS["all"]), limit=100000,
                                               search=request.form.get("q", "").strip() or None, extra=extra)]
    if action == "policy":
        pol = request.form.get("policy", "")
        if pol not in POL.LABEL:
            flash("Pick a kind of album")
            return redirect(request.referrer or url_for("web.library"))
        aa = request.form.get("new_artist", "").strip() or None
        n = 0
        for i in ids:
            a = db.album_get(i)
            if a and os.path.isdir(a["path"]):
                POL.accept(a["path"], pol, aa, None, "set by you")
                n += 1
        flash(f"Decided {n} album(s) as {POL.LABEL[pol]}" + (f" with Album Artist “{aa}”" if aa else "")
              + ": writing to every file now. Undo is on each album's page.")
        return redirect(request.referrer or url_for("web.library"))
    if action == "setartist":
        new = request.form.get("new_artist", "").strip()
        if not new:
            flash("Type the album artist to set")
            return redirect(request.referrer or url_for("web.library"))
        composer = request.form.get("also_composer") == "on"
        n = 0
        for i in ids:
            if db.album_get(i):
                db.job_add("setartist", i, {"name": new, "composer": composer}, priority=db.PRIORITY_USER)
                n += 1
        flash(f"Setting album artist to “{new}” on {n} album(s)" + (" (also Composer)" if composer else "")
              + ". Only that tag changes; Undo on each album page puts the old value back.")
        return redirect(request.referrer or url_for("web.library"))
    if action == "only":
        return _update_only(ids)
    n = 0
    for i in ids:
        a = db.album_get(i)
        if not a:
            continue
        if action in ("approve", "approve_confident") and a["status"] == "review":
            db.album_update(i, status="applying")
            db.job_add("apply", i, worker.default_apply_options(), priority=db.PRIORITY_USER)
            n += 1
        elif action == "reject":
            db.album_update(i, status="rejected")
            n += 1
        elif action == "skip":
            n += 1 if worker.skip(i) else 0
        elif action == "reanalyze":
            worker.queue_album(a["path"], origin=a["origin"] or "manual", priority=db.PRIORITY_USER)
            n += 1
        elif action == "review":
            send_back(a["path"], "Library")
            n += 1
    if action == "plexrefresh":
        if not plex.enabled():
            flash("Set up Plex in Settings → Plex first")
        else:
            n = plexfix.refresh_many([a["path"] for a in (db.album_get(i) for i in ids) if a])
            flash(f"Refreshing {n} album(s) in Plex in the background. When Plex has caught up (a few minutes), "
                  "press Check against Plex to see what's left.")
        return redirect(request.referrer or url_for("web.library"))
    if action == "plexreadd":
        if not plex.enabled():
            flash("Set up Plex in Settings → Plex first")
        else:
            n = plexfix.start([i for i in ids if db.album_get(i)])
            flash(f"GO:{url_for('web.activity')}:Re-adding {n} album(s) in Plex, one at a time "
                  "(a minute or two each). Progress shows in the sidebar.")
        return redirect(request.referrer or url_for("web.library"))
    if action == "skip":
        flash(f"Skipped {n} album(s): their tags stay as they are, and later scans leave them alone "
              "(Library → Skipped lists them)")
        return redirect(request.referrer or url_for("web.library"))
    if action == "review":
        flash(f"GO:{url_for('web.index', f='sentback')}:Sent {n} album(s) back to the review queue. "
              "They're re-checked first, then wait for you under Sent back.")
        return redirect(request.referrer or url_for("web.library"))
    flash(f"{n} album(s): {action.replace('_', ' ')}")
    return redirect(request.referrer or url_for("web.index"))


def _update_only(ids):
    """Library → Update only…: just the parts you picked, for the selected albums."""
    from . import plexmeta
    picked = request.form.getlist("only")
    plex_parts = [p for p in plexmeta.PLEX_PARTS if p in picked]
    file_parts = [p for p in matcher.ONLY_PARTS if p in picked]
    back = request.referrer or url_for("web.library")
    if not plex_parts and not file_parts:
        flash("Pick what to update (Update only…)")
        return redirect(back)
    albums = [a for a in (db.album_get(i) for i in ids) if a and os.path.isdir(a["path"])]
    if not albums:
        flash("Select the albums to update first")
        return redirect(back)
    said = []
    if plex_parts:
        off = [plexmeta.PLEX_PARTS[p] for p in plex_parts if not plexmeta.part_on(p)]
        plex_parts = [p for p in plex_parts if plexmeta.part_on(p)]
        if not plexmeta.enabled():
            said.append("Plex isn't set up (Settings → Plex), so nothing was sent there")
        elif off:
            said.append(f"{', '.join(off)}: switched off in Settings → Plex, so left alone")
        if plex_parts and plexmeta.enabled():
            if plexmeta.sync_all(force=True, folders=[a["path"] for a in albums], only=plex_parts):
                said.append(f"Setting {', '.join(plexmeta.PLEX_PARTS[p].lower() for p in plex_parts)} in Plex "
                            f"for {len(albums)} album(s) in the background; the files aren't touched")
            else:
                said.append("An update of Plex is already running: try again when it's done (Activity → Log)")
    if file_parts:
        write = request.form.get("only_write") == "write"
        for a in albums:
            worker.queue_album(a["path"], origin=a["origin"] or "manual", priority=db.PRIORITY_USER,
                               only=file_parts, write=write)
        names = ", ".join(matcher.ONLY_PARTS[p][0].lower() for p in file_parts)
        said.append(f"Re-checking {len(albums)} album(s) for {names} only; nothing else in the files changes. "
                    + ("Albums that pass the auto-approve rules are written straight away; the rest wait "
                       "in the review queue." if write else "The changes wait for you in the review queue."))
    flash(("GO:" + url_for("web.index") + ":" if file_parts else "") + " ".join(s.rstrip(".") + "." for s in said))
    return redirect(back)


# ------------------------------------------------------------------ album

@bp.route("/album/<int:aid>")
def album(aid):
    a = db.album_get(aid)
    if not a:
        abort(404)
    backups = db.q("SELECT batch, COUNT(*) n, MIN(created) created, MAX(restored) restored "
                   "FROM backups WHERE album_id=? GROUP BY batch ORDER BY batch DESC", (aid,))
    prop = a["proposal"]
    name = ((prop or {}).get("album") or {}).get("album") or a["path"].rstrip("/").split("/")[-1]
    head = {"tone": views.tone(name), "initial": views.initial(name), "cover": views.cover_url(prop), "name": name}
    conf = views.pct(a["confidence"]) if a["confidence"] is not None else None
    st = plexfix.status()
    plex_job = ({"ok": None, "msg": "re-adding the album now…", "at": None} if st["current"] == aid else
                {"ok": None, "msg": "waiting to be re-added…", "at": None} if aid in st["queued"] else plexfix.last(aid))
    wide = applier.album_wide_tags(a) if prop else {}
    pol = POL.get(a["path"])
    song_rows = [dict(r) for r in db.q("SELECT folder, albumartist, album, grouping, composer, releasetype FROM songs "
                                       "WHERE folder=?", (a["path"],))]
    sug = POL.suggest(a["path"], song_rows, a) if song_rows else None
    from . import plexmeta
    pm = None
    if plexmeta.enabled() and plexmeta.any_on():
        pm = plexmeta.wanted(a["path"], fetch=False)
        if not song_rows and prop and isinstance(prop.get("review"), dict):
            pm["review"] = prop["review"]               # not written yet: what the analysis found
        pm["last"] = a.get("plex_meta")
    return render_template("album.html", a=a, p=prop, backups=backups, head=head, plex_job=plex_job, wide=wide,
                           plex_meta=pm,
                           pol=pol, sug=sug, policies=POL.POLICIES, pol_label=POL.LABEL, n_songs=len(song_rows),
                           conf=conf, conf_cls=views.conf_class(conf) if conf is not None else "")


def _form_options(prop):
    f = request.form
    opts = {
        "rename": f.get("rename") == "on",
        "artwork": f.get("artwork", "missing"),
        "lyrics": f.get("lyrics", "missing"),
        "album": {k: f.get(f"a-{k}", "").strip() for k in ("album", "albumartist", "date", "genre", "label")
                  if f.get(f"a-{k}") is not None},
        "files": {},
    }
    for i, _fp in enumerate(prop["files"]):
        new = {}
        for k in ("title", "artist", "tracknumber", "discnumber"):
            v = f.get(f"f-{i}-{k}")
            if v is not None:
                new[k] = v.strip()
        opts["files"][str(i)] = {"include": f.get(f"f-{i}-include") == "on", "new": new}
    return opts


@bp.route("/album/<int:aid>/apply", methods=["POST"])
def album_apply(aid):
    a = db.album_get(aid)
    if not a or not a["proposal"]:
        abort(404)
    db.album_update(aid, status="applying")
    db.job_add("apply", aid, _form_options(a["proposal"]), priority=db.PRIORITY_USER)
    flash("Approved — writing tags now")
    nxt = [r for r in db.albums_list(["review"], limit=2) if r["id"] != aid]
    return redirect(url_for("web.album", aid=nxt[0]["id"]) if nxt and request.form.get("next") == "1"
                    else _next_in_queue(aid))


def _next_in_queue(after_id):
    nxt = [r for r in db.albums_list(QUEUE_STATUSES, limit=2, sort="confidence", direction="desc")
           if r["id"] != after_id]
    return url_for("web.index", a=nxt[0]["id"]) if nxt else url_for("web.index")


@bp.route("/album/<int:aid>/approve", methods=["POST"])
def album_quick_approve(aid):
    """Approve from the review queue with the default options (matched files, rename, art, lyrics)."""
    a = db.album_get(aid)
    if not a or not a["proposal"]:
        abort(404)
    if a["status"] in QUEUE_STATUSES:
        db.album_update(aid, status="applying")
        db.job_add("apply", aid, worker.default_apply_options(), priority=db.PRIORITY_USER)
        flash(f"Approved {a['title'] or 'album'}: writing tags now")
    back = request.form.get("back") or ""
    return redirect(back if back.startswith("/") and not back.startswith("//") else _next_in_queue(aid))


@bp.route("/album/<int:aid>/<action>", methods=["POST"])
def album_action(aid, action):
    a = db.album_get(aid)
    if not a:
        abort(404)
    if action == "skip":
        if worker.skip(aid):
            flash(f"Skipped {a['title'] or 'album'}: its tags stay as they are, and later scans leave it alone. "
                  "Analyze it again any time from its page.")
        else:
            flash("This album is being worked on right now; skip it when that's done")
            return redirect(url_for("web.album", aid=aid))
        back = request.form.get("back") or ""
        return redirect(back if back.startswith("/") and not back.startswith("//") else _next_in_queue(aid))
    if action == "reject":
        db.album_update(aid, status="rejected")
        flash(f"Rejected {a['title'] or 'album'}: nothing was written")
        back = request.form.get("back") or ""
        return redirect(back if back.startswith("/") and not back.startswith("//") else _next_in_queue(aid))
    if action == "reanalyze":
        worker.queue_album(a["path"], origin=a["origin"] or "manual", priority=db.PRIORITY_USER)
        flash("Re-analyzing — this page updates by itself when it's done")
    elif action == "plexcheck":
        try:
            res = plex.check_folder(a["path"])
            flash("Plex shows these tags" if res["ok"] else
                  f"Plex differs: {len(res['diffs'])} difference(s), {res['missing']} track(s) not found in Plex")
        except Exception as e:  # noqa: BLE001
            flash(f"Couldn't check Plex: {e}")
    elif action == "plexrefresh":
        if not plex.enabled():
            flash("Set up Plex in Settings → Plex first")
        else:
            plexfix.refresh_many([a["path"]])
            flash("Refreshing this album in Plex (fields unlocked first). It takes Plex a minute to show it.")
    elif action == "plexreadd":
        if not plex.enabled():
            flash("Set up Plex in Settings → Plex first")
        else:
            plexfix.start([aid])
            flash("Re-adding this album in Plex: it disappears from Plex for a minute or two, then comes back under "
                  "the album artist in its tags. Progress shows in the sidebar.")
    elif action == "review":
        send_back(a["path"], "the album page")
        flash("Sent back to the review queue: re-checking it now. It stays in the queue even if nothing looks wrong, "
              "so you can fix it here or pick another match under Wrong album?")
    elif action == "rematch":
        ref = matcher.parse_ref(request.form.get("collection_id", ""))
        if not ref:
            flash("Paste an Apple Music / iTunes, JioSaavn or Wikipedia album URL")
            return redirect(url_for("web.album", aid=aid))
        worker.queue_album(a["path"], origin=a["origin"] or "manual", ref=ref, priority=db.PRIORITY_USER)
        flash("Re-matching against the selected album — this page updates by itself when it's done")
    elif action == "undo":
        db.job_add("undo", aid, priority=db.PRIORITY_USER)
        flash("Undo queued")
    return redirect(url_for("web.album", aid=aid))


# ------------------------------------------------------------------ scan

@bp.route("/scan", methods=["GET", "POST"])
def scan():
    if request.method == "POST":
        path = _safe_path(request.form.get("path"))
        if not path or not os.path.isdir(path):
            flash("Folder not found inside the music library")
            return redirect(url_for("web.scan"))
        mode = request.form.get("mode", "changed")
        n, scan_id = worker.queue_folder(path, recursive=request.form.get("recursive") == "on",
                                         origin="manual", skip_applied=mode != "all", track=True)
        db.log(f"Manual scan of {path}: {n} folder(s)")
        flash(f"Scanning {n} album folder(s) in {rel(path) if path != Config.MUSIC_DIR else 'the whole library'}"
              if n else "Nothing to scan: every folder there is already up to date or waiting for review")
        return redirect(url_for("web.scan"))
    recent = db.albums_list(None, limit=5, sort="updated", direction="desc", extra=("origin = 'watch'", []))
    sc = db.scan_latest()
    prog = worker.scan_progress(sc["id"]) if sc else None
    return render_template("scan.html", start=request.args.get("path", ""), recent=recent, prog=prog,
                           log_lines=views.scan_log(sc["id"]) if sc else [], watch=watcher.status(),
                           wfields={s_["key"]: s_ for s_ in S.export(Config) if s_["section"] == "watcher"})


@bp.route("/scan/<action>", methods=["POST"])
def scan_control(action):
    if action in ("pause", "resume"):
        worker.set_paused(action == "pause")
        flash("Paused: nothing is analyzed or written until you resume" if action == "pause" else "Resumed")
    return redirect(request.referrer or url_for("web.scan"))


@bp.route("/api/scan")
def api_scan():
    sc = db.scan_latest()
    if not sc:
        return jsonify({"scan": None})
    p = worker.scan_progress(sc["id"])
    return jsonify({"scan": {k: p[k] for k in ("id", "path", "done", "total", "pct", "correct", "review", "missed",
                                               "running", "paused")},
                    "folder": rel(p["path"]) if p["path"] != Config.MUSIC_DIR else "Whole library",
                    "log": views.scan_log(sc["id"])})


@bp.route("/api/browse")
def api_browse():
    path = _safe_path(request.args.get("path"))
    if not path or not os.path.isdir(path):
        return jsonify({"error": "not found"}), 404
    dirs, audio = [], 0
    try:
        for e in sorted(os.scandir(path), key=lambda e: e.name.lower()):
            if e.is_dir() and not e.name.startswith((".", "@")):
                dirs.append(e.name)
            elif e.name.lower().endswith(Config.AUDIO_EXTS):
                audio += 1
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"path": path, "rel": rel(path), "dirs": dirs, "audio": audio,
                    "parent": rel(os.path.dirname(path)) if path != os.path.realpath(Config.MUSIC_DIR) else None})


@bp.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    src = request.args.get("source", "itunes")
    if not q:
        return jsonify([])
    mod = matcher.SOURCE_MODS.get(src, itunes)
    try:
        return jsonify(mod.search_albums(q, limit={"itunes": 20, "jiosaavn": 15, "deezer": 15}.get(src, 4)))
    except SourceError as e:
        return jsonify({"error": str(e)}), 502


@bp.route("/api/album/<int:aid>")
def api_album(aid):
    a = db.album_get(aid)
    if not a:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": a["status"], "updated": a["updated"], "error": a["error"],
                    "ahead": db.job_position(aid), "busy": worker.current()})


def _tasks():
    from . import tasks
    return tasks.snapshot(url_for)


def _watch_state():
    w = watcher.status()
    return "paused" if worker.paused() else ("off" if w.get("mode") in (None, "off") else "live")


@bp.route("/api/status")
def api_status():
    """Polled by every page (sidebar): what's running, and the counts shown in the menu."""
    return jsonify({"counts": db.status_counts(), "busy": worker.current(),
                    "pending_jobs": db.jobs_pending_count(), "watch": watcher.status(),
                    "watch_state": _watch_state(), "tasks": _tasks(),
                    "queue_count": db.albums_count(QUEUE_STATUSES),
                    "dupe_count": db.q("SELECT COUNT(*) n FROM dupe_groups WHERE ignored=0", one=True)["n"]})


# ------------------------------------------------------------------ misc pages

@bp.route("/history")
def history():
    total = db.q("SELECT COUNT(*) n FROM (SELECT 1 FROM backups GROUP BY album_id, batch)", one=True)["n"]
    pg = pager(total, _per_page())
    sorts = {"when": "b.batch", "album": "COALESCE(NULLIF(a.title,''), a.path) COLLATE NOCASE",
             "artist": "NULLIF(a.album_artist,'') COLLATE NOCASE", "folder": "a.path COLLATE NOCASE",
             "files": "COUNT(*)", "undone": "MAX(b.restored)"}
    srt = _sort(sorts, "when")
    d = "ASC" if srt["dir"] == "asc" else "DESC"
    expr = sorts[srt["key"]]
    rows = db.q("SELECT b.album_id, b.batch, COUNT(*) n, MAX(b.restored) restored, a.path, a.title, "
                "a.album_artist FROM backups b JOIN albums a ON a.id=b.album_id "
                f"GROUP BY b.album_id, b.batch ORDER BY ({expr}) IS NULL, {expr} {d}, b.batch {d} "
                "LIMIT ? OFFSET ?", (pg["per"], pg["offset"]))
    return render_template("activity.html", tab="changes", rows=rows, pg=pg, srt=srt)


@bp.route("/log")
def log():
    pg = pager(db.log_count(), _per_page(100))
    srt = _sort(db.LOG_SORTS, "time")
    return render_template("activity.html", tab="log",
                           rows=db.log_recent(pg["per"], pg["offset"], srt["key"], srt["dir"]), pg=pg, srt=srt)


@bp.route("/activity")
def activity():
    """Activity opens on what's running now when something is, else on the changes written."""
    return redirect(url_for("web.activity_now" if _tasks()["running"] else "web.history"))


@bp.route("/activity/now")
def activity_now():
    return render_template("activity.html", tab="now", paused=worker.paused())


SOURCE_INFO = {
    "itunes": "Album, titles, track numbers, year, cover art",
    "jiosaavn": "Music director and singers credited separately; older Tamil albums",
    "wikipedia": "Film soundtrack track lists with singers; no artwork",
    "discogs": "LP / cassette / CD releases with credits by role; needs a token",
    "deezer": "Complete track lists, track and disc numbers, 1000 px covers; singers from song credits",
}


def _check_sources():
    """Try each enabled source with a tiny request and report what happened."""
    from .sources import discogs, musixmatch, shazam, tmdb
    out = []
    for src in Config.SOURCES:
        label = matcher.SOURCE_LABEL.get(src, src)
        if src == "discogs" and not discogs.enabled_search():
            out.append((label, False, "no token: searching is off (pasting a Discogs link still works)"))
            continue
        try:
            n = len(matcher.SOURCE_MODS[src].search_albums("Roja"))
            out.append((label, True, f"reachable, {n} result(s) for “Roja”"))
        except Exception as e:  # noqa: BLE001
            out.append((label, False, str(e)))
    if Config.TMDB_CHECK and not Config.TMDB_API_KEY:
        out.append(("TMDB", False, "no key: the music director check is off"))
    elif tmdb.enabled():
        try:
            r = tmdb.check("Roja", "1992", "A. R. Rahman", "Tamil")
            out.append(("TMDB", r["status"] == "agree",
                        "reachable, Roja (1992): " + (", ".join(r["composers"]) or r["status"])))
        except Exception as e:  # noqa: BLE001
            out.append(("TMDB", False, str(e)))
    if Config.SHAZAM_ENABLED:
        out.append(("Shazam", shazam.available(),
                    "on: songs no source finds by title are identified by their sound" if shazam.available()
                    else "the shazamio library is missing from this image"))
    if musixmatch.enabled():
        try:
            hit = musixmatch.lookup("Kadhal Rojave", "S. P. Balasubrahmanyam", "Roja", 300)
            out.append(("Musixmatch", True, "reachable" + (", found a test song" if hit else ", no test match")))
        except Exception as e:  # noqa: BLE001
            out.append(("Musixmatch", False, str(e)))
    return out


SETTINGS_PAGES = [
    ("Library", [("tagging", "Tag rules", "tag"), ("kinds", "Kinds of album", "user"), ("renaming", "File renaming", "file"),
                 ("sources", "Metadata sources", "library"), ("lyrics", "Lyrics", "lyrics"),
                 ("matching", "Matching", "target"), ("duplicates", "Duplicates", "copy")]),
    ("Automation", [("auto", "Auto-approve", "zap")]),
    ("Connections", [("plex", "Plex", "play"), ("discord", "Discord", "chat"), ("ai", "AI & playlists", "sparkle")]),
    ("App", [("security", "Login", "lock"), ("appearance", "Appearance", "palette"), ("system", "System", "server")]),
]
SETTINGS_DESC = {
    "tagging": "What gets written into your files.",
    "kinds": "What Isaiarr suggests for Various Artists, collections and singles, and what a decision writes.",
    "renaming": "How tracks are renamed when an album is approved.",
    "sources": "Where album and song details come from, in the order they're tried.",
    "lyrics": "Musixmatch first, then JioSaavn.",
    "matching": "How sure Isaiarr must be before it trusts a match.",
    "duplicates": "Finding the same song more than once, and what happens to the extra copies.",
    "auto": "Rules an album must pass to be written without waiting in the queue.",
    "plex": "Rescan the music library after each write, and save playlists.",
    "discord": "Messages about albums waiting, changes written and scans.",
    "ai": "The AI model used for playlists, and playlist defaults.",
    "security": "Optional password for this web interface.",
    "appearance": "",
    "system": "",
}


def _setting_tab(spec):
    if spec["section"] == "integrations":
        return spec.get("group") or "plex"
    if spec["key"] == "AUTO_APPROVE":
        return "auto"
    return spec["section"]


def _settings_form(sections, keys=None):
    """Read the settings form -> (updates, clears, errors). With `keys`, only those settings are
    read (the ones shown on the page that was saved); otherwise every setting in `sections`."""
    f = request.form
    updates, clears, errors = {}, [], []
    for spec in S.SCHEMA:
        if keys is not None:
            if spec["key"] not in keys:
                continue
        elif spec["section"] not in sections:
            continue
        k, t = spec["key"], spec["type"]
        try:
            if k == "AUTO_APPROVE":
                # shown as "Queue every change for my approval" (ticked = auto-approve off)
                if k in f:
                    updates[k] = S.coerce(spec, f.get(k))
                elif "has-QUEUE_ALL" in f:
                    updates[k] = "off" if f.get("QUEUE_ALL") == "on" else "on"
                continue
            if t == "bool":
                updates[k] = S.coerce(spec, f.get(k) == "on")
            elif t in ("secret", "password"):
                if f.get(f"clear-{k}") == "on":
                    clears.append(k)
                elif f.get(k, "").strip():
                    updates[k] = f.get(k).strip() if t == "password" else S.coerce(spec, f.get(k))
            elif t == "sources":
                on = [src for src in S.ALL_SOURCES if f.get(f"src-{src}") == "on"]

                def order(src):
                    try:
                        return int(f.get(f"order-{src}", 99))
                    except ValueError:
                        return 99
                updates[k] = S.coerce(spec, sorted(on, key=lambda x: (order(x), S.ALL_SOURCES.index(x))))
            elif t == "fields":
                updates[k] = S.coerce(spec, f.getlist(k))
            elif k in f:
                updates[k] = S.coerce(spec, f.get(k))
        except ValueError as e:
            errors.append(str(e))
    return updates, clears, errors


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    result, checks = None, None
    if request.method == "POST":
        what = request.form.get("test")
        section = request.form.get("section")
        reset = request.form.get("reset")
        if reset in S.BY_KEY:
            S.save(Config, {}, resets=[reset])
            flash(f"{S.BY_KEY[reset]['label']}: back to the compose / default value")
            nxt = request.form.get("next") or ""
            if nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt + f"#row-{reset}")
            return redirect(url_for("web.settings", tab=_setting_tab(S.BY_KEY[reset])) + f"#row-{reset}")
        if section:
            keys = None
            if section == "page":                     # one Settings page: only the settings it showed
                keys = set(request.form.getlist("k"))
                secs = []
            else:
                secs = [x for x in (request.form.get("sections") or "").split(",") if x] if section == "all" else [section]
            updates, clears, errors = _settings_form(secs, keys)
            before = {k: getattr(Config, k, None) for k in updates}
            S.save(Config, updates, clears=clears)
            changed = [k for k in updates if before.get(k) != getattr(Config, k, None)] + clears
            for e in errors:
                flash("Not saved: " + e)
            title = (request.form.get("page_title") or "Settings") if section == "page" else (
                "Settings" if section == "all" else dict((s[0], s[1]) for s in S.SECTIONS).get(section, "Settings"))
            if changed:
                flash(f"{title}: saved")
                db.log(f"Settings changed: {', '.join(changed)}")
            elif not errors:
                flash(f"{title}: nothing changed")
            if S.restart_needed(changed):
                flash("RESTART: Restart Isaiarr for the watcher changes to take effect.")
            nxt = request.form.get("next") or ""
            if section == "page" and not nxt:
                nxt = url_for("web.settings")
            if nxt.startswith("/") and not nxt.startswith("//"):
                return redirect(nxt)
            return redirect(url_for("web.settings") + ("" if section == "all" else f"#{section}"))
        if what == "discord":
            ok = notify.send(content="✅ Isaiarr test notification")
            result = ("Discord: sent" if ok else "Discord: failed (check the webhook URL)")
        elif what == "plex":
            try:
                result = "Plex check:\n" + "\n".join(plex.diagnose())
            except Exception as e:  # noqa: BLE001
                result = f"Plex failed: {e}"
        elif what == "notify_now":
            n = notify.flush(force=True)
            result = f"Sent summary for {n} album(s)"
        elif what == "sources":
            checks = _check_sources()
        elif what == "ai":
            try:
                data, u = ai.test()
                who = ai.PROVIDERS[u["provider"]][0] + " · " + u["model"]
                result = (f"AI OK: {who} answered" + (f" (suggested “{data.get('song')}”)" if data.get("song") else "")
                          + f" · {u['in'] + u['out']} tokens"
                          + (" · the first service failed, so the second one answered (see Activity → Log)"
                             if u.get("fallback") else ""))
            except Exception as e:  # noqa: BLE001
                result = f"AI failed: {e}"
    items = S.export(Config)
    by_section, by_key = {}, {}
    for it in items:
        by_section.setdefault(it["section"], []).append(it)
        by_key[it["key"]] = it
    all_tabs = {k: (t, ic) for _, tabs in SETTINGS_PAGES for k, t, ic in tabs}
    tab = request.args.get("tab", "tagging")
    if tab not in all_tabs:
        tab = "tagging"
    if checks is not None and tab == "tagging":
        tab = "sources"
    search_index = [{"k": it["key"], "l": it["label"], "h": it.get("help") or "",
                     "t": _setting_tab(it), "tl": all_tabs.get(_setting_tab(it), ("",))[0]}
                    for it in items if not it.get("hidden") and _setting_tab(it) in all_tabs]
    search_index += [{"k": "QUEUE_ALL", "l": "Queue every change for my approval", "h": "auto-approve",
                      "t": "tagging", "tl": "Tag rules"},
                     {"k": "src-itunes", "l": "Album sources: order and on/off", "h": "iTunes JioSaavn Wikipedia Discogs Deezer",
                      "t": "sources", "tl": "Metadata sources"}]
    ai_state = ("ok" if ai.enabled() and (db.state_get("ai_ok") is not False) else "off") if ai.enabled() else "off"
    price_note = "; ".join(f"{v[0]} ${v[3]} in / ${v[4]} out" for v in ai.PROVIDERS.values() if v[3])
    return render_template("settings.html", sections=[x for x in S.SECTIONS if x[0] in S.SETTINGS_PAGE],
                           page_sections=",".join(S.SETTINGS_PAGE), items=by_section, f=by_key,
                           pages=SETTINGS_PAGES, tab=tab, page_title=all_tabs[tab][0], page_desc=SETTINGS_DESC.get(tab, ""),
                           search_index=search_index, ai_state=ai_state, price_note=price_note,
                           result=result, checks=checks, all_sources=S.ALL_SOURCES, tag_fields=S.TAG_FIELDS,
                           accents=S.ACCENTS, auth_on=_auth_active(),
                           plex_state=_conn_state("plex", Config.PLEX_URL and Config.PLEX_TOKEN), plex_prefix=plex.detected_prefix(),
                           discord_state=_conn_state("discord", Config.DISCORD_WEBHOOK),
                           source_info=SOURCE_INFO, ai_providers=ai.PROVIDERS, ai_spend=ai.month_spend(),
                           reset_info=RS.preview() if tab == "matching" else None)


# ------------------------------------------------------------------ playlists

def _song_view(r, why=""):
    name = r.get("film") or r.get("album") or r.get("title") or "?"
    return {**r, "why": why, "tone": views.tone(name), "initial": views.initial(name), "cover": ""}


def _mosaic(ids):
    """Up to four tinted tiles (one per film) for a playlist's cover."""
    tiles, seen = [], set()
    for r in songs.get(list(ids)[:24]):
        name = r.get("film") or r.get("album") or r.get("title") or "?"
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        tiles.append({"tone": views.tone(name), "initial": views.initial(name)})
        if len(tiles) == 4:
            break
    return tiles


def _filters_from_form(f):
    out = {}
    if f.get("f_director"):
        out["directors"] = [f.get("f_director")]
    if f.get("f_singer", "").strip():
        out["singers"] = [f.get("f_singer").strip()]
    if f.get("f_decade", "").isdigit():
        out["decades"] = [int(f.get("f_decade"))]
    if f.get("f_genre"):
        out["genres"] = [f.get("f_genre")]
    if f.get("f_mood") in PL.MOODS:
        out["moods"] = [f.get("f_mood")]
    if f.get("f_lyrics") == "on":
        out["lyrics_only"] = True
    if f.get("f_rating", "").isdigit() and int(f.get("f_rating")):
        out["min_rating"] = int(f.get("f_rating")) * 2      # Plex stores 0-10; the form says stars
    if songs.status()["in_plex"]:
        out["in_plex"] = True                                # only songs Plex can play
    return out


@bp.route("/playlists")
def playlists_page():
    tab = request.args.get("tab", "describe")
    if tab not in ("describe", "song", "radio"):
        tab = "describe"
    st = songs.status()
    pool = st["in_plex"] or st["songs"]
    est_in, est_cost = ai.estimate(min(pool, ai.max_songs()))
    history = PL.history()
    for h in history:
        ids = [i["id"] for i in json.loads(h.get("items") or "[]")]
        h["n"], h["mosaic"] = len(ids), _mosaic(ids)
    seed = None
    if request.args.get("seed", "").isdigit():
        got = songs.get([int(request.args["seed"])])
        seed = got[0] if got else None
    return render_template("playlists.html", tab=tab, st=st, prof=PL.profile_status(), ai_on=ai.enabled(),
                           ai_label=ai.label(), ai_free=ai.prices() == (0.0, 0.0), est_in=est_in, est_cost=est_cost,
                           spend=ai.month_spend(), history=history, examples=PL.EXAMPLES,
                           directors=songs.directors(80), singers=songs.singers(150), decades=songs.decades(),
                           genres=songs.genres(), moods=PL.MOODS, dims=PL.DIMENSIONS, seed=seed,
                           have_profiles=st["profiled"] > 0, prompt=request.args.get("prompt", ""))


@bp.route("/playlists/new", methods=["POST"])
def playlist_new():
    f = request.form
    mode = f.get("mode", "describe")
    params = {"size": max(5, min(200, int(f.get("size") or Config.PLAYLIST_SIZE))), "filters": _filters_from_form(f),
              "title": f.get("title", "").strip()}
    prompt = f.get("prompt", "").strip()
    if mode == "describe":
        if not prompt:
            flash("Describe the playlist you want")
            return redirect(url_for("web.playlists_page"))
        if not ai.enabled():
            flash("Set up an AI service in Settings → AI & playlists first, or use Radio")
            return redirect(url_for("web.playlists_page", prompt=prompt))
    if mode in ("song", "radio") and f.get("seed", "").isdigit():
        params["seed"] = int(f.get("seed"))
        params["dims"] = [d for d, _ in PL.DIMENSIONS if f.get(f"dim-{d}") == "on"] or [d for d, _ in PL.DIMENSIONS]
    if mode == "song" and "seed" not in params:
        flash("Pick a song to start from")
        return redirect(url_for("web.playlists_page", tab="song"))
    if not songs.status()["songs"]:
        flash("The song index is empty: build it first (Playlists → Song index)")
        return redirect(url_for("web.playlists_page", tab=mode))
    pid = PL.create(mode, prompt, params)
    return redirect(url_for("web.playlist_view", pid=pid))


@bp.route("/playlists/<int:pid>")
def playlist_view(pid):
    pl = PL.get(pid)
    if not pl:
        abort(404)
    rows = songs.get([i["id"] for i in pl["items"]])
    why = {i["id"]: i.get("why", "") for i in pl["items"]}
    items = [_song_view(r, why.get(r["id"], "")) for r in rows]
    targets, plex_err = [], None
    if pl["status"] == "ready" and plex.enabled():
        try:
            targets = [t for t in plex.playlists() if not t["smart"]]
        except Exception as e:  # noqa: BLE001
            plex_err = str(e)
    total = sum(r.get("duration") or 0 for r in rows)
    return render_template("playlist.html", pl=pl, items=items, targets=targets, plex_err=plex_err,
                           mosaic=_mosaic([i["id"] for i in pl["items"]]),
                           plex_on=plex.enabled(), total=total,
                           missing=sum(1 for r in rows if not r.get("plex_key")))


@bp.route("/playlists/<int:pid>/save", methods=["POST"])
def playlist_save(pid):
    pl = PL.get(pid)
    if not pl or pl["status"] != "ready":
        abort(404)
    f = request.form
    keep = [int(i) for i in f.getlist("keep") if i.isdigit()]
    title = f.get("title", "").strip() or pl["title"] or "Isaiarr playlist"
    why = {i["id"]: i.get("why", "") for i in pl["items"]}
    rows = songs.get(keep)
    keys = [r["plex_key"] for r in rows if r.get("plex_key")]
    mode, target = f.get("save_mode", "new"), f.get("target") or None
    db.x("UPDATE playlists SET items=?, title=? WHERE id=?",
         (json.dumps([{"id": i, "why": why.get(i, "")} for i in keep]), title, pid))
    if not plex.enabled():
        flash("Plex isn't set up (Settings → Plex)")
        return redirect(url_for("web.playlist_view", pid=pid))
    if not keys:
        flash("None of these songs are in Plex yet. Run “Sync with Plex” on the Playlists page.")
        return redirect(url_for("web.playlist_view", pid=pid))
    try:
        plex_id, n = plex.save_playlist(title, keys, mode if target else "new", target)
    except Exception as e:  # noqa: BLE001
        flash(f"Plex didn't accept the playlist: {e}")
        return redirect(url_for("web.playlist_view", pid=pid))
    db.x("UPDATE playlists SET plex_id=?, saved=? WHERE id=?", (plex_id, time.time(), pid))
    skipped = len(keep) - len(keys)
    verb = {"append": "Added", "replace": "Replaced"}.get(mode if target else "new", "Saved")
    flash(f"{verb} “{title}” in Plex: {n} song(s)" + (f" · {skipped} not in Plex yet, left out" if skipped else ""))
    db.log(f"Playlist “{title}” saved to Plex ({n} songs)")
    return redirect(url_for("web.playlist_view", pid=pid))


@bp.route("/playlists/<int:pid>/again", methods=["POST"])
def playlist_again(pid):
    pl = PL.get(pid)
    if not pl:
        abort(404)
    new = PL.create(pl["mode"], pl["prompt"] or "", pl["params"])
    return redirect(url_for("web.playlist_view", pid=new))


@bp.route("/playlists/<int:pid>/delete", methods=["POST"])
def playlist_delete(pid):
    db.x("DELETE FROM playlists WHERE id=?", (pid,))
    flash("Removed from history (Plex playlists are left alone)")
    return redirect(url_for("web.playlists_page"))


@bp.route("/api/playlists/<int:pid>")
def api_playlist(pid):
    pl = PL.get(pid)
    if not pl:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": pl["status"], "error": pl["error"], "n": len(pl["items"]), "progress": pl["progress"]})


@bp.route("/api/playlists/count")
def api_playlist_count():
    f = _filters_from_form(request.args)
    n = PL.count(f)
    tin, cost = ai.estimate(min(n, ai.max_songs()))
    return jsonify({"count": n, "tokens": tin, "cost": cost})


@bp.route("/api/songs/search")
def api_song_search():
    q = request.args.get("q", "").strip()
    return jsonify(songs.search(q) if len(q) >= 2 else [])


@bp.route("/songs/<action>", methods=["POST"])
def songs_action(action):
    if action == "index":
        flash("Indexing songs in the background…" if songs.start_index(request.form.get("force") == "on")
              else "The song index is already being built")
    elif action == "plexsync":
        if not plex.enabled():
            flash("Plex isn't set up (Settings → Plex)")
        else:
            threading.Thread(target=worker._plex_sync, name="plexsync", daemon=True).start()
            flash("Syncing with Plex in the background…")
    elif action == "profiles":
        if not ai.enabled():
            flash("Set up an AI service first (Settings → AI & playlists)")
        elif PL.start_profiles():
            flash("Describing songs in the background. You can keep using Isaiarr.")
        else:
            flash("Already running, or nothing left to describe")
    elif action == "profiles-stop":
        PL.stop_profiles()
        flash("Stopping after the current batch")
    return redirect(url_for("web.playlists_page", tab=request.form.get("tab") or None) + "#library")


def send_back(folder, where, song=None):
    """Put the album a song (or folder) belongs to back in the review queue, re-checked first."""
    a = db.album_by_path(os.path.normpath(folder))
    note = f"from {where}" + (f" (“{song}”)" if song else "")
    return worker.queue_album(folder, origin=(a or {}).get("origin") or "manual", priority=db.PRIORITY_USER,
                              review=True, sent_from=note)


@bp.route("/songs/<int:sid>/review", methods=["POST"])
def song_review(sid):
    """"Tags look wrong": send the whole album of this track back for review, from anywhere."""
    s = db.q("SELECT folder, title, film, album FROM songs WHERE id=?", (sid,), one=True)
    back = request.referrer or url_for("web.index")
    quiet = request.headers.get("X-Requested-With") == "fetch"     # from a page with unsaved edits: no reload
    if not s or not os.path.isdir(s["folder"]):
        msg = "Couldn't find that song's folder any more; re-check songs on the Playlists page"
        if quiet:
            return jsonify(ok=False, error=msg), 404
        flash(msg)
        return redirect(back)
    where = {"duplicates": "Duplicates", "playlists": "a playlist"}.get(request.form.get("from", ""), "the app")
    aid = send_back(s["folder"], where, s["title"])
    name = s["album"] or s["film"] or os.path.basename(s["folder"])
    if quiet:
        return jsonify(ok=True, album=name, url=url_for("web.album", aid=aid))
    flash(f"GO:{url_for('web.album', aid=aid)}:Sent “{name}” back to the review queue. It's being re-checked "
          "and will wait for you under Review queue → Sent back.")
    return redirect(back)


@bp.route("/api/songs/status")
def api_songs_status():
    return jsonify({"index": songs.status(), "profiles": PL.profile_status()})


# ------------------------------------------------------------------ duplicates

DUPE_TABS = [("all", "All", None), ("exact", "Identical files", "exact"),
             ("recording", "Same song, another file", "recording"), ("likely", "Possible", "likely"),
             ("ignored", "Not duplicates", None)]


@bp.app_template_filter("bytes")
def human_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


@bp.app_template_filter("quality")
def quality_label(s):
    if s.get("lossless"):
        bits, sr = s.get("bits") or 16, (s.get("sample_rate") or 44100) / 1000
        name = "FLAC" if (s.get("path") or "").lower().endswith(".flac") else "ALAC"
        return f"{name} {bits}-bit · {sr:g} kHz"
    kbps = (s.get("bitrate") or 0) // 1000
    kind = os.path.splitext(s.get("path") or "")[1].lstrip(".").upper() or "Audio"
    return f"{kind} {kbps} kbps" if kbps else kind


@bp.route("/duplicates")
def duplicates_page():
    tab = request.args.get("tab", "all")
    tabs = {k: (l, kind) for k, l, kind in DUPE_TABS}
    if tab not in tabs:
        tab = "all"
    ignored = tab == "ignored"
    kind = tabs[tab][1]
    total = dupes.count(kind, ignored)
    pg = pager(total, _per_page(20))
    rows = dupes.groups(kind, ignored, pg["per"], pg["offset"])
    if dupes.prune_missing(rows):          # files deleted outside Isaiarr: show the list without them
        total = dupes.count(kind, ignored)
        pg = pager(total, _per_page(20))
        rows = dupes.groups(kind, ignored, pg["per"], pg["offset"])
    excl = dupes.exclusions() if ignored else []
    counts = {"all": dupes.count(), "exact": dupes.count("exact"), "recording": dupes.count("recording"),
              "likely": dupes.count("likely"),
              "ignored": dupes.count(ignored=True) + len(dupes.exclusions(10000))}
    return render_template("duplicates.html", tab=tab, tabs=DUPE_TABS, groups=rows, pg=pg, st=dupes.status(), excl=excl,
                           counts=counts, kind_label=dupes.KIND_LABEL, songs_st=songs.status())


@bp.route("/duplicates/scan", methods=["POST"])
def duplicates_scan():
    if not songs.status()["songs"]:
        flash("The song index is empty: it's built automatically shortly after start, or from Playlists → Re-check songs")
    elif dupes.start_scan():
        flash("Checking the library for duplicates in the background…")
    else:
        flash("A duplicate check is already running")
    return redirect(url_for("web.duplicates_page"))


@bp.route("/duplicates/<int:gid>/<action>", methods=["POST"])
def duplicates_action(gid, action):
    if action == "resolve":
        keep = request.form.get("keep", "")
        moved, _folders = dupes.resolve(gid, int(keep) if keep.isdigit() else None)
        flash(f"Kept one copy; moved {moved} file(s) to the bin" if moved else "Nothing was moved")
    elif action == "ignore":
        db.x("UPDATE dupe_groups SET ignored=1 WHERE id=?", (gid,))
        flash("Marked as not duplicates; this group won't be listed again")
    elif action == "unignore":
        db.x("UPDATE dupe_groups SET ignored=0 WHERE id=?", (gid,))
        flash("Listed again")
    return redirect(request.referrer or url_for("web.duplicates_page"))


@bp.route("/duplicates/<int:gid>/exclude/<int:sid>", methods=["POST"])
def duplicates_exclude(gid, sid):
    row = db.q("SELECT title, path FROM songs WHERE id=?", (sid,), one=True)
    res = dupes.exclude(gid, sid)
    if not res:
        flash("That file isn't in this group any more")
        return redirect(request.referrer or url_for("web.duplicates_page"))
    name = (row["title"] or row["path"].rsplit("/", 1)[-1]) if row else "File"
    msg = f"“{name}” taken out of the group (the file wasn't touched). " + _dupe_rest(res["others"], res["left"])
    flash(f"UNDO:{res['eid']}:{msg}" if res["eid"] else msg)
    return redirect(request.referrer or url_for("web.duplicates_page"))


def _dupe_rest(others, left):
    """Say plainly what became of the files left behind, so a vanished group isn't a mystery."""
    tab_name = {k: l for k, l, _ in DUPE_TABS}
    if not left:
        if others == 1:
            return "The group is closed: only one file was left, so there's nothing to compare it with."
        return ("The group is closed: the other files only matched through this one, "
                "and don't match each other closely enough on their own.")
    if len(left) == 1:
        g = left[0]
        where = "Not duplicates" if g["ignored"] else tab_name.get(g["kind"], "All")
        stay = f"The other {g['n']} files are still a group" if g["n"] == others else \
            f"{g['n']} of the other {others} files are still a group"
        return f"{stay}, under {where}."
    return f"The other files split into {len(left)} smaller groups."


@bp.route("/duplicates/exclusions/<int:eid>/delete", methods=["POST"])
def duplicates_unexclude(eid):
    dupes.unexclude(eid)
    flash("Put back: those files are grouped again")
    ref = request.referrer or ""
    return redirect(ref if "/duplicates" in ref else url_for("web.duplicates_page", tab="ignored"))


@bp.route("/duplicates/resolve-exact", methods=["POST"])
@bp.route("/duplicates/resolve-all/<kind>", methods=["POST"])
def duplicates_resolve_exact(kind="exact"):
    """Keep the suggested best copy of every group of one kind; the rest go to the bin (restorable)."""
    if kind not in ("exact", "recording"):       # "possible" groups always need a look, one by one
        abort(404)
    total = groups = 0
    for g in db.q("SELECT id, keep_id FROM dupe_groups WHERE kind=? AND ignored=0", (kind,)):
        moved, _ = dupes.resolve(g["id"], g["keep_id"])
        total += moved
        groups += 1
    what = "identical" if kind == "exact" else "same-song"
    flash(f"Moved {total} {what} extra file(s) from {groups} group(s) to the bin, keeping the best copy of each. "
          "Restore any of them from the bin.")
    return redirect(url_for("web.duplicates_page", tab=None if kind == "exact" else "recording"))


@bp.route("/duplicates/bin")
def duplicates_bin():
    return render_template("dupes_bin.html", items=dupes.bin_items(), st=dupes.status(), bin_dir=dupes.bin_dir())


@bp.route("/duplicates/bin/<int:mid>/restore", methods=["POST"])
def duplicates_restore(mid):
    flash("Restored to its folder" if dupes.restore(mid) else "Couldn't restore that file (is it still in the bin?)")
    return redirect(url_for("web.duplicates_bin"))


@bp.route("/duplicates/bin/empty", methods=["POST"])
def duplicates_empty():
    n = dupes.empty_bin()
    flash(f"Deleted {n} file(s) from the bin for good")
    return redirect(url_for("web.duplicates_bin"))


@bp.route("/api/duplicates/status")
def api_dupes_status():
    st = dupes.status()
    return jsonify({k: st[k] for k in ("running", "phase", "done", "total", "groups", "new", "error")})


@bp.route("/settings/restart", methods=["POST"])
def restart():
    """Exit so Docker's restart policy starts a fresh container process."""
    db.log("Restart requested from Settings")
    threading.Timer(1.0, lambda: os._exit(3)).start()
    return render_template("restarting.html")


@bp.route("/healthz")
def healthz():
    return jsonify({"ok": True, "version": __version__})


# ------------------------------------------------------------------ optional login

def _auth_active():
    if os.environ.get("AUTH_DISABLE", "").lower() in ("1", "true", "yes"):
        return False          # emergency switch if the password is forgotten
    return bool(Config.AUTH_USERNAME and Config.AUTH_PASSWORD_HASH)


@bp.before_app_request
def _require_login():
    if not _auth_active() or request.endpoint in ("web.healthz", "static"):
        return None
    a = request.authorization
    if a and a.username == Config.AUTH_USERNAME and S.check_password(Config, a.password):
        return None
    return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Isaiarr", charset="UTF-8"'})
