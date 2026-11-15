"""Nexus music commands powered by Lavalink + Wavelink.

No local downloads, FFmpeg process, yt-dlp, or YouTube provider is used here.
Text searches are routed explicitly to a selected non-YouTube provider.
"""
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
import wavelink

_bot = None
_queue_locks = {}
DEFAULT_VOLUME = 50
MAX_QUEUE_DISPLAY = 10

SOURCES = {
    "SoundCloud": "scsearch",
    "Bandcamp": "bcsearch",
}
SOURCE_CHOICES = [
    app_commands.Choice(name=name, value=name)
    for name in SOURCES
]


def _source_name(track):
    uri = getattr(track, "uri", None) or ""
    source = (getattr(track, "source", None) or "").lower()
    if "soundcloud" in uri.lower() or source == "soundcloud":
        return "SoundCloud"
    if "bandcamp" in uri.lower() or source == "bandcamp":
        return "Bandcamp"
    if "deezer" in uri.lower() or source == "deezer":
        return "Deezer"
    if "spotify" in uri.lower():
        return "Spotify (mirror)"
    if "apple" in uri.lower():
        return "Apple Music (mirror)"
    if "tidal" in uri.lower():
        return "Tidal (mirror)"
    return source.title() if source else "External source"


def _duration(track):
    length = getattr(track, "length", None)
    if length is None:
        return "Live / unknown"
    total = int(length // 1000)
    return f"{total // 60}:{total % 60:02d}"


def _node():
    try:
        return wavelink.Pool.get_node()
    except (wavelink.InvalidNodeException, RuntimeError):
        return None


async def _check_access(interaction, command_name):
    if not await _bot.check_access(interaction, command_name, None):
        return False
    return True


async def _get_player(interaction):
    """Connect to the member's voice channel using Wavelink's VoiceProtocol."""
    member = interaction.user
    if not member.voice or not member.voice.channel:
        await interaction.followup.send(
            "❌ You need to join a voice channel first.", ephemeral=True
        )
        return None

    if _node() is None:
        await interaction.followup.send(
            "⚠️ The music service is temporarily unavailable. Please try again shortly.",
            ephemeral=True,
        )
        return None

    channel = member.voice.channel
    player = interaction.guild.voice_client
    if isinstance(player, wavelink.Player):
        if player.channel != channel:
            await player.move_to(channel)
        return player

    try:
        player = await channel.connect(cls=wavelink.Player, self_deaf=True)
        await player.set_volume(DEFAULT_VOLUME)
        return player
    except Exception as exc:
        await interaction.followup.send(
            f"❌ I couldn't join that voice channel: {str(exc)[:180]}",
            ephemeral=True,
        )
        return None


async def _resolve(query, source_name):
    # URLs are resolved by Lavalink's registered source managers/plugins.
    # Plain text is always sent to an explicit non-YouTube search prefix.
    if query.startswith(("https://", "http://")):
        lowered = query.lower()
        if any(host in lowered for host in ("youtube.com", "youtu.be", "youtube-nocookie.com")):
            raise ValueError("YouTube links are disabled on Nexus music.")
        return await wavelink.Playable.search(query)

    prefix = SOURCES.get(source_name, SOURCES["SoundCloud"])
    return await wavelink.Playable.search(query, source=prefix)


async def _enqueue(player, tracks):
    """Add one track or a playlist and start playback if the player is idle."""
    if isinstance(tracks, wavelink.Playlist):
        items = list(tracks.tracks)
    else:
        items = list(tracks)

    if not items:
        return 0

    lock = _queue_locks.setdefault(player.guild.id, asyncio.Lock())
    async with lock:
        for track in items:
            player.queue.put(track)
        if player.current is None:
            next_track = await player.queue.get_wait()
            await player.play(next_track)

    return len(items)


class Music(commands.Cog):
    """Multi-source music commands backed by a separate Lavalink node."""

    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="play", description="Play music from SoundCloud, Bandcamp or a supported link")
    @app_commands.describe(query="Song name or a supported music link", source="Search provider for text queries")
    @app_commands.choices(source=SOURCE_CHOICES)
    async def play(
        self,
        interaction: discord.Interaction,
        query: str,
        source: app_commands.Choice[str] = None,
    ):
        if not await _check_access(interaction, "play"):
            return
        await interaction.response.defer()
        player = await _get_player(interaction)
        if player is None:
            return

        was_playing = player.playing or player.paused
        try:
            tracks = await _resolve(query, source.value if source else "SoundCloud")
            count = await _enqueue(player, tracks)
        except Exception as exc:
            await interaction.followup.send(
                f"❌ Couldn't load that track. Check the link/provider and try again.\n"
                f"`{str(exc)[:180]}`"
            )
            return

        if not count:
            await interaction.followup.send("❌ No playable tracks were found.")
            return

        current = player.current
        if not was_playing and current:
            link = f"({current.uri})" if current.uri else ""
            message = f"🎵 **Now playing:** [{current.title}]{link}"
        else:
            message = f"➕ Added **{count}** track(s) to the queue."
        await interaction.followup.send(message)

    @app_commands.command(name="search", description="Search a music provider and show the top results")
    @app_commands.describe(query="Song, artist or album", source="Provider to search")
    @app_commands.choices(source=SOURCE_CHOICES)
    async def search(
        self,
        interaction: discord.Interaction,
        query: str,
        source: app_commands.Choice[str] = None,
    ):
        if not await _check_access(interaction, "search"):
            return
        await interaction.response.defer(ephemeral=True)
        if _node() is None:
            await interaction.followup.send("⚠️ Music service unavailable.", ephemeral=True)
            return
        try:
            tracks = await _resolve(query, source.value if source else "SoundCloud")
        except Exception as exc:
            await interaction.followup.send(
                f"❌ Search failed: `{str(exc)[:160]}`", ephemeral=True
            )
            return

        items = list(tracks.tracks) if isinstance(tracks, wavelink.Playlist) else list(tracks)
        if not items:
            await interaction.followup.send("No results found.", ephemeral=True)
            return

        lines = []
        for index, track in enumerate(items[:5], start=1):
            lines.append(
                f"**{index}.** [{track.title}]({track.uri}) · {_duration(track)} · {_source_name(track)}"
            )
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(name="pause", description="Pause the current song")
    async def pause(self, interaction: discord.Interaction):
        if not await _check_access(interaction, "pause"):
            return
        player = interaction.guild.voice_client
        if not isinstance(player, wavelink.Player) or not player.playing or player.paused:
            await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
            return
        await player.pause(True)
        await interaction.response.send_message("⏸️ Paused.")

    @app_commands.command(name="resume", description="Resume the current song")
    async def resume(self, interaction: discord.Interaction):
        if not await _check_access(interaction, "resume"):
            return
        player = interaction.guild.voice_client
        if not isinstance(player, wavelink.Player) or not player.paused:
            await interaction.response.send_message("❌ Nothing is paused.", ephemeral=True)
            return
        await player.pause(False)
        await interaction.response.send_message("▶️ Resumed.")

    @app_commands.command(name="skip", description="Skip the current song")
    async def skip(self, interaction: discord.Interaction):
        if not await _check_access(interaction, "skip"):
            return
        player = interaction.guild.voice_client
        if not isinstance(player, wavelink.Player) or player.current is None:
            await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)
            return
        lock = _queue_locks.setdefault(player.guild.id, asyncio.Lock())
        async with lock:
            if player.queue:
                next_track = await player.queue.get_wait()
                await player.play(next_track)
            else:
                await player.stop()
        await interaction.response.send_message("⏭️ Skipped.")

    @app_commands.command(name="volume", description="Set music volume (0 to 200%)")
    async def volume(
        self,
        interaction: discord.Interaction,
        percent: app_commands.Range[int, 0, 200],
    ):
        if not await _check_access(interaction, "volume"):
            return
        player = interaction.guild.voice_client
        if not isinstance(player, wavelink.Player):
            await interaction.response.send_message("❌ I'm not in a voice channel.", ephemeral=True)
            return
        await player.set_volume(percent)
        await interaction.response.send_message(f"🔊 Volume set to **{percent}%**.")

    @app_commands.command(name="stop", description="Stop playback, clear the queue and disconnect")
    async def stop(self, interaction: discord.Interaction):
        if not await _check_access(interaction, "stop"):
            return
        player = interaction.guild.voice_client
        if not isinstance(player, wavelink.Player):
            await interaction.response.send_message("❌ I'm not in a voice channel.", ephemeral=True)
            return
        player.queue.clear()
        await player.stop()
        await player.disconnect()
        await interaction.response.send_message("⏹️ Playback stopped and queue cleared.")

    @app_commands.command(name="queue", description="Show the current music queue")
    async def queue_cmd(self, interaction: discord.Interaction):
        player = interaction.guild.voice_client
        if not isinstance(player, wavelink.Player):
            await interaction.response.send_message("The music player is not connected.", ephemeral=True)
            return

        embed = discord.Embed(title="🎶 Music Queue", color=0x3399ff)
        if player.current:
            embed.add_field(
                name="Now playing",
                value=f"[{player.current.title}]({player.current.uri}) · {_duration(player.current)}",
                inline=False,
            )
        else:
            embed.add_field(name="Now playing", value="Nothing", inline=False)

        queued = list(player.queue)
        if queued:
            embed.add_field(
                name="Up next",
                value="\n".join(
                    f"{index}. [{track.title}]({track.uri}) · {_duration(track)}"
                    for index, track in enumerate(queued[:MAX_QUEUE_DISPLAY], start=1)
                ),
                inline=False,
            )
            if len(queued) > MAX_QUEUE_DISPLAY:
                embed.set_footer(text=f"+ {len(queued) - MAX_QUEUE_DISPLAY} more")
        else:
            embed.add_field(name="Up next", value="Queue is empty", inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="musicstatus", description="Check the Lavalink music service")
    async def musicstatus(self, interaction: discord.Interaction):
        node = _node()
        if node is None:
            await interaction.response.send_message(
                "🔴 Music service offline or not configured.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"🟢 Lavalink connected · Node: **{node.identifier}** · Status: **{node.status.name}**",
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload):
        # Only advance on natural completion. /skip and /stop handle their own transitions.
        player = payload.player
        if player is None or str(payload.reason).lower() != "finished":
            return
        lock = _queue_locks.setdefault(player.guild.id, asyncio.Lock())
        async with lock:
            if player.current is None and player.queue:
                try:
                    await player.play(await player.queue.get_wait())
                except Exception as exc:
                    print(f"[MUSIC] Failed to start next queued track: {exc}", flush=True)

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload):
        print(
            f"[MUSIC] Track exception in guild "
            f"{payload.player.guild.id if payload.player and payload.player.guild else 'unknown'}: "
            f"{payload.exception}",
            flush=True,
        )


async def setup(bot):
    global _bot
    _bot = bot
    await bot.add_cog(Music(bot))
