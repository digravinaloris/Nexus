"""Compatibility re-export; dashboard helpers live in web.dashboard_utils."""
from web import dashboard_utils as _implementation

globals().update({name: value for name, value in vars(_implementation).items() if not name.startswith("__")})
