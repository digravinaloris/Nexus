"""Persistence helpers for public dashboard templates."""

from core import col


def list_public_templates(limit=50):
    """Return the newest public templates, bounded for dashboard rendering."""
    return list(col.templates.find({"public": True}).sort("created_at", -1).limit(limit))


def get_template_by_code(code):
    """Look up a template by its normalized share code."""
    return col.templates.find_one({"code": str(code).strip()})
