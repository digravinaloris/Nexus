"""Persistence helpers for internal staff notes on members."""
from nexus_runtime.core import col


def add_member_note(guild_id, user_id, text, moderator_id, timestamp):
    return col.notes.insert_one({
        "guild_id": str(guild_id),
        "user_id": str(user_id),
        "text": text,
        "moderator_id": str(moderator_id),
        "timestamp": timestamp,
    })


def get_member_notes(guild_id, user_id, limit=15):
    return list(
        col.notes.find({
            "guild_id": str(guild_id),
            "user_id": str(user_id),
        }).sort("timestamp", -1).limit(limit)
    )
