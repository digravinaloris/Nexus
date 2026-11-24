"""Owner-only GitHub release announcements for the single Nexus home server."""
import asyncio
import datetime as dt

import discord
import requests
from discord import app_commands
from discord.ext import commands

RELEASE_API = "https://api.github.com/repos/digravinaloris/Nexus/releases/latest"
POLL_SECONDS = 600
STATE_ID = "github_release_announcements"


def fetch_latest_release():
    """Fetch the latest published, non-draft, non-prerelease GitHub release."""
    response = requests.get(
        RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Nexus-Discord-Bot",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=10,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    release = response.json()
    if not isinstance(release, dict):
        raise ValueError("GitHub returned an invalid release response.")
    if release.get("draft") or release.get("prerelease"):
        return None
    required = ("id", "tag_name", "html_url")
    if any(not release.get(key) for key in required):
        raise ValueError("GitHub release is missing required fields.")
    return release


def build_release_embed(release):
    name = (release.get("name") or release["tag_name"]).strip()[:256]
    body = (release.get("body") or "").strip()
    if not body:
        body = "A new Nexus version is available."
    if len(body) > 4000:
        body = body[:3997].rstrip() + "..."
    embed = discord.Embed(
        title=f"🚀 Nexus {name}",
        url=release["html_url"],
        description=body,
        color=0x5865F2,
        timestamp=_parse_published_at(release.get("published_at")),
    )
    embed.add_field(name="Version", value=release["tag_name"][:256], inline=True)
    embed.add_field(name="Release notes", value=f"[View on GitHub]({release['html_url']})", inline=True)
    embed.set_footer(text="Nexus · Automatic update announcement")
    return embed


def _parse_published_at(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError):
        return None


class GitHubUpdates(commands.Cog):
    updates = app_commands.Group(
        name="updates",
        description="Manage automatic GitHub release announcements (Nexus owner only)",
    )

    def __init__(self, bot):
        self.bot = bot
        self.task = None

    def state(self):
        return self.bot.db["bot_state"]

    async def owner_and_home_server(self, interaction):
        if interaction.guild is None or interaction.user.id != interaction.guild.owner_id:
            await interaction.response.send_message(
                "❌ This command is restricted to the bot owner, who must also own this server.",
                ephemeral=True,
            )
            return False
        if not await self.bot.is_owner(interaction.user):
            await interaction.response.send_message(
                "❌ This command is restricted to the bot owner.",
                ephemeral=True,
            )
            return False
        return True

    async def post_release(self, guild_id, channel_id, release):
        guild = self.bot.get_guild(int(guild_id))
        channel = guild.get_channel(int(channel_id)) if guild else None
        if channel is None or not isinstance(channel, discord.TextChannel):
            return False
        await channel.send(
            content="📢 **A new Nexus update is available!**",
            embed=build_release_embed(release),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.state().update_one(
            {"_id": STATE_ID, "guild_id": str(guild_id)},
            {"$set": {
                "last_release_id": str(release["id"]),
                "last_release_tag": release["tag_name"],
                "last_release_published_at": release.get("published_at"),
            }},
        )
        return True

    async def poll_once(self):
        state = self.state().find_one({"_id": STATE_ID})
        if not state:
            return
        release = await asyncio.to_thread(fetch_latest_release)
        if not release:
            return
        if str(release["id"]) == str(state.get("last_release_id", "")):
            return
        await self.post_release(state["guild_id"], state["channel_id"], release)

    async def scheduler(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                await self.poll_once()
            except Exception as exc:
                print(f"[GITHUB UPDATES] Poll failed: {exc}", flush=True)
            await asyncio.sleep(POLL_SECONDS)

    @updates.command(name="channel", description="Set the channel for automatic Nexus release announcements")
    async def set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not await self.owner_and_home_server(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            release = await asyncio.to_thread(fetch_latest_release)
        except Exception as exc:
            await interaction.followup.send(f"❌ Couldn't contact GitHub: {str(exc)[:500]}", ephemeral=True)
            return
        if not release:
            await interaction.followup.send("❌ No published GitHub release was found.", ephemeral=True)
            return
        self.state().update_one(
            {"_id": STATE_ID},
            {"$set": {
                "guild_id": str(interaction.guild_id),
                "channel_id": str(channel.id),
                "last_release_id": str(release["id"]),
                "last_release_tag": release["tag_name"],
                "last_release_published_at": release.get("published_at"),
            }},
            upsert=True,
        )
        await interaction.followup.send(
            f"✅ GitHub updates are now configured in {channel.mention}. I'll announce future releases automatically.",
            ephemeral=True,
        )

    @updates.command(name="check", description="Fetch and publish the latest GitHub release now")
    async def check_now(self, interaction: discord.Interaction):
        if not await self.owner_and_home_server(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        state = self.state().find_one({"_id": STATE_ID, "guild_id": str(interaction.guild_id)})
        if not state:
            await interaction.followup.send("❌ Configure a channel first with /updates channel.", ephemeral=True)
            return
        try:
            release = await asyncio.to_thread(fetch_latest_release)
            if not release:
                await interaction.followup.send("❌ No published GitHub release was found.", ephemeral=True)
                return
            sent = await self.post_release(state["guild_id"], state["channel_id"], release)
        except Exception as exc:
            await interaction.followup.send(f"❌ GitHub check failed: {str(exc)[:500]}", ephemeral=True)
            return
        await interaction.followup.send(
            "✅ Latest release published." if sent else "❌ The configured channel is missing or unavailable.",
            ephemeral=True,
        )

    @updates.command(name="status", description="Show the automatic GitHub announcement configuration")
    async def status(self, interaction: discord.Interaction):
        if not await self.owner_and_home_server(interaction):
            return
        state = self.state().find_one({"_id": STATE_ID, "guild_id": str(interaction.guild_id)})
        if not state:
            await interaction.response.send_message("GitHub release announcements are not configured.", ephemeral=True)
            return
        channel = interaction.guild.get_channel(int(state["channel_id"]))
        await interaction.response.send_message(
            f"📡 GitHub announcements: {channel.mention if channel else 'configured channel is missing'}\n"
            f"Last announced release: **{state.get('last_release_tag', 'Not yet recorded')}**",
            ephemeral=True,
        )

    @updates.command(name="disable", description="Disable automatic GitHub release announcements")
    async def disable(self, interaction: discord.Interaction):
        if not await self.owner_and_home_server(interaction):
            return
        self.state().delete_one({"_id": STATE_ID, "guild_id": str(interaction.guild_id)})
        await interaction.response.send_message("✅ Automatic GitHub announcements disabled.", ephemeral=True)

    async def cog_load(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.scheduler(), name="github-release-announcements")

    async def cog_unload(self):
        if self.task:
            self.task.cancel()


async def setup(bot):
    await bot.add_cog(GitHubUpdates(bot))
