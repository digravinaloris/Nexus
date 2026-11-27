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
