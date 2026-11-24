# Isaiarr 3.0: redesign

Status: done. Phases 1–3 shipped in 2.13.0–2.15.0; phase 4 (clean-up) completes it as 3.0.0.

## Why

The app grew one fix at a time, and it shows:

- **Too many places to change one thing.** Album artist alone can be changed in five places: the review page, *Set album artist…*, the spelling merges, *Several names in one tag* and *Fix tagged albums*. Each has its own rules.
- **Decisions don't stick.** A re-check can overwrite what you chose, and songs no source matched were skipped. You approve something, and part of it doesn't happen.
- **Plex is a black box.** Isaiarr asks Plex to rescan and hopes. It never checks the result, and the counts are only compared once a night.
- **Nine sidebar items.** Several of them (Album artists, Differs in Plex, Scan) are views of the same thing.

## Ideas taken from other apps

| App | Idea | How Isaiarr uses it |
|---|---|---|
| **MusicBrainz Picard** | Files are grouped into an album *cluster* first. The whole cluster is matched and saved together. | A folder is the unit. One decision covers every file in it, matched or not. |
| **Mp3tag** | Tag from the folder name: `%_directory%` becomes Album. | Folder rules: "folder name contains *Hits, Collection, Best of*" means a collection named after the folder, with Album Artist = Various Artists. |
| **beets** | Import "as-is" and mark compilations (`comp=1`, albumartist = Various Artists). Plugins tell Plex after a write. | A *Collection* policy per folder; a Plex step after every write. |
| **Plex itself** | The `RELEASETYPE` tag decides the section: `single`, `ep`, `album;compilation`, `album;soundtrack`. A live websocket reports every item added, updated or deleted, without Plex Pass. | Write the release type. Listen live instead of polling nightly. |

## The new model: every folder has one decision

Each album folder gets a **policy**, chosen by rules and confirmed or changed by you:

| Policy | Album Artist | Album | Release type | Typical case |
|---|---|---|---|---|
| **Film soundtrack** | the music director | the film | `album;soundtrack` | One composer, matched film |
| **Various Artists** | Various Artists | the film or album | `album;soundtrack` / `album` | Several composers on one album |
| **Collection** | Various Artists, or the shared composer | the folder name | `album;compilation` | "Hits of Mohd. Rafi", bundles |
| **Single** | Various Artists (suggested) or the composer | the single's title | `single` | One-track releases |
| **Artist album** | the artist | from the source | `album` | Non-film albums, Western music |
| **Keep as is** | — | — | — | Folders you don't want touched |

**Rules (suggestions, never silent writes):**
- More than one composer across the tracks → **Various Artists**.
- Exactly one track, released as a single → **Single → Various Artists**.
- Folder name matches *Hits / Collection / Best of / Top / Songs of / Melodies*, or songs from three or more films → **Collection**.
- Album Artist currently holds several names, or the singers → suggest **Film soundtrack** with the composer, or **Various Artists**.
- A spelling variant of a known artist ("A R Rahman") → the canonical spelling.

**The policy is stored per folder and wins over every re-check.** It's written to every file in the folder, matched or not. Track-level tags (title, singers, track number) still come from the sources, file by file.

## Screens (sidebar goes from 9 items to 6)

1. **Home.** Library health in one place:
   - Counts: files, albums, album artists, tracks, side by side with Plex's live counts.
   - A to-do list: folders needing a decision, Plex differences, duplicates, unmatched songs.
   - Plex status: connected, scanning, the last change Plex reported.
2. **Review.** As today, plus the folder policy at the top of each album ("Collection → Various Artists, because the folder is named *Hits of …*"). Change it there.
3. **Library**, with three views of the same data:
   - **Folders.** A tree like a file manager. Every folder shows its policy, current Album Artist(s), file and Plex state, and a ⚠ when tags disagree. Select a folder (or many, or a parent and all its subfolders), choose a policy or type an Album Artist, preview what changes, apply. This is requirement 4.
   - **Albums.** Today's list with filters (needs a decision, differs in Plex, not checked yet…) replacing the separate pages.
   - **Artists.** Every Album Artist with album counts, spelling variants and same-name duplicates in Plex, each with a fix.
4. **Duplicates.** Unchanged.
5. **Playlists.** Unchanged.
6. **Activity.** Every write with Undo, every Plex step, and failures that need attention.

Scan, Album artists and Differs in Plex fold into Home and Library.

## One write path, verified

Every change goes through the same steps, whether it's an approval, a policy change or a spelling merge:

1. **Plan.** List exactly which files get which tags, and show it before writing.
2. **Write.** Back up first, then write; Undo as today.
3. **Check the files.** Read every file back and confirm each tag is what was planned. Anything that didn't stick is reported on the album with the reason.
4. **Tell Plex.** Rescan that folder, unlock fields, refresh, and merge same-name artists.
5. **Check Plex.** Wait for Plex's live "item processed" events, then compare Plex's album and Album Artist with the files. The album shows **✓ Files · ✓ Plex**, or which step failed.

This makes "hit or miss" visible and fixable: you see the album that didn't take, and why.

## Live Plex sync

- **A websocket connection to Plex.** It gets the timeline events for added, processed and deleted items as they happen, so counts and each album's Plex state update within seconds.
- **No Plex Pass needed.** Webhooks would need it; the websocket doesn't.
- **A full comparison stays as a safety net.** It runs nightly, and on demand from Home.

## What goes away or merges

- *Set album artist…*, spelling merges, *Several names in one tag*, *Fix tagged albums* → one action: **Set folder policy / Album Artist**, from the Folders and Artists views.
- *Differs in Plex* tab → a filter in Albums, plus the ✓/✗ Plex state on every album.
- The Scan page → Home (auto-detect status, *Check folder…*) and Library (*not checked yet*).

## Build phases

1. **Foundation.** Folder policies, the rules engine, the release-type tag, the single verified write path. Every existing action switches to it.
2. **Library Folders view and Artists view.** The new sidebar.
3. **Live Plex sync and Home.**
4. **Remove the old pages and tidy up.** Settings regrouped to match.

Each phase is shipped and testable on its own. Existing data (albums, backups, history, playlists, duplicates) is kept and migrated.
