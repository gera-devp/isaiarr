"""End-to-end test with generated audio and faked iTunes/Musixmatch/Discord/Plex HTTP."""
import importlib
import os
import json
import shutil
import subprocess
import time

import pytest


def make_audio(path, seconds, tags=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
           f"sine=frequency=440:duration={seconds}", "-ac", "1", "-ar", "22050"]
    for k, v in (tags or {}).items():
        cmd += ["-metadata", f"{k}={v}"]
    if path.endswith(".mp3"):
        cmd += ["-c:a", "libmp3lame", "-b:a", "64k", "-id3v2_version", "3"]
    elif path.endswith(".m4a"):
        cmd += ["-c:a", "aac", "-b:a", "64k"]
    cmd.append(path)
    subprocess.run(cmd, check=True)


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200

ITUNES_SEARCH = {"resultCount": 2, "results": [
    {"wrapperType": "collection", "collectionId": 111, "collectionName": "Vikram (Original Motion Picture Soundtrack)",
     "artistName": "Anirudh Ravichander", "trackCount": 3, "releaseDate": "2022-05-15T07:00:00Z",
     "primaryGenreName": "Tamil", "artworkUrl100": "https://is1.mzstatic.com/a/100x100bb.jpg",
     "collectionViewUrl": "https://music.apple.com/in/album/vikram/111"},
    {"wrapperType": "collection", "collectionId": 222, "collectionName": "Vikram (Telugu)",
     "artistName": "Anirudh Ravichander", "trackCount": 5, "releaseDate": "2022-05-15T07:00:00Z",
     "primaryGenreName": "Telugu", "artworkUrl100": "https://is1.mzstatic.com/b/100x100bb.jpg"},
]}


def lookup(cid):
    alb = [r for r in ITUNES_SEARCH["results"] if r["collectionId"] == cid][0]
    tracks = [("Pathala Pathala", "Kamal Haasan & Anirudh Ravichander", 20),
              ("Vikram Title Track", "Anirudh Ravichander", 25),
              ("Porkanda Singam", "Ravi G", 31)]
    res = [alb]
    for i, (t, a, d) in enumerate(tracks, 1):
        res.append({"wrapperType": "track", "kind": "song", "trackId": cid * 10 + i, "trackName": t,
                    "artistName": a, "trackNumber": i, "trackCount": 3, "discNumber": 1, "discCount": 1,
                    "trackTimeMillis": d * 1000, "primaryGenreName": "Tamil",
                    "releaseDate": "2022-05-15T07:00:00Z", "collectionName": alb["collectionName"]})
    return {"resultCount": len(res), "results": res}


class Resp:
    def __init__(self, data=None, content=b"", status=200):
        self._d, self.content, self.status_code = data, content, status
        self.ok = status < 400

    def json(self):
        return self._d

    def raise_for_status(self):
        if not self.ok:
            raise Exception(self.status_code)


CALLS = []
HEADERS_SEEN = {}
NO_WIKI = False


# --- "Salangai Oli": iTunes has an incomplete album (3 songs), JioSaavn has 4,
#     and one file in the folder is a song from another film (found only song-by-song).
SAL_ITUNES = {"wrapperType": "collection", "collectionId": 333,
              "collectionName": "Salangai Oli (Original Motion Picture Soundtrack)",
              "artistName": "Ilaiyaraaja", "trackCount": 3, "releaseDate": "1983-06-03T07:00:00Z",
              "primaryGenreName": "Tamil", "artworkUrl100": "https://is1.mzstatic.com/s/100x100bb.jpg"}
SAL_SONGS = [("Naatham Ennum", 20), ("Om Namasivaya", 22), ("Vedham Anuvilum", 24), ("Mounamana Neram", 26)]
SINGERS = [{"name": "S. P. Balasubrahmanyam", "role": "singer"}, {"name": "S. Janaki", "role": "singer"},
           {"name": "Ilaiyaraaja", "role": "music"}]


def saavn_song(i, title, dur, album="Salangai Oli", music="Ilaiyaraaja", sid=None):
    return {"id": sid or f"s{i}", "title": title.replace("&", "&amp;"), "type": "song", "year": "1983",
            "language": "tamil", "image": "https://c.saavncdn.com/x-150x150.jpg",
            "perma_url": f"https://www.jiosaavn.com/song/x/{i}",
            "more_info": {"album": album, "album_id": "777", "music": music, "duration": str(dur),
                          "has_lyrics": "true", "artistMap": {"artists": SINGERS, "primary_artists": SINGERS[:2]}}}


def fake_saavn(params):
    call = params.get("__call")
    q = (params.get("q") or "").lower()
    if call == "search.getAlbumResults":
        if "salangai" in q:
            return {"results": [{"id": "777", "title": "Salangai Oli", "subtitle": "Ilaiyaraaja", "year": "2016",
                                 "language": "tamil", "image": "https://c.saavncdn.com/a-150x150.jpg",
                                 "perma_url": "https://www.jiosaavn.com/album/salangai-oli/AbC_1",
                                 "more_info": {"song_count": "4", "music": "Ilaiyaraaja"}}]}
        return {"results": []}
    if call in ("content.getAlbumDetails", "webapi.get"):
        return {"id": "777", "title": "Salangai Oli", "year": "2016", "language": "tamil",
                "image": "https://c.saavncdn.com/a-150x150.jpg",
                "perma_url": "https://www.jiosaavn.com/album/salangai-oli/AbC_1",
                "list": [saavn_song(i, t, d) for i, (t, d) in enumerate(SAL_SONGS, 1)]}
    if call == "search.getResults":
        for key, song in SOUND_SAAVN.items():
            if key in q:
                return {"results": [song]}
        for key, (title, film, dur, year) in HITS.items():
            if key in q:
                song = saavn_song(50, title, dur, album=film, sid="h-" + key.replace(" ", ""))
                song["year"] = year
                return {"results": [song]}
        if "thakita" in q:
            return {"results": [saavn_song(9, "Thakita Thadimi", 28, album="Sagara Sangamam", sid="s9")]}
        return {"results": []}
    if call == "lyrics.getLyrics":
        return {"lyrics": "Saavn line one<br>Saavn line two"}
    return {}


WIKI_MODERN = """{{Use dmy dates}}
{{Infobox film
| name = Vikram
| music = [[Anirudh Ravichander]]<ref>{{cite web|title=x}}</ref>
| released = {{Film date|2022|06|03|df=y}}
| language = Tamil
}}
'''Vikram''' is a 2022 Indian [[Tamil language|Tamil]]-language film.
== Music ==
{{Main|Vikram (soundtrack)}}
{{Track listing
| headline = Vikram – Tamil
| extra_column = Singer(s)
| lyrics_credits = yes
| total_length = 20:15
| title1 = Pathala Pathala
| lyrics1 = [[Kamal Haasan]]
| extra1 = {{hlist|[[Kamal Haasan]]|[[Anirudh Ravichander|Anirudh]]}}
| length1 = 3:58
| title2 = "Wasted"
| extra2 = [[Anirudh Ravichander]], Ravi G<ref name=a/>
| length2 = 2:41
| title3 = Porkanda Singam
| note3 = Reprise
| extra3 = Ravi G
| music3 = [[Girishh Gopalakrishnan]]
| length3 = 4:05
}}
{{Track listing
| headline = Vikram – Telugu
| extra_column = Singer(s)
| title1 = Pathala Pathala (Telugu)
| extra1 = Someone
| length1 = 3:58
}}
[[Category:Tamil-language films]]
"""

WIKI_OLD = """{{Infobox film
| name = Punnagai Mannan
| music = [[Ilaiyaraaja]]
| released = 1 November 1986
| language = Tamil
}}
== Soundtrack ==
The soundtrack was composed by [[Ilaiyaraaja]].
{| class="wikitable"
|+ Tamil
! No. !! Song !! Singers !! Lyrics !! Length
|-
| 1 || "Ennapadi Ennapadi" || [[K. J. Yesudas]], [[K. S. Chithra]] || [[Vairamuthu]] || 04:31
|-
| 2 || "Kavithai Kelungal" || [[Vani Jairam]]<br />[[P. Jayachandran]] || Vairamuthu || 05:05
|-
| 3 || "Vaan Megam" || {{nowrap|[[K. S. Chithra]]}} || Vairamuthu || 04:40
|-
| colspan="4" | Total || 14:16
|}
"""


WIKI_EXTRA = {}       # title -> wikitext; a search containing the title's first part finds it


def fake_wiki(params):
    if params.get("list") == "search":
        q = params.get("srsearch", "").lower()
        hits = [{"title": "Punnagai Mannan"}] if "punnagai" in q and not NO_WIKI else []
        hits += [{"title": t} for t in WIKI_EXTRA if t.split(" (")[0].lower() in q]
        return {"query": {"search": hits}}
    if params.get("prop", "").startswith("revisions"):
        t = params.get("titles")
        wt = {"Punnagai Mannan": WIKI_OLD, "Vikram (2022 film)": WIKI_MODERN, **WIKI_EXTRA}.get(t)
        if not wt:
            return {"query": {"pages": [{"title": t, "missing": True}]}}
        return {"query": {"pages": [{"title": t, "revisions": [{"slots": {"main": {"content": wt}}}],
                                     "categories": [{"title": "Category:Tamil-language films"}]}]}}
    return {}



# --- Discogs: an Indian LP pressing with release-level credits that name the tracks they cover
DISCOGS_LP = {
    "id": 4455, "title": "Punnagai Mannan", "year": 1986, "country": "India",
    "uri": "https://www.discogs.com/release/4455-Ilaiyaraaja-Punnagai-Mannan",
    "artists": [{"name": "Ilaiyaraaja", "anv": "", "join": ""}],
    "genres": ["Stage & Screen"], "styles": ["Soundtrack"], "notes": "Tamil film soundtrack.",
    "formats": [{"name": "Vinyl"}],
    "images": [{"type": "primary", "uri": "https://i.discogs.com/full.jpg", "uri150": "https://i.discogs.com/150.jpg"}],
    "extraartists": [
        {"name": "Ilaiyaraaja", "anv": "", "role": "Music By", "tracks": ""},
        {"name": "Vairamuthu", "anv": "", "role": "Lyrics By", "tracks": ""},
        {"name": "K.J. Yesudas", "anv": "K. J. Yesudas", "role": "Vocals", "tracks": "A1"},
        {"name": "K.S. Chithra (2)", "anv": "", "role": "Vocals", "tracks": "A1, B1"},
        {"name": "Vani Jairam", "anv": "", "role": "Vocals", "tracks": "A2"},
        {"name": "P. Jayachandran", "anv": "", "role": "Vocals", "tracks": "A2"},
        {"name": "Studio Chorus", "anv": "", "role": "Backing Vocals", "tracks": "A1 to B1"},
    ],
    "tracklist": [
        {"position": "", "type_": "heading", "title": "Side A"},
        {"position": "A1", "type_": "track", "title": "Ennapadi Ennapadi", "duration": "4:31"},
        {"position": "A2", "type_": "track", "title": "Kavithai Kelungal", "duration": "5:05"},
        {"position": "B1", "type_": "track", "title": "Vaan Megam", "duration": ""},
        {"position": "B2", "type_": "track", "title": "Theme Music", "duration": "2:10",
         "extraartists": [{"name": "Gangai Amaran", "anv": "", "role": "Music By", "tracks": ""}]},
    ],
}

# --- Discogs: a label compilation CD, artist "Various", singers credited as track artists
DISCOGS_COMP = {
    "id": 7788, "title": "Hits Of Ilaiyaraaja Vol. 1", "year": 2004, "country": "India",
    "artists": [{"name": "Various", "join": ""}], "tracklist": [
        {"position": "1-1", "type_": "track", "title": "Ilamai Itho Itho", "duration": "4:21",
         "artists": [{"name": "S.P. Balasubrahmanyam", "join": ""}],
         "extraartists": [{"name": "Ilaiyaraaja", "role": "Composed By", "tracks": ""}]},
        {"position": "1-2", "type_": "track", "title": "Kanmani Anbodu", "duration": "6:02",
         "artists": [{"name": "Kamal Haasan", "join": "&"}, {"name": "S. Janaki", "join": ""}],
         "extraartists": [{"name": "Ilaiyaraaja", "role": "Composed By", "tracks": ""}]},
        {"position": "2-1", "type_": "track", "title": "Poongatru", "duration": "4:40",
         "artists": [{"name": "Malaysia Vasudevan", "join": ""}],
         "extraartists": [{"name": "Ilaiyaraaja", "role": "Composed By", "tracks": ""}]},
    ], "images": [],
}

# --- songs for a downloaded "hits" collection: each from a different film (JioSaavn song search)
SOUND_SAAVN = {}     # song search replies for the Shazam tests: words in the query -> song
HITS = {"ilamai itho": ("Ilamai Itho Itho", "Sakalakala Vallavan", 261, "1982"),
        "raja raja": ("Raja Raja Chozhan", "Rettai Vaal Kuruvi", 301, "1987"),
        "kanmani": ("Kanmani Anbodu", "Guna", 362, "1991"),
        "poongatru": ("Poongatru Pudhithanadhu", "Moondram Pirai", 280, "1982")}


def fake_discogs(url, params):
    if url.endswith("/database/search"):
        q = params.get("q", "").lower()
        if "punnagai" in q:
            return {"results": [{"id": 4455, "type": "release", "country": "India", "master_id": 99}]}
        return {"results": []}
    if url.endswith("/releases/4455"):
        return DISCOGS_LP
    if url.endswith("/releases/7788"):
        return DISCOGS_COMP
    return {}


DEEZER = {}          # path -> reply, filled by the Deezer tests
ITUNES_SONGS = {}    # Apple Music song id -> iTunes track result (Shazam tests)
FLAGS = {"itunes_strict": False}   # True: iTunes album search finds only Vikram and Salangai Oli
TMDB = {}            # path -> reply, filled by the TMDB tests


def fake_get(url, params=None, headers=None, timeout=None):
    CALLS.append(url)
    params = params or {}
    if "api.deezer.com" in url:
        path = url.split("api.deezer.com", 1)[1]
        if path.startswith("/search"):
            q = (params.get("q") or "").lower()
            return Resp(next((v for k, v in DEEZER.items() if k.startswith("search:") and k[7:] in q), {"data": [], "total": 0}))
        return Resp(DEEZER.get(path, {"error": {"type": "DataException", "message": "no data", "code": 800}}))
    if "api.themoviedb.org" in url:
        HEADERS_SEEN["tmdb"] = {"headers": dict(headers or {}), "params": dict(params)}
        path = url.split("/3", 1)[1]
        if path == "/search/movie":
            q = (params.get("query") or "").lower()
            return Resp(TMDB.get("search:" + q, {"results": []}))
        if path in TMDB:
            return Resp(TMDB[path])
        return Resp(status=404)
    if "api.discogs.com" in url:
        HEADERS_SEEN["discogs"] = dict(headers or {})
        return Resp(fake_discogs(url, params))
    if "wikipedia.org/w/api.php" in url:
        assert "Isaiarr/" in (headers or {}).get("User-Agent", "")
        return Resp(fake_wiki(params))
    if "jiosaavn.com/api.php" in url:
        return Resp(fake_saavn(params))
    if "itunes.apple.com/search" in url:
        if params.get("entity") == "song":
            return Resp({"resultCount": 0, "results": []})
        if "salangai" in params.get("term", "").lower():
            return Resp({"resultCount": 1, "results": [SAL_ITUNES]})
        if FLAGS["itunes_strict"] and "vikram" not in params.get("term", "").lower():
            return Resp({"resultCount": 0, "results": []})
        return Resp(ITUNES_SEARCH)
    if "itunes.apple.com/lookup" in url:
        if int(params["id"]) in ITUNES_SONGS:
            return Resp({"resultCount": 1, "results": [ITUNES_SONGS[int(params["id"])]]})
        if int(params["id"]) == 333:
            res = [SAL_ITUNES] + [{"wrapperType": "track", "kind": "song", "trackId": 3330 + i, "trackName": t,
                                   "artistName": "S. P. Balasubrahmanyam & S. Janaki", "trackNumber": i,
                                   "trackCount": 3, "discNumber": 1, "discCount": 1, "trackTimeMillis": d * 1000,
                                   "primaryGenreName": "Tamil", "releaseDate": "1983-06-03T07:00:00Z"}
                                  for i, (t, d) in enumerate(SAL_SONGS[:3], 1)]
            return Resp({"resultCount": len(res), "results": res})
        return Resp(lookup(int(params["id"])))
    if "saavncdn" in url:
        return Resp(content=JPEG)
    if "mzstatic" in url:
        return Resp(content=JPEG)
    if "token.get" in url:
        return Resp({"message": {"header": {"status_code": 200}, "body": {"user_token": "tok123"}}})
    if "macro.subtitles.get" in url:
        t = params["q_track"]
        if any(x[0].split()[0].lower() in t.lower() for x in SAL_SONGS) or "thakita" in t.lower():
            return Resp({"message": {"header": {"status_code": 404}, "body": {"macro_calls": {}}}})
        return Resp({"message": {"header": {"status_code": 200}, "body": {"macro_calls": {
            "matcher.track.get": {"message": {"body": {"track": {
                "track_id": 9, "track_name": t, "artist_name": "Anirudh Ravichander",
                "album_name": "Vikram", "track_length": int(params.get("q_duration", 20)),
                "instrumental": 0}}}},
            "track.lyrics.get": {"message": {"body": {"lyrics": {"lyrics_body": f"{t} line one\nline two"}}}},
            "track.subtitles.get": {"message": {"body": {"subtitle_list": [{"subtitle": {
                "subtitle_body": f"[00:01.00] {t} line one\n[00:05.00] line two"}}]}}},
        }}}})
    if "/library/sections" in url and url.endswith("/library/sections"):
        xml = b'<MediaContainer><Directory key="7" title="Music" type="artist" agent="tv.plex.agents.music"><Location path="/music"/></Directory></MediaContainer>'
        return Resp(content=xml)
    if "/refresh" in url:
        return Resp(content=b"")
    return Resp(status=404)


SHAZAM = {}          # file name -> Shazam answer; anything else is "not recognised"
SHAZAM_CALLS = []


def fake_shazam(path):
    SHAZAM_CALLS.append(os.path.basename(path))
    return SHAZAM.get(os.path.basename(path), {"matches": [], "timestamp": 1})


@pytest.fixture()
def env(tmp_path, monkeypatch):
    music, data = tmp_path / "music", tmp_path / "config"
    monkeypatch.setenv("MUSIC_DIR", str(music))
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("ITUNES_RPM", "6000")
    monkeypatch.setenv("MUSIXMATCH_RPM", "6000")
    monkeypatch.setenv("JIOSAAVN_RPM", "6000")
    monkeypatch.setenv("WIKIPEDIA_RPM", "6000")
    monkeypatch.setenv("DISCOGS_RPM", "6000")
    monkeypatch.setenv("DEEZER_RPM", "6000")
    monkeypatch.setenv("TMDB_RPM", "6000")
    monkeypatch.setenv("SHAZAM_RPM", "6000")
    monkeypatch.setenv("SETTLE_SECONDS", "0")
    monkeypatch.setenv("PLEX_URL", "http://plex:32400")
    monkeypatch.setenv("PLEX_TOKEN", "x")
    monkeypatch.setenv("PLEX_MUSIC_PATH", "/music")
    import app.config, app.db
    importlib.reload(app.config)
    app.db.reset_for_tests()
    for m in ("app.db", "app.tags", "app.sources.http", "app.sources.itunes", "app.sources.jiosaavn", "app.sources.wikipedia", "app.sources.discogs", "app.sources.musixmatch",
              "app.sources.deezer", "app.sources.tmdb", "app.sources.shazam",
              "app.matcher", "app.applier", "app.plex", "app.plexfix", "app.plexlive", "app.notify", "app.watcher", "app.songs", "app.ai",
              "app.playlists", "app.dupes", "app.policy", "app.folders", "app.artists", "app.nfo", "app.trackcheck", "app.plexmeta",
              "app.worker", "app.web"):
        importlib.reload(importlib.import_module(m))
    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    DEEZER.clear(); TMDB.clear(); SHAZAM.clear(); SHAZAM_CALLS.clear(); SOUND_SAAVN.clear(); ITUNES_SONGS.clear(); FLAGS["itunes_strict"] = False; WIKI_EXTRA.clear()
    from app.sources import shazam as _sz
    monkeypatch.setattr(_sz, "_recognize", fake_shazam)
    album = music / "Anirudh" / "Vikram"
    make_audio(str(album / "01 pathala.flac"), 20, {"title": "Pathala Pathala", "artist": "Anirudh"})
    make_audio(str(album / "Track02.mp3"), 25, {"title": "vikram title", "album": "Vikram"})
    make_audio(str(album / "porkanda singam.m4a"), 31)
    with open(album / "porkanda singam.lrc", "w") as fh:
        fh.write("[00:00.00] old lrc\n")
    yield music, data, album


def run_jobs():
    from app import db, worker
    while True:
        j = db.job_next()
        if not j:
            return
        worker._run(j)
        db.job_finish(j["id"])


def test_full_flow(env):
    music, data, album = env
    from app import applier, db, tags, worker
    snapshot = {f: tags.read(str(album / f))["fields"] for f in os.listdir(album) if not f.endswith(".lrc")}

    aid = worker.queue_album(str(album), origin="manual")
    run_jobs()
    a = db.album_get(aid)
    assert a["status"] == "review", a["error"]
    p = a["proposal"]
    assert p["album"]["collection_id"] == "itunes:111"          # Tamil edition beats Telugu
    assert p["album"]["album_artist"] == "Anirudh Ravichander"
    assert a["confidence"] >= 7, a["confidence"]
    byfile = {f["file"]: f for f in p["files"]}
    assert byfile["Track02.mp3"]["new"]["title"] == "Vikram Title Track"
    assert byfile["porkanda singam.m4a"]["new"]["tracknumber"] == "3"
    assert byfile["01 pathala.flac"]["new"]["artist"] == "Kamal Haasan & Anirudh Ravichander"
    assert byfile["01 pathala.flac"]["changes"]["artist"] == "fix"
    assert "title" not in byfile["01 pathala.flac"]["changes"]   # already correct → skipped
    assert byfile["Track02.mp3"]["rename_to"] == "02 - Vikram Title Track.mp3"
    assert byfile["Track02.mp3"]["synced"].startswith("[00:01.00]")

    # approve through the web form, editing one title
    from app import create_app
    client = create_app(start_background=False).test_client()
    form = {"a-album": p["album"]["album"], "a-albumartist": "Anirudh Ravichander",
            "a-date": "2022", "a-genre": "Tamil", "rename": "on", "artwork": "missing", "lyrics": "missing"}
    for i, f in enumerate(p["files"]):
        form[f"f-{i}-include"] = "on"
        for k in ("title", "artist", "tracknumber", "discnumber"):
            form[f"f-{i}-{k}"] = f["new"].get(k) or f["current"][k]
    i3 = [i for i, f in enumerate(p["files"]) if f["file"].startswith("porkanda")][0]
    form[f"f-{i3}-title"] = "Porkanda Singam (Edited)"
    r = client.post(f"/album/{aid}/apply", data=form)
    assert r.status_code == 302
    run_jobs()
    a = db.album_get(aid)
    assert a["status"] == "applied", a["error"]

    files = sorted(os.listdir(album))
    assert "01 - Pathala Pathala.flac" in files
    assert "02 - Vikram Title Track.mp3" in files
    assert "03 - Porkanda Singam (Edited).m4a" in files
    assert "03 - Porkanda Singam (Edited).lrc" in files       # existing sidecar followed the rename
    assert "02 - Vikram Title Track.lrc" in files            # new synced sidecar
    for name, kind in (("01 - Pathala Pathala.flac", "flac"), ("02 - Vikram Title Track.mp3", "mp3"),
                       ("03 - Porkanda Singam (Edited).m4a", "m4a")):
        info = tags.read(str(album / name))
        f = info["fields"]
        assert f["albumartist"] == "Anirudh Ravichander", (kind, f)
        assert f["composer"] == "Anirudh Ravichander"
        assert f["album"] == "Vikram (Original Motion Picture Soundtrack)"
        assert f["date"] == "2022" and f["genre"] == "Tamil" and f["tracktotal"] == "3"
        assert info["has_art"] and info["has_lyrics"], kind
    assert tags.read(str(album / "03 - Porkanda Singam (Edited).m4a"))["fields"]["title"] == "Porkanda Singam (Edited)"
    # existing .lrc was not overwritten in 'missing' mode
    assert open(album / "03 - Porkanda Singam (Edited).lrc").read().startswith("[00:00.00] old lrc")
    assert any("/library/sections/7/refresh" in c for c in CALLS)

    # undo restores names, tags and sidecars
    applier.undo(aid)
    assert sorted(os.listdir(album)) == sorted(list(snapshot) + ["porkanda singam.lrc"])
    for fname, before in snapshot.items():
        assert tags.read(str(album / fname))["fields"] == before, fname
        assert not tags.read(str(album / fname))["has_art"]


def test_various_and_artist_modes(env, monkeypatch):
    from app import matcher
    from app.config import Config
    assert matcher.resolve_album_artist("Yuvan Shankar Raja & Anirudh Ravichander") == "Various Artists"
    assert matcher.resolve_album_artist("Various Artists") == "Various Artists"
    assert matcher.resolve_album_artist("A.R. Rahman") == "A.R. Rahman"
    monkeypatch.setattr(Config, "ARTIST_MODE", "md_first")
    assert matcher.resolve_artist("Sid Sriram, A.R. Rahman", "A.R. Rahman") == "A.R. Rahman; Sid Sriram"
    monkeypatch.setattr(Config, "ARTIST_MODE", "singers")
    assert matcher.resolve_artist("Sid Sriram", "A.R. Rahman") == "Sid Sriram"
    assert matcher.clean_album("Vikram (Original Motion Picture Soundtrack)") == "Vikram"
    assert matcher.safe_filename('a/b: "c"?') == "a-b- -c"


def test_watcher_detects_new_folder(env):
    music, data, album = env
    from app import db, watcher
    watcher.RECHECK = 0
    watcher._snapshot.update(watcher._walk_snapshot())
    new = music / "A.R. Rahman" / "Roja"
    make_audio(str(new / "01.flac"), 5)
    watcher.poll_once()
    assert str(new) in watcher._pending
    watcher.settle_once()                 # first pass records sizes
    watcher.settle_once()                 # sizes stable → queued
    a = db.album_by_path(str(new))
    assert a and a["status"] == "queued" and a["origin"] == "watch"
    # our own renames must not requeue an album
    watcher.suppress(str(album))
    os.rename(album / "Track02.mp3", album / "02 - x.mp3")
    watcher.poll_once()
    assert str(album) not in watcher._pending


def test_pages_render(env):
    music, data, album = env
    from app import create_app, worker
    worker.queue_album(str(album))
    run_jobs()
    c = create_app(start_background=False).test_client()
    for url in ("/", "/?tab=all", "/album/1", "/scan", "/history", "/log", "/settings",
                "/api/status", "/api/browse?path=Anirudh", "/api/search?q=vikram"):
        r = c.get(url)
        assert r.status_code == 200, url
    assert c.get("/api/browse?path=../../etc").status_code == 404
    r = c.post("/scan", data={"path": "Anirudh", "recursive": "on"})
    assert r.status_code == 302


def test_discord_summary(env, monkeypatch):
    music, data, album = env
    import requests
    from app import notify, worker
    from app.config import Config
    sent = []
    monkeypatch.setattr(Config, "DISCORD_WEBHOOK", "https://discord.test/hook")
    monkeypatch.setattr(Config, "PUBLIC_URL", "http://nas:8687")
    monkeypatch.setattr(requests, "post", lambda url, json=None, timeout=None: (sent.append(json), Resp({}))[1])
    worker.queue_album(str(album))
    run_jobs()
    assert notify.flush() == 1
    emb = sent[0]["embeds"][0]
    assert "1 album(s) waiting" in emb["title"] and "http://nas:8687/album/1" in emb["description"]
    assert notify.flush() == 0          # not sent twice


def test_jiosaavn_fallback_and_song_lookup(env):
    music, data, album = env
    from app import applier, db, tags, worker
    folder = music / "Ilaiyaraaja" / "Salangai Oli"
    make_audio(str(folder / "Naatham.mp3"), 20, {"title": "Naatham"})
    make_audio(str(folder / "Om Namasivaya.flac"), 22)
    make_audio(str(folder / "Vedham.mp3"), 24, {"title": "Vedham"})
    make_audio(str(folder / "Mounamana Neram.mp3"), 26)
    make_audio(str(folder / "Thakita Thadimi.mp3"), 28)
    make_audio(str(folder / "Unknown Bonus.mp3"), 33)
    aid = worker.queue_album(str(folder))
    run_jobs()
    a = db.album_get(aid)
    assert a["status"] == "review", a["error"]
    p = a["proposal"]
    assert p["album"]["source"] == "jiosaavn"            # iTunes album was incomplete
    assert p["album"]["collection_id"] == "jiosaavn:777"
    assert p["album"]["album_artist"] == "Ilaiyaraaja"
    assert p["album"]["date"] == "1983"                  # earliest year (iTunes), not JioSaavn's 2016 re-release
    assert any("earliest release" in n for n in p["notes"])
    assert any(c["source"] == "itunes" for c in a["candidates"]) and any(c["source"] == "jiosaavn" for c in a["candidates"])
    by = {f["file"]: f for f in p["files"]}
    m = by["Mounamana Neram.mp3"]
    assert m["match"] == "album" and m["new"]["tracknumber"] == "4"
    assert m["new"]["artist"] == "S. P. Balasubrahmanyam, S. Janaki"     # singers, not the composer
    assert m["new"]["composer"] == "Ilaiyaraaja"
    assert m["lyrics_source"] == "jiosaavn" and "Saavn line two" in m["lyrics"]
    t = by["Thakita Thadimi.mp3"]
    assert t["match"] == "track" and t["source_track"]["album"] == "Sagara Sangamam"
    assert t["new"]["album"] == "Salangai Oli" and "tracknumber" not in t["new"]   # folder stays one album
    u = by["Unknown Bonus.mp3"]
    assert u["match"] is None and set(u["changes"]) == {"album", "albumartist", "date", "genre", "composer"}
    assert u["new"]["albumartist"] == "Ilaiyaraaja" and "title" not in u["new"] and not u["rename_to"]
    assert p["matched"] == 5

    # page renders with the new fields; bulk-style apply skips the unmatched file
    from app import create_app
    c = create_app(start_background=False).test_client()
    html = c.get(f"/album/{aid}").get_data(as_text=True)
    assert "JioSaavn" in html and "no match" in html and "from Sagara Sangamam" in html
    db.album_update(aid, status="applying")
    db.job_add("apply", aid, {"rename": True, "artwork": "missing", "lyrics": "missing"})
    run_jobs()
    assert db.album_get(aid)["status"] == "applied"
    names = sorted(os.listdir(folder))
    assert "04 - Mounamana Neram.mp3" in names and "Unknown Bonus.mp3" in names
    ub = tags.read(str(folder / "Unknown Bonus.mp3"))
    assert not ub["has_art"]                                              # unmatched: no cover, no rename…
    assert ub["fields"]["albumartist"] == "Ilaiyaraaja" and ub["fields"]["album"] == "Salangai Oli"   # …but in the album
    f4 = tags.read(str(folder / "04 - Mounamana Neram.mp3"))
    assert f4["fields"]["albumartist"] == "Ilaiyaraaja" and f4["has_art"] and f4["has_lyrics"]
    db.job_add("undo", aid); run_jobs()
    assert (tags.read(str(folder / "Unknown Bonus.mp3"))["fields"].get("albumartist") or "") == ""   # undo covers it too
    db.job_add("apply", aid, {"rename": True, "artwork": "missing", "lyrics": "missing"}); run_jobs()


def test_refs_and_rematch(env):
    music, data, album = env
    from app import db, matcher, worker
    assert matcher.parse_ref("https://music.apple.com/in/album/vikram/111") == "itunes:111"
    assert matcher.parse_ref("https://itunes.apple.com/in/album/x/id222?uo=4") == "itunes:222"
    assert matcher.parse_ref("https://www.jiosaavn.com/album/salangai-oli/AbC_1") == "jiosaavn:AbC_1"
    assert matcher.parse_ref("jiosaavn:777") == "jiosaavn:777"
    assert matcher.parse_ref("hello") is None
    aid = worker.queue_album(str(album))
    run_jobs()
    from app import create_app
    c = create_app(start_background=False).test_client()
    c.post(f"/album/{aid}/rematch", data={"collection_id": "https://music.apple.com/in/album/vikram-telugu/222"})
    run_jobs()
    assert db.album_get(aid)["proposal"]["album"]["collection_id"] == "itunes:222"
    assert c.get("/api/search?source=jiosaavn&q=salangai").get_json()[0]["ref"] == "jiosaavn:777"


def test_wikipedia_parser():
    from app.sources import wikipedia as W
    alb, tr = W.parse_page("Vikram (2022 film)", WIKI_MODERN, ["Category:Tamil-language films"])
    assert alb["album"] == "Vikram" and alb["album_artist"] == "Anirudh Ravichander"
    assert alb["date"] == "2022" and alb["genre"] == "Tamil" and alb["track_count"] == 3   # Telugu listing ignored
    assert [t["title"] for t in tr] == ["Pathala Pathala", "Wasted", "Porkanda Singam"]
    assert tr[0]["artist"] == "Kamal Haasan, Anirudh"
    assert tr[1]["artist"] == "Anirudh Ravichander, Ravi G"
    assert tr[2]["composer"] == "Girishh Gopalakrishnan" and tr[0]["composer"] == "Anirudh Ravichander"
    assert tr[0]["duration"] == 238
    alb, tr = W.parse_page("Punnagai Mannan", WIKI_OLD)
    assert alb["album_artist"] == "Ilaiyaraaja" and alb["date"] == "1986" and alb["genre"] == "Tamil"
    assert [t["title"] for t in tr] == ["Ennapadi Ennapadi", "Kavithai Kelungal", "Vaan Megam"]
    assert tr[0]["artist"] == "K. J. Yesudas, K. S. Chithra"
    assert tr[1]["artist"] == "Vani Jairam, P. Jayachandran"
    assert tr[2]["artist"] == "K. S. Chithra" and tr[0]["duration"] == 271
    assert W.parse_ref("https://en.wikipedia.org/wiki/Punnagai_Mannan#Soundtrack") == "Punnagai_Mannan"
    assert W.parse_ref("https://en.m.wikipedia.org/wiki/Vikram_(2022_film)") == "Vikram_(2022_film)"


def test_wikipedia_as_last_resort(env):
    music, data, album = env
    from app import db, matcher, worker
    folder = music / "Ilaiyaraaja" / "Punnagai Mannan"
    make_audio(str(folder / "Ennapadi.mp3"), 271)
    make_audio(str(folder / "Kavithai Kelungal.mp3"), 305)
    make_audio(str(folder / "Vaan Megam.flac"), 280)
    aid = worker.queue_album(str(folder))
    run_jobs()
    a = db.album_get(aid)
    assert a["status"] == "review", a["error"]
    p = a["proposal"]
    assert p["album"]["source"] == "wikipedia" and p["album"]["collection_id"] == "wikipedia:Punnagai_Mannan"
    by = {f["file"]: f for f in p["files"]}
    k = by["Kavithai Kelungal.mp3"]
    assert k["new"]["albumartist"] == "Ilaiyaraaja" and k["new"]["artist"] == "Vani Jairam, P. Jayachandran"
    assert k["new"]["tracknumber"] == "2" and k["new"]["date"] == "1986-11-01"   # full date from the infobox
    assert k["rename_to"] == "02 - Kavithai Kelungal.mp3"
    assert p["matched"] == 3
    assert matcher.parse_ref("https://en.wikipedia.org/wiki/Punnagai_Mannan") == "wikipedia:Punnagai_Mannan"
    from app import create_app
    c = create_app(start_background=False).test_client()
    assert "Wikipedia" in c.get(f"/album/{aid}").get_data(as_text=True)
    assert c.get("/api/search?source=wikipedia&q=punnagai mannan").get_json()[0]["album"] == "Punnagai Mannan"


def test_rematch_jumps_the_queue_and_page_updates(env):
    music, data, album = env
    from app import db, worker, create_app
    aid = worker.queue_album(str(album))
    run_jobs()
    for i in range(30):                      # a big library scan waiting in the queue
        d = music / "Bulk" / f"A{i}"
        d.mkdir(parents=True)
        db.job_add("analyze", db.album_upsert(str(d), "queued"))
    c = create_app(start_background=False).test_client()
    r = c.post(f"/album/{aid}/rematch", data={"collection_id": "itunes:222"})
    assert r.status_code == 302
    st = c.get(f"/api/album/{aid}").get_json()
    assert st["status"] == "queued" and st["ahead"] == 0
    html = c.get(f"/album/{aid}").get_data(as_text=True)
    assert 'id="working"' in html and "reloads by itself" in html
    j = db.job_next()
    assert j["album_id"] == aid and j["payload"]["ref"] == "itunes:222"
    # a second click while still queued replaces the target instead of being ignored
    db.x("UPDATE jobs SET status='pending' WHERE id=?", (j["id"],))
    c.post(f"/album/{aid}/rematch", data={"collection_id": "itunes:111"})
    assert db.job_next()["payload"]["ref"] == "itunes:111"


def test_pagination_and_refresh(env):
    music, data, album = env
    from app import db, create_app
    for i in range(120):
        aid = db.album_upsert(f"/x/album{i:03d}", "review")
        db.album_update(aid, title=f"Album {i:03d}", confidence=8.0)
    c = create_app(start_background=False).test_client()
    html = c.get("/?tab=review&per=25&page=3").get_data(as_text=True)
    assert "51–75 of 120 albums" in html
    assert 'aria-current="page" data-pages="5">3<' in html and "page=5" in html and "per=25" in html
    assert "Refresh" in html
    html = c.get("/?tab=review&per=25&page=99").get_data(as_text=True)   # out of range → last page
    assert "101–120 of 120" in html
    for url in ("/history", "/log", "/log?page=2"):
        assert c.get(url).status_code == 200


def test_song_lookup_rejects_lookalikes():
    from app import matcher
    def f(title, dur):
        return {"fields": {"title": title}, "path": f"/x/{title}.mp3", "duration": dur}
    wrong1 = {"title": "En Manam Ponnambalam", "album": "Ayyappa Devotional Songs Malayalam, Vol. 2",
              "duration": 326, "genre": "Malayalam"}
    wrong2 = {"title": "TeenMaar Jinthaak", "album": "TeenMaar Jinthaak", "duration": 150, "genre": "Telugu"}
    right = {"title": "Enn Manam", "album": "Paasa Paravaigal", "duration": 341, "genre": "Tamil"}
    other_album_same_song = {"title": "Muthil Mugam", "album": "Ilaiyaraaja Hits", "duration": 200, "genre": "Tamil"}
    assert not matcher._track_acceptable(f("Enn Manam", 342), wrong1, "Paasa Paravaigal", "Tamil")
    assert not matcher._track_acceptable(f("Jintha Jintha", 157), wrong2, "Paasa Paravaigal", "Tamil")
    assert matcher._track_acceptable(f("Enn Manam", 342), right, "Paasa Paravaigal", "Tamil")
    assert matcher._track_acceptable(f("Muthil Mugam", 201), other_album_same_song, "Paasa Paravaigal", "Tamil")
    assert matcher.clean_folder("Ilayaraja (2800 Tamil Mp3 Songs - from 585 Movies)") == "Ilayaraja"


def test_m4a_flags_and_bad_files(env):
    """1.2.2: M4A files with single-value flags (compilation, gapless) used to crash apply."""
    music, data, album = env
    from mutagen.mp4 import MP4
    from app import applier, db, tags, worker
    m4a = album / "porkanda singam.m4a"
    a = MP4(str(m4a)); a["cpil"] = True; a["pgap"] = True; a["\xa9nam"] = ["www.TamilBlasters.net"]; a.save()
    before = dict(MP4(str(m4a)).tags)
    (album / "broken.mp3").write_bytes(b"not really an mp3")
    aid = worker.queue_album(str(album))
    run_jobs()
    al = db.album_get(aid)
    assert al["status"] == "review", al["error"]
    assert any("broken.mp3" in n for n in al["proposal"]["notes"])
    db.album_update(aid, status="applying")
    db.job_add("apply", aid, {"rename": True, "artwork": "missing", "lyrics": "missing"})
    run_jobs()
    al = db.album_get(aid)
    assert al["status"] == "applied" and not al["error"], al["error"]
    new = MP4(str(album / "03 - Porkanda Singam.m4a"))
    assert new.tags["\xa9nam"] == ["Porkanda Singam"] and new.tags["cpil"] is True
    applier.undo(aid)
    assert dict(MP4(str(m4a)).tags) == before


def test_sorting_everywhere(env):
    music, data, album = env
    from app import db, create_app
    rows = [("Roja", "A.R. Rahman", 9.1), ("anniyan", "Harris Jayaraj", 6.2),
            ("Mouna Ragam", "Ilaiyaraaja", None), ("Baashha", "Deva", 7.7)]
    for i, (t, aa, c) in enumerate(rows):
        aid = db.album_upsert(f"/m/{t}", "review")
        db.album_update(aid, title=t, album_artist=aa, confidence=c)
    db.log("zeta message", "warn"); db.log("alpha message", "error")
    c = create_app(start_background=False).test_client()

    def order(url):
        html = c.get(url).get_data(as_text=True)
        return [t for t, _, _ in sorted(rows, key=lambda r: html.find(f">{r[0]}<"))]

    assert order("/?tab=review&sort=album&dir=asc") == ["anniyan", "Baashha", "Mouna Ragam", "Roja"]   # case-insensitive
    assert order("/?tab=review&sort=album&dir=desc") == ["Roja", "Mouna Ragam", "Baashha", "anniyan"]
    assert order("/?tab=review&sort=confidence&dir=desc") == ["Roja", "Baashha", "anniyan", "Mouna Ragam"]  # blank last
    assert order("/?tab=review&sort=confidence&dir=asc") == ["anniyan", "Baashha", "Roja", "Mouna Ragam"]   # blank last
    assert order("/?tab=review&sort=artist&dir=asc")[0] == "Roja"
    html = c.get("/?tab=review&sort=confidence&dir=desc").get_data(as_text=True)
    assert 'selected>Match · high → low' in html and "sort=confidence" in html and "dir=asc" in html
    html = c.get("/library?sort=album&dir=asc").get_data(as_text=True)
    assert 'aria-sort="ascending"' in html
    # untrusted sort values fall back to the default instead of reaching SQL
    assert c.get("/?sort=title;DROP TABLE albums&dir=sideways").status_code == 200
    assert db.albums_count() >= 4
    html = c.get("/log?sort=message&dir=asc").get_data(as_text=True)
    assert html.find("alpha message") < html.find("zeta message")
    assert c.get("/history?sort=files&dir=asc").status_code == 200
    from app import worker
    aid = worker.queue_album(str(album))
    run_jobs()
    html = c.get(f"/album/{aid}").get_data(as_text=True)
    assert 'class="tracks csort cards"' in html and 'data-type="num">Time' in html and 'data-sort="20.' in html
    src_page = c.get("/settings?tab=sources").get_data(as_text=True)
    assert 'id="row-src-itunes"' in src_page
    import re as _re                                                  # each source shows its own position
    orders = _re.findall(r'name="order-\w+".*?<option value="(\d)" selected', src_page, _re.S)
    assert orders == ["1", "2", "3", "4", "5"], orders
    assert 'id="row-AUTO_APPROVE_MIN"' in c.get("/settings?tab=auto").get_data(as_text=True)
    assert 'data-preview="pv-main"' in c.get("/settings?tab=renaming").get_data(as_text=True)


def test_mobile_shell_theme_and_confirm(env):
    music, data, album = env
    from app import db, worker, create_app
    worker.queue_album(str(album)); run_jobs()
    c = create_app(start_background=False).test_client()
    html = c.get("/").get_data(as_text=True)
    assert 'class="side"' in html and 'viewport-fit=cover' in html and 'manifest.webmanifest' in html
    assert "data-theme" not in html.split("<head>")[0]                    # one dark theme in 2.0
    assert c.get("/theme/light").status_code == 302                         # old links still work
    for url in ("/library", "/scan", "/history", "/log", "/settings"):
        assert c.get(url).status_code == 200, url
    # no native confirm() anywhere (blocked in some app web views); forms use data-confirm instead
    for url in ("/", "/album/1", "/history", "/settings", "/scan"):
        page = c.get(url).get_data(as_text=True)
        assert "confirm(" not in page.replace("In-page confirmation", ""), url
    assert 'data-confirm=' in c.get("/review").get_data(as_text=True)
    page = c.get("/album/1").get_data(as_text=True)
    assert 'class="page-album"' in page and 'data-label="Singers"' in page and "actionbar" in page
    assert c.get("/static/manifest.webmanifest").status_code == 200


def test_discogs_parser():
    from app.sources import discogs as D
    alb, tr = D.parse_release(DISCOGS_LP)
    assert alb["album"] == "Punnagai Mannan" and alb["album_artist"] == "Ilaiyaraaja"
    assert alb["ref"] == "discogs:4455" and alb["date"] == "1986" and alb["genre"] == "Tamil"
    assert alb["artwork"] == "https://i.discogs.com/full.jpg" and alb["track_count"] == 4   # heading skipped
    assert [t["title"] for t in tr] == ["Ennapadi Ennapadi", "Kavithai Kelungal", "Vaan Megam", "Theme Music"]
    assert [t["tracknumber"] for t in tr] == [1, 2, 3, 4]                # A/B sides run on
    assert tr[0]["artist"] == "K. J. Yesudas, K.S. Chithra"              # credited name; '(2)' dropped; no chorus
    assert tr[1]["artist"] == "Vani Jairam, P. Jayachandran"
    assert tr[2]["artist"] == "K.S. Chithra" and tr[2]["duration"] == 0
    assert tr[3]["composer"] == "Gangai Amaran" and tr[0]["composer"] == "Ilaiyaraaja"
    alb, tr = D.parse_release(DISCOGS_COMP)
    assert alb["album_artist"] == "Ilaiyaraaja"                          # every song composed by him
    assert [(t["discnumber"], t["tracknumber"]) for t in tr] == [(1, 1), (1, 2), (2, 1)]
    assert tr[0]["disctotal"] == 2 and tr[0]["tracktotal"] == 2
    assert tr[1]["artist"] == "Kamal Haasan & S. Janaki"
    assert D.parse_ref("https://www.discogs.com/release/4455-Ilaiyaraaja-Punnagai-Mannan") == "4455"
    assert D.parse_ref("https://www.discogs.com/master/99-x") == "master-99"


def test_discogs_after_wikipedia(env, monkeypatch):
    music, data, album = env
    global NO_WIKI
    from app import db, matcher, worker
    from app.config import Config
    folder = music / "Ilaiyaraaja" / "Punnagai Mannan"
    make_audio(str(folder / "Ennapadi.mp3"), 271)
    make_audio(str(folder / "Kavithai Kelungal.mp3"), 305)
    make_audio(str(folder / "Vaan Megam.flac"), 280)
    NO_WIKI = True
    try:
        # without a token Discogs isn't searched, and the album page says why
        monkeypatch.setattr(Config, "DISCOGS_TOKEN", "")
        aid = worker.queue_album(str(folder)); run_jobs()
        p = db.album_get(aid)["proposal"]
        assert p["album"]["source"] != "discogs"
        assert any("DISCOGS_TOKEN" in n for n in p["notes"])
        # with a token it is, after iTunes, JioSaavn and Wikipedia came up empty
        monkeypatch.setattr(Config, "DISCOGS_TOKEN", "tok")
        worker.queue_album(str(folder)); run_jobs()
        a = db.album_get(aid)
        p = a["proposal"]
        assert p["album"]["source"] == "discogs", (p["album"], p["notes"])
        assert HEADERS_SEEN["discogs"]["Authorization"] == "Discogs token=tok"
        assert "Isaiarr/" in HEADERS_SEEN["discogs"]["User-Agent"]
        by = {f["file"]: f for f in p["files"]}
        k = by["Kavithai Kelungal.mp3"]["new"]
        assert k["artist"] == "Vani Jairam, P. Jayachandran" and k["albumartist"] == "Ilaiyaraaja"
        assert k["tracknumber"] == "2" and k["date"] == "1986"
        assert matcher.parse_ref("https://www.discogs.com/release/7788-Hits") == "discogs:7788"
        from app import create_app
        html = create_app(start_background=False).test_client().get(f"/album/{aid}").get_data(as_text=True)
        assert "Discogs" in html
    finally:
        NO_WIKI = False


def test_collection_mode(env):
    music, data, album = env
    from app import applier, db, tags, worker
    folder = music / "Ilaiyaraaja" / "Ilaiyaraaja Hits (www.masstamilan.com)"
    for name, dur in [("Ilamai Itho.mp3", 261), ("Raja Raja.mp3", 300), ("Kanmani.flac", 362),
                      ("Poongatru.m4a", 281)]:
        make_audio(str(folder / name), dur)
    aid = worker.queue_album(str(folder)); run_jobs()
    a = db.album_get(aid)
    p = a["proposal"]
    assert p.get("collection") is True, p["notes"]
    assert a["status"] == "review" and a["confidence"] >= 8, (a["status"], a["confidence"])
    assert p["album"]["album"] == "Ilaiyaraaja Hits"                     # folder name, junk removed
    assert p["album"]["album_artist"] == "Ilaiyaraaja"                   # all four by the same composer
    by = {f["file"]: f["new"] for f in p["files"]}
    assert by["Kanmani.flac"]["grouping"] == "Guna" and by["Kanmani.flac"]["date"] == "1991"
    assert by["Raja Raja.mp3"]["grouping"] == "Rettai Vaal Kuruvi" and by["Raja Raja.mp3"]["date"] == "1987"
    assert all(n["compilation"] == "1" and n["album"] == "Ilaiyaraaja Hits" for n in by.values())
    from app import create_app
    html = create_app(start_background=False).test_client().get(f"/album/{aid}").get_data(as_text=True)
    assert "Collection" in html and "4 different films" in html
    db.album_update(aid, status="applying")
    db.job_add("apply", aid, {"rename": False, "artwork": "none", "lyrics": "none"}); run_jobs()
    assert db.album_get(aid)["status"] == "applied", db.album_get(aid)["error"]
    f = tags.read(str(folder / "Poongatru.m4a"))["fields"]
    assert f["compilation"] == "1" and f["grouping"] == "Moondram Pirai" and f["album"] == "Ilaiyaraaja Hits"
    applier.undo(aid)
    f = tags.read(str(folder / "Poongatru.m4a"))["fields"]
    assert f["compilation"] == "" and f["grouping"] == ""


def test_wrong_album_with_no_songs_is_not_used(env):
    """A source always returns *some* album; if it holds none of the folder's songs it must not win."""
    music, data, album = env
    from app import db, worker
    folder = music / "Mixed" / "Random Songs"
    make_audio(str(folder / "Kanmani.mp3"), 362)
    make_audio(str(folder / "Something Unknown.mp3"), 200)
    aid = worker.queue_album(str(folder)); run_jobs()
    p = db.album_get(aid)["proposal"]
    assert p["fallback"] and any("songs were matched one by one" in n for n in p["notes"])
    assert {f["file"]: f["new"].get("title") for f in p["files"]}["Kanmani.mp3"] == "Kanmani Anbodu"


def _client():
    from app import create_app
    return create_app(start_background=False).test_client()


def test_settings_precedence_secrets_and_validation(env, monkeypatch):
    music, data, album = env
    import json
    from app.config import Config
    from app.sources import itunes
    monkeypatch.setenv("ITUNES_COUNTRY", "sg")
    monkeypatch.setenv("DISCOGS_TOKEN", "compose-secret-123")
    Config.reload()
    c = _client()
    page = c.get("/settings?tab=sources").get_data(as_text=True)
    assert "compose-secret-123" not in page                       # secrets are never echoed back
    assert 'value="sg"' in page and 'title="From docker-compose.yml"' in page
    # save a section: country + a rate limit; app value overrides compose, applies live
    r = c.post("/settings", data={"section": "sources", "ITUNES_COUNTRY": "in", "ITUNES_RPM": "30",
                                   "src-itunes": "on", "order-itunes": "2", "src-jiosaavn": "on", "order-jiosaavn": "1",
                                   "order-wikipedia": "3", "order-discogs": "4"})
    assert r.status_code == 302
    assert Config.ITUNES_COUNTRY == "in" and Config.ITUNES_RPM == 30 and itunes._limiter.interval == 2.0
    assert Config.SOURCES == ["jiosaavn", "itunes"]                  # reordered; wikipedia/discogs off
    saved = json.load(open(data / "settings.json"))
    assert saved["ITUNES_COUNTRY"] == "in" and "DISCOGS_TOKEN" not in saved   # untouched secret stays from compose
    assert Config.DISCOGS_TOKEN == "compose-secret-123"
    # reset one field -> back to compose, and back to the page it's on
    r = c.post("/settings", data={"reset": "ITUNES_COUNTRY"})
    assert Config.ITUNES_COUNTRY == "sg" and "tab=sources" in r.headers["Location"] and "#row-ITUNES_COUNTRY" in r.headers["Location"]
    # a Settings page only saves the settings it shows: switches on other pages are left alone
    before = Config.NOTIFY_REVIEW
    r = c.post("/settings?tab=plex", data={"section": "page", "page_title": "Plex", "next": "/settings?tab=plex",
                                           "k": ["PLEX_RESCAN", "PLEX_URL"], "PLEX_URL": "http://plex:32400"})
    assert r.headers["Location"].endswith("/settings?tab=plex") and Config.PLEX_RESCAN is False
    assert Config.NOTIFY_REVIEW == before                          # Discord switch (same old section) untouched
    page = c.get("/settings?tab=plex").get_data(as_text=True)
    assert "Plex: saved" in page and 'aria-current="page" data-tab="plex"' in page and 'id="savebar"' in page
    for t in ("tagging", "renaming", "sources", "lyrics", "matching", "auto", "plex", "discord", "ai", "security", "appearance", "system"):
        assert c.get(f"/settings?tab={t}").status_code == 200, t
    idx = c.get("/settings").get_data(as_text=True)
    assert '"k": "GEMINI_API_KEY"' in idx or '"k":"GEMINI_API_KEY"' in idx  # search covers every page
    # invalid values are refused with a message, valid ones in the same form still save
    c.post("/settings", data={"section": "renaming", "RENAME_ENABLED": "on", "RENAME_PATTERN": "{track:02d} - {nope}",
                              "RENAME_PATTERN_MULTIDISC": "{disc}.{track:02d} {title}"})
    page = c.get("/settings?tab=renaming").get_data(as_text=True)
    assert "invalid pattern" in page
    assert Config.RENAME_PATTERN == "{track:02d} - {title}" and Config.RENAME_PATTERN_MULTIDISC == "{disc}.{track:02d} {title}"
    c.post("/settings", data={"section": "matching", "FALLBACK_BELOW": "12", "FALLBACK_COVERAGE": "0.8",
                              "MIN_ALBUM_COVERAGE": "0.3", "LOW_CONFIDENCE": "7", "COLLECTION_MIN_FILMS": "3"})
    assert Config.FALLBACK_BELOW == 8.0 and Config.FALLBACK_COVERAGE == 0.8
    c.post("/settings", data={"section": "sources"})                # every source off -> refused
    assert Config.SOURCES == ["jiosaavn", "itunes"]
    # clearing a secret overrides compose with "not set"
    c.post("/settings", data={"section": "sources", "clear-DISCOGS_TOKEN": "on", "src-itunes": "on", "order-itunes": "1"})
    assert Config.DISCOGS_TOKEN == ""
    # protected fields + a restart-required change
    c.post("/settings", data={"section": "tagging", "ARTIST_MODE": "md_first", "VARIOUS_ARTISTS": "Various",
                              "PROTECTED_FIELDS": ["genre", "date", "bogus"], "ARTWORK_SIZE": "1000", "MP3_ID3_VERSION": "3"})
    assert Config.PROTECTED_FIELDS == ["genre", "date"] and Config.ARTWORK_SIZE == 1000 and Config.MP3_ID3_VERSION == 3
    r = c.post("/settings", data={"section": "watcher", "WATCH_ENABLED": "on", "WATCH_MODE": "inotify",
                                  "POLL_INTERVAL": "60", "SETTLE_SECONDS": "120"}, follow_redirects=True)
    assert "Restart now" in r.get_data(as_text=True)
    assert oct(os.stat(data / "settings.json").st_mode)[-3:] == "600"


def test_auto_approve(env, monkeypatch):
    music, data, album = env
    from app import db, notify, watcher, worker
    from app.config import Config
    import requests
    c = _client()
    sent = []
    c.post("/settings", data={"section": "integrations", "DISCORD_WEBHOOK": "https://discord.test/x",
                              "NOTIFY_APPLIED": "on", "NOTIFY_REVIEW": "on", "PLEX_RESCAN": "on",
                              "DISCORD_BATCH_MINUTES": "0", "PUBLIC_URL": "http://nas:8687"})
    monkeypatch.setattr(requests, "post", lambda url, json=None, timeout=None: (sent.append(json), Resp({}))[1])
    # off by default: watcher-found album waits for review
    aid = worker.queue_album(str(album), origin="watch"); run_jobs()
    a = db.album_get(aid)
    assert a["status"] == "review" and a["proposal"]["auto"]["why"] == "Auto-approve is off"
    c.post("/settings", data={"section": "auto", "AUTO_APPROVE": "on", "AUTO_APPROVE_MIN": "7",
                              "AUTO_APPROVE_TRACK_MIN": "7", "AUTO_APPROVE_ALL_MATCHED": "on", "AUTO_APPROVE_SCOPE": "new",
                              "DEFAULT_ARTWORK": "missing", "DEFAULT_LYRICS": "missing"})
    assert Config.AUTO_APPROVE == "on"
    worker.queue_album(str(album), origin="watch"); run_jobs()
    a = db.album_get(aid)
    assert a["status"] == "applied" and a["auto"] == 1, (a["status"], a["proposal"]["auto"])
    assert "02 - Vikram Title Track.mp3" in os.listdir(album)
    html = c.get(f"/album/{aid}").get_data(as_text=True)
    assert "Auto-approved" in html
    assert notify.flush(force=True) == 1
    assert sent[-1]["embeds"][0]["title"] == "1 album(s) auto-approved and tagged"
    # manual scans are not auto-approved with scope "new"; album-page re-analysis never is
    folder = music / "Ilaiyaraaja" / "Salangai Oli"
    for name, d in [("Naatham.mp3", 20), ("Om Namasivaya.flac", 22), ("Vedham.mp3", 24), ("Mounamana Neram.mp3", 26)]:
        make_audio(str(folder / name), d)
    sid = worker.queue_album(str(folder), origin="manual"); run_jobs()
    s_ = db.album_get(sid)
    assert s_["status"] == "review" and "Manual scans" in s_["proposal"]["auto"]["why"]
    assert "Not auto-approved: Manual scans" in c.get(f"/album/{sid}").get_data(as_text=True)
    c.post(f"/album/{sid}/reanalyze"); run_jobs()
    assert db.album_get(sid)["proposal"]["auto"]["why"].startswith("Analyzed on request")
    # a weak song blocks the album
    c.post("/settings", data={"section": "auto", "AUTO_APPROVE": "on", "AUTO_APPROVE_MIN": "5",
                              "AUTO_APPROVE_TRACK_MIN": "9.9", "AUTO_APPROVE_SCOPE": "all",
                              "DEFAULT_ARTWORK": "missing", "DEFAULT_LYRICS": "missing"})
    worker.queue_album(str(folder), origin="manual"); run_jobs()
    assert "below 9.9" in db.album_get(sid)["proposal"]["auto"]["why"]


def test_login(env, monkeypatch):
    music, data, album = env
    import base64, json
    c = _client()
    assert c.get("/").status_code == 200
    c.post("/settings", data={"section": "security", "AUTH_USERNAME": "gera", "AUTH_PASSWORD": "s3cret!"})
    assert "s3cret!" not in open(data / "settings.json").read()      # only a hash is stored
    assert c.get("/").status_code == 401 and "Basic" in c.get("/").headers["WWW-Authenticate"]
    ok = {"Authorization": "Basic " + base64.b64encode(b"gera:s3cret!").decode()}
    bad = {"Authorization": "Basic " + base64.b64encode(b"gera:nope").decode()}
    assert c.get("/", headers=ok).status_code == 200 and c.get("/", headers=bad).status_code == 401
    assert c.get("/healthz").status_code == 200 and c.get("/static/style.css").status_code == 200
    monkeypatch.setenv("AUTH_DISABLE", "true")                        # forgotten-password escape hatch
    assert c.get("/").status_code == 200


def test_v2_screens_scan_quick_approve_uptodate_and_fill_mode(env):
    music, data, album = env
    from app import db, worker, create_app
    from app.config import Config
    c = create_app(start_background=False).test_client()
    # manual scan is tracked and reported on the Scan page and /api/scan
    r = c.post("/scan", data={"path": "", "mode": "changed", "recursive": "on"})
    assert r.status_code == 302
    run_jobs()
    s = c.get("/api/scan").get_json()
    assert s["scan"]["total"] >= 1 and s["scan"]["done"] == s["scan"]["total"] and not s["scan"]["running"]
    assert any(st in ("MATCH", "FIX", "FILL") for st, _ in s["log"])
    page = c.get("/scan").get_data(as_text=True)
    assert 'id="runpanel"' in page and "Finished" in page
    aid = db.albums_list(["review"])[0]["id"]
    # queue shows the album detail with a quick approve button
    q = c.get(f"/?a={aid}").get_data(as_text=True)
    assert f"/album/{aid}/approve" in q and "% match" in q
    # fill-only mode never proposes replacing a value that is already set
    Config.OVERWRITE_MODE = "fill"
    try:
        worker.queue_album(album_path := db.album_get(aid)["path"], priority=db.PRIORITY_USER); run_jobs()
        prop = db.album_get(aid)["proposal"]
        assert all(k != "fix" for f in prop["files"] for k in f["changes"].values())
    finally:
        Config.OVERWRITE_MODE = "fix"
    worker.queue_album(album_path, priority=db.PRIORITY_USER); run_jobs()
    # one-click approve writes with the default options
    assert c.post(f"/album/{aid}/approve").status_code == 302
    run_jobs()
    assert db.album_get(aid)["status"] == "applied"
    # analysing a tagged album again finds nothing to change -> up to date, not back in the queue
    worker.queue_album(album_path, priority=db.PRIORITY_USER); run_jobs()
    assert db.album_get(aid)["status"] == "uptodate"
    assert f"?a={aid}" not in c.get("/").get_data(as_text=True)
    assert "Up to date" in c.get("/library?tab=uptodate").get_data(as_text=True)


def test_album_artist_edit_is_kept(env):
    """An edited music director is written, shown afterwards, and not 'fixed' back by a re-check."""
    music, data, album = env
    from app import db, worker, tags, create_app
    from app.config import Config
    aid = worker.queue_album(str(album)); run_jobs()
    p = db.album_get(aid)["proposal"]
    c = create_app(start_background=False).test_client()
    form = {"a-album": p["album"]["album"], "a-albumartist": "Ilaiyaraaja", "a-date": "2022", "a-genre": "Tamil",
            "rename": "on", "artwork": "missing", "lyrics": "missing"}
    for i, f in enumerate(p["files"]):
        form[f"f-{i}-include"] = "on"
        for k in ("title", "artist", "tracknumber", "discnumber"):
            form[f"f-{i}-{k}"] = f["new"].get(k) or f["current"][k]
    Config.ARTIST_MODE = "md_first"
    try:
        c.post(f"/album/{aid}/apply", data=form); run_jobs()
        a = db.album_get(aid)
        assert a["status"] == "applied" and a["album_artist"] == "Ilaiyaraaja"
        for n in os.listdir(album):
            if n.endswith(".lrc"):
                continue
            f = tags.read(str(album / n))["fields"]
            assert f["albumartist"] == "Ilaiyaraaja" and f["composer"] == "Ilaiyaraaja", n
            assert f["artist"].startswith("Ilaiyaraaja"), n          # music director first in Artist
        assert 'name="a-albumartist" value="Ilaiyaraaja"' in c.get(f"/album/{aid}").get_data(as_text=True)
        worker.queue_album(str(album), priority=db.PRIORITY_USER); run_jobs()
        a = db.album_get(aid)
        assert a["status"] == "uptodate", [f["changes"] for f in a["proposal"]["files"]]
        assert a["proposal"]["album"]["album_artist"] == "Ilaiyaraaja"
    finally:
        Config.ARTIST_MODE = "singers"


def test_album_artist_aliases_from_other_taggers(tmp_path):
    """Files tagged elsewhere may hold the album artist as 'ALBUM ARTIST', TXXX or a free-form atom.
    We read those, and writing the album artist removes them so players can't show the old value."""
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3, TXXX
    from mutagen.mp4 import MP4, MP4FreeForm
    from app import tags
    fl, mp, m4 = str(tmp_path / "a.flac"), str(tmp_path / "b.mp3"), str(tmp_path / "c.m4a")
    for p in (fl, mp, m4):
        make_audio(p, 2)
    a = FLAC(fl); a["ALBUM ARTIST"] = ["Old Director"]; a["albumartist"] = ["Old Director"]; a.save()
    t = ID3(mp); t.add(TXXX(encoding=3, desc="ALBUM ARTIST", text=["Old Director"])); t.save()
    m = MP4(m4); m["----:com.apple.iTunes:ALBUM ARTIST"] = [MP4FreeForm(b"Old Director")]; m.save()
    for p in (fl, mp, m4):
        assert tags.read(p)["fields"]["albumartist"] == "Old Director", p
        tags.write(p, {"albumartist": "Ilaiyaraaja"})
        assert tags.read(p)["fields"]["albumartist"] == "Ilaiyaraaja", p
    pairs = [(k.upper(), v) for k, v in FLAC(fl).tags]
    assert [v for k, v in pairs if k in ("ALBUMARTIST", "ALBUM ARTIST")] == ["Ilaiyaraaja"]
    assert not ID3(mp).getall("TXXX:ALBUM ARTIST") and str(ID3(mp)["TPE2"]) == "Ilaiyaraaja"
    assert "----:com.apple.iTunes:ALBUM ARTIST" not in MP4(m4).tags


def test_batch_set_album_artist(env):
    """Library → Set album artist: writes only Album Artist to every file, keeps it on re-check, undoable."""
    music, data, album = env
    from app import db, worker, tags, create_app
    aid = worker.queue_album(str(album)); run_jobs()
    before = {n: tags.read(str(album / n))["fields"] for n in os.listdir(album) if not n.endswith(".lrc")}
    c = create_app(start_background=False).test_client()
    page = c.get("/library").get_data(as_text=True)
    assert 'value="policy"' in page and 'id="aa-names"' in page and "Set kind of album" in page
    assert c.post("/bulk", data={"action": "setartist", "ids": [str(aid)], "new_artist": ""}).status_code == 302
    assert not db.q("SELECT 1 FROM jobs WHERE kind='setartist'")           # empty name refused
    c.post("/bulk", data={"action": "setartist", "all_matching": "on", "tab": "all", "q": "Vikram",
                          "new_artist": "Ilaiyaraaja"})
    run_jobs()
    for n, old in before.items():
        f = tags.read(str(album / n))["fields"]
        assert f["albumartist"] == "Ilaiyaraaja", n
        assert {k: v for k, v in f.items() if k != "albumartist"} == \
               {k: v for k, v in old.items() if k != "albumartist"}, n     # nothing else touched
    assert sorted(before) == sorted(n for n in os.listdir(album) if not n.endswith(".lrc"))   # no renames
    a = db.album_get(aid)
    assert a["album_artist"] == "Ilaiyaraaja" and a["proposal"]["album"]["album_artist"] == "Ilaiyaraaja"
    assert all(fp["new"].get("albumartist") in (None, "Ilaiyaraaja") for fp in a["proposal"]["files"])
    # undo puts the old values back
    worker.applier.undo(aid)
    for n, old in before.items():
        assert tags.read(str(album / n))["fields"]["albumartist"] == old["albumartist"], n



# ------------------------------------------------------------------ 2.2 playlists

def _library(music):
    """A small tagged library: 4 films, 3 music directors, shared singers."""
    from app import tags
    spec = [
        ("Ilaiyaraaja", "Mouna Ragam", 1986, [("Mandram Vandha", "S. P. Balasubrahmanyam"),
                                              ("Nilaave Vaa", "S. P. Balasubrahmanyam"),
                                              ("Chinna Chinna Vanna Kuyil", "S. Janaki")]),
        ("Ilaiyaraaja", "Punnagai Mannan", 1986, [("Enna Satham Indha Neram", "S. P. Balasubrahmanyam"),
                                                  ("Kavithai Kelungal", "Vani Jairam; P. Jayachandran")]),
        ("A.R. Rahman", "Roja", 1992, [("Kadhal Rojave", "S. P. Balasubrahmanyam; Sujatha"),
                                       ("Chinna Chinna Aasai", "Minmini"), ("Roja Karaoke", "Instrumental")]),
        ("Anirudh Ravichander", "3", 2012, [("Why This Kolaveri Di", "Dhanush"), ("Idhazhin Oram", "Ajesh")]),
    ]
    paths = []
    for md, film, year, tracks in spec:
        for i, (t, sing) in enumerate(tracks, 1):
            p = str(music / md / film / f"{i:02d} - {t}.mp3")
            make_audio(p, 2)
            tags.write(p, {"title": t, "artist": sing, "albumartist": md, "album": film, "date": str(year),
                           "genre": "Tamil", "tracknumber": str(i)})
            paths.append(p)
    return paths


def test_song_index_radio_and_filters(env):
    music, data, album = env
    from app import songs, playlists as PL, db
    _library(music)
    songs.index_all()
    st = songs.status()
    assert st["songs"] == 13 and not st["running"]           # 10 tagged + 3 untagged Vikram files
    r = db.q("SELECT * FROM songs WHERE title='Nilaave Vaa'", one=True)
    assert r["albumartist"] == "Ilaiyaraaja" and r["film"] == "Mouna Ragam" and r["year"] == 1986
    # unchanged files are skipped on the next pass, deleted ones disappear
    assert songs.index_folder(r["folder"]) == 0
    assert PL.count({"directors": ["ilaiyaraaja"]}) == 5
    assert PL.count({"singers": ["Balasubrahmanyam"], "decades": [1980]}) == 3
    assert all("Karaoke" not in x["title"] for x in PL.candidates({}))    # junk skipped by default
    assert any("Karaoke" in x["title"] for x in PL.candidates({"skip_junk": False}))
    seed = songs.get([r["id"]])[0]
    items, info = PL.radio(seed, {}, n=4)
    ids = [i["id"] for i in items]
    assert seed["id"] not in ids and len(ids) >= 3
    first = songs.get(ids[:3])
    assert all(x["albumartist"] == "Ilaiyaraaja" or "Balasubrahmanyam" in x["artist"] for x in first)
    assert len([x for x in songs.get(ids) if x["film"] == "Mouna Ragam"]) <= 3    # per-film cap
    pid = PL.create("radio", "", {"size": 5, "seed": seed["id"]}, background=False)
    pl = PL.get(pid)
    assert pl["status"] == "ready" and pl["items"] and pl["title"] == "Nilaave Vaa radio"
    os.remove(r["path"])
    songs.index_folder(r["folder"])
    assert not db.q("SELECT 1 FROM songs WHERE title='Nilaave Vaa'")


class _AIResp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.text = payload, status, json.dumps(payload)

    def json(self):
        return self.payload


def _gemini(answer, tin=1000, tout=200):
    return _AIResp({"candidates": [{"content": {"parts": [{"text": json.dumps(answer)}]}}],
                    "usageMetadata": {"promptTokenCount": tin, "candidatesTokenCount": tout}})


def test_describe_playlist_with_ai(env, monkeypatch):
    music, data, album = env
    import requests
    from app import songs, playlists as PL, db, ai
    from app.config import Config
    _library(music)
    songs.index_all()
    Config.AI_PROVIDER, Config.GEMINI_API_KEY, Config.AI_RPM, Config.AI_FREE_TIER = "gemini", "k", 6000, False
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append((url, json, headers))
        user = json["contents"][0]["parts"][0]["text"]
        if "Request:" in user and "Songs (" not in user:      # step 1: understand the request
            assert "Ilaiyaraaja (5)" in user                 # the AI sees the library's own names
            return _gemini({"title": "Raja evenings", "directors": ["Ilayaraja"], "singers": ["S P Balasubrahmanyam"],
                            "decades": [1980], "moods": [], "kinds": []})
        lines = [l for l in user.splitlines() if l[:1].isdigit()]
        ids = [int(l.split("|")[0]) for l in lines]
        assert all("Ilaiyaraaja" in l for l in lines)       # only the filtered songs are sent
        assert not any("/" in l.split("|")[4] for l in lines)    # no file paths
        return _gemini({"title": "Rainy Raja", "description": "Soft 80s melodies",
                        "tracks": [{"id": ids[0], "why": "classic"}, {"id": 99999, "why": "made up"},
                                   {"id": ids[0], "why": "duplicate"}]})
    monkeypatch.setattr(requests, "post", post)
    pid = PL.create("describe", "rainy evening ilayaraja melodies", {"size": 4}, background=False)
    pl = PL.get(pid)
    assert pl["status"] == "ready", pl["error"]
    got = songs.get([i["id"] for i in pl["items"]])
    assert len(got) == 4 and len({g["id"] for g in got}) == 4           # invented and duplicate IDs dropped, topped up
    assert all(g["albumartist"] == "Ilaiyaraaja" for g in got)
    assert pl["title"] == "Rainy Raja" and pl["items"][0]["why"] == "classic"
    assert any("loosened" in n for n in pl["notes"])                    # SPB alone was too narrow
    assert pl["in_tokens"] == 2000 and pl["cost"] > 0
    assert calls[0][2]["x-goog-api-key"] == "k" and ai.month_spend()["calls"] == 2
    # errors are reported, not raised
    monkeypatch.setattr(requests, "post", lambda *a, **k: _AIResp({"error": {"message": "bad key"}}, 401))
    pid = PL.create("describe", "anything", {"size": 4}, background=False)
    assert PL.get(pid)["status"] == "error" and "API key" in PL.get(pid)["error"]
    # monthly limit stops paid calls
    Config.AI_MONTHLY_LIMIT = 0.000001
    pid = PL.create("describe", "anything", {"size": 4}, background=False)
    assert "limit" in PL.get(pid)["error"]
    Config.AI_MONTHLY_LIMIT, Config.AI_FREE_TIER = 1.0, True


def test_groq_and_song_profiles(env, monkeypatch):
    music, data, album = env
    import requests
    from app import songs, playlists as PL
    from app.config import Config
    _library(music)
    songs.index_all()
    Config.AI_PROVIDER, Config.GROQ_API_KEY, Config.AI_RPM = "groq", "g", 6000

    def post(url, json=None, headers=None, timeout=None):
        assert url == "https://api.groq.com/openai/v1/chat/completions" and headers["Authorization"] == "Bearer g"
        user = json["messages"][1]["content"]
        rows = [l.split("|") for l in user.splitlines() if l[:1].isdigit()]
        if user.startswith("Request: Songs like"):
            assert "Keep: same music director" in user and "romantic" in user     # moods are sent once known
            ans = {"title": "Like Kadhal Rojave", "tracks": [{"id": int(r[0]), "why": "same feel"} for r in rows[:2]]}
            return _AIResp({"choices": [{"message": {"content": __import__("json").dumps(ans)}}], "usage": {}})
        out = [{"id": int(r[0]), "moods": ["romantic", "made-up-mood"] if "Kadhal" in r[1] else ["happy"],
                "kind": ["melody"], "tempo": "slow"} for r in rows]
        return _AIResp({"choices": [{"message": {"content": __import__("json").dumps({"songs": out})}}],
                        "usage": {"prompt_tokens": 500, "completion_tokens": 300}})
    monkeypatch.setattr(requests, "post", post)
    assert PL.start_profiles(batch=5)
    for _ in range(100):
        if not PL.profile_status()["running"]:
            break
        time.sleep(0.05)
    assert PL.profile_status()["left"] == 0 and songs.status()["profiled"] == 13
    roman = PL.candidates({"moods": ["romantic"]})
    assert [r["title"] for r in roman] == ["Kadhal Rojave"]
    assert roman[0]["profile"]["moods"] == ["romantic"]                 # unknown labels dropped
    # "From a song" with the AI: only the qualities you tick are asked for
    pid = PL.create("song", "", {"size": 3, "seed": roman[0]["id"], "dims": ["director", "mood"]}, background=False)
    pl = PL.get(pid)
    assert pl["status"] == "ready", pl["error"]
    assert pl["title"] == "Like Kadhal Rojave" and roman[0]["id"] not in [i["id"] for i in pl["items"]]
    assert len(pl["items"]) == 3 and pl["items"][0]["why"] == "same feel"


def test_plex_sync_and_save_playlist(env, monkeypatch):
    music, data, album = env
    import requests
    from app import songs, plex, playlists as PL, db, create_app
    paths = _library(music)
    songs.index_all()
    root = "/volume2/Multimedia/Audio Songs"                              # how a Synology Plex package sees it
    tracks = "".join(f'<Track ratingKey="{100 + i}" title="t" userRating="{8 if i == 0 else 0}" viewCount="{i}">'
                     f'<Media><Part file="{root}{p[len(str(music)):]}"/></Media></Track>' for i, p in enumerate(paths))
    sent = []

    class R:
        def __init__(self, content):
            self.content, self.status_code = content.encode(), 200

        def raise_for_status(self):
            pass

    def req(method, url, params=None, timeout=None, headers=None):
        sent.append((method, url, dict(params or {})))
        if url.endswith("/identity"):
            return R('<MediaContainer machineIdentifier="MID"/>')
        if "/library/sections/7/all" in url:
            return R(f'<MediaContainer size="{len(paths)}" totalSize="{len(paths)}">{tracks}</MediaContainer>')
        if url.endswith("/playlists") and method == "GET":
            return R('<MediaContainer><Playlist ratingKey="55" title="Old" leafCount="2" smart="0"/></MediaContainer>')
        if url.endswith("/playlists") and method == "POST":
            return R('<MediaContainer><Playlist ratingKey="77" title="x"/></MediaContainer>')
        if url.endswith("/playlists/55/items") and method == "GET":
            return R('<MediaContainer><Track ratingKey="100"/></MediaContainer>')
        return R("")
    monkeypatch.setattr(requests, "request", req)
    monkeypatch.setattr(requests, "get", lambda url, params=None, timeout=None, headers=None:
                        fake_get(url, params) if url.endswith("/library/sections") else req("GET", url, params))
    m, t = plex.sync_tracks()
    assert (m, t) == (len(paths), len(paths)) and songs.status()["in_plex"] == len(paths)
    assert db.q("SELECT rating FROM songs WHERE plex_key='100'", one=True)["rating"] == 8
    pid = PL.create("radio", "", {"size": 4, "filters": {"in_plex": True}}, background=False)
    pl = PL.get(pid)
    keep = [i["id"] for i in pl["items"]][::-1]                         # reordered on the page
    c = create_app(start_background=False).test_client()
    page = c.get(f"/playlists/{pid}").get_data(as_text=True)
    assert 'name="keep"' in page and "Old (2)" in page
    r = c.post(f"/playlists/{pid}/save", data={"keep": [str(k) for k in keep[:3]], "title": "My mix", "save_mode": "new"})
    assert r.status_code == 302
    post = [s for s in sent if s[0] == "POST"][-1]
    keys = [db.q("SELECT plex_key FROM songs WHERE id=?", (k,), one=True)["plex_key"] for k in keep[:3]]
    assert post[2]["title"] == "My mix" and post[2]["uri"].endswith("/library/metadata/" + ",".join(keys))
    assert post[2]["uri"].startswith("server://MID/")
    assert PL.get(pid)["saved"] and PL.get(pid)["plex_id"] == "77"
    assert [i["id"] for i in PL.get(pid)["items"]] == keep[:3]
    # add to an existing playlist: songs already in it are skipped
    c.post(f"/playlists/{pid}/save", data={"keep": ["%d" % db.q("SELECT id FROM songs WHERE plex_key='100'", one=True)["id"]] +
                                           [str(keep[0])], "save_mode": "append", "target": "55"})
    put = [s for s in sent if s[0] == "PUT"][-1]
    assert put[1].endswith("/playlists/55/items") and "100" not in put[2]["uri"].split("/")[-1].split(",")


def test_playlist_pages_render(env):
    music, data, album = env
    from app import songs, create_app
    _library(music)
    songs.index_all()
    c = create_app(start_background=False).test_client()
    for tab in ("describe", "song", "radio"):
        page = c.get(f"/playlists?tab={tab}").get_data(as_text=True)
        assert "New playlist" in page and "Song library" in page, tab
    assert "AI not set up" in c.get("/playlists").get_data(as_text=True)
    assert c.get("/api/songs/search?q=roja").get_json()[0]["film"] == "Roja"
    assert c.get("/api/playlists/count?f_director=A.R.+Rahman").get_json()["count"] == 2
    s = c.get("/settings?tab=ai").get_data(as_text=True)
    assert 'data-tab="ai"' in s and 'name="GEMINI_API_KEY"' in s and 'data-provider="groq"' in s
    r = c.post("/playlists/new", data={"mode": "describe", "prompt": "x"})
    assert r.status_code == 302 and "/playlists" in r.headers["Location"]        # AI off -> back with a message
    r = c.post("/playlists/new", data={"mode": "radio", "size": "5"})
    assert "/playlists/" in r.headers["Location"]


def test_gemini_finds_a_working_model(env, monkeypatch):
    """Google limits older models to existing users; a new key gets 404 and Isaiarr picks the newest Flash."""
    import requests
    from app import ai, db
    from app.config import Config
    Config.AI_PROVIDER, Config.GEMINI_API_KEY, Config.AI_RPM, Config.AI_MODEL = "gemini", "k", 6000, ""
    db.x("DELETE FROM state WHERE key='ai_auto_model_gemini'")
    used = []

    def post(url, json=None, headers=None, timeout=None):
        used.append(url.split("/models/")[1].split(":")[0])
        if "gemini-3.8-flash" in url and len(used) == 1:
            return _AIResp({"error": {"message": "not found"}}, 404)
        return _gemini({"ok": True, "song": "Kadhal Rojave"})

    class L:
        status_code = 200

        def json(self):
            return {"models": [{"name": f"models/{n}", "supportedGenerationMethods": ["generateContent"]} for n in
                               ("gemini-2.5-flash", "gemini-3.6-flash", "gemini-3.9-flash", "gemini-3.9-flash-lite",
                                "gemini-4.0-flash-preview-11-2026", "gemini-3.9-flash-tts", "gemini-4-argon")]}
    monkeypatch.setattr(requests, "post", post)
    real_get = requests.get
    monkeypatch.setattr(requests, "get", lambda url, *a, **k: L() if url.endswith("/v1beta/models") else real_get(url, *a, **k))
    data, u = ai.test()
    assert data["ok"] and used == ["gemini-3.8-flash", "gemini-3.9-flash"] and ai.model() == "gemini-3.9-flash"
    # a model you typed yourself is never swapped; you get a clear message instead
    Config.AI_MODEL = "gemini-1.0-pro"
    monkeypatch.setattr(requests, "post", lambda *a, **k: _AIResp({"error": {"message": "nf"}}, 404))
    try:
        ai.test()
        assert False
    except ai.AIError as e:
        assert "Clear the Model field" in str(e)
    Config.AI_MODEL = ""


def test_gemini_thinking_level_and_slow_answers(env, monkeypatch):
    import requests
    from app import ai, db
    from app.config import Config
    Config.AI_PROVIDER, Config.GEMINI_API_KEY, Config.AI_RPM, Config.AI_MODEL = "gemini", "k", 6000, "gemini-3.8-flash"
    Config.AI_THINKING = "medium"
    seen = []

    def slow_then_ok(url, json=None, headers=None, timeout=None):
        seen.append(json["generationConfig"].get("thinkingConfig"))
        assert timeout == (15, Config.AI_TIMEOUT)
        if len(seen) == 1:
            raise requests.ReadTimeout("read timed out")
        return _gemini({"ok": True})
    monkeypatch.setattr(requests, "post", slow_then_ok)
    data, _ = ai.test()
    assert data["ok"] and seen == [{"thinkingLevel": "medium"}, {"thinkingLevel": "low"}]   # retried, thinking less
    # a model that doesn't take a thinking level: dropped once, remembered
    seen.clear()

    def no_thinking(url, json=None, headers=None, timeout=None):
        tc = json["generationConfig"].get("thinkingConfig")
        seen.append(tc)
        if tc:
            return _AIResp({"error": {"message": "Unknown name \"thinkingLevel\" at 'generation_config.thinking_config'"}}, 400)
        return _gemini({"ok": True})
    monkeypatch.setattr(requests, "post", no_thinking)
    ai.test(); ai.test()
    assert seen == [{"thinkingLevel": "medium"}, None, None] and db.state_get("ai_gemini_no_thinking_level")
    # twice too slow -> a clear message, not a stack of library errors
    db.x("DELETE FROM state WHERE key='ai_gemini_no_thinking_level'")
    monkeypatch.setattr(requests, "post", lambda *a, **k: (_ for _ in ()).throw(requests.ReadTimeout("x")))
    try:
        ai.test(); assert False
    except ai.AIError as e:
        assert "twice" in str(e) and "Thinking to Low" in str(e)
    Config.AI_MODEL, Config.AI_THINKING = "", "low"



def _noise(path, seed, seconds=40, tags=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"anoisesrc=d={seconds}:c=pink:seed={seed}",
           "-af", "lowpass=f=3000,volume=0.5", "-ac", "1", "-ar", "22050"]
    for k, v in (tags or {}).items():
        cmd += ["-metadata", f"{k}={v}"]
    if path.endswith(".mp3"):
        cmd += ["-c:a", "libmp3lame", "-b:a", "128k"]
    cmd.append(path)
    subprocess.run(cmd, check=True)


@pytest.mark.skipif(shutil.which("fpcalc") is None, reason="needs fpcalc")
def test_duplicates_find_resolve_restore(env, monkeypatch):
    music, data, album = env
    from app import songs, dupes, db, tags, create_app
    from app.config import Config
    a = str(music / "Ilaiyaraaja" / "Mouna Ragam" / "01 - Mandram Vandha.flac")
    _noise(a, 1, tags={"title": "Mandram Vandha", "artist": "S. P. Balasubrahmanyam", "album": "Mouna Ragam"})
    copy = str(music / "Hits (www.masstamilan.com)" / "Mandram Vandha.flac")       # same file, other tags
    os.makedirs(os.path.dirname(copy), exist_ok=True)
    shutil.copy(a, copy)
    tags.write(copy, {"title": "Mandram Vandha (From Mouna Ragam)", "album": "Raja Hits"})
    mp3 = str(music / "MP3s" / "Mandram.mp3")                                          # same recording, MP3
    _noise(mp3, 1)
    tags.write(mp3, {"title": "Track 7"})
    other = str(music / "Ilaiyaraaja" / "Mouna Ragam" / "02 - Nilaave Vaa.flac")      # a different song
    _noise(other, 2, tags={"title": "Nilaave Vaa", "artist": "S. P. Balasubrahmanyam"})
    lk1 = str(music / "A" / "Kadhal Rojave.mp3"); _noise(lk1, 3, 30, {"title": "Kadhal Rojave", "artist": "SPB"})
    lk2 = str(music / "B" / "Kadhal Rojave.mp3"); _noise(lk2, 4, 31, {"title": "Kadhal Rojave", "artist": "SPB"})
    songs.index_all()
    Config.DUPES_NOTIFY = False
    assert dupes.scan()
    st = dupes.status()
    groups = dupes.groups()
    paths = [sorted(s["path"] for s in g["songs"]) for g in groups]
    assert sorted([a, copy, mp3]) in paths                       # identical + same recording in one group
    assert sorted([lk1, lk2]) in paths                           # same title, singer, length: possible
    assert not any(other in p for p in paths)
    g = [g for g in groups if a in [s["path"] for s in g["songs"]]][0]
    assert g["kind"] == "recording"                              # weakest link names the group
    keep = [s for s in g["songs"] if s["id"] == g["keep_id"]][0]
    assert keep["path"].endswith(".flac") and "masstamilan" not in keep["path"]   # lossless, proper album folder
    assert [x for x in groups if lk1 in [s["path"] for s in x["songs"]]][0]["kind"] == "likely"
    assert st["groups"] == 2 and st["saving"] > 0
    # strict mode drops the "possible" ones
    Config.DUPES_LEVEL = "strict"
    dupes.rebuild()
    assert dupes.count() == 1
    Config.DUPES_LEVEL = "normal"
    dupes.rebuild()
    # the page lists them; resolve keeps one and moves the rest to the hidden bin
    c = create_app(start_background=False).test_client()
    page = c.get("/duplicates").get_data(as_text=True)
    assert "Best copy" in page and "Same song, another file" in page
    assert "FLAC 24-bit · 22.05 kHz" in page and "MP3 128 kbps" in page
    assert c.get("/settings?tab=duplicates").status_code == 200
    r = c.post(f"/duplicates/{g['id']}/resolve", data={"keep": str(keep["id"])})
    assert r.status_code == 302
    assert os.path.exists(a) and not os.path.exists(copy) and not os.path.exists(mp3)
    bin_root = music / ".isaiarr-bin"
    assert (bin_root / ".plexignore").exists()
    moved = dupes.bin_items()
    assert len(moved) == 2 and all(os.path.exists(m["bin_path"]) for m in moved)
    assert not db.q("SELECT 1 FROM songs WHERE path=?", (copy,))
    songs.index_all()                                            # the bin is never indexed as library music
    assert not db.q("SELECT 1 FROM songs WHERE path LIKE ?", ("%isaiarr-bin%",))
    # "not duplicates" hides a group for good, even after a re-check
    lg = [x for x in dupes.groups() if lk1 in [s["path"] for s in x["songs"]]][0]
    c.post(f"/duplicates/{lg['id']}/ignore")
    dupes.scan()
    assert dupes.count() == 0 and dupes.count(ignored=True) == 1
    # restore one, empty the rest
    mid = [m for m in moved if m["orig_path"] == copy][0]["id"]
    c.post(f"/duplicates/bin/{mid}/restore")
    assert os.path.exists(copy) and db.q("SELECT 1 FROM songs WHERE path=?", (copy,))
    assert "Empty bin" in c.get("/duplicates/bin").get_data(as_text=True)
    c.post("/duplicates/bin/empty")
    assert not dupes.bin_items() and not any(m["bin_path"] and os.path.exists(m["bin_path"]) for m in moved if m["id"] != mid)


def test_audio_hash_ignores_tags(tmp_path):
    from app import dupes, tags
    for ext in ("mp3", "m4a"):
        p1, p2 = str(tmp_path / f"a.{ext}"), str(tmp_path / f"b.{ext}")
        make_audio(p1, 3)
        shutil.copy(p1, p2)
        tags.write(p2, {"title": "Something else entirely", "album": "Other", "artist": "X"})
        assert dupes.audio_hash(p1, ext) == dupes.audio_hash(p2, ext), ext
        make_audio(p2, 4)
        assert dupes.audio_hash(p1, ext) != dupes.audio_hash(p2, ext), ext


def test_new_music_is_checked_for_duplicates(env):
    music, data, album = env
    from app import songs, dupes, worker
    from app.config import Config
    Config.DUPES_AUTO, Config.DUPES_NOTIFY = True, False
    first = str(music / "X" / "One" / "song.mp3")
    make_audio(first, 5, {"title": "Same Song", "artist": "Singer"})
    songs.index_all()
    dupes.scan()
    assert dupes.count() == 0
    second = music / "Y" / "Two"
    os.makedirs(second)
    shutil.copy(first, second / "song copy.mp3")             # new folder arrives with a copy
    worker._reindex(str(second))                             # what happens after a new album is analyzed/written
    for _ in range(200):
        if not dupes.status()["running"] and dupes.count():
            break
        time.sleep(0.05)
    assert dupes.count("exact") == 1


def test_take_one_file_out_of_a_duplicate_group(env):
    music, data, album = env
    from app import songs, dupes, db, tags, create_app
    from app.config import Config
    Config.DUPES_NOTIFY, Config.DUPES_FINGERPRINT = False, False
    src = str(music / "A" / "Song.mp3")
    make_audio(src, 5, {"title": "Same Song", "artist": "Singer"})
    copies = [src]
    for d in ("B", "C"):
        p = str(music / d / "Song.mp3")
        os.makedirs(os.path.dirname(p))
        shutil.copy(src, p)
        copies.append(p)
    songs.index_all()
    dupes.scan()
    g = dupes.groups()[0]
    assert len(g["songs"]) == 3
    c = create_app(start_background=False).test_client()
    page = c.get("/duplicates").get_data(as_text=True)
    assert 'class="xbtn"' in page and 'aria-label="Not a duplicate: remove' in page
    out = [s for s in g["songs"] if s["path"] == copies[2]][0]
    r = c.post(f"/duplicates/{g['id']}/exclude/{out['id']}")
    assert r.status_code == 302
    after = c.get("/duplicates").get_data(as_text=True)
    assert "taken out of the group" in after and "Undo" in after
    assert "The other 2 files are still a group, under Identical files" in after          # one-click undo right away
    g2 = dupes.groups()
    assert len(g2) == 1 and sorted(s["path"] for s in g2[0]["songs"]) == sorted(copies[:2])
    # remembered: a re-check, and even renaming + retagging the file, doesn't bring it back
    new_path = str(music / "C" / "Renamed.mp3")
    os.rename(copies[2], new_path)
    tags.write(new_path, {"title": "Other title"})
    songs.index_all()
    dupes.scan()
    assert [len(x["songs"]) for x in dupes.groups()] == [2]
    page = c.get("/duplicates?tab=ignored").get_data(as_text=True)
    assert "Files taken out of groups" in page
    eid = dupes.exclusions()[0]["id"]
    c.post(f"/duplicates/exclusions/{eid}/delete")                      # put back -> grouped again
    assert [len(x["songs"]) for x in dupes.groups()] == [3]
    # taking one out of a pair leaves no group at all
    for s in dupes.groups()[0]["songs"][:1]:
        dupes.exclude(dupes.groups()[0]["id"], s["id"])
    assert [len(x["songs"]) for x in dupes.groups()] == [2]
    gg = dupes.groups()[0]
    dupes.exclude(gg["id"], gg["songs"][0]["id"])
    assert dupes.count() == 0
    assert all(os.path.exists(p) for p in (copies[0], copies[1], new_path))   # nothing was moved


def test_duplicates_exclude_says_where_the_rest_went(env):
    from app import songs, dupes, create_app
    from app.config import Config
    music = env[0]
    Config.DUPES_NOTIFY, Config.DUPES_FINGERPRINT = False, False
    # a "possible" chain: 40s ~ 43s ~ 46s, but 40s and 46s are too far apart to match on their own
    for i, (d, sec) in enumerate([("A", 40), ("B", 43), ("C", 46)]):
        _noise(str(music / d / "Kannazhaga.mp3"), i + 1, seconds=sec, tags={"title": "Kannazhaga", "artist": "Dhanush"})
    for i, d in enumerate(("D", "E")):
        _noise(str(music / d / "Pair.mp3"), 9 + i, seconds=30, tags={"title": "Pair Song", "artist": "Chitra"})
    songs.index_all()
    dupes.scan()
    c = create_app(start_background=False).test_client()
    chain = [g for g in dupes.groups() if len(g["songs"]) == 3][0]
    mid = [s for s in chain["songs"] if 42 < s["duration"] < 44][0]
    c.post(f"/duplicates/{chain['id']}/exclude/{mid['id']}")
    page = c.get("/duplicates?tab=likely").get_data(as_text=True)
    # the other two only matched through the middle one, but they stay a group for you to review
    assert "The other 2 files are still a group, under Possible" in page
    left = [g for g in dupes.groups("likely") if len(g["songs"]) == 2 and "Kannazhaga" in g["songs"][0]["path"]]
    assert len(left) == 1 and mid["id"] not in [s["id"] for s in left[0]["songs"]]
    dupes.scan()                                                          # and a re-check keeps it that way
    assert len([g for g in dupes.groups() if "Kannazhaga" in g["songs"][0]["path"]]) == 1
    eid = dupes.exclusions()[0]["id"]
    c.post(f"/duplicates/exclusions/{eid}/delete")                        # undo: all three together again
    assert [len(g["songs"]) for g in dupes.groups() if "Kannazhaga" in g["songs"][0]["path"]] == [3]
    pair = [g for g in dupes.groups() if "Pair" in g["songs"][0]["path"]][0]
    c.post(f"/duplicates/{pair['id']}/exclude/{pair['songs'][0]['id']}")
    page = c.get("/duplicates?tab=likely").get_data(as_text=True)
    assert "only one file was left" in page and dupes.count() == 1


def test_duplicates_title_track_numbers():
    from app.dupes import _norm_title
    assert _norm_title("02 - Dil Ke Paas") == _norm_title('29 - Dil Ke Paas (From "Wajah Tum Ho")') == "dil ke paas"
    assert _norm_title("1. Kannazhaga") == _norm_title("CD1 - 07 - Kannazhaga") == "kannazhaga"
    assert _norm_title("7G Rainbow Colony") == "7g rainbow colony"


def test_send_album_back_to_review(env):
    music, data, album = env
    from app import db, worker, songs, create_app
    c = create_app(start_background=False).test_client()
    aid = worker.queue_album(str(album)); run_jobs()
    c.post(f"/album/{aid}/approve"); run_jobs()
    worker.queue_album(str(album), priority=db.PRIORITY_USER); run_jobs()
    assert db.album_get(aid)["status"] == "uptodate"
    # from a track (Duplicates, a playlist…): its whole album goes back to the queue, even though nothing looks wrong
    songs.index_all()
    sid = db.q("SELECT id FROM songs WHERE folder=?", (str(album),), one=True)["id"]
    r = c.post(f"/songs/{sid}/review", data={"from": "duplicates"}, headers={"Referer": "http://x/duplicates"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/duplicates")
    flash = c.get("/duplicates").get_data(as_text=True)
    assert "back to the review queue" in flash and f'href="/album/{aid}"' in flash
    run_jobs()
    a = db.album_get(aid)
    assert a["status"] == "review" and a["sent_back"] and "Duplicates" in a["sent_from"]
    q = c.get("/?f=sentback").get_data(as_text=True)
    assert "Sent back" in q and "You sent this back" in q and "nothing is suggested" in q
    assert "You sent this back for review" in c.get(f"/album/{aid}").get_data(as_text=True)
    assert db.albums_count(["review"], None, "sentback") == 1
    c.post(f"/album/{aid}/approve"); run_jobs()                 # dealt with: no longer marked
    a = db.album_get(aid)
    assert a["status"] == "applied" and not a["sent_back"]
    # the album page and the library's bulk bar do the same
    assert "Send to review" in c.get(f"/album/{aid}").get_data(as_text=True)
    c.post("/bulk", data={"action": "review", "ids": [str(aid)]}); run_jobs()
    assert db.album_get(aid)["status"] == "review" and "Library" in db.album_get(aid)["sent_from"]
    c.post(f"/album/{aid}/reject")
    assert not db.album_get(aid)["sent_back"]
    c.post(f"/album/{aid}/review"); run_jobs()
    assert db.album_get(aid)["status"] == "review"


def test_duplicates_keep_best_of_all_same_song(env):
    import json as _j
    from app import db, songs, dupes, create_app
    from app.config import Config
    music = env[0]
    Config.DUPES_NOTIFY, Config.DUPES_FINGERPRINT = False, False
    a, b = str(music / "X" / "Song.flac"), str(music / "Y" / "Song.mp3")
    _noise(a, 1, seconds=20); _noise(b, 2, seconds=20)
    songs.index_all()
    ids = [r["id"] for r in db.q("SELECT id FROM songs WHERE path IN (?,?) ORDER BY path", (a, b))]
    db.x("INSERT INTO dupe_groups(gkey, kind, members, keep_id, saving, found, notified) VALUES('t','recording',?,?,0,0,1)",
         (_j.dumps(ids), ids[0]))
    c = create_app(start_background=False).test_client()
    page = c.get("/duplicates?tab=recording").get_data(as_text=True)
    assert "Keep the best copy of all same-song files" in page
    assert "Keep the best copy of all same-song files" not in c.get("/duplicates?tab=likely").get_data(as_text=True)
    r = c.post("/duplicates/resolve-all/recording")
    assert r.status_code == 302 and "tab=recording" in r.headers["Location"]
    assert os.path.exists(a) and not os.path.exists(b) and dupes.count("recording") == 0
    assert c.post("/duplicates/resolve-all/likely").status_code == 404


def test_duplicates_forget_folders_deleted_outside(env):
    from app import songs, dupes, db, create_app
    from app.config import Config
    music = env[0]
    Config.DUPES_NOTIFY, Config.DUPES_FINGERPRINT = False, False
    src = str(music / "Orig" / "Song.mp3")
    make_audio(src, 5, {"title": "Copy Song", "artist": "Singer"})
    for d in ("Copy1", "Copy2"):
        os.makedirs(music / d); shutil.copy(src, music / d / "Song.mp3")
    songs.index_all(); dupes.scan()
    assert [len(g["songs"]) for g in dupes.groups()] == [3]
    c = create_app(start_background=False).test_client()
    shutil.rmtree(music / "Copy1")                                   # deleted in File Station
    page = c.get("/duplicates").get_data(as_text=True)               # just opening the page drops it
    assert "Copy1/" not in page and [len(g["songs"]) for g in dupes.groups()] == [2]
    shutil.rmtree(music / "Copy2")
    assert dupes.scan()                                              # Check library reads the disk again
    assert dupes.count() == 0
    assert not db.q("SELECT 1 FROM songs WHERE folder LIKE ?", (str(music / "Copy") + "%",))
    # a music folder that isn't mounted is not taken as "everything was deleted"
    n = db.q("SELECT COUNT(*) n FROM songs", one=True)["n"]
    real = Config.MUSIC_DIR
    try:
        Config.MUSIC_DIR = str(music.parent / "not-mounted")
        os.makedirs(Config.MUSIC_DIR)
        songs.index_all()
        assert db.q("SELECT COUNT(*) n FROM songs", one=True)["n"] == n
        assert not dupes.prune_missing([{"songs": [{"path": "/nope/x.mp3", "folder": "/nope"}]}])
    finally:
        Config.MUSIC_DIR = real


def test_plex_path_is_learned_and_albums_refreshed(env, monkeypatch):
    music, data, album = env
    import requests
    from app import songs, plex, db
    from app.config import Config
    paths = _library(music)
    songs.index_all()
    root = "/volume2/Multimedia/Audio Songs"                              # Synology Plex package's view
    rows = {r["path"]: dict(r) for r in db.q("SELECT path, title FROM songs")}
    tracks = "".join(f'<Track ratingKey="{100 + i}" parentRatingKey="{900 + i}" title="{rows[p]["title"]}">'
                     f'<Media><Part file="{root}{p[len(str(music)):]}"/></Media></Track>' for i, p in enumerate(paths))
    sent = []

    class R:
        def __init__(self, content):
            self.content, self.status_code = content.encode(), 200

        def raise_for_status(self):
            pass

    def req(method, url, params=None, timeout=None, headers=None):
        params = dict(params or {})
        sent.append((method, url.replace(Config.PLEX_URL, ""), params))
        if url.endswith("/library/sections"):
            return R(f'<MediaContainer><Directory key="7" title="Music" type="artist" agent="tv.plex.agents.music">'
                     f'<Location path="{root}"/></Directory></MediaContainer>')
        if url.endswith("/library/sections/7/all"):
            t = params.get("title")
            body = "".join(x for x in tracks.split("</Track>") if x and (not t or f'title="{t}"' in x))
            body = "</Track>".join(body.split("</Track>")) + ("</Track>" if body else "")
            return R(f"<MediaContainer>{tracks if not t else body}</MediaContainer>")
        if url.endswith("/prefs"):
            return R('<MediaContainer><Setting id="respectTags" label="Prefer local metadata" value="false"/>'
                     '<Setting id="other" label="Something else" value="1"/></MediaContainer>')
        return R("")
    monkeypatch.setattr(requests, "request", req)
    monkeypatch.setattr(requests, "get", lambda url, params=None, timeout=None, headers=None: req("GET", url, params))
    monkeypatch.setattr(Config, "PLEX_MUSIC_PATH", "/music")             # the common mistake: the container's path
    folder = os.path.dirname(paths[0])
    assert plex.refresh(folder, metadata=False)
    scan = [p for m, u, p in sent if u.endswith("/library/sections/7/refresh")]
    assert scan and scan[-1]["path"] == root + folder[len(str(music)):]
    assert plex.detected_prefix() == root
    assert any("isn't in any Plex music library" in r["message"] for r in db.q("SELECT message FROM log"))
    sent.clear()
    plex._refresh_albums(folder)                                         # found by title + folder, even after renames
    refreshes = [(u, p) for m, u, p in sent if m == "PUT" and u.endswith("/refresh")]
    assert refreshes and all(u.startswith("/library/metadata/9") and p.get("force") == 1 for u, p in refreshes)
    unlocks = [p for m, u, p in sent if m == "PUT" and u == "/library/sections/7/all"]
    assert unlocks and all(p["type"] == 9 and p["title.locked"] == 0 and p["originallyAvailableAt.locked"] == 0
                           for p in unlocks)
    lines = plex.diagnose()
    assert any(f"as {root}" in l for l in lines)
    assert any("Prefer local metadata: off" in l and "turn this on" in l for l in lines)
    assert any("but Plex uses" in l for l in lines)


def test_library_lists_folders_never_checked(env):
    music, data, album = env
    from app import songs, db, create_app
    make_audio(str(music / "New Artist" / "New Album" / "01.mp3"), 3, {"title": "Fresh"})
    songs.index_all()
    c = create_app(start_background=False).test_client()
    page = c.get("/library").get_data(as_text=True)
    assert "haven't been checked yet" in page and "Shift" in page
    r = c.post("/library/check-unchecked")
    assert r.status_code == 302
    assert db.album_by_path(str(music / "New Artist" / "New Album"))["status"] == "queued"
    assert "haven't been checked yet" not in c.get("/library").get_data(as_text=True)


def test_plex_readd_album_restores_ratings(env, monkeypatch):
    music, data, album = env
    import requests
    from app import songs, plexfix, db, worker, create_app
    from app.config import Config
    aid = worker.queue_album(str(album)); run_jobs()
    c = create_app(start_background=False).test_client()
    c.post(f"/album/{aid}/approve"); run_jobs()
    songs.index_all()
    folder = db.album_get(aid)["path"]
    files = sorted(os.listdir(folder))
    titles = {r["path"].rsplit("/", 1)[-1]: r["title"] for r in db.q("SELECT path, title FROM songs WHERE folder=?", (folder,))}
    root = "/volume2/Multimedia/Audio Songs"
    plexdir = root + folder[len(str(music)):]
    st = {"album": "900", "deleted": False, "missing": False, "gen": 0}
    sent = []

    class R:
        def __init__(self, content, code=200):
            self.content, self.status_code = content.encode(), code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(str(self.status_code))

    def track_xml(i, f):
        base = 100 + 10 * st["gen"]
        rating = 'userRating="8" viewCount="3"' if st["gen"] == 0 and i == 0 else ""
        return (f'<Track ratingKey="{base + i}" parentRatingKey="{st["album"]}" title="{titles.get(f, f)}" {rating}>'
                f'<Media><Part file="{plexdir}/{f}"/></Media></Track>')

    def present():
        return not st["deleted"] or st["gen"] > 0

    def req(method, url, params=None, timeout=None, headers=None):
        params = dict(params or {})
        u = url.replace(Config.PLEX_URL, "")
        sent.append((method, u, params))
        if u == "/library/sections":
            return R(f'<MediaContainer><Directory key="7" title="Music" type="artist" refreshing="0">'
                     f'<Location path="{root}"/></Directory></MediaContainer>')
        if u == "/library/sections/7/refresh":
            here = os.path.isdir(folder)
            if not here:
                st["missing"] = True
            elif st["deleted"] and st["gen"] == 0:
                st.update(gen=1, album="960")           # added afresh: new IDs
            return R("")
        if u == "/library/sections/7/emptyTrash":
            if st["missing"]:
                st["deleted"] = True
            return R("")
        if u == "/library/sections/7/all" and method == "GET":
            if not present():
                return R("<MediaContainer/>")
            t = params.get("title")
            return R("<MediaContainer>" + "".join(track_xml(i, f) for i, f in enumerate(files)
                                                   if not t or titles.get(f, f) == t) + "</MediaContainer>")
        if u.endswith("/children"):
            return R("<MediaContainer>" + "".join(track_xml(i, f) for i, f in enumerate(files)) + "</MediaContainer>")
        if u.startswith("/library/metadata/"):
            k = u.split("/")[3]
            if k == "900" and st["deleted"]:
                return R("", 404)
            return R(f'<MediaContainer><Directory ratingKey="{k}"/></MediaContainer>')
        return R("")
    monkeypatch.setattr(requests, "request", req)
    monkeypatch.setattr(requests, "get", lambda url, params=None, timeout=None, headers=None: req("GET", url, params))
    monkeypatch.setattr(Config, "PLEX_POLL", 0)
    monkeypatch.setattr(Config, "PLEX_SCAN_TIMEOUT", 2)
    assert plexfix.readd(aid)
    assert os.path.isdir(folder) and sorted(os.listdir(folder)) == files       # folder is back, untouched
    assert not plexfix.staged()
    rate = [p for m, u, p in sent if u == "/:/rate"]
    plays = [p for m, u, p in sent if u == "/:/scrobble"]
    assert rate == [{"key": "110", "identifier": plexfix.IDENT, "rating": 8.0, "X-Plex-Token": Config.PLEX_TOKEN}]
    assert len(plays) == 3 and all(p["key"] == "110" for p in plays)
    assert "Re-added in Plex" in plexfix.last(aid)["msg"]
    # the album page offers it and shows the result
    page = c.get(f"/album/{aid}").get_data(as_text=True)
    assert "Re-add in Plex" in page and "Re-added in Plex" in page
    # if Plex never lets go, the folder goes straight back and nothing is lost
    st.update(deleted=False, missing=False, gen=0, album="900")
    monkeypatch.setattr(plexfix, "_gone", lambda keys: False)
    assert not plexfix.readd(aid)
    assert os.path.isdir(folder) and sorted(os.listdir(folder)) == files and not plexfix.staged()
    assert "back where it was" in plexfix.last(aid)["msg"]


def test_plex_readd_recovers_after_crash(env):
    music, data, album = env
    from app import plexfix, db
    tmp = os.path.join(plexfix._stage_root(), "1-123")
    shutil.move(str(album), tmp)
    plexfix._set_staged({tmp: str(album)})
    plexfix.recover()
    assert os.path.isdir(album) and not os.path.exists(tmp) and not plexfix.staged()


def test_library_shows_albums_that_differ_in_plex(env, monkeypatch):
    music, data, album = env
    import requests
    from app import songs, plex, plexfix, db, create_app
    from app.config import Config
    paths = _library(music)
    songs.index_all()
    for f in sorted({os.path.dirname(p) for p in paths}):
        db.album_upsert(f, "applied")
    rows = {r["path"]: dict(r) for r in db.q("SELECT * FROM songs")}

    def shown(p):          # what Plex shows for each track
        r = rows[p]
        if "/Roja/" in p:
            return "Various Artists", r["album"], r["title"]                   # filed under the wrong artist
        if "/Mouna Ragam/" in p:
            return r["albumartist"], "Mouna Ragam (Old Name)", r["title"]     # stale album name
        if "/3/" in p:
            return "Anirudh Ravichander", "3", r["title"]
        return r["albumartist"], r["album"], r["title"]
    tracks = "".join(f'<Track ratingKey="{100 + i}" grandparentTitle="{shown(p)[0]}" parentTitle="{shown(p)[1]}" '
                     f'title="{shown(p)[2]}"><Media><Part file="/volume2/x{p[len(str(music)):]}"/></Media></Track>'
                     for i, p in enumerate(paths))

    class R:
        def __init__(self, content):
            self.content, self.status_code = content.encode(), 200

        def raise_for_status(self):
            pass

    def req(method, url, params=None, timeout=None, headers=None):
        if url.endswith("/library/sections"):
            return R('<MediaContainer><Directory key="7" title="Music" type="artist"><Location path="/volume2/x"/></Directory></MediaContainer>')
        if "/library/sections/7/all" in url:
            return R(f'<MediaContainer size="{len(paths)}" totalSize="{len(paths)}">{tracks}</MediaContainer>')
        return R("")
    monkeypatch.setattr(requests, "request", req)
    monkeypatch.setattr(requests, "get", lambda url, params=None, timeout=None, headers=None: req("GET", url, params))
    plex.sync_tracks()
    d = plex.differences()
    roja, mouna = str(music / "A.R. Rahman" / "Roja"), str(music / "Ilaiyaraaja" / "Mouna Ragam")
    assert set(d) == {roja, mouna}
    assert d[roja]["kind"] == "artist" and d[roja]["plex_artist"] == "Various Artists"
    assert d[mouna]["kind"] == "details" and d[mouna]["plex_album"] == "Mouna Ragam (Old Name)"
    c = create_app(start_background=False).test_client()
    page = c.get("/library?tab=plex").get_data(as_text=True)
    assert "Differs in Plex · <span class=\"n\">2</span>" in page and "Wrong artist in Plex" in page and "Old details in Plex" in page
    assert "Punnagai Mannan" not in page
    page = c.get("/library?tab=plex&kind=artist").get_data(as_text=True)
    assert "Roja" in page and "Mouna Ragam" not in page.split("<tbody>")[1]
    # "select all in this view" on that tab only takes the differing albums, never the whole library
    started = []
    monkeypatch.setattr(plexfix, "start", lambda ids: started.extend(ids) or len(ids))
    c.post("/bulk", data={"action": "plexreadd", "all_matching": "on", "tab": "plex", "kind": "artist", "q": ""})
    assert [db.album_get(i)["path"] for i in started] == [roja]
    refreshed = []
    monkeypatch.setattr(plexfix, "refresh_many", lambda fs: refreshed.extend(fs) or len(fs))
    c.post("/bulk", data={"action": "plexrefresh", "all_matching": "on", "tab": "plex", "kind": "all", "q": ""})
    assert sorted(refreshed) == sorted([roja, mouna])


def test_album_artists_page_merges_spellings_and_sends_to_review(env):
    music, data, album = env
    from app import songs, db, tags, artists as AR, create_app
    def mk(folder, name, aa, artist="Singer"):
        p = str(music / folder / f"{name}.mp3")
        make_audio(p, 2)
        tags.write(p, {"title": name, "artist": artist, "album": folder.split("/")[-1], **({"albumartist": aa} if aa else {})})
        return p
    a1 = mk("ARR/Roja", "Kadhal Rojave", "A.R. Rahman")
    a2 = mk("ARR/Roja", "Chinna Chinna Aasai", "A.R. Rahman")
    b1 = mk("ARR/Bombay", "Uyire", "A. R. Rahman")
    c1 = mk("ARR/Duet", "Anjali", "A R Rahman")
    other = mk("ARR/Duet", "Other MD song", "Ilaiyaraaja")                 # same folder, another artist: untouched
    mk("Loose/Singles", "Song X", None, "Sid Sriram, Shreya Ghoshal")       # no Album Artist
    mk("Old/Hits", "Song Y", "S. P. Balasubrahmanyam, Janaki")              # singers as Album Artist
    songs.index_all()
    for f in ("ARR/Roja", "ARR/Bombay", "ARR/Duet", "Old/Hits"):
        db.album_upsert(str(music / f), "applied")
    groups = AR.variant_groups()
    g = [x for x in groups if "A.R. Rahman" in [n["name"] for n in x["names"]]][0]
    assert {n["name"] for n in g["names"]} == {"A.R. Rahman", "A. R. Rahman", "A R Rahman"} and g["suggested"] == "A.R. Rahman"
    assert str(music / "Loose" / "Singles") in [d["folder"] for d in AR.missing()]
    assert [d["name"] for d in AR.multi()] == ["S. P. Balasubrahmanyam, Janaki"]
    c = create_app(start_background=False).test_client()
    page = c.get("/artists").get_data(as_text=True)
    assert "Same name, other spelling" in page and "A. R. Rahman" in page and "Loose/Singles" in page and "Janaki" in page
    c.post("/artists/merge", data={"names": [n["name"] for n in g["names"]], "target": "A.R. Rahman"})
    run_jobs()
    assert tags.read(b1)["fields"]["albumartist"] == "A.R. Rahman" and tags.read(c1)["fields"]["albumartist"] == "A.R. Rahman"
    assert tags.read(other)["fields"]["albumartist"] == "Ilaiyaraaja"
    assert not [x for x in AR.variant_groups() if "A.R. Rahman" in [n["name"] for n in x["names"]]]
    c.post("/artists/review", data={"folders": [str(music / "Loose" / "Singles")], "names": ["S. P. Balasubrahmanyam, Janaki"]})
    for f in ("Loose/Singles", "Old/Hits"):
        a = db.album_by_path(str(music / f))
        assert a and a["sent_back"] and a["status"] == "queued"
    # "different artists" keeps a group apart for good
    mk("X/One", "s1", "Deva Raj"); mk("X/Two", "s2", "Devaraj")
    songs.index_all()
    g2 = [x for x in AR.variant_groups() if "Devaraj" in [n["name"] for n in x["names"]]][0]
    c.post("/artists/ignore", data={"key": g2["key"]})
    assert not [x for x in AR.variant_groups() if "Devaraj" in [n["name"] for n in x["names"]]]


def test_plex_readd_hides_whole_multidisc_album(env, monkeypatch):
    music, data, album = env
    from app import plexfix, db
    root = music / "Bob Marley 3CD"
    for d in ("CD 1", "CD 2", "CD 3"):
        os.makedirs(root / d)
    assert plexfix.album_root(str(root / "CD 1")) == str(root)
    assert plexfix.album_root(str(root / "Disc2")) == str(root)
    assert plexfix.album_root(str(music / "Ilaiyaraaja" / "Punnagai Mannan")) == str(music / "Ilaiyaraaja" / "Punnagai Mannan")
    assert plexfix.album_root(str(music / "CD 1")) == str(music / "CD 1")              # never the whole library
    assert plexfix._tail("/x/Bob/CD 2/01 - Song.flac") == "cd 2/01 - song.flac"
    ids = [db.album_upsert(str(root / d), "applied") for d in ("CD 1", "CD 2", "CD 3")]
    monkeypatch.setattr(plexfix, "_loop", lambda: None)
    assert plexfix.start(ids) == 1                       # one re-add for the whole album, not three


def test_unmatched_songs_of_already_tagged_albums_get_album_tags(env, monkeypatch):
    music, data, album = env
    from app import db, tags, worker, songs, create_app
    from app.config import Config
    folder = music / "Ilaiyaraaja" / "Salangai Oli"
    make_audio(str(folder / "Naatham.mp3"), 20, {"title": "Naatham"})
    make_audio(str(folder / "Om Namasivaya.flac"), 22)
    make_audio(str(folder / "Vedham.mp3"), 24, {"title": "Vedham"})
    make_audio(str(folder / "Mounamana Neram.mp3"), 26)
    make_audio(str(folder / "Thakita Thadimi.mp3"), 28)
    make_audio(str(folder / "Unknown Bonus.mp3"), 33, {"artist": "Some Singer"})
    monkeypatch.setattr(Config, "ALBUM_TAGS_UNMATCHED", False)          # how albums were tagged before 2.11
    aid = worker.queue_album(str(folder)); run_jobs()
    c = create_app(start_background=False).test_client()
    c.post(f"/album/{aid}/approve"); run_jobs()
    assert db.album_get(aid)["status"] == "applied"
    ub = str(folder / "Unknown Bonus.mp3")
    assert not tags.read(ub)["fields"].get("albumartist")
    monkeypatch.setattr(Config, "ALBUM_TAGS_UNMATCHED", True)
    songs.index_all()
    page = c.get("/artists").get_data(as_text=True)
    assert "were already tagged" in page and "Fix 1 tagged album" in page
    c.post("/artists/albumwide", data={"all": "1"}); run_jobs()
    f = tags.read(ub)["fields"]
    assert f["albumartist"] == "Ilaiyaraaja" and f["album"] == "Salangai Oli" and f["artist"] == "Some Singer"
    assert os.path.exists(ub)                                            # not renamed
    assert "were already tagged" not in c.get("/artists").get_data(as_text=True)


def test_plex_duplicate_artists_are_merged(env, monkeypatch):
    music, data, album = env
    import requests
    from app import plex, create_app
    from app.config import Config
    arts = [("10", "Various Artists", 5, True), ("11", "Various Artists", 1, False), ("12", "Various Artists", 1, False),
            ("20", "A.R. Rahman", 3, True), ("21", "A.R. Rahman\u200b", 1, False), ("22", "a.r.  rahman", 1, False),
            ("30", "Ilaiyaraaja", 9, True)]
    thumb = ' thumb="/t"'
    xml = "".join(f'<Directory ratingKey="{k}" title="{t}" childCount="{n}"{thumb if th else ""}/>' for k, t, n, th in arts)
    sent = []

    class R:
        def __init__(self, content):
            self.content, self.status_code = content.encode(), 200

        def raise_for_status(self):
            pass

    def req(method, url, params=None, timeout=None, headers=None):
        u = url.replace(Config.PLEX_URL, "")
        sent.append((method, u, dict(params or {})))
        if u == "/library/sections":
            return R('<MediaContainer><Directory key="7" title="Music" type="artist"><Location path="/m"/></Directory></MediaContainer>')
        if u == "/library/sections/7/all":
            return R(f'<MediaContainer size="{len(arts)}" totalSize="{len(arts)}">{xml}</MediaContainer>')
        return R("")
    monkeypatch.setattr(requests, "request", req)
    monkeypatch.setattr(requests, "get", lambda url, params=None, timeout=None, headers=None: req("GET", url, params))
    groups = plex.duplicate_artists()
    assert [(g["keep"], g["merge"]) for g in groups] == [("10", ["11", "12"]), ("20", ["21", "22"])]
    assert groups[1]["hidden_diff"] and not groups[0]["hidden_diff"]
    c = create_app(start_background=False).test_client()
    page = c.get("/artists").get_data(as_text=True)
    assert "Same artist more than once in Plex" in page and "Merge all 2" in page and "Ilaiyaraaja" not in page.split('id="plexdupes"')[1].split("</section>")[0]
    sent.clear()
    c.post("/artists/plex-merge", data={"keep": "20"})
    assert [(m, u, p.get("ids")) for m, u, p in sent if m == "PUT"] == [("PUT", "/library/metadata/20/merge", "21,22")]
    sent.clear()
    c.post("/artists/plex-merge", data={})
    assert [(u, p.get("ids")) for m, u, p in sent if m == "PUT"] == [("/library/metadata/10/merge", "11,12"), ("/library/metadata/20/merge", "21,22")]


def test_old_review_still_gives_unmatched_songs_album_tags(env, monkeypatch):
    music, data, album = env
    from app import db, tags, worker, create_app
    from app.config import Config
    folder = music / "Ilaiyaraaja" / "Salangai Oli"
    make_audio(str(folder / "Naatham.mp3"), 20, {"title": "Naatham"})
    make_audio(str(folder / "Om Namasivaya.flac"), 22)
    make_audio(str(folder / "Vedham.mp3"), 24, {"title": "Vedham"})
    make_audio(str(folder / "Mounamana Neram.mp3"), 26)
    make_audio(str(folder / "Thakita Thadimi.mp3"), 28)
    make_audio(str(folder / "Unknown Bonus.mp3"), 33, {"artist": "Aadmi", "albumartist": "Aadmi"})
    monkeypatch.setattr(Config, "ALBUM_TAGS_UNMATCHED", False)          # review made by an older version
    aid = worker.queue_album(str(folder)); run_jobs()
    u = [f for f in db.album_get(aid)["proposal"]["files"] if f["file"] == "Unknown Bonus.mp3"][0]
    assert u["match"] is None and not u["changes"]
    monkeypatch.setattr(Config, "ALBUM_TAGS_UNMATCHED", True)           # …approved after updating
    c = create_app(start_background=False).test_client()
    page = c.get(f"/album/{aid}").get_data(as_text=True)
    assert "gets the album&#39;s tags (album, albumartist" in page or "gets the album's tags (album, albumartist" in page
    idx = [i for i, f in enumerate(db.album_get(aid)["proposal"]["files"]) if f["file"] == "Unknown Bonus.mp3"][0]
    form = {"a-album": "Salangai Oli", "a-albumartist": "Ilaiyaraaja", "rename": "on", "artwork": "missing", "lyrics": "missing",
            f"f-{idx}-include": "on"}                                    # even with the header box ticked
    c.post(f"/album/{aid}/apply", data=form); run_jobs()
    f = tags.read(str(folder / "Unknown Bonus.mp3"))["fields"]
    assert f["albumartist"] == "Ilaiyaraaja" and f["album"] == "Salangai Oli" and f["artist"] == "Aadmi"


def test_folder_policies_suggest_accept_write_and_stick(env):
    music, data, album = env
    from app import db, tags, songs, worker, policy as POL, create_app
    from app.config import Config
    def mk(folder, name, **t):
        p = str(music / folder / f"{name}.mp3")
        make_audio(p, 3)
        tags.write(p, {"title": name, **t})
        return p
    hits = [mk("Hits of Mohd. Rafi", f"Song {i}", artist="Mohd. Rafi", album=f"Film {i}", albumartist="Mohd. Rafi",
               composer=c) for i, c in enumerate(["Shankar Jaikishan", "Naushad", "S.D. Burman", "Madan Mohan"])]
    single = mk("Singles/Kutty Story", "Kutty Story", artist="Vijay", album="Kutty Story - Single",
                albumartist="Vijay, Anirudh Ravichander", composer="Anirudh Ravichander")
    multi = [mk("Films/Kaaviya Thalaivan", f"Track {i}", artist="Singer", album="Kaaviya Thalaivan", albumartist="A.R. Rahman",
                composer=c) for i, c in enumerate(["A.R. Rahman", "A.R. Rahman", "Anirudh Ravichander"])]
    songs.index_all()
    sug = {d["folder"]: d for d in POL.suggestions()}
    h, s1, m = str(music / "Hits of Mohd. Rafi"), str(music / "Singles" / "Kutty Story"), str(music / "Films" / "Kaaviya Thalaivan")
    assert sug[h]["policy"] == "collection" and sug[h]["albumartist"] == Config.VARIOUS_ARTISTS and sug[h]["album"] == "Hits of Mohd. Rafi"
    assert sug[s1]["policy"] == "single" and sug[s1]["albumartist"] == Config.VARIOUS_ARTISTS
    assert sug[m]["policy"] == "various" and "2 different composers" in sug[m]["why"][0]
    c = create_app(start_background=False).test_client()
    page = c.get("/library/decisions").get_data(as_text=True)
    assert "Needs a decision" in page and "Hits of Mohd. Rafi" in page and "Collection" in page
    c.post("/folders/policy", data={"folders": [h, s1, m], "policy": "suggested"}); run_jobs()
    for p in hits:
        f = tags.read(p)["fields"]
        assert f["albumartist"] == "Various Artists" and f["album"] == "Hits of Mohd. Rafi"
        assert f["releasetype"] == "album;compilation" and f["compilation"] == "1" and f["title"].startswith("Song")
    f = tags.read(single)["fields"]
    assert f["albumartist"] == "Various Artists" and f["releasetype"] == "single" and f["composer"] == "Anirudh Ravichander"
    for p in multi:
        f = tags.read(p)["fields"]
        assert f["albumartist"] == "Various Artists" and f["compilation"] == "1"
    assert not POL.suggestions()                                          # decided: nothing left to ask
    a = db.album_by_path(h)
    assert a["verify"]["files"] == 4 and a["verify"]["n_failed"] == 0
    page = c.get(f"/album/{a['id']}").get_data(as_text=True)
    assert "Decided: Collection" in page and "every tag is as written" in page
    # a re-check can't undo the decision
    worker.queue_album(h, priority=db.PRIORITY_USER); run_jobs()
    p = db.album_by_path(h)["proposal"]
    assert p["album"]["album_artist"] == "Various Artists" and p["album"]["album"] == "Hits of Mohd. Rafi"
    assert p["policy"]["policy"] == "collection"
    # Undo on the album page puts the old tags back
    db.job_add("undo", a["id"]); run_jobs()
    assert tags.read(hits[0])["fields"]["albumartist"] == "Mohd. Rafi"
    # "Leave as is" stops the suggestion; "Set for ticked" uses your own name
    POL.clear(s1)
    c.post("/folders/dismiss", data={"folders": [s1]})
    assert s1 not in {d["folder"] for d in POL.suggestions()}
    c.post("/folders/policy", data={"folders": [m], "policy": "film", "albumartist": "A.R. Rahman"}); run_jobs()
    assert tags.read(multi[2])["fields"]["albumartist"] == "A.R. Rahman" and tags.read(multi[2])["fields"]["releasetype"] == "album;soundtrack"


def test_folders_view_browse_and_decide_a_whole_tree(env):
    music, data, album = env
    from app import db, tags, songs, folders as FO, create_app
    def mk(folder, name, **t):
        p = str(music / folder / f"{name}.mp3")
        make_audio(p, 2)
        tags.write(p, {"title": name, **t})
        return p
    a = [mk("Rafi/Hits of Rafi", f"S{i}", album=f"F{i}", albumartist="Mohd. Rafi", composer=f"C{i}") for i in range(4)]
    b = mk("Rafi/Singles/Baharon", "Baharon", album="Baharon - Single", albumartist="Mohd. Rafi")
    c = mk("ARR/Roja", "Kadhal Rojave", album="Roja", albumartist="A.R. Rahman", composer="A.R. Rahman")
    songs.index_all()
    top = FO.browse(FO.root())
    names = {r["name"]: r for r in top["children"]}
    assert {"Rafi", "ARR"} <= set(names) and names["Rafi"]["albums"] == 2 and names["Rafi"]["songs"] == 5
    assert names["Rafi"]["has_children"] and names["Rafi"]["top_aa"][0][0] == "Mohd. Rafi"
    inside = FO.browse(FO.abs_path("Rafi"))
    assert [r["name"] for r in inside["children"]] == ["Hits of Rafi", "Singles"] and inside["crumbs"][-1] == ("Rafi", "Rafi")
    assert FO.abs_path("../../etc") == FO.root()                           # can't leave the library
    c_ = create_app(start_background=False).test_client()
    page = c_.get("/library/folders?path=Rafi").get_data(as_text=True)
    assert "Hits of Rafi" in page and "Include subfolders" in page and 'class="viewtabs"' in page
    assert "Hits of Rafi" in c_.get("/library/folders?q=hits").get_data(as_text=True)
    # tick the parent "Rafi" folder, include subfolders: both albums inside get the kind
    c_.post("/folders/policy", data={"folders": [str(music / "Rafi")], "recursive": "on", "policy": "various"}); run_jobs()
    assert all(tags.read(p)["fields"]["albumartist"] == "Various Artists" for p in a + [b])
    assert tags.read(c)["fields"]["albumartist"] == "A.R. Rahman"                       # outside: untouched
    assert db.album_by_path(str(music / "Rafi" / "Singles" / "Baharon"))["status"] == "applied"
    # without "include subfolders", a parent folder with no songs of its own changes nothing
    c_.post("/folders/policy", data={"folders": [str(music / "ARR")], "policy": "single"}); run_jobs()
    assert tags.read(c)["fields"]["albumartist"] == "A.R. Rahman" and not db.album_by_path(str(music / "ARR"))
    # sidebar has six items now; the old pages are views of Library
    nav = c_.get("/").get_data(as_text=True).split('class="nav"')[1].split("</div>")[0]
    assert nav.count("<a ") == 7 and ">Home<" in nav and "Album artists" not in nav and ">Scan<" not in nav


def test_plex_live_counts_checks_and_home(env, monkeypatch):
    music, data, album = env
    import json as _j
    import requests
    from app import db, songs, plex, plexlive, create_app
    from app.config import Config
    paths = _library(music)
    songs.index_all()
    roja = str(music / "A.R. Rahman" / "Roja")
    mouna = str(music / "Ilaiyaraaja" / "Mouna Ragam")
    for f in (roja, mouna):
        db.album_upsert(f, "applied")
    rows = {r["path"]: dict(r) for r in db.q("SELECT * FROM songs")}
    root = "/volume2/Multimedia/Audio Songs"

    def track(i, p):
        r = rows[p]
        aa = "Various Artists" if "/Roja/" in p else r["albumartist"]          # Plex still has Roja wrong
        return (f'<Track ratingKey="{100 + i}" parentRatingKey="{900 + i}" grandparentTitle="{aa}" parentTitle="{r["album"]}" '
                f'title="{r["title"]}"><Media><Part file="{root}{p[len(str(music)):]}"/></Media></Track>')
    tracks = {p: track(i, p) for i, p in enumerate(paths)}

    class R:
        def __init__(self, content):
            self.content, self.status_code = content.encode(), 200

        def raise_for_status(self):
            pass

    def req(method, url, params=None, timeout=None, headers=None):
        u, params = url.replace(Config.PLEX_URL, ""), dict(params or {})
        if u == "/library/sections":
            return R(f'<MediaContainer><Directory key="7" title="Music" type="artist"><Location path="{root}"/></Directory></MediaContainer>')
        if u == "/library/sections/7/all":
            if params.get("X-Plex-Container-Size") == 0 and "title" not in params:
                n = {8: 4, 9: 4, 10: len(paths)}[params["type"]]
                return R(f'<MediaContainer size="0" totalSize="{n}"/>')
            t = params.get("title")
            return R("<MediaContainer>" + "".join(x for p, x in tracks.items() if not t or rows[p]["title"] == t) + "</MediaContainer>")
        if u.startswith("/library/metadata/"):
            k = u.split("/")[3]
            return R("<MediaContainer>" + "".join(x for x in tracks.values() if f'ratingKey="{k}"' in x) + "</MediaContainer>")
        return R("")
    monkeypatch.setattr(requests, "request", req)
    monkeypatch.setattr(requests, "get", lambda url, params=None, timeout=None, headers=None: req("GET", url, params))
    # totals: Plex's own and the files' (grouped the way Plex groups them)
    assert plex.counts() == {"artists": 4, "albums": 4, "tracks": len(paths)}
    lc = plex.local_counts()
    n_songs = db.q("SELECT COUNT(*) n FROM songs", one=True)["n"]
    assert lc["tracks"] == n_songs and lc["folders"] == db.q("SELECT COUNT(DISTINCT folder) n FROM songs", one=True)["n"]
    assert lc["albums"] >= 4 and lc["artists"] >= 3
    # step 5 of a write: does Plex show what the files say?
    ok = plex.check_folder(mouna)
    assert ok["ok"] and ok["checked"] == 3 and not ok["diffs"]
    bad = plex.check_folder(roja)
    assert not bad["ok"] and bad["diffs"][0]["field"] == "album artist" and bad["diffs"][0]["plex"] == "Various Artists"
    c = create_app(start_background=False).test_client()
    aid = db.album_by_path(mouna)["id"]
    assert "Plex shows these tags" in c.get(f"/album/{aid}").get_data(as_text=True)
    assert "Plex differs" in c.get(f"/album/{db.album_by_path(roja)['id']}").get_data(as_text=True)
    # live messages from Plex: processed / deleted items and scans
    k = rows[[p for p in paths if "/Roja/" in p][0]]
    plexlive.handle(_j.dumps({"NotificationContainer": {"type": "timeline", "TimelineEntry": [
        {"identifier": "com.plexapp.plugins.library", "itemID": 101, "type": 10, "state": 5, "title": "Kadhal Rojave"}]}}))
    assert "101" in plexlive._items and plexlive._dirty.is_set() and "Kadhal Rojave" in plexlive.status()["last_text"]
    plexlive.handle({"NotificationContainer": {"type": "activity", "ActivityNotification": [
        {"event": "updated", "Activity": {"type": "library.update.section", "title": "Scanning Music", "progress": 40}}]}})
    assert plexlive.status()["scanning"]["progress"] == 40
    plexlive.handle({"NotificationContainer": {"type": "activity", "ActivityNotification": [
        {"event": "ended", "Activity": {"type": "library.update.section"}}]}})
    assert plexlive.status()["scanning"] is None
    db.x("UPDATE songs SET plex_key='555' WHERE id=?", (k["id"],))
    plexlive.handle({"NotificationContainer": {"type": "timeline", "TimelineEntry": [
        {"identifier": "com.plexapp.plugins.library", "itemID": 555, "type": 10, "state": 9}]}})
    assert db.q("SELECT plex_key FROM songs WHERE id=?", (k["id"],), one=True)["plex_key"] is None
    plexlive.recheck_items(["101"])                       # the albums of processed items get a fresh check
    assert db.album_by_path(roja)["plex_check"]["at"] >= bad["at"]
    # Home: files vs Plex side by side, and the to-do list
    plexlive._state.update(counts=plex.counts(), counts_at=1)
    page = c.get("/").get_data(as_text=True)
    assert "Live link to Plex" in page or "checking every few minutes" in page
    assert "in your files" in page and "in Plex" in page and "To do" in page
    assert "albums don&#39;t match Plex after their last write" in page or "albums don't match Plex after their last write" in page
    j = c.get("/api/home").get_json()
    assert j["plex"]["tracks"] == len(paths) and j["local"]["tracks"] == n_songs
    # old review-queue links still open the queue
    assert "Review queue" in c.get(f"/?a={aid}").get_data(as_text=True).split("<title>")[1]



def test_library_bulk_set_kind_of_album(env):
    music, data, album = env
    from app import db, tags, worker, policy as POL, create_app
    aid = worker.queue_album(str(album)); run_jobs()
    c = create_app(start_background=False).test_client()
    c.post(f"/album/{aid}/approve"); run_jobs()
    c.post("/bulk", data={"action": "policy", "ids": [str(aid)], "policy": "various", "new_artist": ""}); run_jobs()
    assert POL.get(str(album))["policy"] == "various"
    assert all(tags.read(os.path.join(str(album), f))["fields"]["albumartist"] == "Various Artists"
               for f in os.listdir(album) if f.endswith((".mp3", ".flac", ".m4a")))
    page = c.get("/library").get_data(as_text=True)
    assert "rowmarks" in page and ">Various Artists</span>" in page



def test_kinds_of_album_settings_change_suggestions(env, monkeypatch):
    music, data, album = env
    from app import tags, songs, policy as POL, create_app
    from app.config import Config
    p = str(music / "Pattu Paadava" / "Song.mp3"); make_audio(p, 2); tags.write(p, {"title": "Song", "albumartist": "X"})
    q = str(music / "Solo" / "One.mp3"); make_audio(q, 2); tags.write(q, {"title": "One", "albumartist": "Y"})
    songs.index_all()
    sug = {d["folder"]: d["policy"] for d in POL.suggestions()}
    assert sug.get(str(music / "Solo")) == "single" and sug.get(str(music / "Pattu Paadava")) == "single"
    c = create_app(start_background=False).test_client()
    page = c.get("/settings?tab=kinds").get_data(as_text=True)
    assert "Kinds of album" in page and "Words that mark a collection folder" in page
    monkeypatch.setattr(Config, "POLICY_COLLECTION_WORDS", "pattu paadava")       # your own word
    assert {d["folder"]: d["policy"] for d in POL.suggestions()}[str(music / "Pattu Paadava")] == "collection"
    monkeypatch.setattr(Config, "POLICY_SUGGEST_SINGLE", False)
    assert str(music / "Solo") not in {d["folder"] for d in POL.suggestions()}


def test_nfo_files_for_plex_music(env, monkeypatch):
    music, data, album = env
    import xml.etree.ElementTree as ET
    from app import db, songs, tags, nfo, worker, create_app
    from app.config import Config
    monkeypatch.setattr(Config, "WRITE_NFO", True)
    paths = _library(music)                       # Ilaiyaraaja/Mouna Ragam, Ilaiyaraaja/Punnagai Mannan, A.R. Rahman/Roja…
    songs.index_all()
    mouna = str(music / "Ilaiyaraaja" / "Mouna Ragam")
    for p in paths:
        if "/Mouna Ragam/" in p:
            tags.write(p, {"releasetype": "album;soundtrack"})
    songs.index_all()
    assert nfo.write_album(mouna).startswith("album.nfo written, artist.nfo written")
    text = open(os.path.join(mouna, "album.nfo"), encoding="utf-8").read()
    assert nfo.MARK in text
    root = ET.fromstring(text.split("-->", 1)[1])
    assert root.tag == "album" and root.findtext("title") == "Mouna Ragam" and root.findtext("year") == "1986"
    assert root.find("albumArtistCredits").findtext("artist") == "Ilaiyaraaja" and root.findtext("genre") == "Tamil"
    assert root.findtext("releasetype") == "album" and root.findtext("type") == "Soundtrack" and root.findtext("compilation") == "false"
    tr = root.findall("track")
    assert [t.findtext("position") for t in tr] == ["1", "2", "3"] and tr[0].findtext("title") == "Mandram Vandha"
    art = ET.fromstring(open(str(music / "Ilaiyaraaja" / "artist.nfo"), encoding="utf-8").read().split("-->", 1)[1])
    assert art.tag == "artist" and art.findtext("name") == "Ilaiyaraaja"
    # an NFO you made yourself is never replaced
    own = os.path.join(str(music / "A.R. Rahman" / "Roja"), "album.nfo")
    open(own, "w").write("<album><title>Mine</title></album>")
    assert "left alone" in nfo.write_album(str(music / "A.R. Rahman" / "Roja"))
    assert open(own).read() == "<album><title>Mine</title></album>"
    # written automatically after a change (here: a folder decision), following the new tags
    db.album_upsert(mouna, "applied")
    c = create_app(start_background=False).test_client()
    c.post("/folders/policy", data={"folders": [mouna], "policy": "various"}); run_jobs()
    root = ET.fromstring(open(os.path.join(mouna, "album.nfo"), encoding="utf-8").read().split("-->", 1)[1])
    assert root.findtext("artistdesc") == "Various Artists" and root.findtext("compilation") == "true"
    # the settings button writes them for every tagged album
    db.album_upsert(str(music / "Ilaiyaraaja" / "Punnagai Mannan"), "applied")
    assert "Write NFO files" in c.get("/settings?tab=plex").get_data(as_text=True)
    w, s = nfo.write_many([str(music / "Ilaiyaraaja" / "Punnagai Mannan")])
    assert w == 1 and os.path.exists(str(music / "Ilaiyaraaja" / "Punnagai Mannan" / "album.nfo"))


def test_write_nfo_button_really_submits(env, monkeypatch):
    music, data, album = env
    import threading, re as _re
    from app import db, worker, create_app
    from app.config import Config
    aid = worker.queue_album(str(album)); run_jobs()
    c = create_app(start_background=False).test_client()
    c.post(f"/album/{aid}/approve"); run_jobs()
    page = c.get("/settings?tab=plex").get_data(as_text=True)
    title = page.split("<title>")[1].split("</title>")[0]
    assert "nfoform" not in title                                        # the form must be in the page, not the title
    body = page.split("<body", 1)[1]
    assert _re.search(r'<form id="nfoform"[^>]*action="/nfo/all"', body) and 'form="nfoform"' in body
    started = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: started.append(self.name) or self.run())
    r = c.post("/nfo/all")
    assert r.status_code == 302 and "nfo" in started
    assert os.path.exists(os.path.join(db.album_get(aid)["path"], "album.nfo"))
    assert any("NFO files: written for 1 album" in m["message"] for m in db.q("SELECT message FROM log"))


def test_approve_all_with_your_own_minimum(env, monkeypatch):
    music, data, album = env
    from app import db, worker, create_app
    from app.config import Config
    aid = worker.queue_album(str(album)); run_jobs()
    conf = db.album_get(aid)["confidence"]
    pct = round(conf * 10)
    c = create_app(start_background=False).test_client()
    page = c.get("/review").get_data(as_text=True)
    assert 'id="aa-pct"' in page and f'value="{int(round(Config.APPROVE_ALL_MIN * 10))}"' in page and str(pct) in page.split('data-confs="')[1][:20]
    c.post("/bulk", data={"action": "approve_confident", "threshold": str((pct + 1) / 10)})
    assert db.album_get(aid)["status"] == "review"                       # just above it: untouched
    c.post("/bulk", data={"action": "approve_confident", "threshold": "1"})
    st = db.album_get(aid)["status"]
    assert st == ("applying" if pct >= 50 else "review")                 # never below 50 %, however low you type
    monkeypatch.setattr(Config, "APPROVE_ALL_MIN", 9.0)
    assert 'value="90"' in c.get("/review").get_data(as_text=True)
    assert "Approve all" in c.get("/settings?tab=matching").get_data(as_text=True)


def test_track_names_check_finds_swapped_and_wrong_titles(env, monkeypatch):
    music, data, album = env
    from app import db, trackcheck as TC, create_app
    folder = str(music / "Check" / "Kadhalan")
    os.makedirs(folder, exist_ok=True)
    aid = db.album_upsert(folder, "applied")
    songs_ = [  # (original file name, title written, file length, matched song's length)
        ("01 - Ennavale Adi Ennavale.mp3", "Ennavalae Adi Ennavalae", 352, 351),     # right (spelling differs)
        ("02 - Mukkala Mukkabla.mp3", "Pettai Rap", 335, 210),                        # swapped with track 3
        ("03 - Pettai Rap.mp3", "Mukkabla", 210, 335),
        ("04 Track 04.mp3", "Kaadhalikkum Pennin", 290, 291),                         # generic name: length only, fine
        ("05 - Urvasi Urvasi.mp3", "Urvasi Urvasi", 340, 341),                        # right
    ]
    files = []
    for i, (orig, title, d, sd) in enumerate(songs_):
        new = os.path.join(folder, f"{i + 1:02d} - {title}.mp3")
        old = os.path.join(folder, orig)
        open(new, "w").close()
        db.x("INSERT INTO songs(path, folder, title, duration) VALUES(?,?,?,?)", (new, folder, title, d))
        db.x("INSERT INTO backups(album_id,batch,old_path,new_path,backup_file,created) VALUES(?,?,?,?,?,?)",
             (aid, 1, old, new, "x", 0))
        files.append({"path": old, "file": orig, "duration": d, "rename_to": os.path.basename(new),
                      "source_track": {"title": title, "duration": sd}})
    db.album_update(aid, proposal={"files": files})
    assert TC.similar("01 - Ennavale Adi Ennavale.mp3", "Ennavalae Adi Ennavalae") >= 80
    bad = {r["title"]: r for r in TC.check_album(aid)}
    assert set(bad) == {"Pettai Rap", "Mukkabla"}
    assert bad["Pettai Rap"]["severity"] == 3 and any("Mukkala" in x for x in json.loads(bad["Pettai Rap"]["reasons"]))
    assert TC.count() == 2
    c = create_app(start_background=False).test_client()
    page = c.get("/library/tracks").get_data(as_text=True)
    assert "Pettai Rap" in page and "Almost certain" in page and "Track names" in page
    assert "may have another song" in c.get("/").get_data(as_text=True)
    # "It's fine" sticks across re-checks
    c.post("/tracks/act", data={"paths": [bad["Mukkabla"]["path"]], "action": "dismiss"})
    TC.check_album(aid)
    assert TC.count() == 1
    # send to review queues the album again
    c.post("/tracks/act", data={"paths": [bad["Pettai Rap"]["path"]], "action": "review"})
    assert db.q("SELECT COUNT(*) n FROM jobs WHERE album_id=?", (aid,), one=True)["n"] >= 1
    # the AI's verdict is stored per track
    from app import ai
    monkeypatch.setattr(ai, "complete", lambda *a, **k: ({"tracks": [{"id": 0, "verdict": "wrong", "reason": "names swap",
                                                                      "title": "Mukkala Mukkabla"}]}, {}))
    assert TC.ask_ai([bad["Pettai Rap"]["path"]]) == 1
    row = TC.flagged()[0]
    assert row["ai"]["verdict"] == "wrong" and row["ai"]["title"] == "Mukkala Mukkabla"


def test_tamil_spellings_grouped_and_plex_spelling_suggested(env):
    music, data, album = env
    from app import db, artists as AR
    assert AR.sound_key("Ilayaraja") == AR.sound_key("Ilaiyaraaja") == AR.sound_key("Ilaiyaraja")
    assert AR.sound_key("Bharathwaj") == AR.sound_key("Bharadwaj")
    assert AR.sound_key("Deva") != AR.sound_key("Devan")
    for i, (aa, plex) in enumerate([("Ilayaraja", "Ilaiyaraaja"), ("Ilayaraja", "Ilaiyaraaja"), ("Ilayaraja", "Ilaiyaraaja"),
                                    ("Ilaiyaraaja", "Ilaiyaraaja"), ("Bharathwaj", None), ("Bharadwaj", None),
                                    ("Deva", None), ("Devan", None), ("Harris Jayaraj", None)]):
        db.x("INSERT INTO songs(path, folder, albumartist, plex_artist) VALUES(?,?,?,?)",
             (f"/music/x{i}/a.mp3", f"/music/x{i}", aa, plex))
    groups = {frozenset(n["name"] for n in g["names"]): g for g in AR.variant_groups()}
    ila = groups[frozenset({"Ilayaraja", "Ilaiyaraaja"})]
    assert ila["suggested"] == "Ilaiyaraaja"            # what Plex shows, even though Ilayaraja has more songs
    assert frozenset({"Bharathwaj", "Bharadwaj"}) in groups
    assert not any("Devan" in k for k in groups)


def test_folder_policy_keeps_the_stored_plan_in_step(env):
    music, data, album = env
    from app import db, policy as POL, applier, worker
    from app.config import Config
    album = str(album)
    a = db.album_by_path(album) or db.album_get(db.album_upsert(album, "applied"))
    db.album_update(a["id"], status="applied", proposal={"album": {"album": "X", "album_artist": "Someone"},
                    "files": [{"path": os.path.join(album, f), "file": f, "new": {"albumartist": "Someone"}}
                              for f in os.listdir(album) if f.lower().endswith(Config.AUDIO_EXTS)]})
    POL.set_policy(album, "various")
    applier.apply_policy(a["id"])
    prop = db.album_get(a["id"])["proposal"]
    assert prop["album"]["album_artist"] == Config.VARIOUS_ARTISTS
    assert all(f["new"]["albumartist"] == Config.VARIOUS_ARTISTS for f in prop["files"])


def test_reset_analysis_starts_over_without_touching_files(env):
    music, data, album = env
    album = str(album)
    from app import db, policy as POL, create_app, tags as T
    from app.config import Config
    aid = db.album_upsert(album, "applied")
    db.album_update(aid, proposal={"album": {"album": "X"}, "files": []}, overrides={"albumartist": "Someone"},
                    verify={"n_failed": 0})
    db.x("INSERT INTO backups(album_id,batch,old_path,new_path,backup_file,created) VALUES(?,?,?,?,?,?)",
         (aid, 1, "/a", "/a", "x", 0))
    POL.set_policy(album, "various")
    POL.dismiss(["/music/other"])
    files = sorted(f for f in os.listdir(album) if f.lower().endswith(Config.AUDIO_EXTS))
    before = {f: os.path.getmtime(os.path.join(album, f)) for f in files}
    c = create_app(start_background=False).test_client()
    page = c.get("/settings?tab=matching").get_data(as_text=True)
    assert "Reset the tag analysis" in page and "1 folder decision" in page
    c.post("/analysis/reset", data={"scope": "all", "confirm": "nope"})
    assert db.album_get(aid)["status"] == "applied"           # not confirmed: nothing happened
    c.post("/analysis/reset", data={"scope": "all", "confirm": "reset"})
    a = db.album_get(aid)
    assert a["id"] == aid and a["status"] == "queued" and not a.get("proposal") and not a.get("overrides")
    assert not POL.get(album) and not POL.dismissed()
    assert db.q("SELECT COUNT(*) n FROM backups WHERE album_id=?", (aid,), one=True)["n"] == 1   # Undo kept
    job = db.q("SELECT payload FROM jobs WHERE album_id=? AND kind='analyze' AND status='pending'", (aid,), one=True)
    assert json.loads(job["payload"]).get("no_auto")
    assert {f: os.path.getmtime(os.path.join(album, f)) for f in files} == before
    # just some folders, from Library → Folders
    db.album_update(aid, status="applied")
    POL.set_policy(album, "single")
    from app import songs
    songs.index_all()
    c.post("/analysis/reset", data={"folders": [album]})
    assert db.album_get(aid)["status"] == "queued" and not POL.get(album)


def test_pages_have_no_duplicate_ids_and_reset_button_reaches_its_form(env):
    import re
    from collections import Counter
    from app import create_app, settings as S
    c = create_app(start_background=False).test_client()
    pages = ["/", "/review", "/library", "/library/folders", "/library/artists", "/library/decisions", "/library/tracks",
             "/duplicates", "/playlists", "/activity"] + [f"/settings?tab={t}" for t in
                                                          ("tagging", "kinds", "renaming", "sources", "lyrics", "matching",
                                                           "duplicates", "auto", "plex", "discord", "ai", "security", "appearance", "system")]
    for url in pages:
        r = c.get(url)
        if r.status_code != 200:
            continue
        html = r.get_data(as_text=True)
        dup = [k for k, n in Counter(re.findall(r'\sid="([^"{}]+)"', html)).items() if n > 1]
        assert not dup, f"{url}: duplicate id(s) {dup}"
        for fid in set(re.findall(r'\sform="([^"]+)"', html)):          # every form="x" must name a real form
            assert re.search(rf'<form[^>]*\sid="{re.escape(fid)}"', html), f"{url}: form={fid} has no form"
    html = c.get("/settings?tab=matching").get_data(as_text=True)
    fid = re.search(r'<button[^>]*form="([^"]+)"[^>]*>Reset and re-check', html).group(1)
    assert re.search(rf'<form id="{fid}"[^>]*action="/analysis/reset"', html)



# ====================================================================== 3.6: Deezer, TMDB, Shazam

DZ_ALBUM = {"id": 9001, "title": "Thiruda Thiruda", "link": "https://www.deezer.com/album/9001",
            "cover_xl": "https://cdn-images.dzcdn.net/images/cover/x/1000x1000.jpg",
            "cover_medium": "https://cdn-images.dzcdn.net/images/cover/x/250x250.jpg",
            "genres": {"data": [{"name": "Bollywood"}]}, "label": "Pyramid", "nb_tracks": 3,
            "release_date": "1993-11-13", "record_type": "album", "artist": {"name": "A.R. Rahman"},
            "tracks": {"data": []}}
DZ_TRACKS = {"data": [
    {"id": 71, "title": "Konjam Nilavu", "duration": 271, "track_position": 1, "disk_number": 1,
     "isrc": "INP000001", "artist": {"name": "A.R. Rahman"}},
    {"id": 72, "title": "Kannum Kannum", "duration": 305, "track_position": 2, "disk_number": 1,
     "artist": {"name": "A.R. Rahman"}},
    {"id": 73, "title": "Theme of Thiruda", "duration": 120, "track_position": 1, "disk_number": 2,
     "artist": {"name": "A.R. Rahman"}},
], "total": 3}


def deezer_album_fixtures():
    DEEZER.update({
        "search:thiruda": {"data": [{"id": 9001, "title": "Thiruda Thiruda", "nb_tracks": 3,
                                     "artist": {"name": "A.R. Rahman"}, "link": "https://www.deezer.com/album/9001",
                                     "cover_xl": DZ_ALBUM["cover_xl"], "cover_medium": DZ_ALBUM["cover_medium"]}]},
        "/album/9001": DZ_ALBUM, "/album/9001/tracks": DZ_TRACKS,
        "/track/71": {"id": 71, "isrc": "INP000001", "contributors": [
            {"name": "A. R. Rahman", "role": "Main"}, {"name": "Anupama", "role": "Main"},
            {"name": "Suresh Peters", "role": "Featured"}]},
        "/track/72": {"id": 72, "contributors": [{"name": "A.R. Rahman", "role": "Main"}]},
    })


def test_deezer_album_source(env):
    music, data, album = env
    from app import db, matcher, worker
    from app.sources import deezer as DZ
    deezer_album_fixtures()
    alb, tr = DZ.lookup_album(9001)
    assert alb["ref"] == "deezer:9001" and alb["album_artist"] == "A.R. Rahman" and alb["date"] == "1993"
    assert alb["artwork"].endswith("1000x1000.jpg") and alb["track_count"] == 3
    assert [(t["discnumber"], t["tracknumber"], t["tracktotal"], t["disctotal"]) for t in tr] == [
        (1, 1, 2, 2), (1, 2, 2, 2), (2, 1, 1, 2)]
    assert tr[0]["artist"] == ""                       # the track artist is just the music director
    assert DZ.parse_ref("https://www.deezer.com/en/album/9001") == "9001"
    assert matcher.parse_ref("https://www.deezer.com/album/9001") == "deezer:9001"
    assert matcher.parse_ref("deezer:9001") == "deezer:9001"

    # no other source knows this album: Deezer, fifth in line, matches it
    folder = music / "Rahman" / "Thiruda Thiruda"
    make_audio(str(folder / "konjam nilavu.mp3"), 271, {"title": "Konjam Nilavu", "artist": "Anupama"})
    make_audio(str(folder / "kannum kannum.mp3"), 305, {"title": "Kannum Kannum", "artist": "Swarnalatha"})
    make_audio(str(folder / "theme.flac"), 120, {"title": "Theme of Thiruda"})
    aid = worker.queue_album(str(folder)); run_jobs()
    p = db.album_get(aid)["proposal"]
    assert p["album"]["source"] == "deezer", (p["album"], p["notes"])
    by = {f["file"]: f for f in p["files"]}
    k = by["konjam nilavu.mp3"]["new"]
    assert k["albumartist"] == "A.R. Rahman" and k["tracknumber"] == "1" and k["discnumber"] == "1"
    assert k["artist"] == "Anupama, Suresh Peters"     # singers from the song credits, music director left out
    assert "artist" not in by["kannum kannum.mp3"]["changes"]      # Deezer names no singer: the file's stays
    assert by["theme.flac"]["new"]["discnumber"] == "2" and by["theme.flac"]["new"]["disctotal"] == "2"
    assert any("names no singers" in n for n in p["notes"])
    assert p["album"]["artwork"].endswith("1000x1000.jpg")


def test_deezer_quota_reply_is_retried_not_cached(env, monkeypatch):
    from app import db
    from app.sources import deezer as DZ, http
    monkeypatch.setattr(http.time, "sleep", lambda s: None)
    replies = [{"error": {"type": "Exception", "message": "Quota limit exceeded", "code": 4}},
               {"data": [{"id": 5, "title": "Roja", "nb_tracks": 6, "artist": {"name": "A.R. Rahman"}}]}]
    import requests
    monkeypatch.setattr(requests, "get", lambda url, params=None, headers=None, timeout=None: Resp(replies.pop(0)))
    out = DZ.search_albums("Roja quota")
    assert [a["album"] for a in out] == ["Roja"] and not replies
    # a "no data" reply is an answer (not found), but it isn't cached either
    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp({"error": {"message": "no data", "code": 800}}))
    assert DZ.lookup_album(404404) == (None, [])
    assert not db.q("SELECT 1 FROM http_cache WHERE key LIKE 'deezer:%' AND value LIKE '%no data%'")


def test_new_source_is_switched_on_once(env, monkeypatch, tmp_path):
    music, data, album = env
    from app import settings as S, create_app
    from app.config import Config
    # a 3.5 install: the source list saved in the app, without Deezer
    os.makedirs(data, exist_ok=True)
    with open(data / "settings.json", "w") as fh:
        json.dump({"SOURCES": ["jiosaavn", "itunes", "wikipedia"]}, fh)
    Config.reload()
    assert Config.SOURCES == ["jiosaavn", "itunes", "wikipedia", "deezer"]     # yours, then the new one
    # you switch Deezer off in Settings: that stands from now on
    c = create_app(start_background=False).test_client()
    c.post("/settings", data={"section": "sources", "src-jiosaavn": "on", "order-jiosaavn": "1",
                              "src-itunes": "on", "order-itunes": "2"})
    assert Config.SOURCES == ["jiosaavn", "itunes"]
    Config.reload()
    assert Config.SOURCES == ["jiosaavn", "itunes"]
    # a list from the compose file also gets the new source until you decide in Settings
    os.remove(data / "settings.json")
    monkeypatch.setenv("SOURCES", "itunes,jiosaavn")
    Config.reload()
    assert Config.SOURCES == ["itunes", "jiosaavn", "deezer"]
    html = c.get("/settings?tab=sources").get_data(as_text=True)
    assert 'name="src-deezer" role="switch" checked' in html
    assert "Music director check (TMDB)" in html and "not endorsed or certified by TMDB" in html
    assert "Identify songs by their sound (Shazam)" in html


def test_tmdb_names_and_film_choice(env, monkeypatch):
    from app.config import Config
    from app.sources import tmdb as T
    assert T.same_person("A.R. Rahman", "A. R. Rahman")
    assert T.same_person("Ilayaraja", "Ilaiyaraaja")
    assert T.same_person("Harris Jeyaraj", "Harris Jayaraj")
    assert T.same_person("Yuvan Shankar Raja", "Yuvan Shanker Raja")
    assert T.matches_any("Viswanathan–Ramamoorthy", ["M. S. Viswanathan"])
    assert T.matches_any("Ilaiyaraaja (songs)", ["Ilaiyaraaja"])
    assert not T.same_person("Anirudh Ravichander", "Ilaiyaraaja")
    assert not T.same_person("Deva", "Vidyasagar")
    monkeypatch.setattr(Config, "TMDB_API_KEY", "k3y")
    TMDB.update({
        "search:vikram": {"results": [
            {"id": 1, "title": "Vikram", "original_title": "விக்ரம்", "release_date": "2022-06-03", "original_language": "ta", "popularity": 50},
            {"id": 2, "title": "Vikram", "original_title": "விக்ரம்", "release_date": "1986-05-29", "original_language": "ta", "popularity": 5},
            {"id": 3, "title": "Vikram Vedha", "release_date": "2022-09-30", "original_language": "hi"}]},
        "/movie/1/credits": {"crew": [{"name": "Anirudh Ravichander", "job": "Original Music Composer"},
                                      {"name": "Lokesh Kanagaraj", "job": "Director"}]},
        "/movie/2/credits": {"crew": [{"name": "Ilaiyaraaja", "job": "Music"}]},
        "search:roja": {"results": [{"id": 9, "title": "Roja", "release_date": "1992-08-15", "original_language": "ta"}]},
        "/movie/9/credits": {"crew": [{"name": "A. R. Rahman", "job": "Original Music Composer"}]},
    })
    r = T.check("Vikram (Original Motion Picture Soundtrack)", "2022", "Anirudh Ravichander", "Tamil")
    assert r["status"] == "agree" and r["film"] == "Vikram" and r["year"] == "2022" and r["url"].endswith("/1")
    # a 1986 album re-issued with the year 2022: the other Vikram credits him, so it agrees
    r = T.check("Vikram", "2022", "Ilaiyaraaja", "Tamil")
    assert r["status"] == "agree" and r["year"] == "1986"
    # someone neither film credits: different, and we know which film (the year fits)
    r = T.check("Vikram", "2022", "Harris Jayaraj", "Tamil")
    assert r["status"] == "differ" and r["composers"] == ["Anirudh Ravichander"]
    # two films of that name and no year: not sure which, so it isn't flagged
    assert T.check("Vikram", "", "Harris Jayaraj", "Tamil")["status"] == "unsure"
    assert T.check("Roja", "2016", "Ilaiyaraaja", "Tamil")["status"] == "differ"   # only one Roja: re-issue year ignored
    assert T.check("Unknown Film", "2001", "Deva", "Tamil")["status"] == "nofilm"
    # the v3 key goes in the query; a v4 read access token in the header instead
    assert HEADERS_SEEN["tmdb"]["params"]["api_key"] == "k3y"
    monkeypatch.setattr(Config, "TMDB_API_KEY", "eyJhbGciOi.token")
    T.check("Roja", "1992", "A. R. Rahman", "Tamil")
    assert "api_key" not in HEADERS_SEEN["tmdb"]["params"]
    assert HEADERS_SEEN["tmdb"]["headers"]["Authorization"] == "Bearer eyJhbGciOi.token"


def test_tmdb_check_in_review(env, monkeypatch):
    music, data, album = env
    from app import create_app, db, worker
    from app.config import Config
    monkeypatch.setattr(Config, "TMDB_API_KEY", "k3y")
    TMDB.update({"search:vikram": {"results": [{"id": 1, "title": "Vikram", "release_date": "2022-06-03",
                                                "original_language": "ta"}]},
                 "/movie/1/credits": {"crew": [{"name": "Anirudh Ravichander", "job": "Original Music Composer"}]}})
    aid = worker.queue_album(str(album)); run_jobs()
    p = db.album_get(aid)["proposal"]
    assert p["md_check"]["status"] == "agree", p.get("md_check")
    c = create_app(start_background=False).test_client()
    from app import songs
    songs.index_all()
    html = c.get(f"/album/{aid}").get_data(as_text=True)
    assert "TMDB agrees: Anirudh Ravichander composed" in html
    assert "TMDB confirms the music director" in html                 # the Kind of album suggestion says so

    # TMDB names someone else: shown with a button, held from auto-approve and "Approve all", on Home's list
    TMDB["/movie/1/credits"] = {"crew": [{"name": "Ilaiyaraaja", "job": "Music Director"}]}
    db.q("DELETE FROM http_cache WHERE key LIKE 'tmdb:%'")
    db.x("DELETE FROM http_cache WHERE key LIKE 'tmdb:%'")
    monkeypatch.setattr(Config, "AUTO_APPROVE", "on")
    monkeypatch.setattr(Config, "AUTO_APPROVE_SCOPE", "all")
    monkeypatch.setattr(Config, "AUTO_APPROVE_MIN", 0.0)
    monkeypatch.setattr(Config, "AUTO_APPROVE_TRACK_MIN", 0.0)
    monkeypatch.setattr(Config, "AUTO_APPROVE_ALL_MATCHED", False)
    worker.queue_album(str(album)); run_jobs()
    a = db.album_get(aid)
    p = a["proposal"]
    assert p["md_check"]["status"] == "differ" and a["status"] == "review"
    assert "TMDB lists Ilaiyaraaja" in p["auto"]["why"]
    html = c.get(f"/album/{aid}").get_data(as_text=True)
    assert "TMDB lists <b>Ilaiyaraaja</b>" in html and 'data-name="Ilaiyaraaja"' in html
    assert "TMDB lists <b>Ilaiyaraaja</b>" in c.get(f"/review?a={aid}").get_data(as_text=True)
    assert db.albums_count(["review", "nomatch", "error"], flt="tmdb") == 1
    assert "TMDB names a different music director" in c.get("/").get_data(as_text=True)
    c.post("/bulk", data={"action": "approve_confident", "threshold": "5"})
    assert db.album_get(aid)["status"] == "review"
    # once you type the album artist yourself, the check stays out of the way
    worker.queue_album(str(album), ref=None)
    db.album_update(aid, overrides={"albumartist": "Anirudh Ravichander"})
    worker.queue_album(str(album)); run_jobs()
    assert "md_check" not in db.album_get(aid)["proposal"]


def test_tmdb_errors_hide_the_key(env, monkeypatch):
    from app.config import Config
    from app.sources import http, tmdb as T
    from app.sources.http import SourceError
    import requests
    monkeypatch.setattr(Config, "TMDB_API_KEY", "s3cretkey")
    monkeypatch.setattr(http.time, "sleep", lambda s: None)

    def boom(url, params=None, headers=None, timeout=None):
        raise requests.ConnectionError(f"failed {url}?api_key={params.get('api_key')}")
    monkeypatch.setattr(requests, "get", boom)
    with pytest.raises(SourceError) as e:
        T.check("Roja", "1992", "A. R. Rahman", "Tamil")
    assert "s3cretkey" not in str(e.value) and "***" in str(e.value)
    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp(status=401))
    with pytest.raises(SourceError) as e:
        T.check("Roja", "1992", "A. R. Rahman", "Tamil")
    assert "refused" in str(e.value)


def shazam_answer(title, artist, album, apple_id="", isrc=""):
    return {"matches": [{"id": "1"}], "track": {
        "key": "555", "title": title, "subtitle": artist, "isrc": isrc, "url": "https://www.shazam.com/track/555",
        "images": {"coverart": "https://is1.mzstatic.com/c/400x400bb.jpg"},
        "hub": {"actions": [{"name": "apple", "type": "applemusicplay", "id": apple_id}] if apple_id else []},
        "sections": [{"type": "SONG", "metadata": [{"title": "Album", "text": album},
                                                   {"title": "Label", "text": "Sony Music"},
                                                   {"title": "Released", "text": "2022"}]}]}}


def test_shazam_parse():
    from app.sources import shazam as Z
    h = Z.parse(shazam_answer("Wasted", "Anirudh Ravichander", "Vikram", "1626", "INS1"))
    assert h["title"] == "Wasted" and h["album"] == "Vikram" and h["apple_id"] == "1626"
    assert h["isrc"] == "INS1" and h["year"] == "2022" and h["label"] == "Sony Music"
    assert Z.parse({"matches": [], "timestamp": 1}) is None


def test_shazam_finds_a_junk_named_song_of_the_album(env, monkeypatch):
    music, data, album = env
    global NO_WIKI
    from app import create_app, db, worker
    from app.config import Config
    # a file the album match already places by its length doesn't need Shazam
    folder = music / "Anirudh" / "Vikram 2"
    make_audio(str(folder / "01 pathala.flac"), 20, {"title": "Pathala Pathala"})
    make_audio(str(folder / "AUD-20220601-WA0007.mp3"), 25)        # really "Vikram Title Track"
    make_audio(str(folder / "porkanda singam.m4a"), 31)
    aid = worker.queue_album(str(folder)); run_jobs()
    assert SHAZAM_CALLS == []

    # an LP on Discogs without song lengths: a junk-named file can only be placed by what Shazam heard
    folder = music / "Ilaiyaraaja" / "Punnagai Mannan"
    make_audio(str(folder / "Ennapadi.mp3"), 271)
    make_audio(str(folder / "Kavithai Kelungal.mp3"), 305)
    make_audio(str(folder / "REC_0003.mp3"), 280)                   # really "Vaan Megam" (B1, no length)
    SHAZAM["REC_0003.mp3"] = shazam_answer("Vaan Megam", "K. S. Chithra", "Punnagai Mannan")
    monkeypatch.setattr(Config, "DISCOGS_TOKEN", "tok")
    NO_WIKI = True
    try:
        aid = worker.queue_album(str(folder)); run_jobs()
    finally:
        NO_WIKI = False
    a = db.album_get(aid)
    p = a["proposal"]
    assert p["album"]["source"] == "discogs", p["notes"]
    f = {x["file"]: x for x in p["files"]}["REC_0003.mp3"]
    assert f["match"] == "album" and f["new"]["title"] == "Vaan Megam" and f["new"]["tracknumber"] == "3"
    assert f["new"]["artist"] == "K.S. Chithra"                     # singers from the album, not from Shazam
    assert f["source_track"]["via"] == "shazam" and f["sound"]["title"] == "Vaan Megam"
    assert f["rename_to"] == "03 - Vaan Megam.mp3"
    assert SHAZAM_CALLS == ["REC_0003.mp3"]                        # only the file no title search found
    assert any("identified by their sound" in n for n in p["notes"])
    c = create_app(start_background=False).test_client()
    assert ">by sound</span>" in c.get(f"/album/{aid}").get_data(as_text=True)
    assert "identified by their sound (Shazam)" in c.get(f"/review?a={aid}").get_data(as_text=True)
    assert db.albums_count(["review"], flt="sound") == 1
    # a re-check doesn't ask Shazam again
    NO_WIKI = True
    try:
        worker.queue_album(str(folder)); run_jobs()
    finally:
        NO_WIKI = False
    assert SHAZAM_CALLS == ["REC_0003.mp3"]


def test_shazam_names_the_album_of_a_junk_folder(env):
    music, data, album = env
    from app import db, worker
    from app.config import Config
    folder = music / "New folder (3)"
    make_audio(str(folder / "track1.mp3"), 20)
    make_audio(str(folder / "track2.mp3"), 22)
    SHAZAM["track1.mp3"] = shazam_answer("Naatham Ennum", "Ilaiyaraaja", "Salangai Oli", "3331")
    SHAZAM["track2.mp3"] = shazam_answer("Om Namasivaya", "S. Janaki", "Salangai Oli", "3332")
    SOUND_SAAVN["naatham ennum"] = saavn_song(1, "Naatham Ennum", 20, sid="sn1")
    SOUND_SAAVN["om namasivaya"] = saavn_song(2, "Om Namasivaya", 22, sid="sn2")
    FLAGS["itunes_strict"] = True
    aid = worker.queue_album(str(folder)); run_jobs()
    a = db.album_get(aid)
    p = a["proposal"]
    # Shazam said "Salangai Oli" for both, so the album was searched by that name and matched
    assert not p["fallback"], p["notes"]
    assert "Salangai Oli" in p["album"]["album"] and p["album"]["album_artist"] == "Ilaiyaraaja"
    assert any("Album found from songs identified by their sound" in n for n in p["notes"])
    by = {f["file"]: f for f in p["files"]}
    assert by["track1.mp3"]["new"]["title"] == "Naatham Ennum" and by["track1.mp3"]["match"] == "album"
    assert by["track2.mp3"]["new"]["tracknumber"] == "2"
    assert all(f["source_track"]["via"] == "shazam" for f in p["files"])
    assert SHAZAM_CALLS.count("track1.mp3") == 1                    # the second pass used the cached answer
    # identified by sound: never auto-approved
    monkeypatch_auto = {"AUTO_APPROVE": "on", "AUTO_APPROVE_SCOPE": "all", "AUTO_APPROVE_MIN": 0.0,
                        "AUTO_APPROVE_TRACK_MIN": 0.0}
    old = {k: getattr(Config, k) for k in monkeypatch_auto}
    try:
        for k, v in monkeypatch_auto.items():
            setattr(Config, k, v)
        ok, why = worker.auto_decision(a, p, {})
        assert not ok and "identified by their sound" in why
    finally:
        for k, v in old.items():
            setattr(Config, k, v)


def test_shazam_catalogue_lookup_limit_and_errors(env, monkeypatch):
    music, data, album = env
    from app import db, worker, matcher
    from app.config import Config
    from app.sources import shazam as Z
    # a song from another film: found in the catalogue by what Shazam heard (iTunes by its Apple id)
    ITUNES_SONGS[4242] = {"wrapperType": "track", "kind": "song", "trackId": 4242, "trackName": "Kaadhal Rojave",
                          "artistName": "S. P. Balasubrahmanyam & Sujatha", "collectionName": "Roja",
                          "trackNumber": 3, "trackCount": 6, "discNumber": 1, "discCount": 1,
                          "trackTimeMillis": 33000, "primaryGenreName": "Tamil", "releaseDate": "1992-08-15T07:00:00Z"}
    make_audio(str(album / "zz unknown.mp3"), 33)
    SHAZAM["zz unknown.mp3"] = shazam_answer("Kaadhal Rojave", "A.R. Rahman", "Roja", "4242")
    aid = worker.queue_album(str(album)); run_jobs()
    f = {x["file"]: x for x in db.album_get(aid)["proposal"]["files"]}["zz unknown.mp3"]
    assert f["match"] == "track" and f["source_track"]["album"] == "Roja" and f["source_track"]["via"] == "shazam"
    assert f["new"]["title"] == "Kaadhal Rojave" and f["new"]["artist"] == "S. P. Balasubrahmanyam & Sujatha"

    # Shazam's own details only (no catalogue has it): the title, but the file's singers stay
    folder = music / "Misc"
    make_audio(str(folder / "x1.mp3"), 21, {"artist": "Unni Menon"})
    make_audio(str(folder / "x2.mp3"), 23)
    make_audio(str(folder / "x3.mp3"), 24)
    SHAZAM["x1.mp3"] = shazam_answer("Some Rare Song", "Somebody", "Rare Film")
    monkeypatch.setattr(Config, "SHAZAM_MAX_PER_ALBUM", 2)
    aid = worker.queue_album(str(folder)); run_jobs()
    p = db.album_get(aid)["proposal"]
    by = {x["file"]: x for x in p["files"]}
    assert by["x1.mp3"]["new"]["title"] == "Some Rare Song" and "artist" not in by["x1.mp3"]["changes"]
    assert by["x1.mp3"]["score"] < 7.5                               # weak: Shazam alone
    assert SHAZAM_CALLS.count("x3.mp3") == 0                         # limit of 2 per album
    assert any("not sent to Shazam (limit of 2" in n for n in p["notes"])
    assert any("didn't recognise 1 song" in n for n in p["notes"])

    # a failure stops Shazam for the rest of that album; a refusal (429) pauses it for 15 minutes
    def broken(path):
        SHAZAM_CALLS.append(os.path.basename(path))
        raise ConnectionError("no route to host")
    monkeypatch.setattr(Z, "_recognize", broken)
    monkeypatch.setattr(Config, "SHAZAM_MAX_PER_ALBUM", 0)
    folder2 = music / "Misc2"
    for i in range(3):
        make_audio(str(folder2 / f"y{i}.mp3"), 21 + i)
    SHAZAM_CALLS.clear()
    aid = worker.queue_album(str(folder2)); run_jobs()
    p = db.album_get(aid)["proposal"]
    assert len(SHAZAM_CALLS) == 1 and any("no route to host" in n for n in p["notes"])
    assert not db.q("SELECT 1 FROM http_cache WHERE key LIKE 'shazam-miss:%y0.mp3%'")   # errors aren't misses

    class RateLimited(Exception):
        pass

    def limited(path):
        raise RateLimited("rate limited the request")
    monkeypatch.setattr(Z, "_recognize", limited)
    with pytest.raises(matcher.SourceError):
        Z.identify(str(folder2 / "y1.mp3"), 22)
    assert Z.identify(str(folder2 / "y2.mp3"), 23)[1].startswith("Shazam is refusing")
    Z._blocked["until"] = 0
    # switched off: never asked
    monkeypatch.setattr(Config, "SHAZAM_ENABLED", False)
    monkeypatch.setattr(Z, "_recognize", broken)
    SHAZAM_CALLS.clear()
    worker.queue_album(str(folder2)); run_jobs()
    assert SHAZAM_CALLS == []



# ====================================================================== 3.7: release date, label, Plex extras

def test_release_date_and_label_written(env, monkeypatch):
    music, data, album = env
    from app import create_app, db, tags, worker, matcher
    from app.config import Config
    monkeypatch.setitem(ITUNES_SEARCH["results"][0], "copyright", "℗ 2022 Sony Music Entertainment India Pvt. Ltd.")
    monkeypatch.setattr(Config, "WRITE_NFO", True)
    aid = worker.queue_album(str(album)); run_jobs()
    p = db.album_get(aid)["proposal"]
    assert p["album"]["release_date"] == "2022-05-15" and p["album"]["label"] == "Sony Music Entertainment India Pvt. Ltd."
    f = p["files"][0]
    assert f["new"]["date"] == "2022-05-15" and f["new"]["label"] == "Sony Music Entertainment India Pvt. Ltd."
    c = create_app(start_background=False).test_client()
    html = c.get(f"/album/{aid}").get_data(as_text=True)
    assert 'name="a-date" value="2022-05-15"' in html and 'name="a-label" value="Sony Music Entertainment India Pvt. Ltd."' in html
    review = c.get(f"/review?a={aid}").get_data(as_text=True)
    assert "Release date" in review and "Record label" in review
    c.post(f"/album/{aid}/approve"); run_jobs()
    a = db.album_get(aid)
    assert a["status"] == "applied" and a["verify"]["n_failed"] == 0, a["verify"]
    for n in os.listdir(album):
        if n.endswith((".lrc", ".nfo")):
            continue
        fl = tags.read(str(album / n))["fields"]
        assert fl["date"] == "2022-05-15" and fl["label"] == "Sony Music Entertainment India Pvt. Ltd.", (n, fl)
    nfo = open(album / "album.nfo", encoding="utf-8").read()
    assert "<releasedate>2022-05-15</releasedate>" in nfo and "<label>Sony Music Entertainment India Pvt. Ltd.</label>" in nfo
    worker.queue_album(str(album)); run_jobs()
    assert db.album_get(aid)["status"] == "uptodate"

    # a full date in the file is never cut back to a year; a year grows into the full date as a fill
    assert matcher.compute_changes({"date": "2022-05-15"}, {"date": "2022"}) == {}
    assert matcher.compute_changes({"date": "2022"}, {"date": "2022-05-15"}) == {"date": "fill"}
    monkeypatch.setattr(Config, "OVERWRITE_MODE", "fill")
    assert matcher.compute_changes({"date": "2022"}, {"date": "2022-05-15"}) == {"date": "fill"}
    assert matcher.compute_changes({"date": "1986"}, {"date": "2022-05-15"}) == {}       # fill mode: a different year stays
    monkeypatch.setattr(Config, "OVERWRITE_MODE", "fix")
    assert matcher.compute_changes({"date": "1986"}, {"date": "2022-05-15"}) == {"date": "fix"}

    # a streaming re-issue date isn't the film's: it's used only when its year is the album's
    al = {"date": "1983"}
    matcher._release_and_label(al, {"release_date": "2016-03-10", "label": "Saregama"}, [], [], [])
    assert al["release_date"] == "" and al["label"] == "Saregama"
    al = {"date": "1983"}
    matcher._release_and_label(al, {"release_date": "2016-03-10"}, [{"release_date": "1983-06-03", "source": "deezer"}], [], [])
    assert al["release_date"] == "1983-06-03"


WIKI_VIKRAM_SOUNDTRACK = """{{Infobox album
| name = Vikram
| type = soundtrack
| artist = [[Anirudh Ravichander]]
| released = {{Start date|2022|5|15}}
| label = [[Sony Music India]]
}}
'''Vikram''' is the soundtrack album to the 2022 film of the same name, composed by [[Anirudh Ravichander]].<ref>x</ref> The album features five songs written by [[Vishnu Edavan]] and others, and was released on 15 May 2022.

The soundtrack received positive reviews, with critics praising the background score and the song "Pathala Pathala" in particular.
== Track listing ==
{{Track listing
| title1 = Pathala Pathala
}}
"""


def plex_fake(monkeypatch, state):
    """A Plex that knows one album (key 501) and records every edit."""
    import requests
    import xml.etree.ElementTree as ET
    from app import plex
    from app.config import Config
    monkeypatch.setattr(plex, "resolve", lambda d: ("7", d, None))
    monkeypatch.setattr(plex, "album_keys", lambda d: ["501"])

    class R:
        def __init__(self, content):
            self.content, self.status_code = content.encode(), 200

        def raise_for_status(self):
            pass

    def req(method, url, params=None, timeout=None, headers=None):
        u, params = url.replace(Config.PLEX_URL, ""), dict(params or {})
        params.pop("X-Plex-Token", None)
        if method == "PUT":
            state["puts"].append(params)
            for k, v in params.items():          # apply the edit, the way Plex does
                if k.endswith("[].tag.tag-"):
                    field = k.split("[")[0]
                    state[field] = [t for t in state[field] if t not in v.split(",")]
                elif k.endswith(".tag.tag"):
                    field = k.split("[")[0]
                    state.setdefault("_new_" + field, []).append(v)
                elif k == "summary.value":
                    state["summary"] = v
            for field in ("collection", "mood"):
                if "_new_" + field in state:
                    state[field] = state.pop("_new_" + field)
            return R("")
        if u == "/library/metadata/501":
            d = ET.Element("Directory", ratingKey="501", type="album", title="Vikram", summary=state["summary"])
            for c in state["collection"]:
                ET.SubElement(d, "Collection", tag=c)
            for m in state["mood"]:
                ET.SubElement(d, "Mood", tag=m)
            return R("<MediaContainer>" + ET.tostring(d, encoding="unicode") + "</MediaContainer>")
        return R("<MediaContainer/>")
    monkeypatch.setattr(requests, "request", req)


def test_plex_collections_review_and_moods(env, monkeypatch):
    music, data, album = env
    from app import create_app, db, plex, plexmeta, worker
    from app.config import Config
    WIKI_EXTRA["Vikram (soundtrack)"] = WIKI_VIKRAM_SOUNDTRACK
    monkeypatch.setattr(Config, "WRITE_NFO", True)
    aid = worker.queue_album(str(album)); run_jobs()
    p = db.album_get(aid)["proposal"]
    r = p["review"]                                           # found during the analysis, shown before writing
    assert r and r["title"] == "Vikram (soundtrack)" and r["text"].startswith("Vikram is the soundtrack album"), r
    assert "positive reviews" in r["text"] and "<ref" not in r["text"] and "Track listing" not in r["text"]
    c = create_app(start_background=False).test_client()
    assert "from Wikipedia, “Vikram (soundtrack)”" in c.get(f"/album/{aid}").get_data(as_text=True)
    c.post(f"/album/{aid}/approve"); run_jobs()
    rows = db.q("SELECT id FROM songs WHERE folder=? ORDER BY path", (str(album),))
    profiles = [{"moods": ["energetic", "romantic"]}, {"moods": ["energetic", "happy"]}, {"moods": ["energetic", "romantic"]}]
    for row, prof in zip(rows, profiles):
        db.x("UPDATE songs SET profile=? WHERE id=?", (json.dumps(prof), row["id"]))

    w = plexmeta.wanted(str(album))
    assert w["collections"] == ["Film soundtracks", "2020s"]
    assert w["moods"] == ["Energetic", "Romantic"]                 # 3 of 3 and 2 of 3 songs; "happy" only 1

    # Plex has a collection and a mood you added yourself, and no review yet
    state = {"collection": ["My favourites"], "mood": ["Sentimental"], "summary": "", "puts": []}
    plex_fake(monkeypatch, state)
    assert plexmeta.push(str(album)).startswith("updated")
    assert state["collection"] == ["My favourites", "Film soundtracks", "2020s"]
    assert state["mood"] == ["Sentimental", "Energetic", "Romantic"]
    assert state["summary"].startswith("Vikram is the soundtrack album") and "CC BY-SA 4.0" in state["summary"]
    put = state["puts"][0]
    assert put["type"] == 9 and put["id"] == "501" and put["collection.locked"] == 1 and put["summary.locked"] == 1
    state["puts"].clear()
    assert plexmeta.push(str(album)) == "unchanged" and not state["puts"]

    # the album moves decade, and someone edits the review in Plex: ours goes, theirs stays
    state["collection"].append("1980s")
    state["summary"] = "My own words about this album."
    monkeypatch.setattr(Config, "PLEX_MOODS", False)
    assert plexmeta.push(str(album), force=True).startswith("updated")
    assert "1980s" not in state["collection"] and "My favourites" in state["collection"]
    assert state["summary"] == "My own words about this album."
    assert state["mood"] == ["Sentimental"]                       # moods switched off: ours are taken back
    # the album page says what goes to Plex; the NFO carries the review and moods too
    html = c.get(f"/album/{aid}").get_data(as_text=True)
    assert ">In Plex</h2>" in html and ">Film soundtracks</span>" in html
    monkeypatch.setattr(Config, "PLEX_MOODS", True)
    from app import nfo
    nfo.write_album(str(album))
    x = open(album / "album.nfo", encoding="utf-8").read()
    assert "<review>Vikram is the soundtrack album" in x and "<mood>Energetic</mood>" in x

    # after a write, the album's Plex refresh sets them (the fields Isaiarr unlocks don't include these)
    state.update(collection=[], mood=[], summary="", puts=[])
    db.album_update(aid, plex_meta=None)
    monkeypatch.setattr(plex, "_later", lambda *a, **k: None)
    plex._refresh_albums(str(album))
    assert "Film soundtracks" in state["collection"] and state["summary"].startswith("Vikram")
    unlocks = [p for p in state["puts"] if any(k.endswith(".locked") and v == 0 for k, v in p.items())]
    assert unlocks and not any(k.startswith(("summary", "collection", "mood")) for p in unlocks for k in p)

    # Settings → Plex: the button runs it for every tagged album in the background
    r = c.post("/plex/meta/all")
    assert r.status_code == 302
    for _ in range(50):
        if not plexmeta.status()["running"]:
            break
        time.sleep(0.1)
    assert plexmeta.status()["done"] >= 1
    html = c.get("/settings?tab=plex").get_data(as_text=True)
    assert "Collections by kind of album" in html and "Album review from Wikipedia" in html

    # everything switched off: what Isaiarr set is taken back out of Plex once, then nothing more
    state.update(collection=["My favourites", "Film soundtracks", "2020s"], mood=["Energetic"], puts=[])
    state["summary"] = plexmeta._review_for_plex(plexmeta.wanted(str(album))["review"])
    db.album_update(aid, plex_meta=json.dumps({"sig": "x", "keys": ["501"], "collections": ["Film soundtracks", "2020s"],
                                               "moods": ["Energetic"], "summary": plexmeta._hash(state["summary"])}))
    for k in ("PLEX_COLLECTIONS_KIND", "PLEX_COLLECTIONS_DECADE", "PLEX_REVIEW", "PLEX_MOODS"):
        monkeypatch.setattr(Config, k, False)
    assert plexmeta.push(str(album)).startswith("updated")
    assert state["collection"] == ["My favourites"] and state["mood"] == [] and state["summary"] == ""
    assert plexmeta.push(str(album)) == "off"


def test_wikipedia_review_needs_the_right_film(env):
    from app.sources import wikipedia as W
    WIKI_EXTRA["Vikram (soundtrack)"] = WIKI_VIKRAM_SOUNDTRACK
    assert W.review("Vikram (Original Motion Picture Soundtrack)", "2022", "Anirudh Ravichander")
    assert W.review("Vikram", "2022", "Ilaiyaraaja") is None              # another music director: not this film
    assert W.review("Vikram", "1986", "Anirudh Ravichander") is None      # another year
    assert W.review("Vikram", "", "") is None                             # nothing to tell same-name films apart


# ====================================================================== 3.8: AI that keeps working

def test_ai_retries_and_second_service(env, monkeypatch):
    import requests
    from app import ai, create_app
    from app.config import Config
    monkeypatch.setattr(ai, "BUSY_WAITS", (0, 0))
    slept = []
    monkeypatch.setattr(ai.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(ai, "_quota_until", {})
    for k, v in dict(AI_PROVIDER="gemini", GEMINI_API_KEY="k", AI_RPM=6000, AI_MODEL="gemini-3.8-flash",
                     AI_FALLBACK="off", AI_FALLBACK_MODEL="", OPENROUTER_API_KEY="", AI_FREE_TIER=False,
                     AI_MAX_SONGS=0).items():
        monkeypatch.setattr(Config, k, v)

    # busy twice, then an answer: nobody sees an error
    seq = [_AIResp({"error": {"message": "The model is overloaded"}}, 503)] * 2 + [_gemini({"ok": True})]
    monkeypatch.setattr(requests, "post", lambda *a, **k: seq.pop(0))
    assert ai.test()[0]["ok"] and not seq
    # busy three times: a clear message
    monkeypatch.setattr(requests, "post", lambda *a, **k: _AIResp({"error": {"message": "overloaded"}}, 503))
    with pytest.raises(ai.AIError, match="busy .503. and didn't answer after 3 tries"):
        ai.test()
    # a per-minute limit: waits as long as Gemini asks, once
    slept.clear()
    seq = [_AIResp({"error": {"code": 429, "message": "Resource exhausted", "details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "17s"}]}}, 429), _gemini({"ok": True})]
    monkeypatch.setattr(requests, "post", lambda *a, **k: seq.pop(0))
    assert ai.test()[0]["ok"] and 17.0 in slept

    # the day's free quota is gone and there's no second service
    quota = _AIResp({"error": {"code": 429, "message": "Quota exceeded for metric: generate_content_free_tier_requests, "
                               "limit: 20", "details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
                                                        {"retryDelay": "40s"}]}}, 429)
    monkeypatch.setattr(requests, "post", lambda *a, **k: quota)
    with pytest.raises(ai.AIQuota, match="daily quota"):
        ai.test()

    # with OpenRouter as the second service, the same request goes there
    monkeypatch.setattr(ai, "_quota_until", {})
    monkeypatch.setattr(Config, "AI_FALLBACK", "openrouter")
    assert ai.fallback() == ""                              # no key yet: not used
    monkeypatch.setattr(Config, "OPENROUTER_API_KEY", "or-key")
    assert ai.fallback() == "openrouter" and ai.fallback_model() == "openai/gpt-oss-120b:free"
    assert ai.label() == "Google Gemini · gemini-3.8-flash (then OpenRouter)"
    assert ai.max_songs() == 500                            # the smaller of Gemini's 1500 and OpenRouter's 500
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append(url)
        if "googleapis" in url:
            return quota
        assert url == "https://openrouter.ai/api/v1/chat/completions" and headers["Authorization"] == "Bearer or-key"
        assert json["model"] == "openai/gpt-oss-120b:free" and "response_format" not in json
        assert json["reasoning"] == {"effort": "low"} and headers["X-Title"] == "Isaiarr"
        assert json["messages"][1]["content"].endswith("Answer with JSON only.")
        return _AIResp({"choices": [{"message": {"content": "```json\n{\"ok\": true, \"song\": \"Ilamai\"}\n```"}}],
                        "usage": {"prompt_tokens": 40, "completion_tokens": 9}})
    monkeypatch.setattr(requests, "post", post)
    data, u = ai.test()
    assert data["song"] == "Ilamai" and u["provider"] == "openrouter" and u["fallback"] and u["cost"] == 0
    assert len(calls) == 2
    from app import db
    assert any("Google Gemini failed" in r["message"] and "asking OpenRouter instead" in r["message"]
               for r in db.q("SELECT message FROM log ORDER BY id DESC LIMIT 5"))
    calls.clear()
    ai.test()
    assert calls == ["https://openrouter.ai/api/v1/chat/completions"]   # Gemini's quota is out: skipped for an hour

    # OpenRouter passes on a model host's error inside a 200 answer; both failing says so
    monkeypatch.setattr(ai, "_quota_until", {})

    def both_fail(url, json=None, headers=None, timeout=None):
        if "googleapis" in url:
            return _AIResp({"error": {"message": "bad request"}}, 400)
        return _AIResp({"error": {"code": 502, "message": "Upstream provider error"}})
    monkeypatch.setattr(requests, "post", both_fail)
    with pytest.raises(ai.AIError, match="Both AI services failed. OpenRouter: The AI service failed: Upstream provider error"):
        ai.test()

    # a cut-off answer from the last service is asked once more
    monkeypatch.setattr(Config, "AI_FALLBACK", "off")
    seq = [_gemini({"ok": True})]
    seq[0].payload["candidates"][0]["content"]["parts"][0]["text"] = '{"ok": tr'
    seq.append(_gemini({"ok": True}))
    monkeypatch.setattr(requests, "post", lambda *a, **k: seq.pop(0))
    assert ai.test()[0]["ok"] and not seq

    # Settings: the second service, its key row and the test result
    monkeypatch.setattr(Config, "AI_FALLBACK", "openrouter")
    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(ai, "_quota_until", {})
    c = create_app(start_background=False).test_client()
    html = c.get("/settings?tab=ai").get_data(as_text=True)
    assert "Second service" in html and 'data-provider="openrouter"' in html and "data-fallback" in html
    assert 'value="openrouter"' in html and "OpenRouter (free models available)" in html
    html = c.post("/settings?tab=ai", data={"test": "ai"}, follow_redirects=True).get_data(as_text=True)
    assert "AI OK: OpenRouter · openai/gpt-oss-120b:free answered" in html and "second one answered" in html


# ====================================================================== 3.8: update only the parts you pick

def test_update_only_chosen_parts(env, monkeypatch):
    music, data, album = env
    from app import create_app, db, matcher, tags, worker
    from app.config import Config
    c = create_app(start_background=False).test_client()
    aid = worker.queue_album(str(album)); run_jobs()
    c.post(f"/album/{aid}/approve"); run_jobs()
    a = db.album_get(aid)
    assert a["status"] == "applied"
    names = sorted(n for n in os.listdir(album) if not n.endswith((".lrc", ".nfo")))
    # your own album artist and genre, and no label or full date yet (tagged before 3.7)
    for n in names:
        tags.write(str(album / n), {"albumartist": "Anirudh (my way)", "genre": "Kollywood", "date": "2022", "label": ""})
    monkeypatch.setitem(ITUNES_SEARCH["results"][0], "copyright", "℗ 2022 Sony Music Entertainment India Pvt. Ltd.")
    seen = []
    real = matcher.analyze
    monkeypatch.setattr(matcher, "analyze", lambda *a, **k: (seen.append((a, k)), real(*a, **k))[1])

    html = c.get("/library").get_data(as_text=True)
    assert "Update only…" in html and 'name="only" value="label"' in html and 'name="only" value="moods"' in html
    r = c.post("/bulk", data={"action": "only", "ids": [str(aid)], "only": ["date", "label"], "only_write": "review"})
    assert r.status_code == 302
    run_jobs()
    assert seen[-1][0][1] == a["collection_id"] and seen[-1][1]["only"] == ["date", "label"]   # the album you approved
    a = db.album_get(aid)
    p = a["proposal"]
    assert a["status"] == "review" and p["only"] == ["date", "label"]
    assert "Updating only: Year and release date, Record label" in p["notes"]
    for f in p["files"]:
        assert set(f["changes"]) == {"date", "label"}, f["changes"]          # not album artist, not genre
        assert not f["rename_to"] and not f["lyrics"]
        assert set(f["new"]) <= {"date", "label"}
    assert "Updating only: Year and release date, Record label" in c.get(f"/album/{aid}").get_data(as_text=True)
    c.post(f"/album/{aid}/approve"); run_jobs()
    assert db.album_get(aid)["status"] == "applied"
    for n in names:
        fl = tags.read(str(album / n))["fields"]
        assert fl["albumartist"] == "Anirudh (my way)" and fl["genre"] == "Kollywood", fl
        assert fl["date"] == "2022-05-15" and fl["label"] == "Sony Music Entertainment India Pvt. Ltd."
    assert sorted(n for n in os.listdir(album) if not n.endswith((".lrc", ".nfo"))) == names

    # "write straight away": no queue for albums that pass the auto-approve rules, even with auto-approve off
    for n in names:
        tags.write(str(album / n), {"label": ""})
    monkeypatch.setattr(Config, "AUTO_APPROVE_MIN", 7.0)
    monkeypatch.setattr(Config, "AUTO_APPROVE_TRACK_MIN", 7.0)
    assert Config.AUTO_APPROVE == "off"
    c.post("/bulk", data={"action": "only", "ids": [str(aid)], "only": ["label"], "only_write": "write"}); run_jobs()
    a = db.album_get(aid)
    assert a["status"] == "applied" and a["proposal"]["auto"]["ok"], a["proposal"]["auto"]
    assert all(tags.read(str(album / n))["fields"]["label"].startswith("Sony") for n in names)
    assert all(tags.read(str(album / n))["fields"]["albumartist"] == "Anirudh (my way)" for n in names)
    # nothing left to change: up to date, not queued
    c.post("/bulk", data={"action": "only", "ids": [str(aid)], "only": ["label"]}); run_jobs()
    assert db.album_get(aid)["status"] == "uptodate"
    # nothing picked: a message, nothing queued
    r = c.post("/bulk", data={"action": "only", "ids": [str(aid)]}, follow_redirects=True)
    assert "Pick what to update" in r.get_data(as_text=True)
    # every part picked is a normal re-check
    assert matcher.clean_only(list(matcher.ONLY_PARTS)) is None and matcher.clean_only(["nope"]) is None


def test_update_only_plex_parts(env, monkeypatch):
    music, data, album = env
    from app import create_app, db, plexmeta, worker
    from app.config import Config
    WIKI_EXTRA["Vikram (soundtrack)"] = WIKI_VIKRAM_SOUNDTRACK
    for k, v in dict(PLEX_URL="http://plex:32400", PLEX_TOKEN="t", PLEX_RESCAN=False, PLEX_REVIEW=False).items():
        monkeypatch.setattr(Config, k, v)
    aid = worker.queue_album(str(album)); run_jobs()
    c = create_app(start_background=False).test_client()
    c.post(f"/album/{aid}/approve"); run_jobs()
    rows = db.q("SELECT id FROM songs WHERE folder=?", (str(album),))
    for row in rows:
        db.x("UPDATE songs SET profile=? WHERE id=?", (json.dumps({"moods": ["energetic"]}), row["id"]))
    state = {"collection": [], "mood": [], "summary": "Plex's own review.", "puts": []}
    plex_fake(monkeypatch, state)
    before = sorted((n, os.path.getmtime(album / n)) for n in os.listdir(album))

    html = c.get("/library").get_data(as_text=True)
    assert 'name="only" value="moods"' in html and 'value="review" disabled' in html     # review is off in Settings
    r = c.post("/bulk", data={"action": "only", "ids": [str(aid)], "only": ["moods", "collections"]},
               follow_redirects=True)
    assert "Setting collections, moods in Plex for 1 album(s)" in r.get_data(as_text=True)
    for _ in range(50):
        if not plexmeta.status()["running"]:
            break
        time.sleep(0.1)
    assert state["mood"] == ["Energetic"] and state["collection"] == ["Film soundtracks", "2020s"]
    assert state["summary"] == "Plex's own review."
    assert sorted((n, os.path.getmtime(album / n)) for n in os.listdir(album)) == before     # files untouched
    assert db.album_get(aid)["status"] == "applied" and not db.q("SELECT 1 FROM jobs WHERE status='queued'")
    # only moods: collections are left as Plex has them
    state["collection"] = ["My favourites"]
    assert plexmeta.push(str(album), only=["moods"]) in ("already as wanted", "updated 0 album(s) in Plex")
    assert state["collection"] == ["My favourites"]
    m = db.album_get(aid)["plex_meta"]
    assert m["sig"] is None and m["moods"] == ["Energetic"] and m["collections"] == ["Film soundtracks", "2020s"]
    # a part that's off in Settings is left alone, with a word why
    r = c.post("/bulk", data={"action": "only", "ids": [str(aid)], "only": ["review"]}, follow_redirects=True)
    assert "Review: switched off in Settings → Plex" in r.get_data(as_text=True)


# ====================================================================== 3.9: what's running, in the sidebar

def test_running_tasks_in_sidebar(env, monkeypatch):
    music, data, album = env
    from app import create_app, db, dupes, plexmeta, songs, tasks, worker
    c = create_app(start_background=False).test_client()
    monkeypatch.setattr(tasks, "_live", {})
    monkeypatch.setattr(tasks, "_recent", [])
    # nothing running
    s = c.get("/api/status").get_json()
    assert s["tasks"] == {"running": [], "recent": []} and s["watch_state"] in ("off", "live", "paused")
    html = c.get("/library").get_data(as_text=True)
    assert 'id="tasks"' in html and 'id="tasks-init"' in html and "All activity" in html
    assert c.get("/activity").headers["Location"].endswith("/history")         # nothing running: changes written

    # the album queue: count, album in hand, what's left, time left
    for i in range(5):
        d = music / "Q" / f"A{i}"
        make_audio(str(d / "01.mp3"), 20)
        worker.queue_album(str(d))
    monkeypatch.setitem(worker._state, "current", "Analyzing A0")
    monkeypatch.setattr(worker, "_batch", {"done": 6, "secs": 60.0, "started": time.time() - 60})
    # other modules at work
    monkeypatch.setattr(songs, "_state", {**songs._state, "running": True, "done": 40, "total": 200, "what": "Indexing songs",
                                          "started": time.time() - 10})
    monkeypatch.setattr(dupes, "_state", {**dupes._state, "running": True, "phase": "Listening to songs", "done": 0, "total": 0})
    monkeypatch.setattr(plexmeta, "_state", {**plexmeta._state, "running": True, "done": 3, "total": 10,
                                             "what": "collections, moods", "started": time.time()})
    s = c.get("/api/status").get_json()
    run = {t["key"]: t for t in s["tasks"]["running"]}
    q = run["queue"]
    assert q["label"] == "Album queue" and q["detail"] == "Analyzing A0" and q["done"] == 6 and q["total"] == 11
    assert q["sub"] == "5 to check" and q["eta"] == 50 and q["url"] == "/library?tab=queued"
    assert run["songs"]["pct"] == 20 and run["songs"]["eta"] == 40 and run["songs"]["url"].startswith("/playlists")
    assert run["dupes"]["detail"] == "Listening to songs" and run["dupes"]["pct"] is None     # no count yet: a moving bar
    assert run["plexmeta"]["label"] == "Plex collections, moods" and run["plexmeta"]["total"] == 10
    assert c.get("/activity").headers["Location"].endswith("/activity/now")
    html = c.get("/activity/now").get_data(as_text=True)
    assert "Running now" in html and 'id="tasks-full"' in html and "Pause the album queue" in html
    assert "4 running" in c.get("/").get_data(as_text=True)                 # the phone status chip

    # a paused queue says so, and has no time left
    db.state_set("paused", True)
    q = {t["key"]: t for t in c.get("/api/status").get_json()["tasks"]["running"]}["queue"]
    assert q["state"] == "paused" and q["eta"] is None and q["detail"].startswith("Paused")
    db.state_set("paused", False)

    # finished tasks stay listed for a while, with their error if they had one
    monkeypatch.setitem(songs._state, "running", False)
    monkeypatch.setitem(songs._state, "error", "The music folder /music is empty or not reachable")
    monkeypatch.setitem(dupes._state, "running", False)
    monkeypatch.setitem(dupes._state, "error", None)
    rec = {t["key"]: t for t in c.get("/api/status").get_json()["tasks"]["recent"]}
    assert rec["songs"]["state"] == "error" and "not reachable" in rec["songs"]["error"]
    assert rec["dupes"]["state"] == "done" and rec["dupes"]["ago"] == 0
    assert "songs" not in {t["key"] for t in c.get("/api/status").get_json()["tasks"]["running"]}

    # threads without a state of their own report through tasks.track()
    with tasks.track("nfo", "Writing NFO files", total=4, url="/settings?tab=plex") as t:
        t.step(2)
        t.detail = "Vikram"
        nfo = {x["key"]: x for x in tasks.snapshot()["running"]}["nfo"]
        assert nfo["done"] == 2 and nfo["pct"] == 50 and nfo["detail"] == "Vikram"
    assert {x["key"]: x for x in tasks.snapshot()["recent"]}["nfo"]["state"] == "done"
    try:
        with tasks.track("plexrefresh", "Refreshing albums in Plex"):
            raise RuntimeError("Plex went away")
    except RuntimeError:
        pass
    tasks.snapshot()      # (it was never seen running by a poll, so it isn't listed as finished either)
    # a broken status read never breaks the page
    monkeypatch.setattr(tasks, "PROVIDERS", tasks.PROVIDERS + (lambda u: 1 / 0,))
    assert c.get("/api/status").status_code == 200
    # the menu counts come with it, so they don't go stale
    s = c.get("/api/status").get_json()
    assert "queue_count" in s and "dupe_count" in s


# ====================================================================== 3.10: Skip

def test_skip_leaves_album_alone(env, monkeypatch):
    music, data, album = env
    from app import create_app, db, reset, tags, worker
    c = create_app(start_background=False).test_client()
    aid = worker.queue_album(str(album)); run_jobs()
    assert db.album_get(aid)["status"] == "review"
    before = {n: tags.read(str(album / n))["fields"] for n in os.listdir(album) if not n.endswith(".lrc")}
    html = c.get(f"/review?a={aid}").get_data(as_text=True)
    assert f"/album/{aid}/skip" in html and ">Skip</button>" in html
    assert ">Skip</button>" in c.get(f"/album/{aid}").get_data(as_text=True)
    r = c.post(f"/album/{aid}/skip")
    assert r.status_code == 302
    a = db.album_get(aid)
    assert a["status"] == "skipped" and aid not in [x["id"] for x in db.albums_list(["review", "nomatch", "error"])]
    assert "Skipped" in c.get(f"/album/{aid}").get_data(as_text=True)
    lib = c.get("/library?tab=skipped").get_data(as_text=True)
    assert "Skipped ·1" in lib or "Skipped" in lib
    # scans, the watcher and a library-wide reset pass it by
    worker.queue_folder(str(music)); run_jobs()
    assert db.album_get(aid)["status"] == "skipped"
    worker.queue_album(str(album), origin="watch"); run_jobs()
    assert db.album_get(aid)["status"] == "skipped"
    res = reset.reset(); run_jobs()
    assert res["kept"] == 1 and db.album_get(aid)["status"] == "skipped"
    after = {n: tags.read(str(album / n))["fields"] for n in os.listdir(album) if not n.endswith(".lrc")}
    assert before == after and sorted(before) == sorted(n for n in os.listdir(album) if not n.endswith(".lrc"))
    # asking on purpose brings it back
    c.post(f"/album/{aid}/reanalyze"); run_jobs()
    assert db.album_get(aid)["status"] == "review"
    # bulk Skip from the Library; an applied album can be skipped from now on too
    c.post("/bulk", data={"action": "skip", "ids": [str(aid)]})
    assert db.album_get(aid)["status"] == "skipped"
    c.post(f"/album/{aid}/reanalyze"); run_jobs()
    c.post(f"/album/{aid}/approve"); run_jobs()
    assert db.album_get(aid)["status"] == "applied"
    assert "Skip from now on" in c.get(f"/album/{aid}").get_data(as_text=True)
    c.post(f"/album/{aid}/skip", data={"back": f"/album/{aid}"})
    assert db.album_get(aid)["status"] == "skipped"
    # a pending check is dropped when you skip
    worker.queue_album(str(album), priority=db.PRIORITY_USER)
    worker.skip(aid)
    assert not db.q("SELECT 1 FROM jobs WHERE album_id=? AND status='pending'", (aid,))


def test_mistral_provider_and_rate_limit_waits(env, monkeypatch):
    import requests
    from app import ai, create_app
    from app.config import Config
    monkeypatch.setattr(Config, "AI_PROVIDER", "mistral")
    monkeypatch.setattr(Config, "MISTRAL_API_KEY", "m-key")
    monkeypatch.setattr(Config, "AI_MODEL", "")
    monkeypatch.setattr(Config, "AI_FALLBACK", "off")
    assert ai.enabled() and ai.model() == "mistral-small-latest" and ai.max_songs() == 800
    slept = []
    monkeypatch.setattr(ai.time, "sleep", lambda s: slept.append(s))
    limited = _AIResp({"object": "error", "message": "Rate limit exceeded", "type": "rate_limited", "code": "1300"}, 429)
    ok = _AIResp({"choices": [{"message": {"content": "{\"ok\": true}"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
    seen = []

    def post(url, json=None, headers=None, timeout=None):
        seen.append((url, headers["Authorization"], json["model"]))
        return seq.pop(0)
    monkeypatch.setattr(requests, "post", post)
    # Mistral's "Rate limit exceeded" (no Retry-After): waits 20 s, then 40 s, and the third try works
    seq = [limited, limited, ok]
    data, usage = ai.complete("sys", "user")
    assert data == {"ok": True} and [s for s in slept if s >= 20] == [20, 40]
    assert seen[0] == ("https://api.mistral.ai/v1/chat/completions", "Bearer m-key", "mistral-small-latest")
    # still limited after three waits: a clear message, no endless loop
    slept.clear()
    seq = [limited] * 4
    with pytest.raises(ai.AIError, match="still over its rate limit after waiting 3 times"):
        ai.complete("sys", "user")
    assert [s for s in slept if s >= 20] == [20, 40, 60] and not seq
    html = create_app(start_background=False).test_client().get("/settings?tab=ai").get_data(as_text=True)
    assert "Mistral API key" in html and "Mistral (free plan available)" in html
