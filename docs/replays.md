# Replays

## Sources of replay files

1. **Plex DVR recordings** of Pitlane's own channels. These are the main path. A single "record series" rule on `Formula 1` (EPG-6) records every session automatically.
2. **Drop folder** (`directory` provider): files the operator adds by hand.
3. **Replay providers**: `replay`-capable providers return `ReplayItemSpec`s, which Pitlane downloads (REP-4).

All three end up in an **inbox** folder and go through the same match → file → metadata pipeline.

## Detecting finished files

- Plex DVR writes into a temporary `.grab/` folder and moves the finished recording into the DVR library folder **[verify]**. Pitlane watches that destination folder and ignores `.grab/`.
- For other inboxes, a file is treated as finished when its size and mtime haven't changed for 30 s and no Pitlane download job owns it.
- Ignored files: names starting with `.`, partial download suffixes (`.part`, `.tmp`), and anything under 5 MB.

## Matching (REP-2)

Pitlane tries the steps in order and stops at the first that gives exactly one session:

1. **Episode number.** Read `SxxxxEyyyy` from the filename, or from the container's metadata tags. `S2026E1705` → season 2026, round 17, order 5.
   - Plex DVR schedules recordings as `Formula 1 - E1703 - Singapore Grand Prix – Sprint`, **without a season** **[verified 2026-10-09, real PMS]** (from PMS's DVR log). Accept `E(\d{3,4})` after the series title as an episode number when the season can be taken unambiguously from the parent folder name (`Season 2026`) or, failing that, from the year of the file's recording start. Otherwise fall through to steps 2 and 3. The final on-disk path Plex DVR uses is still **[verify]** (first real recording).
2. **Filename parsing.** Normalize the name (lowercase, separators → spaces) and extract:
   - season: `\b(19|20)\d{2}\b`
   - round: `\bround\s*(\d{1,2})\b` | `\br(\d{1,2})\b` | `\b(\d{4})x(\d{2})\b` (season×round)
   - event: fuzzy match (token-set ratio ≥ 85) against the season's `raceName`, circuit name, and locality (e.g. "silverstone" → British GP)
   - session: keyword table below
3. **Time.** If the file's recorded start time (container `creation_time`, or mtime as a last resort) falls within a padded session window, that session wins.

| Keywords (any) | Session |
|---|---|
| `fp1`, `practice 1`, `practice one`, `free practice 1` | FP1 |
| `fp2`, `practice 2`, `practice two` | FP2 |
| `fp3`, `practice 3`, `practice three` | FP3 |
| `sprint qualifying`, `sprint shootout`, `sq` | SQ |
| `sprint` (and not one of the above) | SPRINT |
| `qualifying`, `quali` (and not sprint) | QUALI |
| `race`, `grand prix` (and none of the above) | RACE |
| `build up`, `buildup`, `pre race`, `pre qualifying`, `post race`, `analysis`, `notebook`, `show`, `highlights` | SUPP |

Rules:

- If a step yields more than one candidate, it does not match. Pitlane moves on to the next step, and if none match, the file goes to `needs-review` with every candidate listed.
- SUPP items keep their original descriptive title as the episode title and get the next free index in `round × 100 + 11..99`.
- SUPP items have no fixed target before an index is assigned. If an incoming SUPP file is byte-identical to a SUPP item already filed for the **same event**, that existing path counts as its own target ("already filed", REP-3), not as a `duplicate` (decision recorded from the implementer's question during the REP-3 fix).
- A manual assignment in the UI is saved as a rule keyed on the file hash. Re-processing the same file uses the saved assignment.

## Library layout (REP-3)

Plex TV naming, with the season as the year and the episode numbering from `epg.md`:

```
<library_root>/
  Formula 1/
    Season 2026/
      Formula 1 - S2026E0101 - Australian Grand Prix - Practice 1.mkv
      Formula 1 - S2026E0104 - Australian Grand Prix - Qualifying.mkv
      Formula 1 - S2026E0105 - Australian Grand Prix - Race.mkv
      Formula 1 - S2026E0111 - Australian Grand Prix - Pre-Race Build-Up.mkv
```

- The episode number is zero-padded to 4 digits so sorting works. Plex reads `E0105` as episode 105 **[verify]**.
- Filenames are sanitized: `/\:*?"<>|` are removed and whitespace is collapsed.
- The container is kept (`.ts`, `.mkv`, `.mp4`). Optional `remux_to: mkv` runs `ffmpeg -c copy` before filing. This is safe because there is no re-encode.
- `file_mode: move | hardlink | copy` (default `move`; with `hardlink`, the DVR copy stays in place).
- **Idempotency:** if the target exists and its SHA-256 is equal, the source is deleted in `move` mode and the result is "already filed". If the target exists with a different hash, the incoming file goes to `needs-review` with reason `conflict`. Pitlane never overwrites.
- **Duplicates elsewhere in the library:** if a file with the same SHA-256 is already filed under a *different* target path, the incoming file MUST NOT be deleted. It goes to `needs-review` with reason `duplicate` and the existing path listed as the candidate. Pitlane only deletes an incoming file in the exact-target case above. Matching always runs first, and log lines name the episode the incoming file matched, not the episode of the existing file.

Alternative for REP-7 (`supplementary_mode: extras`): supplementary items go to `Season 2026/Extras/<title>.mkv` as Plex local extras **[verify]**.

## Plex metadata (REP-5)

The library MUST use the local-only **Personal Media Shows** agent; an online agent renames the show after an unrelated online match (**[verified 2026-10-09, real PMS]**). Pitlane sets the rest through the PMS HTTP API with `X-Plex-Token`.

1. Targeted scan: `GET /library/sections/{section_id}/refresh?path={url-encoded folder}`
2. Poll `GET /library/sections/{section_id}/all?type=4&episode.index={n}&season.index={year}`, or search by file path through `/library/metadata/{showKey}/allLeaves`, until the item appears (timeout 5 min, backoff).
3. Write the metadata: `PUT /library/sections/{section_id}/all?type=4&id={ratingKey}&title.value=…&title.locked=1&summary.value=…&summary.locked=1&originallyAvailableAt.value=YYYY-MM-DD&originallyAvailableAt.locked=1`
4. Episode thumbnail (optional): `POST /library/metadata/{ratingKey}/posters?url={image_url}`, using a generated title card (see below) served from Pitlane.
   - PMS discards an upload made while it is still processing a newly added item; its own generated thumbnail then wins. Observed: the card was fetched and the POST returned 200, but the card was not kept; the same upload a few minutes later was kept and selected **[verified 2026-10-09, real PMS]**.
   - Pitlane MUST therefore verify: after uploading, `GET /library/metadata/{ratingKey}/posters` and check that an entry with an `upload://` key is `selected`. If not, retry the upload with backoff (30 s, 60 s, 120 s, 240 s; give up after ~10 min), then log a warning. Log success too.
   - The card URL is fetched by PMS from `server.advertised_url`, so `/static/cards/…` MUST stay reachable without the dashboard password (cards are spoiler-safe by design).
5. Show and season (once): title `Formula 1`, season titles `2026 Season`, season poster from config.

Confirm all PMS endpoints against current Plex documentation, and keep them behind one `PlexClient` class so they're easy to change **[verify]**.

### Title cards

Pitlane renders a 1280×720 PNG per session (event name, session, circuit, date, round badge) using a template and a bundled font. Cards carry no results, so they're spoiler-safe.

## Spoiler mode (REP-6)

- Default summary (spoiler-safe): `Round 17 · Marina Bay Street Circuit, Singapore · Sunday 11 October 2026`.
- Every 10 min, Pitlane reads `viewCount` for filed items from PMS for `plex.spoiler_user` (via that user's token, or the server owner's if unset).
- Once an item is watched and `spoiler.reveal_after_watch: true`, Pitlane fetches the matching results from Jolpica and appends the top 3 (plus pole for QUALI or SQ) to the summary. It never changes the title.
- Spoiler mode also hides results in Pitlane's own UI unless the viewer toggles it off for the page.
- Results are never written into thumbnails.

## Downloads from replay providers (REP-4)

- The queue key is `(event_id, session_type, provider_item_id)`. Done or in-progress keys are skipped.
- `ffmpeg -i <input> -c copy -f mpegts <inbox>/.dl/<key>.ts.part`. On success, the file is renamed into the inbox. On failure, the job retries 3 times with backoff and then stays `failed` (visible in the UI, can be retried by hand).
- `downloads.concurrency` defaults to 1. `downloads.window` MAY limit downloads to certain hours.
- If a library item for the same session already exists, the download is skipped unless `prefer_provider_over_dvr: true`.

## Retention (REP-8)

```yaml
retention:
  enabled: false
  dry_run: true
  keep:            # seasons to keep per session type; "all" = forever
    RACE: all
    QUALI: all
    SPRINT: all
    SQ: 2
    FP1: 1
    FP2: 1
    FP3: 1
    SUPP: 1
```

Deletion goes through PMS (`DELETE /library/metadata/{ratingKey}`, when "allow media deletion" is enabled in Plex) so the library stays consistent. Dry-run logs what would be deleted and shows the list in the UI.
