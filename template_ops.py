"""Compatibility shim; template operations live in services.template_ops."""
from services.template_ops import (
    TEMPLATE_AUTOMOD_BOUNDS,
    TEMPLATE_AUTOMOD_KEYS,
    _apply_template_base_config,
)

__all__ = ["TEMPLATE_AUTOMOD_BOUNDS", "TEMPLATE_AUTOMOD_KEYS", "_apply_template_base_config"]
