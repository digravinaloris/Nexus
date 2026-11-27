"""Persistence helpers for shareable server configuration templates.

Templates are intentionally global: they are shared across guilds by code.
"""
from nexus_runtime.core import col


def template_code_exists(code):
    return col.templates.find_one({"code": code}) is not None


def create_template(document):
    return col.templates.insert_one(document)


def get_template_by_code(code):
    return col.templates.find_one({"code": code})
