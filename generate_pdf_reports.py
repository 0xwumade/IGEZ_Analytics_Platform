"""
generate_pdf_reports.py
-----------------------
Converts each segmented JSON file into a clean, readable PDF table report
with attachment PDFs embedded directly after each record row.

Output structure:
  pdf_reports/
    leave_request/   gedu.pdf, surveillance.pdf ...
    cash_advance/    gedu.pdf ...
    expense_claim/   gedu.pdf ...
    rtps/            gedu.pdf ...
"""

import json
import io
import re
from pathlib import Path
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph,
    Spacer, HRFlowable, PageBreak, Image
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from pypdf import PdfWriter, PdfReader

# ── Subsidiary ID → name map ─────────────────────────────────────────────────

def load_subsidiary_name_map() -> dict:
    """Return {sub_id: clean_filename_stem} from subsidiary.json."""
    id_to_name = {}
    sub_file = Path("subsidiary.json")
    if not sub_file.exists():
        return id_to_name
    with open(sub_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                sub = json.loads(line)
                raw_id = sub.get("_id", {})
                sub_id = raw_id.get("$oid") if isinstance(raw_id, dict) else str(raw_id)
                name   = sub.get("subsidiary_name", "").strip()
                if sub_id and name:
                    # Make a safe filename: lowercase, spaces→underscore, strip specials
                    safe = re.sub(r"[^\w\s-]", "", name).strip()
                    safe = re.sub(r"\s+", "_", safe).lower()
                    id_to_name[sub_id] = safe
            except Exception:
                pass
    return id_to_name


# ── Column definitions per report type ───────────────────────────────────────
# (display_header, json_key)

COLUMNS = {
    "leave_request": [
        ("Date Applied",      "createdAt"),
        ("Leave Type",        "leave_Details"),
        ("Days Applying For", "no_days_applying_for"),
        ("Days Entitled",     "no_days_entitled_to"),
        ("Days Taken",        "no_days_taken"),
        ("Days Left",         "no_days_left"),
        ("Start Date",        "commencement_date"),
        ("End Date",          "date_leave_ends"),
        ("Resumption",        "date_of_resumption"),
        ("Status",            "status"),
        ("HOD Approved",      "is_HOD_Approved"),
        ("HR Approved",       "is_HR_Approved"),
        ("Subsidiary",        "subsidiary_name"),
    ],
    "cash_advance": [
        ("Date",          "date"),
        ("Staff Name",    "name"),
        ("Amount (₦)",    "amount"),
        ("Amount Words",  "amount_in_words"),
        ("Justification", "justification"),
        ("Status",        "status"),
        ("HOD Approved",  "is_HOD_Approved"),
        ("CFO Approved",  "is_CFO_Approved"),
        ("CEO Approved",  "is_CEO_Approved"),
        ("Subsidiary",    "subsidiary_name"),
    ],
    "expense_claim": [
        ("Date",          "createdAt"),
        ("Staff Name",    "staff_name"),
        ("Items",         "expense_claim"),
        ("Justification", "justification"),
        ("Status",        "status"),
        ("HOD Approved",  "is_HOD_Approved"),
        ("CFO Approved",  "is_CFO_Approved"),
        ("CEO Approved",  "is_CEO_Approved"),
        ("Subsidiary",    "subsidiary_name"),
    ],
    "rtps": [
        ("Date",            "date"),
        ("Supplier",        "name_of_supplier"),
        ("Amount (₦)",      "amount"),
        ("Amount Words",    "amount_in_words"),
        ("Justification",   "justification"),
        ("Payment Mode",    "mode_of_payment"),
        ("Status",          "status"),
        ("Subsidiary",      "subsidiary_name"),
    ],
}

REPORT_TITLES = {
    "leave_request": "Leave Request Report",
    "cash_advance":  "Cash Advance Report",
    "expense_claim": "Expense Claim Report",
    "rtps":          "Request to Pay Supplier (RTPS) Report",
}

SOURCE_DIRS = {
    "leave_request": "segmented_leave_request",
    "cash_advance":  "segmented_cash_advance",
    "expense_claim": "segmented_expense_claim",
    "rtps":          "segmented_rtps",
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt(value, max_chars=200):
    """Format a value for display in the table."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, dict):
        if "$date" in value:
            return value["$date"][:10]
        if "$oid" in value:
            return str(value["$oid"])[:8] + "..."
        return str(value)
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                label = item.get("label", "")
                val   = item.get("value", "")
                parts.append(f"{label}: \u20a6{val}")
            else:
                parts.append(str(item))
        result = "\n".join(parts) if parts else "—"
    else:
        result = str(value)
    if len(result) > max_chars:
        result = result[:max_chars] + "..."
    return result


def approval_color(value):
    if value is True:
        return colors.HexColor("#d4edda")
    if value is False:
        return colors.HexColor("#f8d7da")
    return colors.white


def build_report_pdf(records, columns, title, subsidiary) -> bytes:
    """Build the summary table PDF and return as bytes."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=1*cm, rightMargin=1*cm,
        topMargin=1.5*cm, bottomMargin=1.5*cm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "title", fontSize=14, fontName="Helvetica-Bold",
        alignment=TA_CENTER, spaceAfter=4
    )
    sub_style = ParagraphStyle(
        "sub", fontSize=10, fontName="Helvetica",
        alignment=TA_CENTER, spaceAfter=10,
        textColor=colors.HexColor("#555555")
    )
    cell_style = ParagraphStyle(
        "cell", fontSize=7.5, fontName="Helvetica",
        leading=10, alignment=TA_LEFT
    )

    elements = []
    elements.append(Paragraph(title, title_style))
    elements.append(Paragraph(
        f"Subsidiary: {subsidiary}  |  Total Records: {len(records)}", sub_style
    ))
    elements.append(HRFlowable(width="100%", thickness=1,
                                color=colors.HexColor("#cccccc")))
    elements.append(Spacer(1, 0.3*cm))

    headers = [col[0] for col in columns]
    keys    = [col[1] for col in columns]

    table_data = [[Paragraph(f"<b>{h}</b>", cell_style) for h in headers]]

    for rec in records:
        row = [Paragraph(fmt(rec.get(k)), cell_style) for k in keys]
        table_data.append(row)

    page_w    = landscape(A4)[0] - 2*cm
    col_w     = page_w / len(columns)
    col_widths = [col_w] * len(columns)

    table = Table(table_data, colWidths=col_widths, repeatRows=1)
    style = TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
        ("TEXTCOLOR",     (0, 0), (-1, 0), colors.white),
        ("FONTNAME",      (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0), 8),
        ("ALIGN",         (0, 0), (-1, 0), "CENTER"),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("TOPPADDING",    (0, 0), (-1, 0), 6),
        ("FONTSIZE",      (0, 1), (-1, -1), 7.5),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f2f2f2")]),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING",    (0, 1), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
        ("GRID",          (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
        ("BOX",           (0, 0), (-1, -1), 0.8, colors.HexColor("#2c3e50")),
    ])

    for row_idx, rec in enumerate(records, start=1):
        for col_idx, key in enumerate(keys):
            if key.startswith("is_"):
                bg = approval_color(rec.get(key))
                style.add("BACKGROUND",
                          (col_idx, row_idx), (col_idx, row_idx), bg)

    table.setStyle(style)
    elements.append(table)
    doc.build(elements)
    return buf.getvalue()


def generate_pdf(records, columns, title, subsidiary, output_path):
    """Build report table then append each record's attachment PDFs."""
    writer = PdfWriter()

    # 1. Add the summary table
    report_bytes = build_report_pdf(records, columns, title, subsidiary)
    writer.append(PdfReader(io.BytesIO(report_bytes)))

    # 2. Append attachment PDFs per record
    for rec in records:
        local_paths = rec.get("attachments_local") or []
        for att_path in local_paths:
            att_file = Path(att_path)
            if att_file.exists() and att_file.suffix.lower() == ".pdf":
                try:
                    writer.append(PdfReader(str(att_file)))
                except Exception as e:
                    print(f"    ⚠ Could not embed {att_file.name}: {e}")

    with open(output_path, "wb") as f:
        writer.write(f)

    print(f"  Created: {output_path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    sub_name_map = load_subsidiary_name_map()

    for report_type, source_dir in SOURCE_DIRS.items():
        source_path = Path(source_dir)
        if not source_path.exists():
            print(f"Skipping {source_dir} — folder not found")
            continue

        out_dir = Path("pdf_reports") / report_type
        out_dir.mkdir(parents=True, exist_ok=True)

        title   = REPORT_TITLES[report_type]
        columns = COLUMNS[report_type]

        print(f"\n{title}")
        print("-" * 40)

        # Track which output stems we've already written to merge duplicates
        merged: dict = {}  # output_stem -> list of records

        for json_file in sorted(source_path.glob("*.json")):
            records = []
            with open(json_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))

            if not records:
                print(f"  Skipping {json_file.name} — empty")
                continue

            # Resolve output filename
            stem = json_file.stem
            if stem.startswith("subsidiary_"):
                sub_id = stem[len("subsidiary_"):]
                resolved = sub_name_map.get(sub_id)
                output_stem = resolved if resolved else stem
            else:
                output_stem = stem

            merged.setdefault(output_stem, [])
            merged[output_stem].extend(records)

        # Write one PDF per resolved subsidiary name
        for output_stem, records in sorted(merged.items()):
            subsidiary = records[0].get("subsidiary_name", output_stem.replace("_", " ").title())
            pdf_path   = out_dir / (output_stem + ".pdf")
            print(f"  Writing {pdf_path.name} ({len(records)} records)")
            generate_pdf(records, columns, title, subsidiary, pdf_path)

        # Remove stale ObjectId-named PDFs that are now replaced
        for old_pdf in out_dir.glob("subsidiary_*.pdf"):
            old_pdf.unlink()
            print(f"  Removed stale: {old_pdf.name}")

    print("\nAll PDF reports generated in: pdf_reports/")


if __name__ == "__main__":
    run()
