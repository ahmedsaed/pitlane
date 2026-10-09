# 012 – Web UI without a password when bound beyond localhost
- Asked by: Implementer agent · 2026-10-09
- Requirement(s): OPS-1, NFR-8 (architecture.md "Security")

## Question
`architecture.md` says the web UI "requires a password or reverse-proxy auth when bound beyond localhost". Pitlane cannot detect whether a reverse proxy is enforcing auth. If `server.ui_password` is unset and the bind address is not loopback, should Pitlane refuse to start, disable the UI, or only warn?

Interim behavior (`# QA-012` in `src/pitlane/app.py`): it logs a warning and serves the UI without auth. With `ui_password` set, the UI and `/api/*` require HTTP Basic auth (any user name). Tuner, XMLTV, `/healthz`, `/metrics` and static assets stay open because PMS and monitoring need them.

## Answer
- Answered by:
- Approved by:
