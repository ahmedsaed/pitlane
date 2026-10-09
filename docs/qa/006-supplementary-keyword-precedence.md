# 006 – Do supplementary keywords take precedence over core session keywords?
- Requirement(s): REP-2, REP-7

## Question
In the keyword table in `docs/replays.md`, RACE matches "race" or "grand prix" "(and none of the above)", and SUPP comes after RACE. Names such as "British Grand Prix Pre-Race Build-Up", "Race Highlights" or "Pre Qualifying" contain core keywords as well as SUPP keywords. As written, they would be classified as RACE or QUALI and take the core session's episode slot.

Interim behavior (`# QA-006` in `src/pitlane/replays/parsing.py`): if any SUPP keyword is present, the file is SUPP. The more conservative error is to file a race as supplementary, not a highlights show as the race. If a name contains two different practice numbers ("fp1 fp2"), the step does not match.

## Answer
