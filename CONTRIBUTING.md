# Contributing to Isaiarr

Feedback is the most useful contribution. Albums that match wrongly are exactly what improves the
matcher.

- **Something wrong?** Open a [bug report](https://github.com/gera-devp/isaiarr/issues/new/choose).
  Include the album/film name, what Isaiarr proposed, and what's correct. A screenshot of the album
  page says the most.
- **Idea or question?** Use [Discussions](https://github.com/gera-devp/isaiarr/discussions).
- **Never paste tokens, webhook URLs or passwords.** The Log page doesn't contain them, but compose
  files and screenshots of Settings might.

## Code

```bash
pip install -r requirements.txt pytest
pytest -q tests              # needs ffmpeg; uses simulated iTunes/JioSaavn/Wikipedia/Discogs/Musixmatch
MUSIC_DIR=./music DATA_DIR=./config python main.py   # http://localhost:8687
```

- Keep changes reversible: every write must go through `applier.apply` so it's backed up and undoable.
- New metadata sources live in `app/sources/` and return the same album/track shapes as `itunes.py`.
- UI changes should work at phone width (390 px) and inside nzb360's web view (no `confirm()`/`alert()`).
