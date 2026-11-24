# Isaiarr — UI design handoff

Design source: Claude Design canvas "Isaiarr" (3 screens). The `.dc.html` files in `screens/` are the canvas sources. They show the exact markup, inline styles and sample data for each screen. Use them as the visual reference, and this document as the spec.

Put this folder in the repo at `docs/design/`, then rebuild the web UI to match.

---

## 1. Design tokens

### Colour (dark only)
| Token | Hex | Use |
|---|---|---|
| `--bg` | `#14110F` | App background |
| `--sidebar` | `#191512` | Sidebar background |
| `--surface` | `#1C1815` | Cards / panels |
| `--surface-2` | `#221C18` | Inset tiles, pipeline strip |
| `--surface-sel` | `#231D18` | Selected list card |
| `--nav-active` | `#2A231E` | Active nav item, progress track |
| `--input-bg` | `#14110F` | Inputs |
| `--log-bg` | `#110E0C` | Log console |
| `--border` | `#2E2722` | Card borders, dividers |
| `--border-strong` | `#3A322B` | Buttons, inputs, chips |
| `--row-line` | `#262019` | Table row dividers |
| `--text` | `#F2ECE4` | Primary text |
| `--text-2` | `#C9BFB3` | Secondary text, nav |
| `--text-muted` | `#A89D90` | Captions, labels |
| `--text-faint` | `#8E8377` | Table headers, struck-through values |
| `--text-ghost` | `#6E655B` | "empty", arrows |
| `--accent` | `#F2A43A` | Marigold: primary buttons, logo, badges, checkboxes |
| `--on-accent` | `#1A1208` | Text on accent |
| `--ok` | `#6FD39A` | Live / Connected / Approved / ≥95% |
| `--warn` | `#F6C27A` | Fix badge text, 80–94% confidence |
| `--warn-bg` | `#3A2A12` | Fix badge background |
| `--info` | `#8DC3F5` | Fill badge text |
| `--info-bg` | `#16283A` | Fill badge background |
| `--danger` | `#F28B7A` | <80% confidence, MISS |

Other accent options shown in Tweaks: `#E8715A`, `#7FB6F0`, `#B9A3F0`. Fix vs Fill are told apart by orange vs blue, which differ in lightness as well as hue.

### Type (Google Fonts)
- Display: **Bricolage Grotesque** 700 for page titles (40px, -0.02em, line-height 1.05), section titles (22px) and the album title (28px).
- Body: **IBM Plex Sans** 400/500/600, base size 14px.
- Mono: **JetBrains Mono** 400/500 for paths, filenames, counts, logs and rename tokens.
- Logo mark (since 2.3): an "i" whose stem runs into a fading sound wave, drawn as an inline SVG in `--on-accent` on an accent gradient tile. No text in the mark. (The original canvas used "இசை" in Catamaran.)

Label style: 12px, uppercase, letter-spacing 0.06em, `--text-muted`.

### Shape & spacing
- Radii: cards 16px, list cards 14px, buttons/inputs 10px, tiles 12px, chips/badges 999px, logo 12px.
- Page padding is 32px top, 40px sides, 48px bottom. Section gap is 24px, card padding 24px, grid gap 20px.
- Every control is at least 44px tall (filter chips are 36px, small secondary buttons 40px).
- Icons are inline stroke SVGs at 18px with 1.8 stroke width. No emoji.

---

## 2. App shell
- Flex row that wraps. The sidebar is `flex: 1 1 232px` and the content is `flex: 999 1 560px; min-width: 0`, so on a phone the sidebar stacks on top.
- Sidebar from top to bottom:
  - Logo tile (44px, accent gradient, wave mark) with "Isaiarr" and "Music tags & playlists" beside it
  - Nav: Review queue (pending-count badge), Library, Scan, Activity, Settings. The active item uses `--nav-active` with white text.
  - Footer status card: green dot with "Watcher live", the library path in mono, and "Plex ✓" / "Discord ✓" pills

## 3. Screens

### 3.1 Review queue (`Main.dc.html`, interactive)
Purpose: approve or reject proposed tag changes, grouped by album. Nothing is written until the user approves.

- **Header**: title, subtitle, search box ("Album, director, singer…") and a secondary "Approve all ≥ 95%" button.
- **Filter tabs**: All · Fixes · Fill blanks · Various Artists · Low confidence, each with a count. The active tab is an inverted pill.
- **Album list** (left, 320–420px):
  - Each album is a button card: a 52px cover tile (art, or a tinted tile with the album's first letter), album name, "Music director · year", "N tracks · source", and the confidence % on the right.
  - Confidence colour: ≥95 `--ok`, 80–94 `--warn`, <80 `--danger`.
  - Selected card gets an accent border. Approved cards drop to 60% opacity and show "Approved".
- **Detail panel** (right):
  - Header: 88px cover, "SOURCE · NN% match" caption, album title, folder path in mono, and the actions Edit · Reject · **Approve N tracks** (primary).
  - **Album tags table** with the columns Field | In file now | Proposed | Action.
    - Badges: **Fix** (current value is wrong; shown struck through), **Fill** (current value is blank; shown as italic "empty"), **OK** (no change).
  - **Tracks table** with the columns # | File rename (old filename struck through, then "→ new name") | Title + singers | Lyrics (Synced / Plain / None found). Scrolls sideways inside its box on narrow screens.
  - **On-approve pipeline strip**: Write tags → Rename files → Plex rescan → Discord summary.

### 3.2 Scan (`Scan.dc.html`)
- **Auto-detect card**: "Watching" toggle, library folder (read-only, mono), "Wait after last copy before scanning" select (2 / 5 / 15 min), and a "Recently detected" list of folder → "N files · queued".
- **Manual scan card**: folder path input with a Browse button. Mode radios: "Only files with missing or wrong tags" / "Re-check every file". An "Include subfolders" checkbox and a primary **Start scan** button.
- **Running scan panel**: "Running · <folder>", a file counter, Pause, a progress bar (accent fill on `--nav-active`), three stat tiles (Already correct / Queued for review / No match found), and a live log console.
  - Log line format: `STATUS  Album / detail`, where STATUS is one of MATCH (ok), FILL (info), FIX (warn), SKIP (faint) or MISS (danger).

### 3.3 Settings (`Settings.dc.html`)
- **Tag rules**: three read-out tiles.
  - Album artist = music director only (no single director → Various Artists)
  - Artist = singers, joined with ", "
  - Title & album exactly as the source returns them
- **Overwrite behaviour**: a radio for "Fix incorrect and fill blanks" vs "Only fill blanks", plus a "Queue every change for my approval" checkbox (on by default).
- **File renaming**: pattern input (default `{track:02} - {title}`), insertable token chips (`{track:02} {title} {artist} {album} {year}`), and a live preview.
- **Metadata sources**: an ordered list. 1 is iTunes Search (album, titles, year, cover art, region India) and 2 is Musixmatch (lyrics, synced when available; singer credits). Each has an Enabled toggle.
- **Integrations** in two columns:
  - Plex: server URL, token (masked), "Rescan music library after each approval"
  - Discord: webhook (masked) and "Notify me when" checkboxes (new albums waiting / changes written / scan finished with misses), plus "Send test message"
- **Save changes** primary button in the header.

## 4. Behaviour notes
- Approving an album runs the pipeline in order: write tags, rename files, ask Plex to rescan, then post a Discord summary. The album then shows as Approved in the list.
- Albums with no single music director default to Album artist = Various Artists and go into that filter.
- Sample data in the designs (albums, counts, `[NAS-IP]`, `[n]`) is illustrative. Wire it to real API data.

## 5. Accessibility
- Use real `<button>`, `<a>`, `<input>` + `<label>` elements throughout. Icon-only buttons need an `aria-label`.
- The current page's nav item has `aria-current="page"`. The progress bar uses `role="progressbar"` with values.
- Text contrast is at least 4.5:1 on the dark surfaces.
