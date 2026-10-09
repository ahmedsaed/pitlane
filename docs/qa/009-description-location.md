# 009 – Location shown in descriptions and summaries: country or locality?
- Requirement(s): EPG-3, REP-6

## Question
The examples read "Marina Bay Street Circuit, Singapore". Jolpica gives both `Location.locality` and `Location.country`. For Singapore they can be the same or differ ("Marina Bay" / "Singapore"). Which field is meant? For most circuits they differ (for example "Silverstone" / "UK").

Interim behavior (`# QA-009` in `src/pitlane/xmltv.py` and `src/pitlane/replays/spoilers.py`): `{circuitName}, {country}`.

## Answer
