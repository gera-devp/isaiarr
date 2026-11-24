# Playlists screens (added in 2.2, extends DESIGN.md)

Same tokens, type and shapes as `DESIGN.md`. New pieces:

- **Playlists page**: composer card (left, like the queue's detail panel) + a sticky *Song library*
  side column (≥1100px). Mode switch uses filter chips: Describe it · From a song · Radio.
  A pipeline strip under the inputs shows what will happen (mirrors the album "On approve" strip).
  *Recent playlists* are tiles with a **mosaic cover**: up to four tinted film tiles (2×2), using the
  same tone + initial rule as album covers.
- **Making a playlist**: hero with a pulsing mosaic and a live console using the Scan page's log style.
  Tags: `ASK` (accent), `SEED` (accent), `FILTER` (info), `LOOSEN` (warn), `PICK` (text),
  `DONE` (ok), `MISS` (danger).
- **Preview**: hero (88px mosaic, uppercase caption "MODE · MODEL · N SONGS · MIN", editable title in
  the display face, request quoted in italics, token/cost badge). Track table columns
  # | ✓ | Song (cover, title, singers, AI reason in italics) | Film (year · music director) | Mood tags |
  Time | ↑ ↓ Like this. Rows can be dragged; numbers follow the order. Save bar is the sticky
  `.actionbar` (New / Replace / Add to a Plex playlist).
- **Phone**: side column stacks under the composer; table rows become cards; the save bar is fixed
  to the bottom like the album editor.
