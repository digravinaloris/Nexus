"""Compatibility alias; audit export implementation lives in audit_services.export."""
import sys

from audit_services import export as _implementation

sys.modules[__name__] = _implementation
