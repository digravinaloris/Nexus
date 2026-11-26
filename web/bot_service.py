"""Transport-neutral bot service boundary used by the web application.

The Flask routes must depend on this small interface rather than reaching into
Discord's Gateway client directly. The first adapter deliberately preserves the
existing in-process deployment; a signed HTTP adapter can implement the same
contract in a later migration step without changing route response shapes.
"""
from typing import Dict, List, Optional
import math
import time

import core
import discord
from bridge import run_coroutine
from discord_logging import log_unmute
import utils


class BotService:
    """Minimal interface consumed by web routes."""

    def health_snapshot(self) -> Dict[str, object]:
        raise NotImplementedError

    def guild_presence(self, guild_id: str) -> Dict[str, bool]:
        """Return bot readiness and guild presence without exposing Discord objects."""
        raise NotImplementedError

    def available_guild_ids(self, candidate_ids: List[str]) -> List[str]:
        """Return candidate guild IDs currently available to the connected bot."""
        raise NotImplementedError

    def is_guild_owner(self, guild_id: str, user_id: str) -> bool:
        """Check ownership using the bot's current guild cache."""
        raise NotImplementedError

    def public_status_snapshot(self) -> Dict[str, object]:
        """Return minimal public status data without leaking Discord client objects."""
        raise NotImplementedError

    def guild_name(self, guild_id: str) -> str:
        """Return a display name for a guild, or a neutral fallback."""
        raise NotImplementedError

    def api_health_snapshot(self, guild_id: str = None) -> Dict[str, object]:
        """Return the existing REST health response without exposing bot objects."""
        raise NotImplementedError

    def api_guild_list_snapshot(self, guild_id: str = None) -> Optional[List[Dict[str, object]]]:
        """Return serializable guild list DTOs, or None when the bot is offline."""
        raise NotImplementedError

    def api_text_channels_snapshot(self, guild_id: str) -> Optional[List[Dict[str, str]]]:
        """Return text channel DTOs for one guild, or None when the bot is offline."""
        raise NotImplementedError

    def api_roles_snapshot(self, guild_id: str) -> Optional[List[Dict[str, str]]]:
        """Return non-default role DTOs for one guild, or None when the bot is offline."""
        raise NotImplementedError

    def api_members_snapshot(self, guild_id: str) -> Optional[List[Dict[str, object]]]:
        """Return serializable member DTOs for one guild, or None when the bot is offline."""
        raise NotImplementedError

    def set_guild_locked(self, guild_id: str, locked: bool) -> bool:
        """Set a guild lock flag; false when the bot runtime is unavailable."""
        raise NotImplementedError

    def api_guild_stats_snapshot(self, guild_id: str, ticket_category_id: str = None) -> Optional[Dict[str, object]]:
        """Return Discord-derived guild stats without exposing runtime objects."""
        raise NotImplementedError

    def api_unmute_member(self, guild_id: str, user_id: str):
        """Remove a timeout from a member and log it; return success/error pair."""
        raise NotImplementedError

    def api_broadcast(self, guild_id: str, channel_id: str, message: str, mention: str = "none"):
        """Send a mobile-app broadcast; return a success/error pair."""
        raise NotImplementedError


class InProcessBotService(BotService):
    """Compatibility adapter for the current single-process bot + web runtime."""

    def is_guild_owner(self, guild_id: str, user_id: str) -> bool:
        bot = core.bot
        if bot is None or not bot.is_ready():
            return False
        guild = bot.get_guild(int(guild_id))
        return bool(guild is not None and str(guild.owner_id) == str(user_id))

    def available_guild_ids(self, candidate_ids: List[str]) -> List[str]:
        bot = core.bot
        if bot is None or not bot.is_ready():
            return []
        available = {str(guild.id) for guild in bot.guilds}
        return [guild_id for guild_id in candidate_ids if guild_id in available]

    def guild_presence(self, guild_id: str) -> Dict[str, bool]:
        bot = core.bot
        bot_ready = bool(bot is not None and bot.is_ready())
        guild_present = bool(bot_ready and bot.get_guild(int(guild_id)) is not None)
        return {"bot_ready": bot_ready, "guild_present": guild_present}

    def guild_name(self, guild_id: str) -> str:
        bot = core.bot
        if bot is None or not bot.is_ready():
            return "the server"
        guild = bot.get_guild(int(guild_id))
        return guild.name if guild is not None else "the server"

    def api_health_snapshot(self, guild_id: str = None) -> Dict[str, object]:
        bot = core.bot
        online = bool(bot is not None and bot.is_ready())
        locked_guilds = getattr(bot, "locked_guilds", set()) if bot is not None else set()
        if guild_id:
            return {
                "online": online,
                "locked": int(guild_id) in locked_guilds,
                "guild_count": 1,
            }
        return {
            "online": online,
            "locked": bool(locked_guilds),
            "guild_count": len(bot.guilds) if online else 0,
        }

    def api_guild_list_snapshot(self, guild_id: str = None) -> Optional[List[Dict[str, object]]]:
        bot = core.bot
        if bot is None or not bot.is_ready():
            return None

        def serialize(guild):
            return {
                "id": str(guild.id),
                "name": guild.name,
                "member_count": guild.member_count,
                "icon_url": str(guild.icon.url) if guild.icon else None,
            }

        if guild_id:
            guild = bot.get_guild(int(guild_id))
            return [serialize(guild)] if guild is not None else []
        return [serialize(guild) for guild in bot.guilds]

    def api_text_channels_snapshot(self, guild_id: str) -> Optional[List[Dict[str, str]]]:
        bot = core.bot
        if bot is None or not bot.is_ready():
            return None
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return []
        return [{"id": str(channel.id), "name": channel.name} for channel in guild.text_channels]

    def api_roles_snapshot(self, guild_id: str) -> Optional[List[Dict[str, str]]]:
        bot = core.bot
        if bot is None or not bot.is_ready():
            return None
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return []
        return [{"id": str(role.id), "name": role.name} for role in guild.roles if not role.is_default()]

    def api_members_snapshot(self, guild_id: str) -> Optional[List[Dict[str, object]]]:
        bot = core.bot
        if bot is None or not bot.is_ready():
            return None
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return []
        return [{
            "id": str(member.id),
            "name": member.display_name,
            "username": str(member),
            "avatar_url": member.display_avatar.url,
            "is_muted": member.is_timed_out(),
        } for member in guild.members if not member.bot]

    def set_guild_locked(self, guild_id: str, locked: bool) -> bool:
        bot = core.bot
        if bot is None or not bot.is_ready():
            return False
        locked_guilds = getattr(bot, "locked_guilds", None)
        if locked_guilds is None:
            return False
        if locked:
            locked_guilds.add(int(guild_id))
        else:
            locked_guilds.discard(int(guild_id))
        return True

    def api_guild_stats_snapshot(self, guild_id: str, ticket_category_id: str = None) -> Optional[Dict[str, object]]:
        bot = core.bot
        if bot is None or not bot.is_ready():
            return None
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return {}

        async def collect_counts():
            bans = [ban async for ban in guild.bans()]
            muted = [member for member in guild.members if member.is_timed_out()]
            return len(bans), len(muted)

        ban_count, muted_count = run_coroutine(collect_counts())
        open_tickets_count = 0
        if ticket_category_id:
            category = guild.get_channel(int(ticket_category_id))
            if isinstance(category, discord.CategoryChannel):
                open_tickets_count = len(category.text_channels)
        return {
            "guild_id": str(guild.id),
            "name": guild.name,
            "member_count": guild.member_count,
            "ban_count": ban_count,
            "muted_count": muted_count,
            "open_tickets_count": open_tickets_count,
            "locked": int(guild_id) in getattr(bot, "locked_guilds", set()),
        }

    def api_unmute_member(self, guild_id: str, user_id: str):
        bot = core.bot
        if bot is None or not bot.is_ready():
            return None
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return False, "Guild not found"

        async def unmute():
            member = guild.get_member(int(user_id))
            if member is None:
                return False, "Member not found"
            await member.timeout(None)
            await log_unmute(guild, member)
            return True, None

        return run_coroutine(unmute())

    def api_broadcast(self, guild_id: str, channel_id: str, message: str, mention: str = "none"):
        bot = core.bot
        if bot is None or not bot.is_ready():
            return None
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return False, "Guild not found"
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            return False, "Channel not found"

        if mention == "everyone":
            ping = "@everyone"
        elif mention == "here":
            ping = "@here"
        else:
            role = discord.utils.get(guild.roles, name=mention)
            ping = role.mention if role else None

        embed = discord.Embed(description=message, color=0x3399ff)
        embed.set_footer(text="📢 Sent from the mobile app")

        async def send():
            await channel.send(content=ping, embed=embed)

        run_coroutine(send())
        return True, None

    def public_status_snapshot(self) -> Dict[str, object]:
        bot = core.bot
        online = bool(bot is not None and bot.is_ready())
        uptime = utils.format_uptime(time.time() - bot.start_time) if online and hasattr(bot, "start_time") else "unknown"
        latency = getattr(bot, "latency", float("nan")) if online else float("nan")
        latency_ms = round(latency * 1000) if isinstance(latency, (int, float)) and math.isfinite(latency) else "—"
        return {"online": online, "guild_count": len(bot.guilds) if online else 0, "latency_ms": latency_ms, "uptime": uptime}

    def health_snapshot(self) -> Dict[str, object]:
        bot = core.bot
        bot_ready = bool(bot is not None and bot.is_ready())

        mongo_ok = False
        try:
            if core.mongo is not None:
                core.mongo.admin.command("ping")
                mongo_ok = True
        except Exception:
            # Health remains a liveness response; dependency state is reported
            # in the payload rather than turning a transient outage into a 5xx.
            pass

        return {
            "status": "alive",
            "discord_gateway": "connected" if bot_ready else "disconnected",
            "mongodb": "connected" if mongo_ok else "unreachable",
            "guild_count": len(bot.guilds) if bot_ready else 0,
        }


def get_bot_service() -> BotService:
    """Return the active adapter (in-process until remote transport is enabled)."""
    return InProcessBotService()
