"""Regression tests for spreadsheet-safe audit CSV export."""
import csv
import io

import pytest


@pytest.mark.parametrize("value", ["=1+1", "+SUM(A1:A2)", "-2+3", "@SUM(A1)", "  =cmd", "\t=cmd", "\r=cmd", "\n=cmd"])
def test_csv_cell_neutralizes_formula_prefixes(value):
    from audit_export_utils import _safe_csv_cell
    assert _safe_csv_cell(value) == "'" + value


@pytest.mark.parametrize("value", ["normal text", "123", "user@example.com", "", "  ordinary"])
def test_csv_cell_preserves_non_formula_text(value):
    from audit_export_utils import _safe_csv_cell
    assert _safe_csv_cell(value) == value


def test_audit_csv_export_neutralizes_untrusted_fields():
    from audit_export_utils import _build_audit_csv_bytes
    rows = [{"actor_name": "=2+2", "action": "warn", "details": "\t=cmd", "timestamp": "2026-01-01"}]
    exported = _build_audit_csv_bytes(rows).decode("utf-8")
    parsed = list(csv.reader(io.StringIO(exported)))
    assert parsed[1][0] == "'=2+2"
    assert parsed[1][2] == "'\t=cmd"
