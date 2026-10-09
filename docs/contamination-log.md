# Contamination Log

Record every exposure to third-party materials in the same problem space (see `clean-room.md` §1 and §4). Exposure is not misconduct, but undeclared exposure is. Add a row **before** you start work on a component.

| Date | Person | Material seen (product, repo, or site; what was looked at) | Components they must not implement |
|---|---|---|---|
| 2026-10-09 | Spec author (initial draft) | Observed a third-party sports-stream aggregator website as a user: its page structure, network requests, and public JSON. No code copied into this spec. | Spec author only; does not implement. |
| 2026-10-09 | Implementer agent (Claude, AI) | None beyond this repository and the permitted references in `clean-room.md` §3 (library documentation/source of dependencies only). Did not visit any streaming/replay/aggregator site or any domain from this log; did not read adjacent projects (Threadfin, xTeVe, Telly, Plex DVR scripts). Jolpica fixtures were hand-written from the response shape in `docs/epg.md`, not fetched. Note: during two manual end-to-end smoke runs (not tests), the running service made its normal startup schedule request to the Jolpica API (the permitted calendar source); the responses were not inspected or used for fixtures. | None. |
