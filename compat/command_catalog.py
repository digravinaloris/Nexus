"""Compatibility shim; command metadata lives in catalogs.commands."""
from catalogs.commands import COMMAND_TEMPLATES, MODERATION_COMMANDS

__all__ = ["COMMAND_TEMPLATES", "MODERATION_COMMANDS"]
