"""Compatibility alias; common utility functions live in helpers.utils."""
import sys

from helpers import utils as _implementation

sys.modules[__name__] = _implementation
