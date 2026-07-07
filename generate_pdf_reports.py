"""
generate_pdf_reports.py
-----------------------
Queries MongoDB directly for all records, downloads any missing attachment
files, then builds one consolidated PDF per subsidiary per module.

No local JSONL or segmented files required.

Output:
  pdf_reports/
    leave_request/   emea.pdf, gedu_technologies.pdf ...
    cash_advance/    ...
    expense_claim/   ...
    rtps/            ...

Run:
  python generate_pdf_reports.py
"""

import io
import os
import re
import shutil
import requests
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from pymongo import MongoClient
from bson import ObjectId
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from pypdf import PdfWriter, PdfReader

load_dotenv()

DATABASE_URL   = os.getenv("DATABASE_URL")
APP_TIMEZONE   = os.getenv("APP_TIMEZONE", "Africa/Lagos")
LOCAL_TZ       = ZoneInfo(APP_TIMEZONE)
ATTACHMENTS_DIR = Path("attachments_local")
OUTPUT_DIR      = Path("pdf_reports")

# Subsidiaries to exclude (test/placeholder entries)
EXCLUDED_SUBSIDIARIES = {"testing", "test"}


# ── MongoDB helpers ───────────────────────────────────────────────────────────

def get_db():
    client = MongoClient(DATABASE_URL, serverSelectionTimeoutMS=10_000)
    return client["Paperless_app_prod"]


def load_subsidiary_map(db) -> tuple[dict, dict]:
    """Return (id→name, id→safe_filename) excluding test subsidiaries."""
    id_to_name = {}
    id_to_safe = {}
    for sub in db["Subsidiary"].find({}):
        name = sub.get("subsidiary_name", "").strip()
        if not name or name.lower() in EXCLUDED_SUBSIDIARIES:
            continue
        sub_id = str(sub["_id"])
        safe   = re.sub(r"[^\w\s-]", "", name).strip()
        safe   = re.sub(r"\s+", "_", safe).lower()
        id_to_name[sub_id] = name
        id_to_safe[sub_id] = safe
    return id_to_name, id_to_safe


def build_attachment_map(db) -> dict:
    """Return {record_id: [filePath, ...]} from Attachment collection."""
    att_map = defaultdict(list)
    for doc in db["Attachment"].find({}):
        for field in ["request_form_id", "cash_advance_form_id", "expense_form_id"]:
            ref = doc.get(field)
            if ref:
                path = doc.get("filePath")
                if path:
                    att_map[str(ref)].append(path)
    return dict(att_map)


# ── Attachment download ───────────────────────────────────────────────────────

def safe_filename(url: str, index: int) -> str:
    name = url.split("/")[-1].split("?")[0]
    name = re.sub(r"%[0-9A-Fa-f]{2}", "_", name)
    name = re.sub(r"[^\w.\-]", "_", name)
    return f"{index:02d}_{name}" if name else f"{index:02d}_file"


def download_attachment(url: str, dest: Path) -> bool:
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        dest.write_bytes(r.content)
        return True
    except Exception as e:
        print(f"    ⚠ Download failed: {e}")
        return False


def to_pdf(src: Path, dest: Path) -> bool:
    from PIL import Image as PILImage
    ext = src.suffix.lower()
    if ext == ".pdf":
        shutil.copy2(src, dest)
        return True
    if ext in (".png", ".jpg", ".jpeg", ".avif", ".bmp", ".gif", ".tiff"):
        try:
            img = PILImage.open(src).convert("RGB")
            img.save(str(dest), "PDF", resolution=150)
            return True
        except Exception as e:
            print(f"    ⚠ Image→PDF failed: {e}")
    if ext in (".xlsx", ".xls", ".docx", ".doc"):
        try:
            import subprocess
            result = subprocess.run(
                ["soffice", "--headless", "--convert-to", "pdf",
                 "--outdir", str(dest.parent), str(src)],
                capture_output=True, timeout=30
            )
            converted = dest.parent / (src.stem + ".pdf")
            if converted.exists():
                converted.rename(dest)
                return True
        except Exception as e:
            print(f"    ⚠ Office→PDF failed: {e}")
    # Fallback placeholder
    try:
        from reportlab.platypus import SimpleDocTemplate, Paragraph as P
        from reportlab.lib.styles import getSampleStyleSheet
        doc = SimpleDocTemplate(str(dest))
        doc.build([P(f"Original file: {src.name} (could not convert automatically.)",
                     getSampleStyleSheet()["Normal"])])
        return True
    except Exception:
        return False


def resolve_local_pdfs(record_id: str, cloudinary_urls: list) -> list:
    """Download missing attachments and return list of local PDF paths."""
    if not cloudinary_urls:
        return []
    rec_dir = ATTACHMENTS_DIR / record_id
    rec_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path("_tmp_dl")
    tmp_dir.mkdir(exist_ok=True)
    local_paths = []
    for i, url in enumerate(cloudinary_urls, 1):
        raw_name = safe_filename(url, i)
        pdf_name = Path(raw_name).stem + ".pdf"
        pdf_file = rec_dir / pdf_name
        if pdf_file.exists():
            local_paths.append(str(pdf_file))
            continue
        tmp_file = tmp_dir / raw_name
        if download_attachment(url, tmp_file):
            if to_pdf(tmp_file, pdf_file):
                local_paths.append(str(pdf_file))
            tmp_file.unlink(missing_ok=True)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return local_paths


# ── Record formatting ─────────────────────────────────────────────────────────

def fmt_value(value, max_chars=200) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, ObjectId):
        return str(value)[:8] + "..."
    if isinstance(value, datetime):
        try:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
        except Exception:
            return str(value)[:10]
    if isinstance(value, dict):
        if "$date" in value:
            return str(value["$date"])[:10]
        if "$oid" in value:
            return str(value["$oid"])[:8] + "..."
        return str(value)
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                label = item.get("label", "")
                val   = item.get("value", "")
                parts.append(f"{label}: ₦{val}" if label else f"₦{val}")
            else:
                parts.append(str(item))
        result = "\n".join(parts) if parts else "—"
    else:
        result = str(value)
    return result if len(result) <= max_chars else result[:max_chars - 1].rstrip() + "..."


def approval_color(value):
    if value is True:
        return colors.HexColor("#d4edda")
    if value is False:
        return colors.HexColor("#f8d7da")
    return colors.white


# ── Column definitions ────────────────────────────────────────────────────────

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
        ("Subsidiary",        "_subsidiary_name"),
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
        ("Subsidiary",    "_subsidiary_name"),
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
        ("Subsidiary",    "_subsidiary_name"),
    ],
    "rtps": [
        ("Date",          "date"),
        ("Supplier",      "name_of_supplier"),
        ("Amount (₦)",    "amount"),
        ("Amount Words",  "amount_in_words"),
        ("Justification", "justification"),
        ("Payment Mode",  "mode_of_payment"),
        ("Status",        "status"),
        ("Subsidiary",    "_subsidiary_name"),
    ],
}

COLLECTIONS = {
    "leave_request": "Leave_Request",
    "cash_advance":  "CashAdvance",
    "expense_claim": "ExpenseClaim",
    "rtps":          "RequestToPaySupplier",
}

REPORT_TITLES = {
    "leave_request": "Leave Request Report",
    "cash_advance":  "Cash Advance Report",
    "expense_claim": "Expense Claim Report",
    "rtps":          "Request to Pay Supplier (RTPS) Report",
}


# ── PDF builders ──────────────────────────────────────────────────────────────

def build_summary_pdf(records, columns, title, subsidiary) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                            leftMargin=1*cm, rightMargin=1*cm,
                            topMargin=1.5*cm, bottomMargin=1.5*cm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("t", fontSize=14, fontName="Helvetica-Bold",
                                  alignment=TA_CENTER, spaceAfter=4)
    sub_style   = ParagraphStyle("s", fontSize=10, fontName="Helvetica",
                                  alignment=TA_CENTER, spaceAfter=10,
                                  textColor=colors.HexColor("#555555"))
    cell_style  = ParagraphStyle("c", fontSize=7.5, fontName="Helvetica",
                                  leading=10, alignment=TA_LEFT)
    elements = [
        Paragraph(title, title_style),
        Paragraph(f"Subsidiary: {subsidiary}  |  Total Records: {len(records)}", sub_style),
        HRFlowable(width="100%", thickness=1, color=colors.HexColor("#cccccc")),
        Spacer(1, 0.3*cm),
    ]
    headers = [col[0] for col in columns]
    keys    = [col[1] for col in columns]
    table_data = [[Paragraph(f"<b>{h}</b>", cell_style) for h in headers]]
    for rec in records:
        row = [Paragraph(fmt_value(rec.get(k)), cell_style) for k in keys]
        table_data.append(row)
    col_w = (landscape(A4)[0] - 2*cm) / len(columns)
    tbl = Table(table_data, colWidths=[col_w]*len(columns), repeatRows=1)
    ts = TableStyle([
        ("BACKGROUND",    (0,0),(-1,0), colors.HexColor("#2c3e50")),
        ("TEXTCOLOR",     (0,0),(-1,0), colors.white),
        ("FONTNAME",      (0,0),(-1,0), "Helvetica-Bold"),
        ("FONTSIZE",      (0,0),(-1,0), 8),
        ("ALIGN",         (0,0),(-1,0), "CENTER"),
        ("BOTTOMPADDING", (0,0),(-1,0), 6),
        ("TOPPADDING",    (0,0),(-1,0), 6),
        ("FONTSIZE",      (0,1),(-1,-1), 7.5),
        ("ROWBACKGROUNDS",(0,1),(-1,-1), [colors.white, colors.HexColor("#f2f2f2")]),
        ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,1),(-1,-1), 4),
        ("BOTTOMPADDING", (0,1),(-1,-1), 4),
        ("LEFTPADDING",   (0,0),(-1,-1), 4),
        ("RIGHTPADDING",  (0,0),(-1,-1), 4),
        ("GRID",          (0,0),(-1,-1), 0.4, colors.HexColor("#cccccc")),
        ("BOX",           (0,0),(-1,-1), 0.8, colors.HexColor("#2c3e50")),
    ])
    for row_idx, rec in enumerate(records, start=1):
        for col_idx, key in enumerate(keys):
            if key.startswith("is_"):
                ts.add("BACKGROUND", (col_idx,row_idx), (col_idx,row_idx),
                       approval_color(rec.get(key)))
    tbl.setStyle(ts)
    elements.append(tbl)
    doc.build(elements)
    return buf.getvalue()


def write_pdf(records, columns, title, subsidiary, output_path):
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(build_summary_pdf(records, columns, title, subsidiary))))
    for rec in records:
        for att_path in (rec.get("_local_pdfs") or []):
            att_file = Path(att_path)
            if att_file.exists():
                try:
                    writer.append(PdfReader(str(att_file)))
                except Exception as e:
                    print(f"    ⚠ Could not embed {att_file.name}: {e}")
    with open(output_path, "wb") as f:
        writer.write(f)


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    print("Connecting to MongoDB...")
    db = get_db()
    id_to_name, id_to_safe = load_subsidiary_map(db)
    print(f"  {len(id_to_name)} subsidiaries loaded.")

    print("Building attachment map...")
    att_map = build_attachment_map(db)
    print(f"  {len(att_map)} records with attachments.")

    ATTACHMENTS_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)

    for report_type, collection_name in COLLECTIONS.items():
        title   = REPORT_TITLES[report_type]
        columns = COLUMNS[report_type]
        out_dir = OUTPUT_DIR / report_type
        out_dir.mkdir(parents=True, exist_ok=True)

        print(f"\n{title}")
        print("-" * 40)

        # Group records by subsidiary directly from MongoDB
        by_sub = defaultdict(list)
        for doc in db[collection_name].find({}):
            sub_id = str(doc.get("subsidiary_id", ""))
            if sub_id not in id_to_name:
                continue  # skip unknown or excluded subsidiaries
            rec = dict(doc)
            rec["_subsidiary_name"] = id_to_name[sub_id]
            # Resolve attachments
            rec_id = str(doc["_id"])
            urls   = att_map.get(rec_id, [])
            rec["_local_pdfs"] = resolve_local_pdfs(rec_id, urls) if urls else []
            by_sub[sub_id].append(rec)

        if not by_sub:
            print("  No records found.")
            continue

        # Write one PDF per subsidiary
        for sub_id, records in sorted(by_sub.items(), key=lambda x: id_to_name[x[0]]):
            safe_name  = id_to_safe[sub_id]
            subsidiary = id_to_name[sub_id]
            pdf_path   = out_dir / f"{safe_name}.pdf"
            print(f"  {safe_name}.pdf  ({len(records)} records)", end="", flush=True)
            write_pdf(records, columns, title, subsidiary, pdf_path)
            print(f"  ✓")

        # Remove any stale ObjectId-named PDFs
        for stale in out_dir.glob("subsidiary_*.pdf"):
            stale.unlink()

    print(f"\nAll PDF reports saved to: {OUTPUT_DIR}/")


if __name__ == "__main__":
    run()
