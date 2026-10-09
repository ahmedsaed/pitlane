# Plex Setup (Operator Guide)

Requirements: Plex Pass, which Plex Live TV & DVR needs; Pitlane reachable from PMS over HTTP; and a configured source for at least one channel.

> You are responsible for having the rights to every source you configure in Pitlane.

## 1. Run Pitlane

```bash
docker run -d --name pitlane \
  -p 5004:5004 \
  -v /srv/pitlane/config:/config \
  -v /srv/pitlane/data:/data \
  -v /srv/media/inbox:/inbox \
  -v /srv/media/tv:/library \
  -v /srv/plex/dvr:/dvr:ro \
  -e PITLANE_PLEX_TOKEN_FILE=/config/plex_token \
  pitlane:latest
```

- Set `server.advertised_url` to an address PMS can reach (for example `http://192.168.1.10:5004`), not `localhost`, unless PMS runs on the same network namespace.
- If the DVR folder will be filed with `file_mode: move`, mount it read-write.

Check: `http://<host>:5004/discover.json` returns JSON, and `/lineup.json` lists your channels.

## 2. Add the tuner

1. Plex Web → **Settings → Live TV & DVR → Set up Plex DVR**.
2. If Pitlane isn't found automatically, choose **"Don't see your HDHomeRun device? Enter its network address manually"** and enter `192.168.1.10:5004`.
3. For the guide, choose **"Have an XMLTV guide on your server? Click here to use that instead"** and enter `http://192.168.1.10:5004/xmltv.xml`.
4. Confirm the channel mapping. Each Pitlane channel should map to its own `display-name` automatically.

The menu wording changes between Plex releases. Adding the tuner by address and using the XMLTV guide option are **[verified 2026-10-09, real PMS]**.

**Finding Live TV afterwards:** in some Plex Web versions, Live TV does not appear in the sidebar (or under **More**) after setup, even though it works. Open it directly at `https://<your-plex>/web/index.html#!/live-tv/<server-id>/tv.plex.providers.epg.xmltv:<n>` (the page Plex shows right after DVR setup) and bookmark it. TV and phone apps list Live TV in their own menus.

## 3. Record every session

1. Plex → **Live TV → Guide**, then find any `Formula 1` programme.
2. **Record → Record all episodes / New episodes only** on any channel (or the channel you prefer).
3. Set the destination library to the DVR library folder that Pitlane watches (`/dvr` in the example above).

Because every session shares the series title and has unique S/E numbers, this one rule covers the whole season.

## 4. Replay library

1. Create a TV Shows library pointing at `/library` (or include `/library/Formula 1` in an existing TV library).
2. Agent: Plex Series. Enable "Use local assets". Episode sorting: oldest first.
3. Put the library's section ID in `plex.library_section_id`. The Pitlane UI lists your libraries to help you find it.

## 5. Optional: spoiler user

Set `plex.spoiler_user` to the Plex user whose watch state controls result reveal. For a managed or home user, give Pitlane that user's token through `PITLANE_SPOILER_TOKEN_FILE`.

## Troubleshooting

| Symptom | Check |
|---|---|
| Plex can't find the tuner | Enter the address manually. Make sure `advertised_url` is reachable from the PMS host (`curl` from there). |
| Channels list but won't play | The Pitlane UI shows source state. Look at the stream logs for that correlation ID. |
| "All tuners in use" | Raise `tuners.count`. PMS may hold a stream briefly after channel changes. |
| Guide empty | Open `/xmltv.xml` in a browser, then force a guide refresh in Plex (Live TV & DVR settings). |
| Recordings not filed | Check that the DVR folder is mounted, and look at the UI's `needs-review` list. |

## Verification log

| Date | PMS | Result |
|---|---|---|
| 2026-10-09 | Plex Pass server on LAN, Plex Web client | Tuner added by address; XMLTV guide accepted and channels auto-mapped; live playback works; mid-stream failover A→B continued playing without re-tune; one "Formula 1" series rule scheduled every session across two weekends. Not yet tested: DVR recording file filing, metadata, spoiler mode (stage 2). |
