"""Persistence helpers for scheduled announcements and user reminders."""
from nexus_runtime.core import col


def create_announcement(guild_id, channel_id, content, send_at, author_id):
    return col.scheduled.insert_one({
        "guild_id": str(guild_id),
        "channel_id": str(channel_id),
        "content": content,
        "send_at": send_at,
        "author_id": str(author_id),
        "sent": False,
    })


def get_due_announcements(now):
    return list(col.scheduled.find({
        "sent": False, "send_at": {"$lte": now},
    }))


def mark_announcement_sent(announcement_id):
    return col.scheduled.update_one(
        {"_id": announcement_id}, {"$set": {"sent": True}},
    )


def create_reminder(user_id, guild_id, text, due_at, created_at):
    return col.reminders.insert_one({
        "user_id": str(user_id),
        "guild_id": str(guild_id) if guild_id is not None else None,
        "text": text,
        "due_at": due_at,
        "sent": False,
        "created_at": created_at,
    })


def get_due_reminders(now):
    return list(col.reminders.find({
        "sent": False, "due_at": {"$lte": now},
    }))


def mark_reminder_sent(reminder_id):
    return col.reminders.update_one(
        {"_id": reminder_id}, {"$set": {"sent": True}},
    )
