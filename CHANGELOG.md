# Changelog

## 3.11.0 (2026-11-25)

### Added
- **Mistral** as an AI service (Settings → AI), with its own API key field. Its free
  "Experiment" plan needs a phone number and no card. The default model is
  `mistral-small-latest`, 800 songs per request. It can also be the second service under
  *If it fails, ask*.

### Fixed
- **"Too many requests" (429) failed a playlist after one short wait.** Services that don't say how
  long to wait, such as Mistral's "Rate limit exceeded", now get up to three waits (20, 40, then
  60 seconds) before Isaiarr gives up. Other AI requests wait too, instead of hitting the limit
  again. Each wait is noted in Activity, and the final error says what to change. Busy (503)
  retries are counted separately, so a 503 followed by a 429 no longer uses up the retries.

## 3.10.1 (2026-10-28)

### Fixed
- **Scan page reloaded itself every few seconds** once any scan had finished, so *Browse* showed
  the folders for a moment and then closed. The page now reloads only when a scan that was running
  when you opened it finishes, and not while the folder browser is open.

## 3.10.0 (2026-10-27)

### Added
- **Skip**, for albums whose tags are already right: the album leaves the review queue, nothing
  is written, and it stays that way. Scans (whole library or "only changed"), the watcher and a
  library-wide *Reset the tag analysis* pass it by; changed files in a skipped folder are logged,
  not checked.
  - On the review queue, the album page (next to Reject) and Library bulk actions. Tagged and
    up-to-date albums get *Skip from now on* on their page.
  - Library → *Skipped* lists them. *Analyze again*, *Send to review*, *Update only…* or resetting
    chosen folders bring an album back; Undo still works.
  - Plex collections, moods and reviews still include skipped albums (they live in Plex, not in the
    files); NFO files and track-name checks leave them out.
- Reject now says what it does: wrong match, nothing written, checked again on the next scan.

## 3.9.0 (2026-10-26)

### Added
- **What's running, always in the sidebar.** Every background task with a progress bar, a count,
  what it's on right now and the time left, refreshed every 2 seconds while something runs (every
  8 when idle, paused while the tab is hidden). Each task links to its page. Tasks that just ended
  stay listed for five minutes with a ✓, or ✗ and the error.
  - Album queue (checks and writes, with what's left of each kind, and *Paused* when it is),
    new music the watcher is waiting on, song index, Syncing with Plex, duplicate check, track-name
    checks, song descriptions (AI), playlists being made, Plex re-adds, Plex collections / moods /
    reviews, NFO files, Plex refreshes, and Plex's own library scan.
  - On phones the status chip in the top bar (*3 running*) opens the same list.
- **Activity → Running now**: the full list, bigger, with *Pause / Resume the album queue*.
  Activity opens there when something is running.

### Fixed
- The sidebar said *Idle* while the song index, duplicate check, Plex sync and other background
  work ran: it only knew about the album queue.
- The menu counts (review queue, duplicates) only changed on a page load; they now follow along.
- A playlist being made when the app restarted stayed "working" for ever; it's now marked as
  stopped, to make again.
- A Plex library scan whose "ended" notice was missed no longer shows as running.

## 3.8.0 (2026-10-26)

### Added
- **Update only…** (Library, for the selected albums or every album in the view): tick the parts
  to change and leave the rest alone.
  - In Plex: collections, moods, review. Set straight away through Plex's API, nothing re-checked,
    files untouched. Parts switched off in Settings → Plex are greyed out.
  - In the files: song titles, singers, album name, album artist, composer, track numbers, year
    and release date, genre, record label, kind of album, artwork, lyrics, file names. Albums are
    re-checked against the album they were matched to (no new search; a new search only if that
    lookup fails), only the ticked tags can change, lyrics aren't looked up unless ticked, and
    nothing is renamed unless *File names* is ticked. Typed edits on the review page still count.
  - Changes wait in the review queue, or are written straight away for albums that pass the
    auto-approve rules (works with auto-approve off too).
- **OpenRouter** as an AI service (`OPENROUTER_API_KEY`). Default model `openai/gpt-oss-120b:free`:
  free, 20 requests a minute and 50 a day, or 1,000 a day after a one-time $10 credit purchase.
- **Second AI service** (`AI_FALLBACK`, `AI_FALLBACK_MODEL`): gets the same request when the first
  fails. *Test AI* says when the second one answered. Songs per request are the smaller of the two
  services' limits so a request fits either.

### Changed
- AI requests are retried: busy replies (HTTP 500, 502, 503, 504, 529) twice with a pause, a
  per-minute rate limit once after the wait the service asks for (Gemini's `retryDelay`, or
  `Retry-After`). A used-up daily quota or credit (HTTP 402) isn't retried; with a second service the
  first is skipped for an hour after one. An answer that isn't valid JSON (often cut off) is asked
  once more. Errors say which of these happened.
- OpenRouter's in-band errors (an HTTP 200 carrying the model host's error) are reported as errors.

## 3.7.0 (2026-10-26)

### Added
- **Release date and record label in the tags**, for Plex's *Originally Available* and *Record Label*.
  - The full date (`YYYY-MM-DD`) is written when a source has it and its year is the album's year;
    a streaming re-issue's upload date is never used.
  - A full date already in a file is never cut back to a year. Year → full date is a *Fill*, also
    in *Only fill blanks* mode.
  - Labels from JioSaavn, Deezer, Discogs, a Wikipedia album infobox or iTunes' copyright line.
    Written as `LABEL` (FLAC), `TPUB` (MP3) and the iTunes `LABEL` atom (M4A).
  - The album page has *Release date* and *Record label* fields; the review panel lists both.
  - album.nfo gets `releasedate` and `label` too.
- **Collections, review and moods in Plex** (Settings → Plex), set through Plex's API after each
  write, nightly for what changed, and with *Update Plex* for every tagged album.
  - Collections by kind of album (Film soundtracks, Compilations, Singles, Non-film albums) and
    by decade.
  - The album review from the film's English Wikipedia page (soundtrack article lead, else its
    Music / Soundtrack section), credited CC BY-SA 4.0, only when the page's year and music
    director agree. Shown on the album page before approving; also written to album.nfo.
  - Album moods from the AI song descriptions (the moods most songs share, up to 3).
  - Only Isaiarr's own values are managed: your collections, moods and reviews in Plex stay.
    The fields are locked, and the unlock-before-refresh step leaves them alone.
  - The album page's new *In Plex* card shows what goes to Plex and when it was last sent.

### Changed
- The song index re-reads every file once (on its next run) to pick up labels and full dates.

## 3.6.0 (2026-10-26)

### Added
- **Deezer as a fifth album source**, after Discogs (`SOURCES=itunes,jiosaavn,wikipedia,discogs,deezer`).
  Official public API, no key. Complete track lists with track and disc numbers, lengths, release
  dates and 1000 px covers. Deezer can't tell the music director from the singers, so for an album
  it wins, each matched song's artists are fetched and the album artist is left out; a song whose
  only artist is the music director keeps the singers already in the file. Deezer album links work
  in the rematch box. Deezer's quota replies (sent as HTTP 200) are retried and never cached.
- **TMDB music director check** (`TMDB_API_KEY`, free). After a film album is matched, TMDB's
  music credits for the film are compared with the Album Artist.
  - Agreement shows on the album page and in the *Film soundtrack* suggestion.
  - A different name holds the album in the review queue under the new **Music director?** filter
    and on Home's to-do list, with a button to use TMDB's name. It isn't auto-approved and
    *Approve all* skips it.
  - Same-name films (Vikram 1986 / 2022) are all checked before a name is called different; when
    the year can't settle which film it is, nothing is flagged.
  - Skipped for Album Artists you typed or decided, Various Artists, collections and singles.
- **Songs identified by their sound (Shazam)**, for files no source finds by title.
  - What Shazam heard is looked up as usual: an unclaimed song of the matched album (with its
    track number), else JioSaavn (music director and singers) or iTunes by its Apple Music id.
  - A junk folder whose songs Shazam places in one album is matched to that album by name.
  - Marked **by sound**, gathered under the new **Found by sound** filter, never auto-approved,
    and skipped by *Approve all*.
  - Answers are cached per file; at most `SHAZAM_MAX_PER_ALBUM` (30) songs per album;
    `SHAZAM_RPM` (12); a 15-minute pause when Shazam refuses requests; `SHAZAM_ENABLED=false`
    switches it off. Unofficial (via the shazamio library).
- Settings → Metadata sources: boxes for the TMDB check and Shazam; *Check sources* tests both.

### Changed
- Upgrading: Deezer is switched on at the end of your existing source list (also one from the
  compose file) until you save the source list in Settings.
- A key or token refused with HTTP 401 is reported at once instead of being retried.
- New dependency: `shazamio`.

## 3.5.1 (2026-10-25)

### Fixed
- Settings → Matching → **Reset and re-check** did nothing: its form had the same name as the
  settings page's own *reset to default* form, so the click went there instead. A new test checks
  every page for clashes like this.

## 3.5.0 (2026-10-25)

### Added
- **Start over**: Settings → Matching → *Reset the tag analysis* (type RESET to confirm), and
  Library → Folders → **Reset analysis** for the ticked folders. Isaiarr forgets its analyses and
  matches, your folder decisions, album edits, “Leave as is” / “It's fine” marks and the Plex and
  track-name checks, then checks every folder again from the tags the files have now.
  - Your files aren't changed, and Undo on each album keeps working (its backups are kept).
  - The re-check never writes on its own: albums with changes wait in the review queue.
  - Albums being analyzed or written at that moment are left alone.

## 3.4.0 (2026-10-25)

### Fixed
- Library → Artists didn't spot Tamil names spelt differently in English letters, such as
  *Ilayaraja* / *Ilaiyaraaja* or *Bharathwaj* / *Bharadwaj*, so both spellings stayed in the
  files and Plex showed its own spelling instead. Names are now also compared by how they sound
  (same consonants; t/d, k/g, p/b treated alike; the same number of words). The suggested spelling
  is the one **Plex already uses** for those songs, marked *the spelling Plex uses*.
- A folder decision (Various Artists, Collection, Single…) now also updates the album's stored
  plan. Before, the plan kept the old album artist, and a later Approve on that album could write
  it back.

## 3.3.0 (2026-10-25)

### Added
- **Library → Track names**: finds tracks that may have been given another song's title, for
  example after a bulk approval. Every tagged album is compared with what Isaiarr already knows,
  with no new lookups:
  - the file's length against the length of the song it was matched to;
  - the file's **original name** (before Isaiarr renamed it, from the backups) against the title
    now in the file, tolerant of Tamil/Hindi spellings (*Kannalanae* = *Kanalane*);
  - **swapped tracks**: the original name or the length fits another track of the same album.
  Each track is marked *Almost certain*, *Likely* or *Maybe*, with the reason. Generic names such
  as "Track 04" are judged by length only.
- From that list: **Send albums to review** (matched again, they wait under *Sent back*),
  **It's fine** (never flagged again) and **Ask the AI** for a second opinion on the ticked
  tracks. The AI's verdict and the title it thinks is right are shown on each track; nothing is
  written by it.
- Home's to-do list counts the *Likely* and *Almost certain* tracks. Each album is also checked
  right after it's applied, with a warning in Activity when something looks off.

## 3.2.0 (2026-10-24)

### Added
- Review queue: **Approve all ≥ N%** with your own N. Type the minimum match next to the button
  (50–100 %); the button and the number of albums it covers update as you type, and the
  confirmation says exactly how many albums will be written. The default is in Settings →
  Matching (*“Approve all” in the review queue: minimum match*, 9.5 = 95 %).

### Fixed
- Approve all now compares matches the way the cards show them (whole percent), so an album shown
  as 95 % is included at 95 %.

## 3.1.1 (2026-10-24)

### Fixed
- Settings → Plex → **Write NFO files** did nothing when clicked (its form had ended up in the
  page title instead of the page). It now asks for confirmation, writes the files in the background
  and logs progress in Activity: a start line, every 100 albums, and the total.

## 3.1.0 (2026-10-23)

### Added
- **NFO files for music** (Settings → Plex → NFO files): Plex Media Server 1.43.5 reads artist
  and album details from NFO files in music libraries (so do Kodi and Jellyfin). Isaiarr writes
  `album.nfo` in each album folder and `artist.nfo` in a folder that holds one artist's albums, in
  Kodi's music NFO format, from the tags it wrote: title, album artist(s), year, genre, release
  type (album/single, Soundtrack/Compilation), compilation flag and the track list. They're kept
  up to date after every change; **Write NFO files** does it for every tagged album at once and
  then rescans Plex. NFO files Isaiarr didn't write are never overwritten. Off by default.

## 3.0.0 (2026-10-21): the redesign is complete

Phases 1–3 shipped in 2.13.0–2.15.0 (folder policies, the Folders view, Home and the live Plex
link). This release finishes it; see docs/design/REVAMP.md.

### Changed
- Library → Albums: *Set album artist…* is now **Set kind of album…** (kind + optional Album
  Artist), the same decision as in the Folders view, written to every file and kept by re-checks.
  Each album row shows its decision, tags that didn't stick, and ✓/✗ Plex.
- Library → Artists: new **All album artists** list (every name Plex will show, song and folder
  counts, findable); *Several names in one tag* now sets a kind of album for those folders
  instead of only rewriting the name.
- Settings → **Kinds of album** (new page): which suggestions to make (several composers, singles,
  collections), the words that mark a collection folder (editable), and what a decision writes
  (Various Artists name, release type, album tags for unmatched songs).
- README rewritten for the new layout.

## 2.15.0 (2026-10-21): 3.0 redesign, phase 3

### Added
- **Home**: album artists, albums and tracks counted from your files (grouped the way Plex
  groups them) next to Plex's own totals, with the difference; Plex's status (live, scanning, the
  last change it reported); a to-do list (review queue, decisions, songs without Album Artist,
  folders not checked, tags that didn't stick, wrong artist / old details in Plex, albums that
  don't match Plex after a write, same-name artists, duplicates); recent activity. Updates by
  itself every few seconds.
- **Live link to Plex** through Plex's notification websocket (no Plex Pass needed): totals
  refresh seconds after Plex changes something, and albums whose tracks Plex just processed are
  checked again. Without the link, totals refresh every 5 minutes.
- **✓ Plex check** after every write: a minute after the refresh, Isaiarr compares what Plex
  shows for the album (album artist, album) with the files and shows **Plex shows these tags** or
  what differs on the album page; **Check in Plex now** runs it on demand.

### Changed
- The review queue moved to `/review`; `/` is Home. Old links (`/?a=12`) still open the queue.
- New dependency: `websocket-client` (installed by the Docker build).

## 2.14.0 (2026-10-21): 3.0 redesign, phase 2

### Added
- Library → **Folders**: your music folder by folder, like a file manager. Every folder shows its
  album folders, songs and Album Artists added up over everything inside it, plus decisions,
  suggestions, folders not checked yet and Plex differences. Tick folders (Shift for a range),
  keep **Include subfolders** on, and set the kind of album and Album Artist for all of them at
  once, accept the suggestions, or send them to review. A live count shows how many album folders
  and songs the ticked folders cover. Search finds folders anywhere below the current one.

### Changed
- Library has four views in one place: **Albums, Folders, Artists, Needs a decision**.
- Sidebar has six items: Review queue, Library, Duplicates, Playlists, Activity, Settings. Album
  artists is now Library → Artists; Scan is *Scan a folder…* in Library.

### Fixed
- The action buttons on Album artists (*Send selected to review*, *Change*, *Give selected the
  album's tags*) and Needs a decision were hidden on wide screens.

## 2.13.0 (2026-10-21): 3.0 redesign, phase 1

See docs/design/REVAMP.md.

### Added
- **Folder policies**: one decision per album folder (*Film soundtrack, Various Artists,
  Collection, Single, Artist album, Keep as is*), written to **every** file in the folder, matched
  or not, and kept by every re-check.
- **Rules that suggest a policy**: several composers → Various Artists; one-track releases →
  Single (Various Artists); folders named *Hits / Collection / Melodies…* or holding songs from
  many films → Collection named after the folder; one composer for every song → Film soundtrack.
  Suggestions never write anything by themselves.
- Library → **Needs a decision**: every suggestion with its reason; accept, pick another kind,
  set your own Album Artist for ticked folders, or leave folders as they are. Shift-click works.
- Album page → **Kind of album** card: the stored decision or the suggestion, change it, write it
  to all files.
- **Release type tag** (Settings → Tagging): Plex shows albums under Soundtracks, Compilations
  and Singles & EPs.
- **Every write is checked**: each file is read back after writing; the album page shows
  "✓ Files checked" or exactly which tag didn't stick.

### Changed
- All album-wide writes (approve, album artist changes, policies, album tags for unmatched
  songs) now go through one write path with backups, Undo and the read-back check.
- The song index re-reads every file once after updating (to learn composers); suggestions are
  complete when that's done.

## 2.12.1 (2026-10-20)

### Fixed
- Songs no source matched still didn't get the album's tags when the review had been made before
  2.11 (the rows said "file left as is"), ticked or not. The album's tags are now worked out when you
  approve, so any review gets them; the row now says which tags it will get.

## 2.12.0 (2026-10-19)

### Added
- Album artists → **Same artist more than once in Plex**: Plex sometimes starts a second (third…)
  artist entry with the same name when albums are scanned at different times, e.g. many separate
  "Various Artists". Isaiarr lists them and merges them with Plex's own Merge, one name at a time or
  all at once. Names that differ only by case, spacing or an invisible character count as the same.
- Plex: same-name artists are merged automatically after **Re-add in Plex** and every night
  (Settings → Plex → *Merge same-name artists in Plex*).

## 2.11.0 (2026-10-19)

### Fixed
- Songs that no source could match were left completely untouched when an album was approved, so
  they kept no Album Artist (or the wrong album name) and Plex filed each under its singers,
  splitting the album and adding hundreds of artists. They now get the album's own tags (album,
  album artist, genre; the year and composer for normal albums, the compilation flag for
  collections). Their title and singers stay as they are and they aren't renamed. Not done for
  albums that are only a best guess. Settings → Tagging → *Give songs no source found the album's tags*.

### Added
- Album artists → *Songs without Album Artist*: **Fix N tagged albums** writes the album's tags to
  the unmatched songs of albums tagged before this version, straight away, without looking anything up
  again (undo per album). **Give selected the album's tags** does it for the folders you tick.

## 2.10.0 (2026-10-19)

### Added
- **Album artists** page: what makes Plex's artist list long, from your files' tags.
  - *Same name, other spelling* ("A.R. Rahman" / "A. R. Rahman" / "A R Rahman"): pick the spelling
    and Isaiarr writes it only to the files spelled another way (Album Artist only, undo per album),
    or mark them as different artists.
  - *Songs without Album Artist*, which Plex files under their singers: send the folders to the
    review queue to get the music director as Album Artist.
  - *Several names in one tag* (often singers instead of the music director): send to review, or
    change them all to one name.

### Fixed
- Re-add in Plex: albums split into disc folders (CD 1, CD 2…) are now hidden as a whole; hiding
  one disc couldn't make Plex let go of the album. Discs of the same album are re-added once.

## 2.9.0 (2026-10-19)

### Added
- Library → **Differs in Plex**: albums whose tags don't match what Plex shows, compared track by
  track. *Wrong artist* (Plex files the album under another artist: use **Re-add in Plex**) and
  *Old details* (album name or titles out of date: use **Refresh in Plex**). Albums with the wrong
  artist are also marked on every Library tab. **Check against Plex** re-reads Plex on demand
  (also done every night).
- **Refresh in Plex** on the album page and in the Library's bulk bar: unlocks and refreshes the
  album's details in Plex.

### Fixed
- "Select all N albums in this view" now only selects the albums of the current tab's filter on
  the Differs in Plex tab.

## 2.8.0 (2026-10-19)

### Added
- **Re-add in Plex** (album page and the Library's bulk bar) for albums Plex keeps under the wrong
  artist. Plex files an album under the artist it had when first added, and refreshing never moves it.
  Isaiarr saves the tracks' star ratings and play counts, hides the folder from Plex, scans and
  empties the trash, puts the folder back and scans again, then restores ratings and play counts.
  One album at a time; progress in Activity and on the album page. If Plex doesn't drop the album the
  folder goes straight back and nothing changes. Tracks drop out of Plex playlists. Your files are
  never changed; an interrupted run is put back on the next start.

### Changed
- Plex: before refreshing an album, Isaiarr unlocks its fields in Plex (album, sort album, date,
  genre, studio, poster, and the tracks' titles, artists and numbers), so the tags always win.
  Fields locked by an old match or a hand edit in Plex used to stay as they were. Can be turned off
  in Settings → Plex.

## 2.7.0 (2026-10-19)

### Added
- **Shift-click to select a range** of rows: in the Library, an album's track list and a playlist.
  Tick one row, hold Shift and tick another; everything in between follows (works for unticking too).
- Library: a note when music folders exist that Isaiarr has never checked, with **Check them** to
  analyze just those (nothing is written; changes wait in the review queue).
- Plex: **Test connection** now checks the whole setup: where Plex sees your music, whether rescans
  reach the right folder, and the library's *Prefer local metadata* setting.

### Fixed
- Plex didn't pick up tags written by Isaiarr when Plex sees the music under another path than this
  container (the Synology Plex package sees `/volume2/…`, the container `/music`). The path Plex
  uses is now learned from Plex itself; a wrong *Music folder as Plex sees it* is detected, logged and
  worked around.
- Plex: after the rescan, the album's details are refreshed too (after a short wait), so Plex
  re-reads changed album artist and titles. Can be turned off in Settings → Plex.

## 2.6.3 (2026-10-19)

### Added
- Hover over any name cut short with "…" (album, track, folder or file name, on any page) to see
  it in full in a small note. Only appears when the text really is cut off; also works with the
  keyboard (Tab to a link).

### Fixed
- Duplicates: on medium-width windows (about 820–1180 px) the file name column shrank to nothing;
  rows now wrap there like on phones.

## 2.6.2 (2026-10-19)

### Fixed
- Duplicates: files or folders deleted outside Isaiarr (File Station, another app) kept showing up.
  **Check library** now reads the disk again before comparing, and opening the Duplicates page
  drops any listed file that's no longer there. If the music folder is empty or not mounted, the
  song index is left alone instead of being emptied.

## 2.6.1 (2026-10-18)

### Added
- Duplicates: **Keep the best copy of all same-song files** on the *Same song, another file* tab
  (and *All*), like the existing button for identical files. Each group keeps the copy marked
  "Best copy"; the extras go to the bin and can be restored. *Possible* groups still need a look one
  by one.

## 2.6.0 (2026-10-18)

### Added
- **Send to review**: when a track or album looks wrongly tagged, send its whole album back to the
  review queue from wherever you spot it: the tag button on each Duplicates row, on each playlist
  song (without leaving the playlist or losing unsaved changes), **Send to review** on the album
  page, or the Library's bulk bar. The album is re-checked first and then stays in the queue even if
  the sources agree with the current tags, marked **Sent back** with where you sent it from. It is
  never auto-approved. New **Sent back** filter in the review queue; the mark clears once you approve
  or reject the album.

## 2.5.4 (2026-10-18)

### Fixed
- Duplicates: taking one file out of a group with **×** now removes only that file. The rest stay
  together in the same tab, even if they only matched through the file you took out (before, the
  whole group could disappear). Undo / Put back restores the full group.
- Duplicates: a leading track number ("02 - ", "1. ", "CD1 - 07 - ") is ignored when comparing
  titles, so untagged files named after the song still match each other.

## 2.5.3 (2026-10-18)

### Changed
- Duplicates: after taking a file out with **×**, the message now says what happened to the
  rest of the group: still a group (and under which tab), split into smaller groups, or closed
  because only one file was left or the others only matched through the removed one.

## 2.5.2 (2026-10-18)

### Changed
- Duplicates: the per-file "Not a duplicate" button is now a small **×** at the end of each row
  (turns red on hover; tooltip and screen-reader label say what it does). Right after removing a
  file, the message at the top has an **Undo** button. Tidier file rows on phones.

## 2.5.1 (2026-10-18)

### Added
- Duplicates: **Not a duplicate** on every file takes just that file out of its group (a remix,
  live or karaoke version grouped by mistake). The file isn't touched; it's simply never matched with
  those files again, even after renaming or retagging. Listed under *Not duplicates → Files taken
  out of groups*, with **Put back** to undo. The group-wide button is now called *Ignore this group*.

## 2.5.0 (2026-10-18)

### Added
- **Duplicates** page: finds the same song more than once in the library, on demand (*Check library*)
  and automatically whenever new music arrives or an album is written, plus a nightly re-check.
  - **Identical files**: the same audio data, even when the tags differ (FLAC's own audio
    checksum, or a hash of the audio part of MP3 / M4A files).
  - **Same song, another file**: the same recording in another file or format (a FLAC and an MP3
    of one song), found by audio fingerprints (Chromaprint, the AcoustID engine).
  - **Possible duplicates**: same title, a shared singer or music director, nearly the same length,
    in different folders.
  - Each group suggests the **best copy** (lossless first, then quality, complete tags, plays;
    bundle folders like "www…com" lose ties). Pick another if you like, then *Keep selected, move
    others to bin*, or mark *Not duplicates* (remembered). *Keep the best copy of all identical
    files* clears the certain ones in one go. Shows how much space you'd free.
  - **Bin**: removed copies move to a hidden `.isaiarr-bin` folder on the same share (with a
    `.plexignore`), can be restored, and are deleted for good after 30 days (adjustable) or when
    you empty the bin. Plex rescans the affected folders.
  - Discord message when new duplicates are found (can be switched off).
  - Settings → Duplicates: automatic checks, sound comparison on/off, strictness, Discord, bin
    folder and how long the bin keeps files.
- The Docker image now includes `fpcalc` (Chromaprint) for the sound comparison.

## 2.4.0 (2026-10-17)

### Changed
- **Settings redesigned** after the pattern used by GitHub, Linear, Vercel and macOS:
  - a side menu (Library · Automation · Connections · App) that opens **one page at a time**
    instead of one long scrolling page; Plex and Discord now have their own pages, and the menu
    shows a status dot for Plex, Discord and AI;
  - every setting is a row: name and explanation on the left, the control on the right, so text
    boxes, numbers and switches line up; on/off settings are switches;
  - a **"You have unsaved changes" bar** slides up at the bottom as soon as you change anything,
    with Save and Discard (Ctrl/Cmd+S also saves); leaving the page with unsaved changes asks first;
  - **search** across every setting (press `/`), jumping straight to the row;
  - test buttons (Check sources, Test connection, Send test message, Test AI) sit at the top of
    their own page; a page only saves the settings it shows.

### Fixed
- Metadata sources: every source's order box showed "4", so saving the page could reset the order.

## 2.3.3 (2026-10-17)

### Fixed
- Groq: the default model is now `openai/gpt-oss-120b` (Llama models left Groq's free tier in
  August 2026), with low reasoning effort, 150 songs per request and smaller song-description
  batches so requests stay inside the free tier's tokens-per-minute limit.

## 2.3.2 (2026-10-17)

### Fixed
- Playlists failing with "Read timed out" on Gemini. Gemini 3 models think at "medium" by default,
  which can be slow; Isaiarr now asks for **low** thinking (Settings → AI → Speed, limits and
  prices → Thinking), waits up to 5 minutes (adjustable), and if an answer is still late it tries
  once more with low thinking before giving up with a clear message.

## 2.3.1 (2026-10-17)

### Fixed
- "AI failed: Model or address not found: gemini-2.5-flash". Google now only serves the 2.5 models
  to keys that already used them. Isaiarr's default is now the current Flash model, and if Google
  says a model isn't available it asks which models your key can use and switches to the newest
  plain Flash model by itself (shown in Settings and on the Playlists page). A model you type in
  Settings is never swapped; you get a clear message instead.

## 2.3.0 (2026-10-16)

### Changed
- New logo: an "i" that runs into a fading sound wave, on an accent gradient tile (follows the accent
  colour you pick; the wave ripples when you hover it). Also the browser and home-screen icon.
- Tagline is now "Music tags & playlists", for libraries in any language.
- The Tamil-script logo font is no longer bundled (a little smaller image).

## 2.2.1 (2026-10-16)

### Changed
- Playlists screens redesigned in the app's design language (see `docs/design/PLAYLISTS.md`):
  composer with a "what happens" strip, sticky Song library column with a described-songs progress
  bar, recent playlists as tiles with mosaic covers built from the films inside.
- While a playlist is being made you now see each step live (reading the request, how many songs
  match, what was loosened, picking), in the same console style as the Scan page.
- Preview: hero with mosaic cover and editable name, a proper track table (#, song, film, mood, time),
  drag-to-reorder on desktop, "Like this" on every row, Try again on failures.

## 2.2.0 (2026-10-16)

### Added
- **Playlists** page: make Plex playlists from the songs you own.
  - *Describe it*: an AI turns your request into filters, Isaiarr narrows the library, and the AI
    picks from that list by song ID (it can't suggest songs you don't have). Filters it can't fill
    are loosened one at a time and listed.
  - *From a song*: songs like one you pick, matching music director, singers, era, mood or sound.
  - *Radio*: no AI; scores by shared director, singers, era and mood. Free and offline.
  - Preview with reorder, untick, rename; save to Plex as new, replace, or add to (duplicates
    skipped); *Make again*; history with tokens and cost.
- **AI services**: Google Gemini and Groq (free tiers), OpenAI, Anthropic, Ollama, and any
  OpenAI-compatible server. Plain HTTP, no extra packages. Monthly spending limit, request-rate
  limit, per-playlist token and cost count, *Test AI* in Settings.
- **Song index** of every file (from its tags), updated after each write and nightly, with a
  **Plex sync** that links songs to Plex by file path and copies ratings and play counts.
- **Song descriptions**: the AI labels each song once with moods, type and tempo, so playlists can
  filter by mood without extra calls. Optional automatic labelling of new songs.
- Settings → *AI & playlists*: service, model, keys, limits, prices, playlist size, songs per film,
  skip karaoke/remix/instrumental.

## 2.1.0 (2026-10-15)

### Added
- **Batch album artist change** (Library → select albums → *Set album artist…*). Writes only Album
  Artist (and, if ticked, Composer) to every file of the selected albums: no other tag changes and
  no files are renamed. Useful for merging spellings such as "Ilayaraja" / "Isaignani Ilaiyaraaja"
  into one name so Plex groups them under one artist. Suggestions come from the album artists
  already in your library. *Select all N albums* applies it to every album matching the current
  search and status, not just the page. Each album is backed up and can be undone from its page,
  and the new name is kept on later re-checks.

## 2.0.2 (2026-10-15)

### Fixed
- The album editor page could not be scrolled (its page style clashed with the review panel's).
- Album artist written by other taggers under another name (FLAC `ALBUM ARTIST` / `ALBUM_ARTIST`,
  MP3 `TXXX:ALBUM ARTIST`, M4A free-form `ALBUM ARTIST`) is now read, and removed when Isaiarr writes
  the album artist. Before, those leftovers kept the old value, so Plex and the review page could
  show it as if the edit had never been written. Same for `YEAR` next to `DATE`.

## 2.0.1 (2026-10-15)

### Fixed
- Editing the album artist (or album, year, genre) on the album page was written to the files, but
  the page and Library kept showing the source's value, and the next check of the folder proposed
  changing it back (and auto-approve could write it back). Your edits are now remembered for that
  folder: they're shown afterwards, and later re-checks keep them. Picking a different album under
  *Wrong album?* uses that album's name, year and genre, but keeps the music director you typed.
- With *Artist tag* set to put the music director in Artist, editing the album artist now updates
  Artist too (unless you also edited Artist yourself).

## 2.0.0 (2026-10-15)

A new interface, built from the design in [`docs/design/`](docs/design/DESIGN.md).

### Added
- **Sidebar layout** with Review queue (with count), Library, Scan, Activity and Settings, and a
  status card showing the watcher, the library folder, what's running, and Plex/Discord health.
- **Review queue as a split view:** album cards on the left, proposed changes on the right (album
  tags as *In file now → Proposed* with Fix / Fill / OK, track renames, singers, lyrics) and an
  *On approve* pipeline (write tags → rename → Plex rescan → Discord). One-click **Approve**;
  *Edit* opens the full editor. Filter chips: Fixes, Fill blanks, Various Artists, Low confidence.
- **Up to date** status: albums whose tags are already correct stay out of the queue.
- **Scan page:** auto-detect switch and settle time (applies without a restart), recently detected
  folders, manual scan with *only changed* or *re-check every file*, and a live progress panel with
  counts (already correct / queued for review / no match), a log, and **pause / resume**.
- **Settings:** *Only fill blanks* mode; *Queue every change for my approval* (the inverse of
  auto-approve); live preview and clickable fields for the rename pattern; Plex and Discord side by
  side with their last status; separate Discord switches for review, written and scan-miss messages;
  *Plex rescan after writing* switch; accent colour; one *Save changes* button for the whole page.
- Library page (every album, any state) and Activity page (changes written + log).

### Changed
- One dark theme with bundled fonts (Bricolage Grotesque, IBM Plex Sans, JetBrains Mono,
  Catamaran for the Tamil logo; all SIL OFL, see `app/static/fonts/LICENSES.txt`). The light/auto
  theme switch is gone; `?theme=` links still work.
- Match confidence is shown as a percentage (95 % and up green, 80–94 % amber, below red).
- Default settle time is 2 minutes, chosen from a list.
- Discord "auto-approved" switch is now "albums written" and covers manual approvals too (your old
  setting is carried over).

## 1.6.1 (2026-10-13)

### Added
- Ready to publish on GitHub: MIT licence, bug-report and feature-idea forms, contributing and
  security notes, README badges, install-from-image instructions, a note on the unofficial sources.
- "Report a problem" and "What's new" links in the footer and in Settings → System.

### Changed
- `docker-compose.example.yml` now uses the published image `ghcr.io/gera-devp/isaiarr:latest`
  (building from source is still one comment away).

## 1.6.0 (2026-10-13)

### Added
- **Settings page you can edit.** Every option except the folders and port can be changed in the
  app, grouped into Auto-approve, Album sources, Lyrics, Matching, Tag rules, File names, Watcher,
  Discord, Plex and Login. Changes apply immediately (watcher on/off and mode need a restart, with a
  *Restart now* button). Saved in `/config/settings.json`, which overrides the compose file; each
  field shows whether its value is saved here, from compose or the default, and can be reset.
- **Tokens and keys in the app**: Discogs token, Musixmatch API key / user token, Plex token,
  Discord webhook. Never shown back once saved; replace or remove them from the page.
- **Album sources**: switch each on/off and set the order; per-source options (iTunes store country
  and rate, JioSaavn rate, Wikipedia contact and rate, Discogs token and rate). *Check sources*
  tries each one and reports what happened.
- **Auto-approve** (off by default): albums that pass every rule (album confidence, every song's
  confidence, every file matched, watcher-found vs. also manual scans, collections allowed or not)
  are written without review. Albums you re-analyze yourself are never auto-approved. The album page
  shows an *Auto-approved* badge, or which rule stopped it; Discord lists auto-approved albums in
  their own summary (can be turned off). Default artwork and lyrics handling for auto-approve and
  *Approve batch* is configurable and preselected on album pages.
- **Optional login** (HTTP basic auth, password stored as a hash), works in browsers and nzb360.
  `AUTH_DISABLE=true` in compose is the escape hatch for a forgotten password.
- Live rename-pattern preview; protected tags as tick boxes; artwork size and MP3 tag version as
  menus; `/healthz` endpoint (used by the Docker health check).

### Changed
- Rate limits are read on every request, so changing them in Settings takes effect at once.
- The Discord notifier always runs, so a webhook added later in Settings works without a restart.

## 1.5.0 (2026-10-13)

### Added
- **Discogs as a fourth album source**, after Wikipedia (`SOURCES=itunes,jiosaavn,wikipedia,discogs`).
  Uses credits by role: "Music By" / "Composed By" for the music director and per-song composer,
  "Vocals" for singers (backing vocals and chorus ignored), including release-level credits that
  name the songs they cover ("A1 to A3, B2"). Reads LP sides, multi-disc CDs and index tracks.
  Searching needs `DISCOGS_TOKEN`; without it Discogs is skipped (noted on the album page) but a
  pasted Discogs release/master link still works. Covers come with the token.
- **Collection mode** (`COLLECTION_MODE`, on by default). Folders whose songs come from three or
  more different films ("Ilaiyaraaja Hits", downloaded bundles) are tagged as one compilation
  album named after the folder: Album Artist = the shared music director or Various Artists, each
  song keeps its own year / composer / singers, its film goes into the **Grouping** tag and the
  **Compilation** flag is set. Collections go to *Needs review* with a confidence based on how well
  the songs matched, and the album page explains what will happen.
- New tags written for all formats: Grouping (FLAC `GROUPING`, MP3 `TIT1`, M4A `©grp`) and
  Compilation (FLAC `COMPILATION`, MP3 `TCMP`, M4A `cpil`). Both are backed up and undone like the rest.

### Fixed
- An album that held almost none of a folder's songs (common for collections, where a source
  always returns *something*) could still be used as the folder's album. It now needs at least a
  third of the songs (`MIN_ALBUM_COVERAGE`); otherwise songs are matched one by one and the album
  page says why.
- Shortened file titles ("Kanmani" for "Kanmani Anbodu") are accepted when every word matches and
  the length agrees to within 3 seconds; look-alike songs from other films are still rejected.

## 1.4.0 (2026-10-12)

### Added: phone layout (works inside nzb360's Web Interfaces)
- Below 760px wide, every page reflows for a phone: no sideways scrolling anywhere.
  - Sections move to a bottom tab bar (Queue with the review count, Scan, History, Log, Settings).
  - Queue, History, Log and Settings show cards instead of tables. Tap a card to open the album;
    tick cards to get a selection bar with Approve / Re-analyze / Reject.
  - Queue tabs become a swipeable row of chips; search and refresh sit underneath.
  - Column headers are hidden on phones, so each list has a Sort picker instead.
  - Album page: each track is a card with labelled Title / Singers / Track / Disc fields, length,
    match, lyrics and changes; Approve / Approve & next / Re-analyze / Reject stay pinned to the
    bottom of the screen; the "Wrong album?" panel follows the tracks.
  - Tap a badge to see what hovering shows on desktop (old → new value, why lyrics are missing).
  - Bigger tap targets, 16px inputs (no zoom-in on focus), notch / gesture-bar safe areas.
- **In-page confirm dialogs** replace the browser's confirm() for "Approve batch" and "Undo".
  App web views can block native popups silently, which would have made those buttons do nothing.
- **Theme per device**: Auto / Dark / Light in Settings, or add `?theme=dark` to any URL (handy for
  the nzb360 Web Interface address). Remembered in a cookie on that device.
- Add-to-home-screen support (web app manifest, theme colour).

### Fixed
- Queue tabs keep their styling on desktop after the layout change.

## 1.3.0 (2026-10-12)

### Added
- **Sort by any column, on every table.** Click a header to sort; click again to reverse.
  - Queue (album, album artist, folder, confidence, status, updated), History (when, album,
    album artist, folder, files, undone) and Log (when, level, message) sort in the database, so
    the order covers every page, not just the one on screen. The chosen sort is kept in the URL,
    survives paging, the per-page choice, the search box and switching queue tabs.
  - An album's track table (file, title, singers, #, disc, time, match, lyrics, other changes) and
    Settings sort instantly in the browser and keep anything you've typed into the fields.
  - Text sorts ignore case and treat numbers naturally ("track 2" before "track 10"); empty
    values always go last; dates and numbers start newest/largest first.
  - Keyboard: headers are focusable and sort with Enter or Space.

## 1.2.2 (2026-10-12)

### Fixed
- **"Apply failed: 'bool' object is not iterable" on M4A files.** iTunes-style M4A files carry
  single on/off flags (part of a compilation, gapless playback) that the tag backup treated as
  lists. Backup and undo now handle them, and undo restores them exactly.
- One bad file no longer stops a whole album. Apply backs up each file first; if anything fails
  for a file, its original tags are put back and the rest of the album carries on. The album page
  lists which files couldn't be updated.
- A corrupt or mis-named audio file no longer fails the analysis of its folder; it's skipped and
  named in the album notes.

## 1.2.1 (2026-10-12)

### Fixed
- **"Use" on the album page looked like it did nothing.** Re-match jobs queued behind every
  album of a library scan, and the page never updated. Actions you click in the UI (Use, Match,
  Re-analyze, Approve, Undo) now jump ahead of background scans, a second click replaces a still
  -waiting request instead of being ignored, the button shows "Matching…" straight away, and the
  album page shows a live status ("Waiting — N jobs ahead", "Analyzing…") and reloads itself when
  the new match is ready.
- **Song-by-song lookup accepted the wrong songs** (e.g. "Enn Manam" → a Malayalam devotional
  "En Manam Ponnambalam", "Jintha Jintha" → "TeenMaar Jinthaak"). It now rejects other-language
  versions for Tamil albums, recordings more than 8 s longer/shorter, and weak title matches;
  a song from a different album must match the title almost exactly and the length within 4 s.
- **Year showed the streaming re-release date** (2022 for a 1988 film). The earliest year given by
  any source for the same album with the same music director is used, with a note saying where
  it came from.
- Folder names like "Ilayaraja (2800 Tamil Mp3 Songs - from 585 Movies)" or "… - www.site.com"
  are cleaned before searching.

### Added
- Refresh button on the queue, album, history and log pages.
- Page numbers on long lists (queue, history, log): "51–75 of 1,234", numbered pages with
  first/last, and a per-page choice (25 / 50 / 100 / 200).

## 1.2.0 (2026-10-12)

### Added
- **Wikipedia as a third album source**, after iTunes and JioSaavn (`SOURCES=itunes,jiosaavn,wikipedia`).
  Reads film and soundtrack articles: the `{{Track listing}}` template on newer pages and the
  Song / Singers / Length table on older ones. Gives per-song singers, per-song composer when
  listed, the music director from the infobox, release year, and "Tamil" genre for Tamil films.
  Picks the Tamil listing on pages that also list dubbed versions.
- When the winning source has no artwork (Wikipedia), the cover is taken from the closest-named
  iTunes/JioSaavn candidate; the review page notes where it came from.
- Album search on the review page has a Wikipedia option; the rematch box accepts
  `en.wikipedia.org/wiki/...` URLs.
- `WIKIPEDIA_RPM` (30) and `WIKIPEDIA_CONTACT` settings.

### Changed
- New dependency: `mwparserfromhell` (wikitext parser). Rebuild the image to pick it up.

## 1.1.0 (2026-10-12)

### Added
- **JioSaavn as a fallback album source.** Searched when the iTunes match is below
  `FALLBACK_BELOW` (8.0) or covers less than `FALLBACK_COVERAGE` (90%) of the files; the more
  complete match wins. Music director and singers come from JioSaavn's separate credits.
- **Song-by-song lookup for left-over files** (`TRACK_FALLBACK`). Files that aren't in the matched
  album's track list are searched individually (JioSaavn, then iTunes). They keep the folder's
  album and get title, singers and composer; no track number is invented.
- Albums with no match at all are now built from song-by-song results instead of being left empty.
- **JioSaavn lyrics fallback** (`JIOSAAVN_LYRICS`) when Musixmatch has nothing.
- Musixmatch retries with looser searches (no artist/album, relaxed length) before giving up.
- Review page: source badge (iTunes / JioSaavn) on the album and every candidate, "N of M tracks
  matched", per-song "found individually" marker with the song's own film, and a reason on hover
  for every "none" lyrics badge.
- Album search on the review page can search either source; the rematch box accepts Apple Music
  URLs (new `/album/name/123` and old `/id123` formats) and JioSaavn album URLs.
- Per-track composer: a JioSaavn album with several composers gets the right one on each song.

### Fixed
- Track table header overlapping the first rows (sticky header inside a scrolling box).
- File names wrapping one letter per line, and the Artist box cutting off singer names.
- Unmatched files showed as included with a 0.0 score and no explanation; they are now unticked,
  labelled "no match" and skipped by bulk approve (tick them to add only artwork).
- Album-level fields on the review form overwrote per-track values even when not edited.
- Musixmatch matches rejected too eagerly for long Tamil titles ("Naan Thedum" vs
  "Naan Thedum Sevvanthi Poovithu").

### Changed
- `docker-compose.yml` ships as `docker-compose.example.yml`, so updating the app no longer
  overwrites your own compose file. The example builds locally on the NAS by default.
- Album references are stored as `itunes:<id>` / `jiosaavn:<id>`. Albums analyzed by 1.0 still
  open and apply normally; re-analyze them to get the new sources.

## 1.0.0 (2026-10-12)
- First release: iTunes + Musixmatch tagging, review queue, watcher, manual folder scan, Discord,
  Plex partial rescans, renaming, full undo.
