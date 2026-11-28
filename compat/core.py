"""Compatibility alias; shared runtime state lives in nexus_runtime.core."""
import sys

from nexus_runtime import core as _implementation

sys.modules[__name__] = _implementation
