# 005 – `<date>` element position differs from the XMLTV DTD
- Requirement(s): EPG-3, acceptance criterion 4

## Question
The example `<programme>` in `docs/epg.md` puts `<date>` after `<episode-num>`. The XMLTV DTD orders a programme's children as title, sub-title, desc, credits, date, category, …, episode-num, …, new. A validating consumer could reject the example's order. Should the output follow the example or the DTD?

Interim behavior (`# QA-005` in `src/pitlane/xmltv.py`): DTD order (`title, sub-title, desc, date, category…, episode-num…, new`). The golden files are generated in that order.

## Answer
