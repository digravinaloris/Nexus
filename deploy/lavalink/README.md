# Lavalink for Nexus

Nexus runs Lavalink as a separate service from the Discord bot.

## Deploy on Render (Free)

Create a new **Web Service** from this repository and use these settings:

- **Branch:** `feature/music-lavalink`
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

## Sources and YouTube policy

The LavaSrc provider list is explicit and contains SoundCloud and Bandcamp only. YouTube, YouTube Music and yt-dlp are disabled; there is no YouTube search fallback.

Spotify and Apple Music can be enabled later as metadata/mirror sources after adding the credentials required by LavaSrc. They do not provide direct audio playback through this integration. Any mirror fallback must remain restricted to the non-YouTube providers listed above.
