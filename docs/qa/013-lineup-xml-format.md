# 013 – Format of `/lineup.xml`
- Asked by: Implementer agent · 2026-10-09
- Requirement(s): LIVE-1

## Question
`tuner.md` lists `GET /lineup.json` "(also `/lineup.xml`)" but does not define the XML shape.

Interim behavior (`# QA-013` in `src/pitlane/tuner.py`): `<Lineup><Program><GuideNumber/><GuideName/><URL/></Program>…</Lineup>`, with the same fields as the JSON entries.

## Answer
- Answered by:
- Approved by:
