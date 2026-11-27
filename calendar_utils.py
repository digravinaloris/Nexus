"""Pure calendar date/time helpers without Discord runtime dependencies."""
import datetime as dt
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = dt.timezone.utc
DEFAULT_TZ = "Europe/Paris"
REMINDERS = (("24h", dt.timedelta(hours=24)), ("1h", dt.timedelta(hours=1)))


def validate_timezone(name):
    try:
        ZoneInfo((name or "").strip())
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("Invalid IANA timezone (example: Europe/Paris).") from None
    return name.strip()


def parse_event_datetime(date_text, time_text, timezone_name, now=None):
    zone = ZoneInfo(validate_timezone(timezone_name))
    try:
        day = dt.date.fromisoformat(date_text.strip())
    except (ValueError, AttributeError):
        raise ValueError("Date must be YYYY-MM-DD.") from None
    hour, minute = 0, 0
    if time_text:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", time_text.strip()):
            raise ValueError("Time must be HH:MM in 24-hour format.")
        hour, minute = map(int, time_text.strip().split(":"))
    local = dt.datetime.combine(day, dt.time(hour, minute), tzinfo=zone)
    if local.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
        raise ValueError("This local time does not exist due to daylight-saving time.")
    now = now or dt.datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if local.astimezone(UTC) <= now.astimezone(UTC):
        raise ValueError("Events must be in the future.")
    return local.astimezone(UTC)


def local_when(event):
    local = event["starts_at"].astimezone(ZoneInfo(event.get("timezone", DEFAULT_TZ)))
    return local.strftime("%Y-%m-%d %H:%M %Z") if event.get("time_set") else local.strftime("%Y-%m-%d (time unspecified)")
