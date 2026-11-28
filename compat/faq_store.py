"""Compatibility re-export; implementation lives in stores.faq_store."""
from stores import faq_store as _implementation

globals().update({name: value for name, value in vars(_implementation).items() if not name.startswith("__")})
