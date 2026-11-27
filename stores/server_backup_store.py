"""Persistence helpers for server structure backups."""
from bson import ObjectId
from bson.errors import InvalidId

from nexus_runtime.core import col


def insert_server_backup(backup_doc):
    result = col.server_backups.insert_one(backup_doc)
    guild_id = str(backup_doc["guild_id"])
    backups = list(col.server_backups.find({"guild_id": guild_id}).sort("created_at", -1))
    for old in backups[5:]:
        col.server_backups.delete_one({"_id": old["_id"]})
    return result.inserted_id


def list_server_backups(guild_id):
    return list(
        col.server_backups.find({"guild_id": str(guild_id)}).sort("created_at", -1)
    )


def get_server_backup(guild_id, backup_id):
    try:
        object_id = ObjectId(backup_id)
    except (TypeError, InvalidId):
        return None
    return col.server_backups.find_one({
        "_id": object_id,
        "guild_id": str(guild_id),
    })
