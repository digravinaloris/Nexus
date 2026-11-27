"""Persistence helpers for public dashboard templates."""

from nexus_runtime.core import col


def list_public_templates(limit=50):
    """Return the newest public templates, bounded for dashboard rendering."""
    return list(col.templates.find({"public": True}).sort("created_at", -1).limit(limit))


def get_template_by_code(code):
    """Look up a template by its normalized share code."""
    return col.templates.find_one({"code": str(code).strip()})


def template_code_exists(code):
    return col.templates.find_one({"code": str(code).strip()}) is not None


def create_template(document):
    return col.templates.insert_one(document)
