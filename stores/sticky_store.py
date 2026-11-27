"""Persistence helpers for per-channel sticky messages."""
from nexus_runtime.core import col


def set_sticky_message(guild_id, channel_id, content):
    col.sticky_messages.update_one(
        {"channel_id": str(channel_id)},
        {"$set": {
            "guild_id": str(guild_id),
            "channel_id": str(channel_id),
            "content": content,
            "last_message_id": None,
            "last_reposted_at": None,
        }},
        upsert=True,
    )


def remove_sticky_message(channel_id):
    col.sticky_messages.delete_one({"channel_id": str(channel_id)})


def get_sticky_message(channel_id):
    return col.sticky_messages.find_one({"channel_id": str(channel_id)})


def mark_sticky_message_posted(channel_id, message_id, posted_at):
    col.sticky_messages.update_one(
        {"channel_id": str(channel_id)},
        {"$set": {"last_message_id": str(message_id), "last_reposted_at": posted_at}},
    )
