"""Discord webhook notifications, batched so a 1,000-album scan doesn't flood the channel."""
import os
import time

import requests

from . import db
from .config import Config

COLORS = {"review": 0x3B82F6, "nomatch": 0xF59E0B, "error": 0xEF4444, "applied": 0x22C55E}


def enabled():
    return bool(Config.DISCORD_WEBHOOK)


def send(content=None, embeds=None):
    if not enabled():
        return False
    try:
        r = requests.post(Config.DISCORD_WEBHOOK,
                          json={"username": "Isaiarr", "content": content, "embeds": embeds or []},
                          timeout=15)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)))
            return send(content, embeds)
        db.state_set("discord", {"ok": bool(r.ok), "at": time.time(),
                                 "error": None if r.ok else f"HTTP {r.status_code}"})
        return r.ok
    except requests.RequestException as e:
        db.log(f"Discord failed: {e}", "warn")
        db.state_set("discord", {"ok": False, "at": time.time(), "error": str(e)})
        return False


def scan_summary(p):
    folder = os.path.basename(p["path"].rstrip("/")) or p["path"]
    link = f"{Config.PUBLIC_URL}/scan" if Config.PUBLIC_URL else None
    return send(embeds=[{"title": f"Scan finished: {folder}", "url": link, "color": COLORS["nomatch"],
                         "description": f"{p['correct']} already correct · {p['review']} queued for review · "
                                        f"**{p['missed']} without a match**"}])


def _link(album_id):
    return f"{Config.PUBLIC_URL}/album/{album_id}" if Config.PUBLIC_URL else ""


def flush(force=False):
    """Send one summary for albums that finished analysis and weren't notified yet.
    Waits until the analysis queue is idle or DISCORD_BATCH_MINUTES has passed."""
    rows = db.q("SELECT * FROM albums WHERE notified=0 AND (status IN ('review','nomatch','error') "
                "OR status='applied') ORDER BY updated")
    if not rows:
        return 0
    oldest = rows[0]["updated"]
    idle = db.jobs_pending_count() == 0
    if not (force or idle or time.time() - oldest > Config.DISCORD_BATCH_MINUTES * 60):
        return 0
    waiting = [r for r in rows if r["status"] != "applied"] if Config.NOTIFY_REVIEW else []
    auto = [r for r in rows if r["status"] == "applied"]

    def line(r, flag):
        name = r["title"] or os.path.basename(r["path"])
        conf = f"{r['confidence']:.1f}" if r["confidence"] is not None else "-"
        link = _link(r["id"])
        label = f"[{name}]({link})" if link else name
        return f"{flag} {label} — {r['album_artist'] or '?'} · {conf}"

    embeds = []
    if waiting:
        counts = {}
        for r in waiting:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        lines = []
        for r in waiting[:15]:
            flag = {"nomatch": "⚠️", "error": "❌"}.get(r["status"], "🎵")
            if r["status"] == "review" and (r["confidence"] or 0) < Config.LOW_CONFIDENCE:
                flag = "🟡"
            lines.append(line(r, flag))
        if len(waiting) > 15:
            lines.append(f"…and {len(waiting) - 15} more")
        status = "error" if counts.get("error") else "nomatch" if counts.get("nomatch") else "review"
        embeds.append({"title": f"{len(waiting)} album(s) waiting for review", "description": "\n".join(lines),
                       "color": COLORS[status], "footer": {"text": ", ".join(f"{n} {s}" for s, n in counts.items())},
                       "url": Config.PUBLIC_URL or None})
    if auto:
        lines = [line(r, "✅" if r["auto"] else "✍️") for r in auto[:15]]
        if len(auto) > 15:
            lines.append(f"…and {len(auto) - 15} more")
        n_auto = sum(1 for r in auto if r["auto"])
        title = (f"{len(auto)} album(s) auto-approved and tagged" if n_auto == len(auto)
                 else f"{len(auto)} album(s) tagged" + (f" ({n_auto} auto-approved)" if n_auto else ""))
        embeds.append({"title": title, "description": "\n".join(lines),
                       "color": COLORS["applied"], "footer": {"text": "Undo from each album's page if needed"}})
    if not embeds:
        db.x("UPDATE albums SET notified=1 WHERE id IN (%s)" % ",".join(str(r["id"]) for r in rows))
        return 0
    ok = send(embeds=embeds)
    if ok or not enabled():
        db.x("UPDATE albums SET notified=1 WHERE id IN (%s)" % ",".join(str(r["id"]) for r in rows))
    return len(rows)
