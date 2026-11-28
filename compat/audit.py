"""Compatibility alias; audit journal implementation lives in audit_services.audit."""
import sys

from audit_services import audit as _implementation

sys.modules[__name__] = _implementation
