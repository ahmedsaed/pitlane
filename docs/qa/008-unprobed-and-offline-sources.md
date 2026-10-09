# 008 – Which sources are tried at tune time before the first probe, and are offline ones ever tried?
- Asked by: Implementer agent · 2026-10-09
- Requirement(s): LIVE-5, LIVE-6

## Question
LIVE-6 says to use the first `online` source, falling back to `degraded` ones. Right after start-up or a config reload, no source has been probed yet. Should unprobed sources be tried, and in what order? And should `offline` sources ever be tried when nothing else is left? A source may have recovered since the last probe, and before a session the probe interval can be up to 5 min.

Interim behavior (`# QA-008` in `src/pitlane/channels.py`): order is `online` → `degraded` → not yet probed, each tier in priority order. `offline` sources are never tried; if none are left, the request gets 503.

## Answer
- Answered by: Spec author · 2026-10-09
- Approved by: (pending monitor) · 2026-10-09

Keep the interim order (`online` → `degraded` → not yet probed, each in priority order). When nothing is left in those tiers, do not fail immediately: re-probe the `offline` sources in priority order within `tune_timeout` and start the first that passes; return 503 only if none pass. Reason: after start-up, or when a source recovers between scheduled probes, the user's first tune should succeed. A successful re-probe updates the source's health state. Encoded in SPEC.md LIVE-6.
