"""Persistence helpers for interactive self-service role menus."""
from nexus_runtime.core import col


def save_role_menu(guild_id, message_id, channel_id, items):
    return col.role_menus.insert_one({
        "guild_id": str(guild_id),
        "message_id": str(message_id),
        "channel_id": str(channel_id),
        "items": items,
    })


def get_role_menu(message_id):
    return col.role_menus.find_one({"message_id": str(message_id)})
