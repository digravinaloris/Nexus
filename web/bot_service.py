"""Transport-neutral bot service boundary used by the web application.

The Flask routes must depend on this small interface rather than reaching into
Discord's Gateway client directly. The first adapter deliberately preserves the
existing in-process deployment; a signed HTTP adapter can implement the same
contract in a later migration step without changing route response shapes.
"""
from typing import Dict, List, Optional
import datetime
import math
import time

import core
import discord
from config_store import get_config, update_config
from faq_store import add_faq_entry, get_faq, remove_faq_entry, set_faq_enabled
from audit import get_audit_log, record_audit
from bridge import run_coroutine
from discord_logging import log_ban, log_dashboard_action, log_kick, log_mute, log_unmute, log_unwarn, log_warn
from sanctions_store import get_sanction_cases, get_sanction_stats, get_warnlist, get_warns, log_sanction, set_warns
import utils
from moderation_actions import _dm_user_appeal_result, post_appeal_for_review, reverse_sanction
from owner_notify import notify_owner_bruteforce, notify_owner_killswitch


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

    def api_guild_id_candidates(self) -> List[str]:
        """Return the connected guild IDs as strings for API-key lookup."""
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

    def api_config_snapshot(self, guild_id: str) -> Dict[str, object]:
        """Return the stored guild config for API auth and config routes."""
        raise NotImplementedError

    def api_update_config(self, guild_id: str, updates: Dict[str, object]) -> None:
        """Persist validated API config updates inside the bot service."""
        raise NotImplementedError

    def api_faq_snapshot(self, guild_id: str) -> Optional[Dict[str, object]]:
        """Return a guild FAQ document for the mobile API."""
        raise NotImplementedError

    def api_faq_add_entry(self, guild_id: str, entry: Dict[str, object]) -> None:
        """Append a validated FAQ entry for the mobile API."""
        raise NotImplementedError

    def api_faq_remove_entry(self, guild_id: str, entry_id: str) -> bool:
        """Remove one FAQ entry and report whether it existed."""
        raise NotImplementedError

    def api_faq_set_enabled(self, guild_id: str, enabled: bool) -> None:
        """Update the guild FAQ enabled state."""
        raise NotImplementedError

    def api_audit_snapshot(self, guild_id: str, limit: int = 20) -> List[Dict[str, object]]:
        """Return the merged audit feed for the mobile API."""
        raise NotImplementedError

    def api_sanction_cases_snapshot(self, guild_id: str, filter_type: str = "", limit: int = 50) -> List[Dict[str, object]]:
        """Return filtered sanction cases for the mobile API."""
        raise NotImplementedError

    def api_record_audit(self, guild_id: str, actor_id: str, actor_name: str, action: str, details: str = "") -> None:
        """Persist a mobile API audit event."""
        raise NotImplementedError

    def api_member_warnings(self, guild_id: str, user_id: str) -> int:
        """Return the persisted warning count for one member."""
        raise NotImplementedError

    def api_warnlist_snapshot(self, guild_id: str) -> List[Dict[str, object]]:
        """Return members with active warnings for the mobile API."""
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

    def dashboard_settings_snapshot(self, guild_id: str) -> Optional[Dict[str, object]]:
        """Return serializable guild, channel, and role data for dashboard settings."""
        raise NotImplementedError

    def dashboard_roles_snapshot(self, guild_id: str) -> Optional[List[Dict[str, str]]]:
        """Return all guild roles, including @everyone, as serializable DTOs."""
        raise NotImplementedError

    def dashboard_overview_snapshot(self, guild_id: str, ticket_category_id: str = None) -> Optional[Dict[str, object]]:
        """Return serializable data needed by the main dashboard page."""
        raise NotImplementedError

    def api_handle_github_release(self, delivery_id: str, release: Dict[str, object]) -> str:
        """Dispatch a verified GitHub release webhook and deduplicate its delivery."""
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

    def api_mute_member(self, guild_id: str, user_id: str, minutes: int, reason: str):
        """Apply a timeout and write its existing sanction/log records."""
        raise NotImplementedError

    def api_ban_member(self, guild_id: str, user_id: str, reason: str):
        """Ban a member and write its existing sanction/log records."""
        raise NotImplementedError

    def api_kick_member(self, guild_id: str, user_id: str, reason: str):
        """Kick a member and write its existing sanction/log records."""
        raise NotImplementedError

    def api_warn_member(self, guild_id: str, user_id: str, reason: str):
        """Record a member warning and attempt the existing Discord log."""
        raise NotImplementedError

    def api_unwarn_member(self, guild_id: str, user_id: str):
        """Remove one warning and best-effort log the updated count."""
        raise NotImplementedError

    def api_accept_appeal(self, guild_id: str, user_id: str, sanction_type: str):
        """Reverse a sanction and return the result without sending notifications."""
        raise NotImplementedError

    def api_notify_appeal_result(self, guild_id: str, user_id: str, case_id: str, accepted: bool, result=None):
        """Best-effort DM the user after the appeal has been resolved."""
        raise NotImplementedError

    def api_announce_calendar_event(self, guild_id: str, channel_id: str, event: Dict[str, object]):
        """Publish a calendar event through the bot runtime without exposing Discord objects."""
        raise NotImplementedError

    def api_grant_verified(self, guild_id: str, user_id: str):
        """Apply verification roles without exposing Discord guild/member objects to web routes."""
        raise NotImplementedError

    def api_log_dashboard_action(self, guild_id: str, actor_name: str, action: str, details: str = ""):
        """Best-effort post of a dashboard audit event to the configured Discord log channel."""
        raise NotImplementedError

    def api_post_appeal_for_review(self, guild_id: str, appeal: Dict[str, object], appeal_text: str):
        """Forward a submitted appeal to its configured Discord review channel."""
        raise NotImplementedError

    def notify_owner_bruteforce(self, ip: str):
        """Send the owner a best-effort alert for a kill-switch brute-force attempt."""
        raise NotImplementedError

    def notify_owner_killswitch(self, locked: bool):
        """Notify the owner when the global kill-switch changes state."""
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

    def api_guild_id_candidates(self) -> List[str]:
        bot = core.bot
        if bot is None or not bot.is_ready():
            return []
        return [str(guild.id) for guild in bot.guilds]

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

    def dashboard_settings_snapshot(self, guild_id: str) -> Optional[Dict[str, object]]:
        bot = core.bot
        if bot is None or not bot.is_ready():
            return None
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return None
        return {
            "id": str(guild.id),
            "name": guild.name,
            "owner_id": str(guild.owner_id),
            "text_channels": [
                {"id": str(channel.id), "name": channel.name}
                for channel in guild.text_channels
            ],
            "voice_channels": [
                {"id": str(channel.id), "name": channel.name}
                for channel in guild.voice_channels
            ],
            "roles": [
                {"id": str(role.id), "name": role.name}
                for role in guild.roles if not role.is_default()
            ],
        }

    def dashboard_roles_snapshot(self, guild_id: str) -> Optional[List[Dict[str, str]]]:
        bot = core.bot
        if bot is None or not bot.is_ready():
            return None
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return None
        return [{"id": str(role.id), "name": role.name} for role in guild.roles]

    def api_handle_github_release(self, delivery_id: str, release: Dict[str, object]) -> str:
        db = core.db
        bot = core.bot
        if db is None or bot is None or not bot.is_ready():
            return "unavailable"
        deliveries = db["github_webhook_deliveries"]
        if deliveries.find_one({"_id": delivery_id}):
            return "duplicate"
        cog = bot.get_cog("GitHubUpdates")
        if cog is None:
            return "handler_unavailable"
        try:
            sent = run_coroutine(cog.handle_webhook_release(release))
        except Exception:
            return "delivery_failed"
        if not sent:
            return "ignored"
        try:
            deliveries.insert_one({"_id": delivery_id})
        except Exception:
            # The Discord message is already sent; do not report a delivery failure.
            return "record_failed"
        return "sent"

    def dashboard_overview_snapshot(self, guild_id: str, ticket_category_id: str = None) -> Optional[Dict[str, object]]:
        bot = core.bot
        if bot is None or not bot.is_ready():
            return None
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return None
        permission_names = ("administrator", "manage_guild", "ban_members", "kick_members", "manage_roles")
        roles = [{
            "id": str(role.id),
            "name": role.name,
            "permissions": {
                name: bool(getattr(role.permissions, name, False))
                for name in permission_names
            },
        } for role in guild.roles if not role.is_default()]
        open_tickets = []
        if ticket_category_id and str(ticket_category_id).isdigit():
            category = guild.get_channel(int(ticket_category_id))
            if isinstance(category, discord.CategoryChannel):
                open_tickets = [
                    {"id": str(channel.id), "name": channel.name}
                    for channel in category.text_channels
                ]
        return {
            "id": str(guild.id),
            "name": guild.name,
            "owner_id": str(guild.owner_id),
            "text_channels": [
                {"id": str(channel.id), "name": channel.name}
                for channel in guild.text_channels
            ],
            "roles": roles,
            "role_names": {str(role.id): role.name for role in guild.roles},
            "open_tickets": open_tickets,
        }

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

    def api_config_snapshot(self, guild_id: str) -> Dict[str, object]:
        return get_config(guild_id)

    def api_update_config(self, guild_id: str, updates: Dict[str, object]) -> None:
        for key, value in updates.items():
            update_config(guild_id, key, value)

    def api_faq_snapshot(self, guild_id: str) -> Optional[Dict[str, object]]:
        return get_faq(guild_id)

    def api_faq_add_entry(self, guild_id: str, entry: Dict[str, object]) -> None:
        add_faq_entry(guild_id, entry)

    def api_faq_remove_entry(self, guild_id: str, entry_id: str) -> bool:
        return remove_faq_entry(guild_id, entry_id)

    def api_faq_set_enabled(self, guild_id: str, enabled: bool) -> None:
        set_faq_enabled(guild_id, enabled)

    def api_audit_snapshot(self, guild_id: str, limit: int = 20) -> List[Dict[str, object]]:
        return get_audit_log(guild_id, limit=limit)

    def api_sanction_cases_snapshot(self, guild_id: str, filter_type: str = "", limit: int = 50) -> List[Dict[str, object]]:
        return get_sanction_cases(guild_id, filter_type, limit)

    def api_record_audit(self, guild_id: str, actor_id: str, actor_name: str, action: str, details: str = "") -> None:
        record_audit(guild_id, actor_id, actor_name, action, details)

    def api_member_warnings(self, guild_id: str, user_id: str) -> int:
        return get_warns(guild_id, user_id)

    def api_warnlist_snapshot(self, guild_id: str) -> List[Dict[str, object]]:
        return get_warnlist(guild_id)

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
        warned_count = core.col.warns.count_documents({
            "guild_id": str(guild_id), "count": {"$gt": 0}
        })
        today_start = datetime.datetime.now(datetime.timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        warns_today = core.col.sanctions.count_documents({
            "guild_id": str(guild_id), "type": "warn", "timestamp": {"$gte": today_start},
        })
        activity_7d = [count for _, count in get_sanction_stats(guild_id, days=7)]
        return {
            "guild_id": str(guild.id),
            "name": guild.name,
            "member_count": guild.member_count,
            "ban_count": ban_count,
            "muted_count": muted_count,
            "open_tickets_count": open_tickets_count,
            "warned_count": warned_count,
            "warns_today": warns_today,
            "activity_7d": activity_7d,
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

    def api_mute_member(self, guild_id: str, user_id: str, minutes: int, reason: str):
        bot = core.bot
        if bot is None or not bot.is_ready():
            return None
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return False, "Guild not found"

        async def mute():
            member = guild.get_member(int(user_id))
            if member is None:
                return False, "Member not found"
            if member.top_role >= guild.me.top_role:
                return False, "Role too high"
            duration = datetime.timedelta(minutes=minutes)
            await member.timeout(duration, reason=reason)
            log_sanction(guild_id, user_id, "mute", f"{reason} ({minutes} min)", "mobile_app")
            await log_mute(guild, member, minutes, reason)
            return True, None

        return run_coroutine(mute())

    def api_ban_member(self, guild_id: str, user_id: str, reason: str):
        bot = core.bot
        if bot is None or not bot.is_ready():
            return None
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return False, "Guild not found"

        async def ban():
            member = guild.get_member(int(user_id))
            if member is None:
                return False, "Member not found"
            if member.top_role >= guild.me.top_role:
                return False, "Role too high"
            await member.ban(reason=reason)
            log_sanction(guild_id, user_id, "ban", reason, "mobile_app")
            await log_ban(guild, member, reason)
            return True, None

        return run_coroutine(ban())

    def api_kick_member(self, guild_id: str, user_id: str, reason: str):
        bot = core.bot
        if bot is None or not bot.is_ready():
            return None
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return False, "Guild not found"

        async def kick():
            member = guild.get_member(int(user_id))
            if member is None:
                return False, "Member not found"
            if member.top_role >= guild.me.top_role:
                return False, "Role too high"
            await member.kick(reason=reason)
            log_sanction(guild_id, user_id, "kick", reason, "mobile_app")
            await log_kick(guild, member, reason)
            return True, None

        return run_coroutine(kick())

    def api_warn_member(self, guild_id: str, user_id: str, reason: str):
        bot = core.bot
        if bot is None or not bot.is_ready():
            return None
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return False, "Guild not found"

        member = guild.get_member(int(user_id))
        if member is None:
            return False, "Member not found"
        count = get_warns(guild_id, user_id) + 1
        set_warns(guild_id, user_id, count)
        log_sanction(guild_id, user_id, "warn", reason, "mobile_app")
        try:
            run_coroutine(log_warn(guild, member, reason, count))
        except Exception:
            pass  # The warning is already recorded; Discord logging is secondary.
        return True, count

    def api_unwarn_member(self, guild_id: str, user_id: str):
        count = get_warns(guild_id, user_id)
        if count <= 0:
            return False, "No warnings to remove"
        count -= 1
        set_warns(guild_id, user_id, count)

        bot = core.bot
        if bot is not None and bot.is_ready():
            guild = bot.get_guild(int(guild_id))
            member = guild.get_member(int(user_id)) if guild is not None else None
            if member is not None:
                try:
                    run_coroutine(log_unwarn(guild, member, count))
                except Exception:
                    pass  # Warning removal remains successful if the log fails.
        return True, count

    def api_accept_appeal(self, guild_id: str, user_id: str, sanction_type: str):
        bot = core.bot
        if bot is None or not bot.is_ready():
            return False, "Bot not ready"
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return False, "Guild not found"
        try:
            result = run_coroutine(reverse_sanction(guild, sanction_type, user_id))
        except Exception:
            return False, "Unable to resolve appeal"
        return True, result

    def api_notify_appeal_result(self, guild_id: str, user_id: str, case_id: str, accepted: bool, result=None):
        bot = core.bot
        guild_name = "the server"
        if bot is not None and bot.is_ready():
            guild = bot.get_guild(int(guild_id))
            if guild is not None:
                guild_name = guild.name
        try:
            run_coroutine(_dm_user_appeal_result(int(user_id), guild_name, case_id, accepted, result))
        except Exception as exc:
            print(f"[APPEAL] Failed to DM user: {exc}", flush=True)
        return True, None

    def api_grant_verified(self, guild_id: str, user_id: str):
        bot = core.bot
        if bot is None or not bot.is_ready():
            return False, "Bot not ready"
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return False, "Guild not found"
        from verification import grant_verified
        return run_coroutine(grant_verified(guild, user_id))

    def api_log_dashboard_action(self, guild_id: str, actor_name: str, action: str, details: str = ""):
        bot = core.bot
        if bot is None or not bot.is_ready():
            return False, "Bot not ready"
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return False, "Guild not found"
        coro = log_dashboard_action(guild, actor_name, action, details)
        if coro is not None:
            run_coroutine(coro)
        return True, None

    def api_post_appeal_for_review(self, guild_id: str, appeal: Dict[str, object], appeal_text: str):
        bot = core.bot
        if bot is None or not bot.is_ready():
            return False, "Bot not ready"
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return False, "Guild not found"
        run_coroutine(post_appeal_for_review(guild, appeal, appeal_text))
        return True, None

    def notify_owner_bruteforce(self, ip: str):
        return run_coroutine(notify_owner_bruteforce(ip))

    def notify_owner_killswitch(self, locked: bool):
        return run_coroutine(notify_owner_killswitch(locked))

    def api_announce_calendar_event(self, guild_id: str, channel_id: str, event: Dict[str, object]):
        bot = core.bot
        if bot is None or not bot.is_ready():
            return False, "Bot not ready"
        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return False, "Guild not found"
        channel = guild.get_channel(int(channel_id))
        if channel is None or not hasattr(channel, "send"):
            return False, "Channel not found"
        role_id = event.get("role_id")
        role = guild.get_role(int(role_id)) if str(role_id or "").isdigit() else None
        if role is None or role.is_default():
            return False, "Role not found"
        from cogs.calendar import make_embed
        async def send():
            await channel.send(
                content=role.mention,
                embed=make_embed(event),
                allowed_mentions=discord.AllowedMentions(roles=[role], users=False, everyone=False),
            )
        try:
            run_coroutine(send())
        except discord.HTTPException:
            return False, "Announcement could not be sent"
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
