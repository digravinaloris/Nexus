"""Persistence helpers for the email/TOTP global kill-switch tokens."""
from core import col


def get_killswitch_token(token):
    return col.killswitch_tokens.find_one({"token": token})


def increment_killswitch_attempts(token):
    result = col.killswitch_tokens.update_one({"token": token}, {"$inc": {"attempts": 1}})
    return result.modified_count == 1


def mark_killswitch_token_used(token):
    result = col.killswitch_tokens.update_one({"token": token}, {"$set": {"used": True}})
    return result.modified_count == 1
