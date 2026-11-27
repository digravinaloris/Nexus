"""Database-backed audit export queries and compatibility re-exports."""
from core import col
from audit_helpers.export_utils import _safe_csv_cell, _build_audit_csv_bytes, _build_audit_pdf_bytes


def _get_audit_records(guild_id):
    return list(col.audit.find({"guild_id": str(guild_id)}).sort("timestamp", -1))
