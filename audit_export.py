"""audit_export -- export de l'audit log en CSV / PDF (dashboard web et commande Discord).

Extrait de main.py (étape 2b-2b). Les collections viennent de `core.col`.
"""
import csv
import io

from core import col


def _get_audit_records(guild_id):
    return list(col.audit.find({"guild_id": str(guild_id)}).sort("timestamp", -1))


def _build_audit_csv_bytes(records):
    """Partagé entre /export audit (Discord) et le bouton d'export du dashboard web."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["actor_name", "action", "details", "timestamp"])
    for r in records:
        writer.writerow([r.get("actor_name", ""), r.get("action", ""), r.get("details", ""), r.get("timestamp", "")])
    return buf.getvalue().encode("utf-8")


def _build_audit_pdf_bytes(records, guild_name):
    """Tableau simple via reportlab -- pas de dépendance système (contrairement
    à weasyprint/wkhtmltopdf, qui demanderaient des libs non dispo sur le free
    tier Render sans apt-get)."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
    from reportlab.lib.styles import getSampleStyleSheet

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, topMargin=0.5 * inch)
    styles = getSampleStyleSheet()
    elements = [Paragraph(f"Audit Log — {guild_name}", styles["Title"])]
    data = [["Actor", "Action", "Details", "Timestamp"]]
    for r in records:
        data.append([
            str(r.get("actor_name", ""))[:30],
            str(r.get("action", ""))[:40],
            str(r.get("details", ""))[:60],
            str(r.get("timestamp", ""))[:19],
        ])
    table = Table(data, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#262b3d")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1f1f6")]),
    ]))
    elements.append(table)
    doc.build(elements)
    buf.seek(0)
    return buf.read()
