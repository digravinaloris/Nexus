"""Compatibility shim; Discord logging lives in services.discord_logging."""
from services.discord_logging import *  # noqa: F401,F403
from services.discord_logging import _send_log_embed
