"""In-memory checks used by Nexus AutoMod and anti-raid protection."""
import re
import time

from helpers.utils import detect_raid_username_pattern
from stores.config_store import get_config

SPAM_MESSAGE_COUNT = 10
SPAM_WINDOW_SECONDS = 5
RAID_JOIN_COUNT = 5
RAID_WINDOW_SECONDS = 10
INVITE_LINK_RE = re.compile(r"(discord\.gg/|discord(?:app)?\.com/invite/)", re.IGNORECASE)

_recent_messages = {}
_recent_joins = {}
_recent_join_names = {}
_media_flood_tracker = {}
_channel_activity_tracker = {}


def is_automod_exempt(member, cfg):
    """Moderators and configured exempt roles are never auto-moderated."""
    if member.guild_permissions.manage_messages:
        return True
    allowed = set(cfg.get("allowed_roles", []))
    member_roles = {role.id for role in member.roles}
    if member_roles & allowed:
        return True
    exempt_roles = {int(role_id) for role_id in cfg.get("automod_exempt_role_ids", []) if str(role_id).isdigit()}
    return bool(member_roles & exempt_roles)


def check_spam(guild_id, user_id, cfg=None):
    cfg = cfg or get_config(guild_id)
    spam_count = cfg.get("automod_spam_count", SPAM_MESSAGE_COUNT)
    spam_window = cfg.get("automod_spam_window", SPAM_WINDOW_SECONDS)
    key = (guild_id, user_id)
    now = time.time()
    timestamps = _recent_messages.get(key, [])
    timestamps = [timestamp for timestamp in timestamps if now - timestamp < spam_window]
    timestamps.append(now)
    _recent_messages[key] = timestamps
    return len(timestamps) > spam_count


def check_raid(guild_id, cfg=None):
    """Return True when enough members join within the configured time window."""
    cfg = cfg or get_config(guild_id)
    raid_count = cfg.get("automod_raid_count", RAID_JOIN_COUNT)
    raid_window = cfg.get("automod_raid_window", RAID_WINDOW_SECONDS)
    now = time.time()
    timestamps = _recent_joins.get(guild_id, [])
    timestamps = [timestamp for timestamp in timestamps if now - timestamp < raid_window]
    timestamps.append(now)
    _recent_joins[guild_id] = timestamps
    return len(timestamps) >= raid_count


def record_join_name_and_check_pattern(guild_id, display_name, window_seconds=30):
    """Track recent display names and detect repeated raid-style patterns."""
    now = time.time()
    entries = _recent_join_names.get(guild_id, [])
    entries = [(timestamp, name) for timestamp, name in entries if now - timestamp < window_seconds]
    entries.append((now, display_name))
    _recent_join_names[guild_id] = entries
    return detect_raid_username_pattern([name for _, name in entries])


def check_media_flood(guild_id, user_id, cfg):
    count_threshold = cfg.get("media_flood_count", 5)
    window = cfg.get("media_flood_window", 10)
    now = time.time()
    key = (guild_id, user_id)
    timestamps = _media_flood_tracker.get(key, [])
    timestamps = [timestamp for timestamp in timestamps if now - timestamp < window]
    timestamps.append(now)
    _media_flood_tracker[key] = timestamps
    return len(timestamps) > count_threshold


def check_channel_burst(channel_id, cfg):
    """Return True when a channel receives too many messages too quickly."""
    threshold = cfg.get("auto_slowmode_threshold", 10)
    window = cfg.get("auto_slowmode_window", 10)
    now = time.time()
    timestamps = _channel_activity_tracker.get(channel_id, [])
    timestamps = [timestamp for timestamp in timestamps if now - timestamp < window]
    timestamps.append(now)
    _channel_activity_tracker[channel_id] = timestamps
    return len(timestamps) > threshold


def check_invite_link(content):
    return bool(INVITE_LINK_RE.search(content))
