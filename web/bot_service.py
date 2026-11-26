"""Transport-neutral bot service boundary used by the web application.

The Flask routes must depend on this small interface rather than reaching into
Discord's Gateway client directly. The first adapter deliberately preserves the
existing in-process deployment; a signed HTTP adapter can implement the same
contract in a later migration step without changing route response shapes.
"""
from typing import Dict, List

import core


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
