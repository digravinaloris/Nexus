"""Persistence helpers for invite attribution and inviter leaderboards."""
from nexus_runtime.core import col


def record_invite_use(guild_id, inviter_id, invited_user_id, invite_code, timestamp):
    return col.invite_uses.insert_one({
        "guild_id": str(guild_id),
        "inviter_id": str(inviter_id),
        "invited_user_id": str(invited_user_id),
        "invite_code": invite_code,
        "timestamp": timestamp,
    })


def get_inviter_leaderboard(guild_id, limit=10):
    pipeline = [
        {"$match": {"guild_id": str(guild_id)}},
        {"$group": {"_id": "$inviter_id", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": limit},
    ]
    return list(col.invite_uses.aggregate(pipeline))


def get_invite_for_member(guild_id, invited_user_id):
    return col.invite_uses.find_one({
        "guild_id": str(guild_id),
        "invited_user_id": str(invited_user_id),
    })
