# 010 – Results for Sprint Qualifying (and "pole")
- Asked by: Implementer agent · 2026-10-09
- Requirement(s): REP-6

## Question
`replays.md` says that once an item is watched, Pitlane appends "the top 3 (plus pole for QUALI or SQ)". `epg.md` lists results endpoints for races, sprints and qualifying, but none for sprint qualifying. Where do SQ results come from? And for QUALI, is "pole" simply P1 of the qualifying results, shown separately?

Interim behavior (`# QA-010` in `src/pitlane/replays/spoilers.py`): results are revealed for RACE (`results`), SPRINT (`sprint`) and QUALI (`qualifying`). QUALI shows "Pole: P1 driver" followed by the top 3. SQ, practice and supplementary items never get results.

## Answer
- Answered by:
- Approved by:
