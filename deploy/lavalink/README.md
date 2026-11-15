# Lavalink for Nexus

Nexus runs Lavalink as a separate service from the Discord bot.

## Deploy on Render (Free)

Create a new **Web Service** from this repository and use these settings:

- **Branch:** `main` after this pull request is merged (or this feature branch while testing)
- **Runtime:** Docker
- **Root Directory:** leave empty
- **Dockerfile Path:** `deploy/lavalink/Dockerfile`
- **Docker Context:** repository root (`.`), if the setting is shown
- **Instance Type:** Free

The Dockerfile uses the official Lavalink v4 Alpine image and copies this directory's `application.yml` into the image.

### Environment variables on the Lavalink service

- `LAVALINK_PASSWORD`: generate a long, random secret.
- Render supplies `PORT` automatically (usually `10000`). The configuration reads it automatically; do not hardcode port 2333 for Render.

After the first deploy, copy the service's public URL, for example `https://nexus-lavalink.onrender.com`.

### Environment variables on the Nexus bot service

Set these on the existing Nexus service:

- `LAVALINK_URI`: the Lavalink service URL, including `https://` and without a trailing path.
- `LAVALINK_PASSWORD`: exactly the same secret as on the Lavalink service.

Keep the password secret. Do not put it in GitHub or in a public issue.

## Included Lavalink plugins

The service downloads these plugins at startup:

- **youtube-source 1.18.2** — YouTube search, video links and playlist links.
- **LavaSearch 1.0.0** — Lavalink's advanced search endpoint for tracks, albums, artists and playlists.
- **LavaSrc 4.8.3** — provider/mirroring framework; Spotify credentials are not enabled in this configuration.

The legacy built-in YouTube source is disabled as required by youtube-source. The separate YouTube plugin is enabled and configured with multiple clients. Nexus text searches default to YouTube; the `/play` and `/search` commands also let you choose SoundCloud or Bandcamp. Direct supported URLs are resolved by Lavalink.

### YouTube reliability notes

YouTube can change its anti-bot checks and playback clients without notice. The plugin currently supports optional PoToken/visitorData and OAuth configuration, but neither is configured by default. If Render logs show bot-check, 403, or playback-client errors, we may need to configure a PoToken or investigate a compatible client update. Do not use your primary Google account for OAuth; the plugin maintainers warn that OAuth is not risk-free.

The YouTube plugin release page currently lists 1.18.2 as its latest stable release. Keep an eye on the official repository for updates.

## Important: Render Free sleep behavior

A Free Web Service can spin down after a period without inbound traffic. The first connection after sleep can take a while while Render starts the container. Music may therefore be temporarily unavailable, and this is not equivalent to an always-on Lavalink node.

**Current bot limitation:** the current Nexus startup code attempts to connect to Lavalink once. If the Lavalink service is asleep or unavailable at that moment, it does not yet guarantee an automatic retry when someone runs `/play`. For reliable Free-tier wake-and-retry behavior, that reconnect flow must be added and tested before relying on this deployment.

## Run locally

1. Use Docker, or a Java 17+ Lavalink v4 distribution.
2. Keep `application.yml` beside the Lavalink distribution (or build with this Dockerfile from the repository root).
3. Set `LAVALINK_PASSWORD` in the environment.
4. Optionally set `PORT=2333` for local use.
5. Start Lavalink.
6. Start Nexus with `LAVALINK_URI=http://127.0.0.1:2333` and the same password.

## Sources and Spotify

YouTube, SoundCloud and Bandcamp are enabled for playback. Spotify is not configured as a native playback source and no Spotify API credentials are required by this change. Spotify links still need a separate metadata-to-playable-track resolver before they can be advertised as supported.
