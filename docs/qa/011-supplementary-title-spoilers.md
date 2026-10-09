# 011 – Supplementary titles taken from filenames may contain spoilers
- Asked by: Implementer agent · 2026-10-09
- Requirement(s): REP-6, REP-7, acceptance criterion 7

## Question
SUPP items "keep their original descriptive title as the episode title". That title comes from a filename Pitlane did not write, and it can contain a result ("… – Rivera wins in Singapore"). Pitlane then writes it to PMS as the episode title. Does REP-6 ("no Pitlane-written string contains a driver or team name from the results") require scrubbing or replacing such titles while spoiler mode is on?

Interim behavior (`# QA-011` in `src/pitlane/replays/matcher.py`): the descriptive title is used after removing series, event, season, round and quality tokens. It is not checked for result names.

## Answer
- Answered by:
- Approved by:
