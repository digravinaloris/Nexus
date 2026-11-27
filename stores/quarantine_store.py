"""Persistence helpers for the new-member quarantine review queue."""
from nexus_runtime.core import col


def create_quarantine_entry(guild_id, user_id, account_age_days, joined_at):
    return col.quarantine.insert_one({
        "guild_id": str(guild_id),
        "user_id": str(user_id),
        "account_age_days": account_age_days,
        "joined_at": joined_at,
        "reviewed": False,
        "reminded": False,
    })


def get_pending_quarantine_reviews():
    return list(col.quarantine.find({"reviewed": False, "reminded": False}))


def mark_quarantine_reminded(entry_id):
    return col.quarantine.update_one(
        {"_id": entry_id}, {"$set": {"reminded": True}},
    )
