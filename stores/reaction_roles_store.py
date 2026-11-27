"""Persistence helpers for message reaction-role mappings."""
from nexus_runtime.core import col


def save_reaction_role(guild_id, message_id, channel_id, emoji, role_id):
    col.reaction_roles.update_one(
        {"guild_id": str(guild_id), "message_id": str(message_id), "emoji": str(emoji)},
        {"$set": {"channel_id": str(channel_id), "role_id": str(role_id)}},
        upsert=True,
    )


def get_reaction_role(guild_id, message_id, emoji):
    return col.reaction_roles.find_one({
        "guild_id": str(guild_id),
        "message_id": str(message_id),
        "emoji": str(emoji),
    })


def delete_reaction_roles(guild_id, message_id):
    col.reaction_roles.delete_many({"guild_id": str(guild_id), "message_id": str(message_id)})
