# Lavalink for Nexus

Nexus runs Lavalink as a separate service from the Discord bot.

## Required environment variables

- `LAVALINK_PASSWORD`: a long random password shared with the bot.
- `LAVALINK_URI`: the Lavalink URL used by the bot, for example `http://your-lavalink-host:2333`.

Set the same `LAVALINK_PASSWORD` in the Lavalink service and Nexus. Set `LAVALINK_URI` only on the Nexus bot service.

## Run locally

1. Use a Java 17+ Lavalink v4 distribution.
2. Copy `application.yml` into the Lavalink working directory.
3. Set `LAVALINK_PASSWORD` in the environment.
4. Start Lavalink.
5. Start Nexus with `LAVALINK_URI=http://127.0.0.1:2333` and the same password.

Do not expose port 2333 publicly unless necessary. Prefer a private network between the bot and Lavalink.

## Sources and YouTube policy

The LavaSrc provider list is explicit and contains Deezer, SoundCloud and Bandcamp only. YouTube, YouTube Music and yt-dlp are disabled; there is no YouTube search fallback.

Spotify and Apple Music can be enabled later as metadata/mirror sources after adding the credentials required by LavaSrc. They do not provide direct audio playback through this integration. Any mirror fallback must remain restricted to the non-YouTube providers listed above.

## Hosting note

Do not use a sleeping free web service for Lavalink. Voice playback needs a continuously available TCP/WebSocket service. A separate always-on VPS or paid private service is recommended. Keep the Lavalink password private.
