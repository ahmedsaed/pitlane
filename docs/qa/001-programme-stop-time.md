# 001 – Is a programme's stop time computed from the official start or the padded start?
- Asked by: Implementer agent · 2026-10-09
- Requirement(s): EPG-4, acceptance criterion 4

## Question
`docs/epg.md` says: "`start` = official start − `pre_roll`. `stop` = start + nominal + `post_roll`." Read literally, `stop` is computed from the *padded* start, so the effective padding after the nominal end is `post_roll − pre_roll` (10 min for a race with the defaults). EPG-4 instead describes the window as the official start plus pre-roll and post-roll padding, which suggests `stop = official start + nominal + post_roll`. Which is intended?

Interim behavior (`# QA-001` in `src/pitlane/xmltv.py`): `stop = official start + nominal + post_roll`. This gives the longer window, so a DVR recording never cuts off the end of a session. With the defaults, a 12:00 UTC race runs 11:50–14:20 UTC.

## Answer
- Answered by:
- Approved by:
