"""Transport-neutral bot service boundary used by the web application.

The Flask routes must depend on this small interface rather than reaching into
Discord's Gateway client directly. The first adapter deliberately preserves the
existing in-process deployment; a signed HTTP adapter can implement the same
contract in a later migration step without changing route response shapes.
"""
from typing import Dict, List, Optional


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

    def api_submitted_appeals(self, guild_id: str) -> List[Dict[str, object]]:
        """Return submitted appeals for one guild."""
        raise NotImplementedError

    def api_get_appeal(self, token: str) -> Optional[Dict[str, object]]:
        """Return one appeal by its token."""
        raise NotImplementedError

    def public_get_killswitch_token(self, token: str) -> Optional[Dict[str, object]]:
        """Look up one global kill-switch token."""
        raise NotImplementedError

    def public_increment_killswitch_attempts(self, token: str) -> Optional[int]:
        """Atomically count one failed kill-switch code attempt."""
        raise NotImplementedError

    def public_mark_killswitch_token_used(self, token: str) -> bool:
        """Atomically consume one valid kill-switch token."""
        raise NotImplementedError

    def public_global_lock_state(self) -> bool:
        """Return the persisted global bot lock state."""
        raise NotImplementedError

    def public_set_global_lock(self, locked: bool) -> None:
        """Persist the global bot lock state."""
        raise NotImplementedError

    def public_submit_ban_appeal(self, token: str, appeal_text: str, submitted_at) -> bool:
        """Atomically submit a public ban appeal."""
        raise NotImplementedError

    def verify_get_valid_challenge(self, token: str) -> Optional[Dict[str, object]]:
        """Return a live, unused verification challenge."""
        raise NotImplementedError

    def verify_record_failed_attempt(self, token: str) -> int:
        """Atomically count one failed Turnstile verification."""
        raise NotImplementedError

    def verify_mark_used(self, token: str) -> bool:
        """Atomically consume one verification challenge."""
        raise NotImplementedError

    def api_claim_appeal(self, guild_id: str, token: str) -> bool:
        """Atomically claim a pending appeal for review."""
        raise NotImplementedError

    def api_release_appeal_claim(self, guild_id: str, token: str) -> None:
        """Release a pending appeal review claim after a failed action."""
        raise NotImplementedError

    def api_resolve_appeal(self, guild_id: str, token: str, status: str) -> bool:
        """Resolve a claimed appeal to an accepted or denied state."""
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

    def dashboard_add_command_role(self, guild_id: str, command: str, role_id: int) -> None:
        """Add one command permission role."""
        raise NotImplementedError

    def dashboard_remove_command_role(self, guild_id: str, command: str, role_id: int) -> None:
        """Remove one command permission role."""
        raise NotImplementedError

    def dashboard_sanction_stats_snapshot(self, guild_id: str, days: int = 30) -> List[object]:
        """Return the dashboard sanction activity series."""
        raise NotImplementedError

    def dashboard_sanction_types_snapshot(self, guild_id: str) -> List[str]:
        """Return the sanction types currently present for one guild."""
        raise NotImplementedError

    def dashboard_calendar_has_duplicate_event(self, guild_id: str, title: str, starts_at) -> bool:
        """Check whether an equivalent calendar event already exists."""
        raise NotImplementedError

    def dashboard_calendar_insert_event(self, event: Dict[str, object]):
        """Persist a prepared calendar event and return its database ID."""
        raise NotImplementedError

    def dashboard_calendar_delete_event(self, guild_id: str, event_id: str) -> bool:
        """Delete one calendar event by its hexadecimal ID, scoped to the guild."""
        raise NotImplementedError

    def dashboard_calendar_upcoming_events(self, guild_id: str, now, limit: int = 50) -> List[Dict[str, object]]:
        """Return upcoming calendar event documents for dashboard rendering."""
        raise NotImplementedError

    def dashboard_audit_export_records(self, guild_id: str) -> List[Dict[str, object]]:
        """Return full audit records for dashboard exports."""
        raise NotImplementedError

    def dashboard_public_templates_snapshot(self) -> List[Dict[str, object]]:
        """Return the public template marketplace entries."""
        raise NotImplementedError

    def dashboard_template_snapshot(self, code: str) -> Optional[Dict[str, object]]:
        """Return one marketplace template by share code."""
        raise NotImplementedError

    def dashboard_apply_template_snapshot(self, guild_id: str, snapshot: Dict[str, object]) -> None:
        """Apply the portable part of a marketplace template."""
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


_service_instance: Optional[BotService] = None


def get_bot_service() -> BotService:
    """Return the lazily created process-wide adapter instance."""
    global _service_instance
    if _service_instance is None:
        from web.in_process_bot_service import InProcessBotService
        _service_instance = InProcessBotService()
    return _service_instance
