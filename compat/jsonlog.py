"""Compatibility re-export; structured logging lives in nexus_logging.jsonlog."""
from nexus_logging.jsonlog import log_json

__all__ = ["log_json"]
