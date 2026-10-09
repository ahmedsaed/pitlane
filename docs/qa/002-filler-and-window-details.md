# 002 – Filler sub-title format, "local time" zone, and guide window boundaries
- Asked by: Implementer agent · 2026-10-09
- Requirement(s): EPG-2, EPG-5

## Question
1. Filler sub-titles are `Next: {next session title} – {local time}`. Which time zone is "local": the server's, the circuit's, or one the operator configures? There is no time-zone setting in `docs/configuration.md`.
2. What exactly is `{next session title}`? Only the session title ("Race") or the event plus the session ("Singapore Grand Prix Race")? What date/time format should `{local time}` use?
3. What should a filler programme's sub-title be when no later session is known (for example, at the end of the season or on channels with no series tag)?
4. The window is "1 day back, 14 days ahead". Are the boundaries aligned to midnight UTC (whole days) or relative to the current instant?

Interim behavior (`# QA-002` in `src/pitlane/xmltv.py` and `src/pitlane/config.py`):
1. Added an optional `epg.timezone` setting (IANA name, default `UTC`). With the default, the output is the same as having no setting. The DST golden test uses `Europe/London`.
2. `Next: Singapore Grand Prix Race – Sun 11 Oct 12:00 UTC`: event name and session title, then weekday, day, month, 24-hour time and the zone abbreviation. Times use the official (unpadded) start.
3. The filler has no sub-title.
4. The window starts at 00:00 UTC `days_back` days before today and ends at 24:00 UTC on the `days_ahead`-th day after today. Session programmes that overlap a boundary are included unclipped. Filler is clipped to the window.

## Answer
- Answered by:
- Approved by:
