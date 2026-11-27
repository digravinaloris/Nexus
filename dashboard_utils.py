"""Pure validation helpers for dashboard form data."""

from utils import parse_bounded_number


def read_bounded_form_numbers(values, fields):
    """Parse bounded numeric form values, ignoring blanks.

    fields contains (key, type, minimum, maximum) tuples. Invalid values
    raise ValueError with the offending field name, matching dashboard routes.
    """
    parsed = {}
    for key, kind, minimum, maximum in fields:
        raw = values.get(key, "")
        if not isinstance(raw, str):
            raw = str(raw)
        if not raw.strip():
            continue
        try:
            parsed[key] = parse_bounded_number(raw, kind, minimum, maximum)
        except ValueError as exc:
            raise ValueError(key) from exc
    return parsed



def validate_channel_selection(raw, channels):
    """Return a selected channel ID if it exists in the supplied channel list."""
    raw = raw.strip()
    if not raw or raw == "none":
        return None
    if raw.isdigit():
        channel_ids = {
            int(channel["id"] if isinstance(channel, dict) else channel.id)
            for channel in channels
        }
        if int(raw) in channel_ids:
            return int(raw)
    raise ValueError("invalid channel selection")


def validate_role_selection(raw, guild):
    """Return a selected role ID, rejecting unknown roles and @everyone."""
    raw = raw.strip()
    if not raw or raw == "none":
        return None
    if raw.isdigit():
        role_id = int(raw)
        if isinstance(guild, dict):
            role = next((r for r in guild["roles"] if int(r["id"]) == role_id), None)
            if role is not None:
                return int(role["id"])
        else:
            role = guild.get_role(role_id)
            if role is not None and not role.is_default():
                return role.id
    raise ValueError("invalid role selection")



def is_safe_relative_redirect(target):
    """Return whether target is a safe, same-site absolute-path redirect."""
    from urllib.parse import urlparse

    if not isinstance(target, str):
        return False
    parsed = urlparse(target)
    return (
        target.startswith("/")
        and not target.startswith("//")
        and "\\" not in target
        and not any(ord(char) < 32 or ord(char) == 127 for char in target)
        and not parsed.scheme
        and not parsed.netloc
    )


def build_feature_toggle_updates(feature_toggles, config, submitted_keys, defaults=None):
    """Return config writes and effective feature changes for a dashboard submission.

    Missing stored values use code defaults when calculating effective changes,
    but are still written when the submitted value differs from the stored state.
    """
    defaults = defaults or {}
    updates = []
    enabled_now = []
    disabled_now = []

    for key, meta in feature_toggles.items():
        config_key = meta["config_key"]
        new_value = key in submitted_keys
        stored = config.get(config_key)
        if config_key in config and bool(stored) == new_value:
            continue

        effective_old = bool(defaults.get(config_key, False)) if config_key not in config else bool(stored)
        updates.append((config_key, new_value))
        if effective_old != new_value:
            (enabled_now if new_value else disabled_now).append(key)

    return updates, enabled_now, disabled_now
