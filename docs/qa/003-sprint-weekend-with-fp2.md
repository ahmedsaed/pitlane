# 003 – Episode order for sprint weekends that also list FP2/FP3
- Requirement(s): EPG-3, REP-2

## Question
The order table in `docs/epg.md` defines a sprint weekend as FP1=1, SQ=2, SPRINT=3, QUALI=4, RACE=5. Some older seasons' sprint weekends also list `SecondPractice` (and, in one format, no sprint qualifying at all). FP2 has no order on a sprint weekend, and taking the normal-weekend value (2) would collide with SQ. What episode number should such sessions get?

Interim behavior (`# QA-003` in `src/pitlane/calendar/jolpica.py`): sessions in the weekend's own order table claim their slots first. A leftover session (for example FP2 on a sprint weekend) uses its normal-weekend slot if that slot is free. Otherwise it is left out of the guide and a warning is logged. Two sessions never share an episode number.

## Answer
