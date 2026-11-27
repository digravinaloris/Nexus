"""Guild-scoped persistence cleanup when Nexus leaves a server."""
from nexus_runtime.core import col


def cleanup_guild_data(guild_id):
    """Remove the guild's persisted configuration and moderation state."""
    guild_id = str(guild_id)
    col.config.delete_one({"guild_id": guild_id})
    col.warns.delete_many({"guild_id": guild_id})
    col.locked_channels.delete_many({"guild_id": guild_id})
    col.sanctions.delete_many({"guild_id": guild_id})
    col.reaction_roles.delete_many({"guild_id": guild_id})
    col.notes.delete_many({"guild_id": guild_id})
