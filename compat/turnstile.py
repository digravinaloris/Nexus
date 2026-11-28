"""Compatibility alias; implementation lives in security.turnstile."""
import sys

from security import turnstile as _implementation

sys.modules[__name__] = _implementation
