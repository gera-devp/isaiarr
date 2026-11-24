"""Every user-adjustable setting, in one place.

Value precedence for each key:  saved in the app (DATA_DIR/settings.json)  >  compose/env  >  default.
Saving in the Settings page writes settings.json and applies the change to the running app
immediately; only settings marked restart=True need a container restart.

Secrets (tokens, webhook, password) are never sent back to the browser.
"""
import json
import os
import re
import threading

from werkzeug.security import check_password_hash, generate_password_hash

ALL_SOURCES = ["itunes", "jiosaavn", "wikipedia", "discogs", "deezer"]
# The sources that existed before a version added new ones. A source added later is switched on
# (at the end of your list) until you save the source list in Settings, even when your list comes
# from the compose file; after that your choice stands. See apply().
SOURCES_BEFORE_3_6 = ["itunes", "jiosaavn", "wikipedia", "discogs"]
TAG_FIELDS = ["title", "artist", "album", "albumartist", "composer", "tracknumber", "discnumber",
              "date", "genre", "label", "grouping", "compilation"]

SECTIONS = [
    ("tagging", "Tag rules", "What gets written into the files."),
    ("kinds", "Kinds of album", "Film soundtracks, Various Artists, collections and singles."),
    ("renaming", "File renaming", "How tracks are renamed when an album is approved."),
    ("sources", "Metadata sources", "Where album and song details come from, in the order they're tried."),
    ("lyrics", "Lyrics", "Musixmatch first, then JioSaavn."),
    ("integrations", "Integrations", "Plex rescans and Discord messages."),
    ("duplicates", "Duplicates", "Finding the same song more than once in the library."),
    ("ai", "AI & playlists", "The AI model used for playlists, and playlist defaults."),
    ("auto", "Auto-approve", "Rules an album must pass to be written without waiting in the queue."),
    ("matching", "Matching", "How sure Isaiarr must be before it trusts a match. Confidence 9.0 = 90 %."),
    ("security", "Login", "Optional password for the web interface (also works with nzb360's basic auth)."),
    ("appearance", "Appearance", ""),
    ("watcher", "Watching for new music", "Shown on the Scan page."),
]
# Sections rendered on the Settings page (the watcher lives on the Scan page)
SETTINGS_PAGE = [k for k, _, _ in SECTIONS if k != "watcher"]
ACCENTS = [("#F2A43A", "Marigold"), ("#E8715A", "Coral"), ("#7FB6F0", "Sky"), ("#B9A3F0", "Lavender")]


AI_CHOICES = [("gemini", "Google Gemini (free tier available)"), ("groq", "Groq (free tier available)"),
              ("openrouter", "OpenRouter (free models available)"), ("mistral", "Mistral (free plan available)"),
              ("openai", "OpenAI"), ("anthropic", "Anthropic Claude"), ("ollama", "Ollama on your own computer"),
              ("custom", "Other OpenAI-compatible server (LM Studio, vLLM…)")]


def S(key, type_, default, section, label, help_="", **kw):
    return {"key": key, "type": type_, "default": default, "section": section, "label": label,
            "help": help_, **kw}


SCHEMA = [
    # ---------------------------------------------------------------- auto-approve
    S("AUTO_APPROVE", "choice", "off", "auto", "Auto-approve",
      "Shown in Tag rules as “Queue every change for my approval” (ticked = off).",
      choices=[("off", "Off: everything waits for review"), ("on", "On: write confident matches automatically")],
      hidden=True),
    S("AUTO_APPROVE_MIN", "float", 9.0, "auto", "Minimum album confidence", "0–10. 9 or higher is very safe.",
      min=0, max=10, step=0.1),
    S("AUTO_APPROVE_TRACK_MIN", "float", 7.5, "auto", "Minimum confidence for every song",
      "One weak song keeps the whole album for review.", min=0, max=10, step=0.1),
    S("AUTO_APPROVE_ALL_MATCHED", "bool", True, "auto", "Every file must be matched",
      "Skip albums with a 'no match' file."),
    S("AUTO_APPROVE_SCOPE", "choice", "new", "auto", "Applies to",
      choices=[("new", "New music found by the watcher"), ("all", "New music and manual folder scans")]),
    S("AUTO_APPROVE_COLLECTIONS", "bool", False, "auto", "Also auto-approve collections",
      "Collections are built song by song; reviewing them first is safer."),
    S("DEFAULT_ARTWORK", "choice", "missing", "auto", "Artwork when approving without the review page",
      "Used by auto-approve and 'Approve batch'; also the preselected choice on album pages.",
      choices=[("missing", "Add where missing"), ("all", "Replace on all tracks"), ("none", "Don't touch")]),
    S("DEFAULT_LYRICS", "choice", "missing", "auto", "Lyrics when approving without the review page",
      choices=[("missing", "Add where missing"), ("all", "Replace existing"), ("none", "Don't touch")]),

    # ---------------------------------------------------------------- sources
    S("SOURCES", "sources", list(ALL_SOURCES), "sources", "Album sources and order",
      "Later sources are tried only when earlier ones don't give a confident, complete match."),
    S("ITUNES_COUNTRY", "str", "in", "sources", "iTunes store country",
      "Two-letter code. 'in' has the most Tamil releases; try 'sg', 'my' or 'lk' for more.",
      pattern=r"^[a-z]{2}$", source="itunes"),
    S("ITUNES_RPM", "int", 18, "sources", "iTunes requests per minute", "Apple asks for about 20.",
      min=1, max=60, source="itunes"),
    S("JIOSAAVN_RPM", "int", 40, "sources", "JioSaavn requests per minute", min=1, max=120, source="jiosaavn"),
    S("WIKIPEDIA_CONTACT", "str", "", "sources", "Wikipedia contact (email or URL)",
      "Wikimedia asks API users to identify themselves; sent only to Wikipedia.", source="wikipedia"),
    S("WIKIPEDIA_RPM", "int", 30, "sources", "Wikipedia requests per minute", min=1, max=120, source="wikipedia"),
    S("DISCOGS_TOKEN", "secret", "", "sources", "Discogs personal access token",
      "discogs.com → Settings → Developers → Generate new token. Needed to search Discogs and for covers.",
      source="discogs"),
    S("DISCOGS_RPM", "int", 0, "sources", "Discogs requests per minute",
      "0 = automatic (50 with a token, 20 without).", min=0, max=60, source="discogs"),
    S("DEEZER_RPM", "int", 60, "sources", "Deezer requests per minute",
      "Deezer allows about 50 requests per 5 seconds.", min=1, max=600, source="deezer"),
    S("TMDB_CHECK", "bool", True, "sources", "Check the music director against TMDB",
      "After a film album is matched, its composer on TMDB is compared with the Album Artist. "
      "A match marks the suggestion confident; a different name holds the album for your decision.",
      group="tmdb"),
    S("TMDB_API_KEY", "secret", "", "sources", "TMDB API key or read access token",
      "Free: themoviedb.org → Settings → API. Either the API key or the longer read access token works.",
      group="tmdb"),
    S("TMDB_RPM", "int", 40, "sources", "TMDB requests per minute", min=1, max=600, group="tmdb"),
    S("SHAZAM_ENABLED", "bool", True, "sources", "Identify songs by their sound",
      "For songs no source found by title: Shazam recognises the audio, then the song is looked up "
      "as usual. Unofficial (Shazam has no public API), so it can stop working without notice.",
      group="shazam"),
    S("SHAZAM_RPM", "int", 12, "sources", "Shazam requests per minute",
      "Keep this low: Shazam blocks clients that ask too often.", min=1, max=60, group="shazam"),
    S("SHAZAM_MAX_PER_ALBUM", "int", 30, "sources", "Songs identified by sound per album, at most",
      "Big downloaded bundles could otherwise keep the queue busy for hours. 0 = no limit.",
      min=0, max=5000, group="shazam"),

    # ---------------------------------------------------------------- lyrics
    S("MUSIXMATCH_ENABLED", "bool", True, "lyrics", "Use Musixmatch"),
    S("MUSIXMATCH_MODE", "choice", "desktop", "lyrics", "Musixmatch access",
      choices=[("desktop", "Desktop app method (no key, full + synced lyrics, unofficial)"),
               ("api", "Official API key (free tier: partial lyrics only)")]),
    S("MUSIXMATCH_API_KEY", "secret", "", "lyrics", "Musixmatch API key", "Only for 'Official API key'."),
    S("MUSIXMATCH_USERTOKEN", "secret", "", "lyrics", "Musixmatch desktop user token",
      "Optional. Set it if the automatic token keeps hitting a captcha."),
    S("MUSIXMATCH_RPM", "int", 30, "lyrics", "Musixmatch requests per minute", min=1, max=120),
    S("JIOSAAVN_LYRICS", "bool", True, "lyrics", "JioSaavn lyrics when Musixmatch has none"),
    S("EMBED_LYRICS", "bool", True, "lyrics", "Embed lyrics in the files"),
    S("WRITE_LRC", "bool", True, "lyrics", "Write synced lyrics as .lrc next to the file", "Plex shows these as timed lyrics."),
    S("TRACK_FIELDS_SOURCE", "choice", "itunes", "lyrics", "Title & singers when Musixmatch also matched",
      choices=[("itunes", "Keep the album source's spelling"), ("musixmatch", "Use Musixmatch's spelling")]),

    # ---------------------------------------------------------------- matching
    S("FALLBACK_BELOW", "float", 8.0, "matching", "Try the next source below this confidence",
      min=0, max=10, step=0.1),
    S("FALLBACK_COVERAGE", "float", 0.9, "matching", "…or when fewer than this share of files matched",
      "0–1. 0.9 = 90 %.", min=0, max=1, step=0.05),
    S("MIN_ALBUM_COVERAGE", "float", 0.34, "matching", "An album must hold at least this share of the songs",
      "Below this, songs are matched one by one instead.", min=0, max=1, step=0.01),
    S("TRACK_FALLBACK", "bool", True, "matching", "Look up left-over files song by song"),
    S("LOW_CONFIDENCE", "float", 7.0, "matching", "Flag albums below this confidence", min=0, max=10, step=0.1),
    S("APPROVE_ALL_MIN", "float", 9.5, "matching", "“Approve all” in the review queue: minimum match",
      "0–10, as on the other confidence settings: 9.5 = 95 %. You can change it for one click next to the button.",
      min=5, max=10, step=0.1),
    S("COLLECTION_MODE", "bool", True, "matching", "Collection mode",
      "Folders of songs from many films become one compilation album named after the folder."),
    S("COLLECTION_MIN_FILMS", "int", 3, "matching", "Films needed to call a folder a collection", min=2, max=50),

    # ---------------------------------------------------------------- tagging
    S("OVERWRITE_MODE", "choice", "fix", "tagging", "When a file already has tags",
      choices=[("fix", "Fix incorrect tags and fill blanks; leave correct ones alone"), ("fill", "Only fill blanks")],
      radio=True),
    S("ARTIST_MODE", "choice", "singers", "tagging", "Artist tag",
      choices=[("singers", "Singers only"), ("md_first", "Music director first, then singers"),
               ("md", "Music director only")]),
    S("VARIOUS_ARTISTS", "str", "Various Artists", "tagging", "Album artist when there's no single music director"),
    S("WRITE_COMPOSER", "bool", True, "tagging", "Write the music director as Composer"),
    S("WRITE_RELEASETYPE", "bool", True, "kinds", "Write the release type",
      "Plex uses it to show albums under Soundtracks, Compilations or Singles & EPs.", ),
    S("POLICY_SUGGEST_MULTI", "bool", True, "kinds", "Suggest Various Artists when an album has several composers"),
    S("POLICY_SUGGEST_SINGLE", "bool", True, "kinds", "Suggest Various Artists for one-track singles",
      "The composer stays in the Composer tag."),
    S("POLICY_SUGGEST_COLLECTION", "bool", True, "kinds", "Suggest collections",
      "For folders named like the words below, or holding songs from three or more films."),
    S("POLICY_COLLECTION_WORDS", "str",
      "hits, collection, collections, best of, top songs, songs of, melodies, melody, evergreen, golden, greatest, "
      "all songs, jukebox, mashup, classics, favourites, favorites, selection", "kinds",
      "Words that mark a collection folder", "Separated by commas; matched as whole words in the folder name."),
    S("ALBUM_TAGS_UNMATCHED", "bool", True, "kinds", "Give songs no source found the album's tags",
      "Album, Album Artist, genre (and year) for the whole folder, so Plex keeps them in the album instead of "
      "filing them under their singers. Their own title and singers are left as they are."),
    S("PROTECTED_FIELDS", "fields", [], "tagging", "Never change these tags",
      "Ticked tags are left exactly as they are in your files."),
    S("ARTWORK_SIZE", "choice", 1400, "tagging", "Artwork size (iTunes)",
      choices=[(600, "600 px"), (1000, "1000 px"), (1400, "1400 px"), (3000, "3000 px (large files)")]),
    S("MP3_ID3_VERSION", "choice", 4, "tagging", "MP3 tag version",
      choices=[(4, "ID3v2.4 (recommended)"), (3, "ID3v2.3 (older players, Windows Explorer)")]),

    # ---------------------------------------------------------------- renaming
    S("RENAME_ENABLED", "bool", True, "renaming", "Rename tracks by default"),
    S("RENAME_PATTERN", "pattern", "{track:02d} - {title}", "renaming", "Pattern",
      "Fields: {track} {track:02d} {disc} {title} {artist} {album} {year}"),
    S("RENAME_PATTERN_MULTIDISC", "pattern", "{disc}-{track:02d} - {title}", "renaming", "Pattern for multi-disc albums"),

    # ---------------------------------------------------------------- watcher
    S("WATCH_ENABLED", "bool", True, "watcher", "Watching"),
    S("WATCH_MODE", "choice", "poll", "watcher", "How to watch",
      choices=[("poll", "Check every few seconds (reliable on Synology)"), ("inotify", "File-system events (faster)")],
      restart=True),
    S("POLL_INTERVAL", "int", 60, "watcher", "Check every (seconds)", min=10, max=3600),
    S("SETTLE_SECONDS", "choice", 120, "watcher", "Wait after last copy before scanning",
      "So half-copied albums aren't touched.",
      choices=[(60, "1 minute"), (120, "2 minutes"), (300, "5 minutes"), (900, "15 minutes"), (1800, "30 minutes")]),

    # ---------------------------------------------------------------- notifications
    S("DISCORD_WEBHOOK", "secret", "", "integrations", "Discord webhook URL",
      "Channel → Edit → Integrations → Webhooks → Copy URL.", url=True, group="discord"),
    S("DISCORD_BATCH_MINUTES", "int", 5, "integrations", "Batch messages for up to (minutes)", min=0, max=240,
      group="discord"),
    S("NOTIFY_REVIEW", "bool", True, "integrations", "New albums are waiting for review", group="discord"),
    S("NOTIFY_APPLIED", "bool", True, "integrations", "Approved changes are written",
      "Includes auto-approved albums.", group="discord"),
    S("NOTIFY_SCAN_MISSES", "bool", False, "integrations", "A scan finishes with misses", group="discord"),
    S("PUBLIC_URL", "url", "", "integrations", "Address of this app for links in messages",
      "e.g. http://<NAS-IP>:8687", group="discord"),

    # ---------------------------------------------------------------- plex
    S("PLEX_URL", "url", "", "integrations", "Server URL", "e.g. http://<NAS-IP>:32400", group="plex"),
    S("PLEX_TOKEN", "secret", "", "integrations", "Token", group="plex"),
    S("PLEX_RESCAN", "bool", True, "integrations", "Rescan the music library after each approval", group="plex"),
    S("PLEX_REFRESH_META", "bool", True, "integrations", "Also refresh the album's details in Plex",
      "A rescan alone often keeps the old album artist or title; this makes Plex re-read the tags.", group="plex"),
    S("PLEX_UNLOCK", "bool", True, "integrations", "Unlock the album's fields in Plex first",
      "Fields locked in Plex (by an old match or a hand edit) never follow the tags. "
      "Edits you made in Plex itself are replaced by the file's tags.", group="plex"),
    S("PLEX_AUTO_MERGE", "bool", True, "integrations", "Merge same-name artists in Plex",
      "Plex sometimes starts a second artist with the same name when albums are scanned at different times. "
      "Isaiarr merges them after a re-add and every night (Plex's own Merge; your files don't change).", group="plex"),
    S("PLEX_COLLECTIONS_KIND", "bool", True, "integrations", "Collections by kind of album",
      "Film soundtracks, Compilations, Singles and Non-film albums, from the release type in the tags.", group="plex"),
    S("PLEX_COLLECTIONS_DECADE", "bool", True, "integrations", "Collections by decade",
      "1980s, 1990s… from the album's year (not for compilations, whose songs span years).", group="plex"),
    S("PLEX_REVIEW", "bool", True, "integrations", "Album review from Wikipedia",
      "The soundtrack paragraph of the film's English Wikipedia page, credited as its licence asks (CC BY-SA). "
      "Only when the page's year and music director match the album. A review you wrote in Plex, or one from "
      "Plex's own agent, is never replaced.", group="plex"),
    S("PLEX_MOODS", "bool", True, "integrations", "Album moods from the AI song descriptions",
      "Up to 3 moods most of the album's songs share. Needs songs described by the AI "
      "(AI & playlists → Describe songs). Moods you add in Plex yourself stay.", group="plex"),
    S("WRITE_NFO", "bool", False, "integrations", "Write album.nfo and artist.nfo files",
      "Plex 1.43.5 or newer reads artist and album details from NFO files in music libraries (so do Kodi and "
      "Jellyfin). Written next to the music after every change, from the tags; NFO files Isaiarr didn't write are left alone.",
      group="plex"),
    S("PLEX_REFRESH_DELAY", "int", 60, "integrations", "Wait before refreshing details (seconds)",
      "Gives Plex time to finish the rescan first.", min=5, max=900, group="plex"),
    S("PLEX_MUSIC_PATH", "path", "", "integrations", "Music folder as Plex sees it",
      "Leave empty: it's detected from Plex (e.g. /volume2/Multimedia/Audio Songs for the Synology Plex package).",
      group="plex"),
    S("PLEX_SECTION_ID", "str", "", "integrations", "Plex library ID", "Leave empty to detect it.", pattern=r"^\d*$",
      group="plex"),

    # ---------------------------------------------------------------- duplicates
    S("DUPES_AUTO", "bool", True, "duplicates", "Check new music for duplicates",
      "Every time an album is added or written, its songs are compared with the rest of the library."),
    S("DUPES_FINGERPRINT", "bool", True, "duplicates", "Compare the sound, not just the tags",
      "Uses audio fingerprints (Chromaprint), so a FLAC and an MP3 of the same song are found even with "
      "different titles. The first check of a big library takes a while; after that only new songs are read."),
    S("DUPES_LEVEL", "choice", "normal", "duplicates", "How sure before calling it a duplicate",
      choices=[("strict", "Strict: identical files and near-identical sound only"),
               ("normal", "Normal: also list possible duplicates by title and length"),
               ("loose", "Loose: catch more, expect a few false alarms")]),
    S("DUPES_NOTIFY", "bool", True, "duplicates", "Tell me on Discord when new duplicates are found"),
    S("DUPES_BIN", "path", "", "duplicates", "Bin folder",
      "Where duplicates you remove are moved. Empty = a hidden folder in the music library "
      "(.isaiarr-bin), which Plex is told to ignore."),
    S("DUPES_BIN_DAYS", "int", 30, "duplicates", "Empty the bin after (days)", "0 = never; empty it yourself.",
      min=0, max=3650),

    # ---------------------------------------------------------------- AI & playlists
    S("AI_PROVIDER", "choice", "gemini", "ai", "AI service",
      choices=AI_CHOICES + [("off", "Off: Radio playlists only")]),
    S("AI_MODEL", "str", "", "ai", "Model", "Leave empty to use the service's default (for Gemini: the newest Flash model your key can use, found automatically)."),
    S("AI_FALLBACK", "choice", "off", "ai", "If it fails, ask",
      "A second service that gets the same request when the first is busy, over its limit or down. "
      "Its key goes below too.",
      choices=[("off", "Nobody: show the error")] + AI_CHOICES),
    S("AI_FALLBACK_MODEL", "str", "", "ai", "Model for the second service", "Leave empty for its default.",
      fallback=True),
    S("GEMINI_API_KEY", "secret", "", "ai", "Gemini API key", "aistudio.google.com → Get API key.", provider="gemini"),
    S("GROQ_API_KEY", "secret", "", "ai", "Groq API key", "console.groq.com → API Keys.", provider="groq"),
    S("OPENROUTER_API_KEY", "secret", "", "ai", "OpenRouter API key",
      "openrouter.ai → Keys. Models ending in “:free” cost nothing: 50 requests a day, or 1,000 a day once "
      "you've bought $10 of credit (one time).", provider="openrouter"),
    S("MISTRAL_API_KEY", "secret", "", "ai", "Mistral API key",
      "console.mistral.ai → API Keys. The free “Experiment” plan needs a phone number, no card; Mistral may use "
      "what you send to train its models. Its per-minute limits are low (see Limits in the console): if you see "
      "“Rate limit exceeded”, lower “Requests per minute at most”. Default model: mistral-small-latest.", provider="mistral"),
    S("OPENAI_API_KEY", "secret", "", "ai", "OpenAI API key", provider="openai"),
    S("ANTHROPIC_API_KEY", "secret", "", "ai", "Anthropic API key", provider="anthropic"),
    S("OLLAMA_URL", "url", "", "ai", "Ollama address", "e.g. http://192.168.0.120:11434", provider="ollama"),
    S("OLLAMA_CONTEXT", "int", 16384, "ai", "Ollama context size (tokens)", min=2048, max=262144, provider="ollama"),
    S("CUSTOM_AI_URL", "url", "", "ai", "Server address (ending in /v1)", "e.g. http://192.168.0.120:1234/v1",
      provider="custom"),
    S("CUSTOM_AI_KEY", "secret", "", "ai", "Server API key", "Only if the server needs one.", provider="custom"),
    S("AI_THINKING", "choice", "low", "ai", "Thinking (Gemini)",
      "How long Gemini thinks before answering. Low is fast and plenty for playlists.",
      choices=[("low", "Low: fastest"), ("medium", "Medium"), ("high", "High: slowest"), ("default", "Model default")]),
    S("AI_TIMEOUT", "int", 300, "ai", "Wait for an answer up to (seconds)", min=30, max=1800),
    S("AI_FREE_TIER", "bool", True, "ai", "I use the free tier (Gemini / Groq / OpenRouter / Mistral)",
      "Costs are shown as free; token counts are still tracked. Free tiers may use what you send to improve "
      "their models: only song titles, singers, directors, films and years are sent."),
    S("AI_MONTHLY_LIMIT", "float", 1.0, "ai", "Monthly spending limit ($)", "0 = no limit. Ignored on the free tier.",
      min=0, max=1000, step=0.5),
    S("AI_RPM", "int", 10, "ai", "Requests per minute at most", "Free tiers allow about 10 (Gemini), 20 (OpenRouter) or 30 (Groq); Mistral's free plan much fewer. Groq also caps tokens per minute.",
      min=1, max=600),
    S("AI_MAX_SONGS", "int", 0, "ai", "Songs sent per request", "0 = automatic for the service (Gemini 1500, "
      "Mistral 800, OpenRouter 500, Groq 150, Ollama 200; with a second service, the smaller of the two).", min=0, max=20000),
    S("AI_PRICE_IN", "float", -1.0, "ai", "Price per million input tokens ($)", "-1 = built-in estimate.",
      min=-1, max=1000, step=0.01),
    S("AI_PRICE_OUT", "float", -1.0, "ai", "Price per million output tokens ($)", "-1 = built-in estimate.",
      min=-1, max=1000, step=0.01),
    S("AI_PROFILE_NEW", "bool", False, "ai", "Describe new songs automatically",
      "Adds mood and type labels to newly tagged songs in the background."),
    S("PLAYLIST_SIZE", "int", 25, "ai", "Songs per playlist", min=5, max=200),
    S("PLAYLIST_MAX_PER_FILM", "int", 3, "ai", "At most this many songs from one film", min=1, max=50),
    S("PLAYLIST_SKIP_JUNK", "bool", True, "ai", "Skip karaoke, remixes, instrumentals and BGM",
      "Unless your prompt asks for them."),

    # ---------------------------------------------------------------- appearance
    S("ACCENT", "choice", "#F2A43A", "appearance", "Accent colour", choices=ACCENTS, swatches=True),

    # ---------------------------------------------------------------- security
    S("AUTH_USERNAME", "str", "", "security", "Username", "Leave empty to turn the login off."),
    S("AUTH_PASSWORD", "password", "", "security", "Password", "Stored only as a hash."),
]
BY_KEY = {s["key"]: s for s in SCHEMA}

_lock = threading.RLock()
_saved = {}
_path = None


# ------------------------------------------------------------------ parsing

def _from_env(spec, raw):
    t = spec["type"]
    raw = raw.strip()
    if t == "bool":
        return raw.lower() in ("1", "true", "yes", "on")
    if t == "int" or (t == "choice" and isinstance(spec["default"], int)):
        return int(raw)
    if t == "float":
        return float(raw)
    if t in ("sources", "fields"):
        return [x.strip().lower() for x in raw.split(",") if x.strip()]
    if t in ("url", "path"):
        return raw.rstrip("/")
    return raw


def coerce(spec, value):
    """Validate a value coming from the settings form. Raises ValueError with a friendly message."""
    t, label = spec["type"], spec["label"]
    if t == "bool":
        return value in (True, "on", "true", "1", 1)
    if t in ("int", "float"):
        try:
            v = int(value) if t == "int" else float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{label}: enter a number")
        if "min" in spec and v < spec["min"] or "max" in spec and v > spec["max"]:
            raise ValueError(f"{label}: must be between {spec.get('min')} and {spec.get('max')}")
        return v
    if t == "choice":
        allowed = [c for c, _ in spec["choices"]]
        v = int(value) if isinstance(spec["default"], int) else str(value)
        if v not in allowed:
            raise ValueError(f"{label}: not one of the options")
        return v
    if t == "sources":
        v = [s for s in value if s in ALL_SOURCES]
        if not v:
            raise ValueError("Album sources: keep at least one source switched on")
        return v
    if t == "fields":
        return [f for f in value if f in TAG_FIELDS]
    v = str(value or "").strip()
    if t in ("url", "secret") and spec.get("url", t == "url") and v and not re.match(r"^https?://", v):
        raise ValueError(f"{label}: must start with http:// or https://")
    if t in ("url", "path"):
        v = v.rstrip("/")
    if t == "pattern":
        if not v:
            raise ValueError(f"{label}: can't be empty")
        try:
            v.format(track=1, disc=1, title="t", artist="a", album="b", year="2000")
        except (KeyError, ValueError, IndexError) as e:
            raise ValueError(f"{label}: invalid pattern ({e})")
        if "/" in v or "\\" in v:
            raise ValueError(f"{label}: file names can't contain / or \\")
    if spec.get("pattern") and v and not re.match(spec["pattern"], v):
        raise ValueError(f"{label}: invalid value")
    return v


# ------------------------------------------------------------------ load / save / apply

def load(data_dir):
    global _path, _saved
    _path = os.path.join(data_dir, "settings.json")
    try:
        with open(_path, encoding="utf-8") as fh:
            data = json.load(fh)
        if "NOTIFY_AUTO" in data and "NOTIFY_APPLIED" not in data:      # renamed in 2.0
            data["NOTIFY_APPLIED"] = data["NOTIFY_AUTO"]
        _saved = {k: v for k, v in data.items() if k in BY_KEY or k in ("AUTH_PASSWORD_HASH", "_sources_known")}
    except (OSError, ValueError):
        _saved = {}


def _write():
    if not _path:
        return
    os.makedirs(os.path.dirname(_path), exist_ok=True)
    tmp = _path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(_saved, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, _path)
    try:
        os.chmod(_path, 0o600)            # tokens live here
    except OSError:
        pass


def origin(key):
    """'app' (saved here), 'env' (compose) or 'default'."""
    if key == "AUTH_PASSWORD":
        return "app" if "AUTH_PASSWORD_HASH" in _saved else "env" if os.environ.get("AUTH_PASSWORD") else "default"
    if key in _saved:
        return "app"
    return "env" if os.environ.get(key) not in (None, "") else "default"


def value(key):
    spec = BY_KEY[key]
    if key in _saved:
        return _saved[key]
    raw = os.environ.get(key)
    if raw not in (None, ""):
        try:
            return _from_env(spec, raw)
        except ValueError:
            pass
    d = spec["default"]
    return list(d) if isinstance(d, list) else d


def new_sources():
    """Sources added in a newer version that you haven't seen in Settings yet."""
    known = _saved.get("_sources_known") or SOURCES_BEFORE_3_6
    return [s for s in ALL_SOURCES if s not in known]


def apply(cfg):
    """Copy effective values onto the Config class (live)."""
    with _lock:
        for spec in SCHEMA:
            if spec["type"] == "password":
                continue
            setattr(cfg, spec["key"], value(spec["key"]))
        cfg.SOURCES = list(cfg.SOURCES) + [s for s in new_sources() if s not in cfg.SOURCES]
        if "AUTH_PASSWORD_HASH" in _saved:
            cfg.AUTH_PASSWORD_HASH = _saved["AUTH_PASSWORD_HASH"]
        elif os.environ.get("AUTH_PASSWORD"):
            cfg.AUTH_PASSWORD_HASH = generate_password_hash(os.environ["AUTH_PASSWORD"])
        else:
            cfg.AUTH_PASSWORD_HASH = ""


def save(cfg, updates, resets=(), clears=()):
    """updates: {key: already-coerced value}. resets: keys to drop back to compose/default.
    clears: secret keys to blank out (saved as empty, overriding compose)."""
    with _lock:
        for k, v in updates.items():
            spec = BY_KEY[k]
            if spec["type"] == "password":
                if v:
                    _saved["AUTH_PASSWORD_HASH"] = generate_password_hash(v)
                continue
            _saved[k] = v
            if k == "SOURCES":
                _saved["_sources_known"] = list(ALL_SOURCES)     # your choice now covers every source
        for k in clears:
            if k == "AUTH_PASSWORD":
                _saved["AUTH_PASSWORD_HASH"] = ""
            else:
                _saved[k] = ""
        for k in resets:
            _saved.pop(k, None)
            if k == "SOURCES":
                _saved["_sources_known"] = list(ALL_SOURCES)
            if k == "AUTH_PASSWORD":
                _saved.pop("AUTH_PASSWORD_HASH", None)
        _write()
        apply(cfg)


def check_password(cfg, password):
    return bool(cfg.AUTH_PASSWORD_HASH) and check_password_hash(cfg.AUTH_PASSWORD_HASH, password or "")


def restart_needed(changed_keys):
    return [k for k in changed_keys if BY_KEY.get(k, {}).get("restart")]


def export(cfg):
    """Settings for display: no secret values, ever."""
    out = []
    for spec in SCHEMA:
        k = spec["key"]
        v = getattr(cfg, k, None) if spec["type"] != "password" else None
        is_set = bool(cfg.AUTH_PASSWORD_HASH) if spec["type"] == "password" else bool(v)
        out.append({**spec, "value": None if spec["type"] in ("secret", "password") else v,
                    "is_set": is_set, "origin": origin(k)})
    return out
