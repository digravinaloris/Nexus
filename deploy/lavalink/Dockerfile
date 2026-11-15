# Render builds from the repository root (Docker context: .).
FROM ghcr.io/lavalink-devs/lavalink:4-alpine

# Override the image's default configuration with Nexus' explicit provider policy.
COPY deploy/lavalink/application.yml /opt/Lavalink/application.yml
