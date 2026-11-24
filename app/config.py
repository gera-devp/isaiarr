"""Runtime configuration.

Paths and the port come only from the environment (they describe the container's mounts).
Everything else is defined in settings.py and can be changed in the app's Settings page;
values saved there override the compose file. Settings.apply() fills in the attributes below.
"""
import os

from . import settings


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


class Config:
    # Fixed by the container (compose only)
    MUSIC_DIR = os.environ.get("MUSIC_DIR", "/music").rstrip("/") or "/"
    DATA_DIR = os.environ.get("DATA_DIR", "/config").rstrip("/")
    PORT = _int("PORT", 8687)
    AUDIO_EXTS = (".flac", ".mp3", ".m4a")
    AUTH_PASSWORD_HASH = ""
    # Plex re-add ("Plex dance") timing
    PLEX_POLL = _int("PLEX_POLL", 3)                   # seconds between "is Plex done scanning?" checks
    PLEX_SCAN_TIMEOUT = _int("PLEX_SCAN_TIMEOUT", 900)
    PLEX_MAX_REPLAYS = _int("PLEX_MAX_REPLAYS", 200)   # play counts are restored one play at a time
    PLEX_LIVE_SETTLE = _int("PLEX_LIVE_SETTLE", 8)     # seconds of quiet before refreshing Plex totals

    @classmethod
    def reload(cls):
        settings.load(cls.DATA_DIR)
        settings.apply(cls)

    @classmethod
    def discogs_rpm(cls):
        return cls.DISCOGS_RPM or (50 if cls.DISCOGS_TOKEN else 20)


Config.reload()
