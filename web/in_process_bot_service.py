"""Transport-neutral bot service boundary used by the web application.

The Flask routes must depend on this small interface rather than reaching into
Discord's Gateway client directly. The first adapter deliberately preserves the
existing in-process deployment; a signed HTTP adapter can implement the same
contract in a later migration step without changing route response shapes.
"""
from typing import Dict, List, Optional
import datetime
import math
import secrets
import time

from nexus_runtime import core
import discord
from stores import calendar_store
from services.template_ops import _apply_template_base_config
from stores.template_store import get_public_template_by_code, list_public_templates
from stores.killswitch_store import get_killswitch_token, increment_killswitch_attempts, mark_killswitch_token_used
from security.verification import get_valid_challenge, mark_used, record_failed_attempt
from stores.config_store import add_command_role, get_config, get_global_lock, remove_command_role, set_global_lock, update_config, update_config_fields
from stores.faq_store import add_faq_entry, get_faq, remove_faq_entry, set_faq_enabled
from audit_services.audit import get_audit_log, record_audit
from audit_services.export import _get_audit_records
from services.bridge import run_coroutine
from services.discord_logging import log_ban, log_dashboard_action, log_kick, log_mute, log_unmute, log_unwarn, log_warn
from stores.sanctions_store import claim_sanction_appeal, claim_sanction_appeal_by_case, get_ban_appeal, get_dashboard_warning_counts, get_sanction_cases, get_sanction_stats, get_sanction_types, get_submitted_appeals, get_warnlist, get_warns, log_sanction, release_sanction_appeal_claim, release_sanction_appeal_claim_by_case, resolve_sanction_appeal, resolve_sanction_appeal_by_case, set_warns, submit_ban_appeal
from helpers import utils
from services.moderation_actions import _dm_user_appeal_result, post_appeal_for_review, reverse_sanction
from services.owner_notify import notify_owner_bruteforce, notify_owner_killswitch
from web.bot_service import BotService


class InProcessBotService(BotService):
    """Compatibility adapter for the current single-process bot + web runtime."""

    PRESENCE_TTL_SECONDS = 25

    def __init__(self):
        self._dashboard_presence = {}

    def dashboard_presence_ping(self, guild_id: str, user_id: str, name: str) -> None:
        now = time.time()
        entries = self._dashboard_presence.setdefault(str(guild_id), {})
        expired = [key for key, (_, last_seen) in entries.items() if now - last_seen >= self.PRESENCE_TTL_SECONDS]
        for key in expired:
            entries.pop(key, None)
        entries[str(user_id)] = (str(name), now)

    def dashboard_presence_viewers(self, guild_id: str) -> List[str]:
        now = time.time()
        guild_id = str(guild_id)
        entries = self._dashboard_presence.get(guild_id, {})
        expired = [key for key, (_, last_seen) in entries.items() if now - last_seen >= self.PRESENCE_TTL_SECONDS]
        for key in expired:
            entries.pop(key, None)
        if not entries:
            self._dashboard_presence.pop(guild_id, None)
        return [name for name, _ in entries.values()]

    def is_guild_owner(self, guild_id: str, user_id: str) -> bool:
        bot = core.bot
        if bot is None or not bot.is_ready():
            return False
        guild = bot.get_guild(int(guild_id))
        return bool(guild is not None and str(guild.owner_id) == str(user_id))

    def is_guild_admin(self, guild_id: str, user_id: str) -> bool:
        """Confirm owner or Administrator access using the current Discord cache."""
        bot = core.bot
        if bot is None or not bot.is_ready():
            return False
        try:
            guild = bot.get_guild(int(guild_id))
            if guild is None:
                return False
            if str(guild.owner_id) == str(user_id):
                return True
            member = guild.get_member(int(user_id))
            return bool(member is not None and member.guild_permissions.administrator)
        except (AttributeError, TypeError, ValueError):
            return False

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

    def api_authenticate_key(self, provided_key: str, guild_id: str = None) -> Optional[str]:
        if (not isinstance(provided_key, str) or not provided_key.isascii()
            or not 0 < len(provided_key) <= 512):
            return None
        candidates = [str(guild_id)] if guild_id is not None else self.api_guild_id_candidates()
        for candidate in candidates:
            expected = get_config(candidate).get("api_key")
            if (isinstance(expected, str) and expected.isascii() and 0 < len(expected) <= 512
                and secrets.compare_digest(provided_key, expected)):
                return candidate
        return None

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


    def dashboard_config_snapshot(self, guild_id: str) -> Dict[str, object]:
        """Project dashboard settings onto an explicit, bounded, secret-free DTO."""
        allowed = {
            "guild_id", "logs_channel", "autorole", "language", "tickets_enabled",
            "ticket_category_id", "appeal_channel_id", "boost_channel_id",
            "member_count_channel_id", "welcome_channel_id", "welcome_banner_enabled",
            "verification_enabled", "verified_role_id", "unverified_role_id",
            "quarantine_enabled", "quarantine_role_id", "quarantine_channel_id",
            "trap_channel_enabled", "trap_channel_id", "jtc_channel_id",
            "detailed_logs_enabled", "antinuke_enabled", "weekly_digest_enabled",
            "log_dashboard_actions", "anti_link_global_enabled",
            "anti_mention_spam_enabled", "anti_media_flood_enabled",
            "auto_slowmode_enabled", "anti_doxx_enabled", "raid_pattern_alert_enabled",
            "auto_translate_enabled", "remindme_enabled", "serverinfo_visual_enabled",
            "automod_spam_count", "automod_spam_window", "automod_caps_ratio",
            "automod_raid_count", "automod_raid_window", "mention_spam_threshold",
            "media_flood_count", "media_flood_window", "auto_slowmode_threshold",
            "auto_slowmode_window", "auto_slowmode_seconds",
            "auto_slowmode_duration_minutes", "warn_escalation_enabled",
            "warn_mute_threshold", "warn_mute_minutes", "warn_kick_threshold",
            "min_account_age_days", "antinuke_threshold",
            "quarantine_suspicion_days", "quarantine_hold_minutes",
            "banned_words", "banned_domains", "calendar_timezone",
            "calendar_channel_id", "calendar_manager_role_ids",
            "command_roles", "command_templates",
        }
        source = get_config(guild_id)
        result = {key: source[key] for key in allowed if key in source}
        for key in ("banned_words", "banned_domains", "calendar_manager_role_ids"):
            if key in result:
                value = result[key]
                if (not isinstance(value, list) or len(value) > 1000
                    or any(not isinstance(item, str) or len(item) > 253 for item in value)):
                    result.pop(key)
        for key in ("command_roles", "command_templates"):
            if key in result:
                value = result[key]
                valid = (
                    isinstance(value, dict) and len(value) <= 100
                    and all(
                        isinstance(name, str) and 0 < len(name) <= 100
                        and isinstance(items, list) and len(items) <= 100
                        and all(
                            (isinstance(item, str) and len(item) <= 100)
                            or (key == "command_roles" and type(item) is int and 0 < item < 2**64)
                            for item in items
                        )
                        for name, items in value.items()
                    )
                )
                if not valid:
                    result.pop(key)
        for key, value in list(result.items()):
            if key in {
                "guild_id", "banned_words", "banned_domains",
                "calendar_manager_role_ids", "command_roles", "command_templates",
            }:
                continue
            if value is not None and type(value) not in {bool, int, float, str}:
                result.pop(key)
            elif isinstance(value, str) and len(value) > 4096:
                result.pop(key)
        if result.get("guild_id") != str(guild_id):
            result["guild_id"] = str(guild_id)
        return result

    def api_rest_config_snapshot(self, guild_id: str) -> Dict[str, object]:
        """Project the legacy config document onto the reviewed mobile API schema."""
        allowed_fields = {
            "guild_id", "logs_channel", "autorole", "allowed_roles",
            "automod_exempt_role_ids", "command_roles", "command_templates",
            "language", "log_dashboard_actions", "log_webhook_channel_id",
            "tickets_enabled", "ticket_category_id", "ticket_support_role_id",
            "ticket_archive_category_id", "ticket_panels", "appeal_channel_id",
            "boost_channel_id", "detailed_logs_enabled", "banned_words",
            "antinuke_enabled", "antinuke_threshold", "weekly_digest_enabled",
            "last_digest_sent_at", "member_count_channel_id", "min_account_age_days",
            "banned_domains", "anti_link_global_enabled", "welcome_channel_id",
            "welcome_banner_enabled", "verification_enabled", "verified_role_id",
            "verification_autorole_id", "unverified_role_id", "quarantine_enabled",
            "quarantine_role_id", "quarantine_channel_id", "quarantine_hold_minutes",
            "quarantine_suspicion_days", "anti_mention_spam_enabled",
            "mention_spam_threshold", "anti_media_flood_enabled", "media_flood_count",
            "media_flood_window", "auto_slowmode_enabled", "auto_slowmode_threshold",
            "auto_slowmode_window", "auto_slowmode_seconds",
            "auto_slowmode_duration_minutes", "anti_doxx_enabled",
            "raid_pattern_alert_enabled", "auto_translate_enabled", "remindme_enabled",
            "serverinfo_visual_enabled", "trap_channel_enabled", "trap_channel_id",
            "automod_spam_count", "automod_spam_window", "automod_caps_ratio",
            "automod_raid_count", "automod_raid_window", "warn_escalation_enabled",
            "warn_mute_threshold", "warn_mute_minutes", "warn_kick_threshold",
            "jtc_channel_id", "jtc_category_id", "pending_role_menu_items",
        }
        config = get_config(guild_id)
        return {key: config[key] for key in allowed_fields if key in config}

    def api_update_config(self, guild_id: str, updates: Dict[str, object]) -> None:
        update_config_fields(guild_id, updates)

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
        return get_warnlist(guild_id, limit=1000)

    def api_submitted_appeals(self, guild_id: str) -> List[Dict[str, object]]:
        return get_submitted_appeals(guild_id)

    def dashboard_submitted_appeals_snapshot(self, guild_id: str, limit: int = 100) -> List[Dict[str, object]]:
        fields = ("case_id", "sanction_type", "user_name", "user_id", "ban_reason", "appeal_text", "submitted_at")
        return [
            {key: appeal[key] for key in fields}
            for appeal in get_submitted_appeals(guild_id, limit)
            if all(key in appeal for key in fields)
        ]

    def api_get_appeal(self, token: str) -> Optional[Dict[str, object]]:
        return get_ban_appeal(token)

    def api_claim_appeal(self, guild_id: str, token: str) -> bool:
        return claim_sanction_appeal(guild_id, token)

    def api_release_appeal_claim(self, guild_id: str, token: str) -> None:
        release_sanction_appeal_claim(guild_id, token)

    def api_resolve_appeal(self, guild_id: str, token: str, status: str) -> bool:
        return resolve_sanction_appeal(guild_id, token, status)

    def api_claim_appeal_by_case(self, guild_id: str, case_id: int) -> bool:
        return claim_sanction_appeal_by_case(guild_id, case_id)

    def api_release_appeal_claim_by_case(self, guild_id: str, case_id: int) -> bool:
        return release_sanction_appeal_claim_by_case(guild_id, case_id)

    def api_resolve_appeal_by_case(self, guild_id: str, case_id: int, status: str) -> bool:
        return resolve_sanction_appeal_by_case(guild_id, case_id, status)

    def dashboard_add_command_role(self, guild_id: str, command: str, role_id: int) -> None:
        add_command_role(guild_id, command, role_id)

    def dashboard_remove_command_role(self, guild_id: str, command: str, role_id: int) -> None:
        remove_command_role(guild_id, command, role_id)

    def dashboard_sanction_stats_snapshot(self, guild_id: str, days: int = 30) -> List[object]:
        return get_sanction_stats(guild_id, days=days)

    def dashboard_sanction_types_snapshot(self, guild_id: str) -> List[str]:
        return get_sanction_types(guild_id)

    def dashboard_calendar_has_duplicate_event(self, guild_id: str, title: str, starts_at) -> bool:
        return calendar_store.find_duplicate_event(guild_id, title, starts_at)

    def dashboard_calendar_insert_event(self, event: Dict[str, object]) -> str:
        return str(calendar_store.insert_event(event))

    def dashboard_calendar_delete_event(self, guild_id: str, event_id: str) -> bool:
        return calendar_store.delete_event(guild_id, event_id)

    def dashboard_calendar_upcoming_events(self, guild_id: str, now, limit: int = 50) -> List[Dict[str, object]]:
        return calendar_store.list_upcoming_events(guild_id, now, limit=limit)

    def dashboard_audit_export_records(self, guild_id: str) -> List[Dict[str, object]]:
        return _get_audit_records(guild_id)

    def public_get_killswitch_token(self, token: str) -> Optional[Dict[str, object]]:
        return get_killswitch_token(token)

    def public_increment_killswitch_attempts(self, token: str) -> Optional[int]:
        return increment_killswitch_attempts(token)

    def public_mark_killswitch_token_used(self, token: str) -> bool:
        return mark_killswitch_token_used(token)

    def public_global_lock_state(self) -> bool:
        return get_global_lock()

    def public_set_global_lock(self, locked: bool) -> None:
        set_global_lock(locked)

    def public_submit_ban_appeal(self, token: str, appeal_text: str, submitted_at) -> bool:
        return submit_ban_appeal(token, appeal_text, submitted_at)

    def verify_get_valid_challenge(self, token: str) -> Optional[Dict[str, object]]:
        return get_valid_challenge(token)

    def verify_record_failed_attempt(self, token: str) -> int:
        return record_failed_attempt(token)

    def verify_mark_used(self, token: str) -> bool:
        return mark_used(token)

    def dashboard_public_templates_snapshot(self) -> List[Dict[str, object]]:
        """Project public templates into a bounded, metadata-only dashboard DTO."""
        documents = list_public_templates(limit=50)
        result = []
        for document in documents:
            if not isinstance(document, dict) or document.get("public") is not True:
                continue
            snapshot = document.get("snapshot")
            if not isinstance(snapshot, dict):
                snapshot = {}
            feature_toggles = snapshot.get("feature_toggles")
            command_roles = snapshot.get("command_roles_by_name")
            result.append({
                "code": str(document.get("code") or "")[:32],
                "name": str(document.get("name") or "Untitled template")[:100],
                "created_guild_name": str(document.get("created_guild_name") or "Unknown server")[:100],
                "created_at": document.get("created_at") if isinstance(document.get("created_at"), datetime.datetime) else datetime.datetime.min.replace(tzinfo=datetime.timezone.utc),
                "feature_toggle_count": len(feature_toggles) if isinstance(feature_toggles, (list, tuple, dict)) else 0,
                "command_role_count": len(command_roles) if isinstance(command_roles, (list, tuple, dict)) else 0,
            })
        return result

    def dashboard_template_snapshot(self, code: str) -> Optional[Dict[str, object]]:
        return get_public_template_by_code(code)

    def dashboard_apply_template_snapshot(self, guild_id: str, snapshot: Dict[str, object]) -> None:
        _apply_template_base_config(guild_id, snapshot)

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
        today_start = datetime.datetime.now(datetime.timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        warning_counts = get_dashboard_warning_counts(guild_id, today_start)
        warned_count = warning_counts["warned_count"]
        warns_today = warning_counts["warns_today"]
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
        from security.verification import grant_verified
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
