"""Compatibility re-export; implementation lives in stores.config_store."""
from stores import config_store as _implementation

globals().update({name: value for name, value in vars(_implementation).items() if not name.startswith("__")})
