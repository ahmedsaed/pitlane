# 004 – Are manual (supplementary) programmes marked `<new/>`?
- Asked by: Implementer agent · 2026-10-09
- Requirement(s): EPG-3, EPG-6, EPG-7

## Question
`docs/epg.md` says `<new/>` is set for "every session programme" and never for filler. Manual programmes (EPG-7) are neither. Should they carry `<new/>`, so that a "new episodes only" series rule records them as well?

Interim behavior (`# QA-004` in `src/pitlane/xmltv.py`): manual programmes are marked `<new/>`, use the series title, and use episode number `round × 100 + 10 + supplementary_index`.

## Answer
- Answered by:
- Approved by:
