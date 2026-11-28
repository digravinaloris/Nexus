"""Compatibility alias; localization helpers live in localization.i18n."""
import sys

from localization import i18n as _implementation

sys.modules[__name__] = _implementation
