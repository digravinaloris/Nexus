"""Owner-only GitHub release announcements, delivered as plain Discord messages."""
import asyncio
import re
from urllib.parse import urlsplit

import discord
import requests
from discord import app_commands
from discord.ext import commands

RELEASE_API = "https://api.github.com/repos/digravinaloris/Nexus/releases/latest"
POLL_SECONDS = 600
STATE_ID = "github_release_announcements"
MAX_DISCORD_MESSAGE_LENGTH = 2000


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
    if any(key not in release for key in ("id", "tag_name", "html_url")):
        raise ValueError("GitHub release is missing required fields.")

    release_id = release.get("id")
    tag = release.get("tag_name")
    if (
        isinstance(release_id, bool)
        or not isinstance(release_id, int)
        or release_id <= 0
        or not isinstance(tag, str)
        or not tag.strip()
        or len(tag) > 255
    ):
        raise ValueError("GitHub release metadata is invalid.")

    url = release.get("html_url")
    if not isinstance(url, str) or len(url) > 500:
        raise ValueError("GitHub release URL is invalid.")
    try:
        parsed_url = urlsplit(url)
        parsed_port = parsed_url.port
    except ValueError as exc:
        raise ValueError("GitHub release URL is invalid.") from exc
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != "github.com"
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_port not in (None, 443)
        or parsed_url.query
        or parsed_url.fragment
        or not re.fullmatch(r"/digravinaloris/Nexus/releases/(?:tag/)?[^/]+", parsed_url.path)
    ):
        raise ValueError("GitHub release URL is invalid.")
    return release


def build_release_message(release):
    """Format release details as a bounded plain-text message (no Discord embed)."""
    tag = str(release["tag_name"]).strip()[:100]
    name = str(release.get("name") or tag).strip()[:160] or tag
    body = str(release.get("body") or "").strip() or "No release notes provided."
    url = str(release["html_url"]).strip()

    prefix = f"📢 New Nexus release!\n{name}\nVersion: {tag}\n\n"
    suffix = f"\n\n{url}"
    body_limit = max(0, MAX_DISCORD_MESSAGE_LENGTH - len(prefix) - len(suffix))
    if len(body) > body_limit and body_limit >= 3:
        body = body[:body_limit - 3].rstrip() + "..."
    else:
        body = body[:body_limit]
    return prefix + body + suffix


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
            content=build_release_message(release),
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

    async def handle_webhook_release(self, release):
        state = self.state().find_one({"_id": STATE_ID})
        if not state:
            return False
        if str(release.get("id", "")) == str(state.get("last_release_id", "")):
            return False
        return await self.post_release(state["guild_id"], state["channel_id"], release)

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
