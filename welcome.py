"""Compatibility alias; welcome delivery lives in services.welcome."""
import sys

from services import welcome as _implementation

sys.modules[__name__] = _implementation
