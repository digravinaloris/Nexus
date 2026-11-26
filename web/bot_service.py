"""Transport-neutral bot service boundary used by the web application.

The Flask routes must depend on this small interface rather than reaching into
Discord's Gateway client directly. The first adapter deliberately preserves the
existing in-process deployment; a signed HTTP adapter can implement the same
contract in a later migration step without changing route response shapes.
"""
from typing import Dict

import core


class BotService:
    """Minimal interface consumed by web routes."""

    def health_snapshot(self) -> Dict[str, object]:
        raise NotImplementedError


class InProcessBotService(BotService):
    """Compatibility adapter for the current single-process bot + web runtime."""

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
