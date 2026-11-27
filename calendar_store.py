"""Persistence helpers for calendar events shared by dashboard and Discord cog.

Keep MongoDB collection access out of Flask routes. A future remote bot-service
adapter can replace this store without changing dashboard request handling.
"""
import core


def _collection():
    bot = core.bot
    if bot is None or getattr(bot, "db", None) is None:
        raise RuntimeError("Calendar storage is unavailable")
    return bot.db["calendar_events"]


def find_duplicate_event(guild_id, title, starts_at):
    return _collection().find_one({
        "guild_id": str(guild_id),
        "title": title,
        "starts_at": starts_at,
    }) is not None


def insert_event(event):
    return _collection().insert_one(event).inserted_id


def delete_event(guild_id, event_id):
    """Delete an event by its public hexadecimal ID inside the persistence layer."""
    from bson import ObjectId
    from bson.errors import InvalidId
    try:
        object_id = ObjectId(event_id)
    except (TypeError, InvalidId):
        return False
    result = _collection().delete_one({
        "_id": object_id,
        "guild_id": str(guild_id),
    })
    return bool(result.deleted_count)


def list_upcoming_events(guild_id, now, limit=50):
    return list(
        _collection()
        .find({"guild_id": str(guild_id), "starts_at": {"$gte": now}})
        .sort("starts_at", 1)
        .limit(limit)
    )
