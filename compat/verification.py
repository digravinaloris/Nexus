"""Compatibility alias; implementation lives in security.verification."""
import sys

from security import verification as _implementation

sys.modules[__name__] = _implementation
