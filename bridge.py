"""Compatibility shim; the runtime bridge lives in services.bridge."""
from services.bridge import run_coroutine

__all__ = ["run_coroutine"]
