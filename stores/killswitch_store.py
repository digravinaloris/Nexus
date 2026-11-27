"""Persistence helpers for the email/TOTP global kill-switch tokens."""
import datetime
import secrets

from pymongo import ReturnDocument

from nexus_runtime.core import col


def get_killswitch_token(token):
    return col.killswitch_tokens.find_one({"token": token})


def increment_killswitch_attempts(token):
    """Atomically count one failed attempt, stopping at five or after use."""
    doc = col.killswitch_tokens.find_one_and_update(
        {"token": token, "used": {"$ne": True}, "attempts": {"$lt": 5}},
        {"$inc": {"attempts": 1}},
        return_document=ReturnDocument.AFTER,
    )
    return doc.get("attempts") if doc else None


def mark_killswitch_token_used(token):
    """Atomically consume an unused, non-locked-out token exactly once."""
    doc = col.killswitch_tokens.find_one_and_update(
        {"token": token, "used": {"$ne": True}, "attempts": {"$lt": 5}},
        {"$set": {"used": True}},
        return_document=ReturnDocument.AFTER,
    )
    return doc is not None



def create_killswitch_token():
    """Create a cryptographically random token valid for 30 minutes."""
    token = secrets.token_urlsafe(32)
    now = datetime.datetime.now(datetime.timezone.utc)
    col.killswitch_tokens.insert_one({
        "token": token,
        "created_at": now,
        "expires_at": now + datetime.timedelta(minutes=30),
        "used": False,
        "attempts": 0,
    })
    return token
