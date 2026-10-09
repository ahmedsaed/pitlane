# Pitlane

Pitlane is a self-hosted bridge between **motorsport video sources you are entitled to use** and **Plex Media Server**.

It does three things:

1. **Live** – presents your live sources to Plex as a virtual HDHomeRun tuner, so they appear in Plex Live TV with a working guide and can be recorded by the Plex DVR.
2. **Guide** – builds an XMLTV guide from the official F1 calendar (Jolpica API), so every practice, qualifying, sprint and race appears as a proper programme with titles, descriptions and episode numbers.
3. **Replays** – takes finished sessions (Plex DVR recordings, files you drop in a folder, or a configured replay provider) and files them into a clean, consistently named Plex library with metadata and optional spoiler protection.

Pitlane ships **no content sources**. Every channel and replay comes from a provider the operator configures.

This repository is a **clean-room specification**. It describes behavior and interfaces only, and it is written so that an implementation team can build Pitlane without consulting any existing product's code. Read [docs/clean-room.md](docs/clean-room.md) before contributing.

## Documents

| Document | Purpose |
|---|---|
| [SPEC.md](SPEC.md) | Requirements (functional and non-functional) and acceptance criteria. The normative document. |
| [docs/clean-room.md](docs/clean-room.md) | Clean-room process: roles, permitted references, contamination rules, scope rules |
| [docs/architecture.md](docs/architecture.md) | Components, data flow, data model, process layout |
| [docs/providers.md](docs/providers.md) | Provider interface and the built-in generic providers |
| [docs/tuner.md](docs/tuner.md) | HDHomeRun emulation and stream relay (live path) |
| [docs/epg.md](docs/epg.md) | Calendar ingestion and XMLTV generation |
| [docs/replays.md](docs/replays.md) | Replay ingestion, naming, metadata, spoiler mode |
| [docs/plex-setup.md](docs/plex-setup.md) | How an operator wires Pitlane into Plex |
| [docs/configuration.md](docs/configuration.md) | Full configuration reference |

## Status

Specification only. No implementation exists yet.

## Conventions

The key words **MUST**, **MUST NOT**, **SHOULD**, **SHOULD NOT** and **MAY** follow RFC 2119. Requirement IDs (`LIVE-3`, `REP-7` …) are stable: never renumber them; mark withdrawn requirements as *withdrawn* instead.

Statements marked **[verify]** describe third-party behavior that the implementation team must confirm against current upstream documentation before relying on it.
