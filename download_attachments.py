"""
download_attachments.py
-----------------------
Downloads all attachments from Cloudinary to local storage and converts
everything to PDF. Output structure:

  attachments_local/
    <record_id>/
      file_1.pdf
      file_2.pdf
      ...

Also updates all segmented JSON files to replace Cloudinary URLs
with local PDF paths.
"""

import json
import os
import re
import shutil
import requests
from pathlib import Path
from dotenv import load_dotenv
from pymongo import MongoClient
from PIL import Image
from reportlab.platypus import SimpleDocTemplate, Paragraph
from reportlab.lib.styles import getSampleStyleSheet

load_dotenv()

OUTPUT_DIR   = Path("attachments_local")
SEGMENTED_DIRS = [
    "segmented_leave_request",
    "segmented_cash_advance",
    "segmented_expense_claim",
    "segmented_rtps",
]

LINK_FIELDS = ["request_form_id", "cash_advance_form_id", "expense_form_id"]

# ── Helpers ───────────────────────────────────────────────────────────────────

def safe_filename(url: str, index: int) -> str:
    """Derive a clean filename from a URL."""
    name = url.split("/")[-1].split("?")[0]
    name = re.sub(r'%[0-9A-Fa-f]{2}', '_', name)  # decode %xx
    name = re.sub(r'[^\w.\-]', '_', name)
    return f"{index:02d}_{name}" if name else f"{index:02d}_file"


def download_file(url: str, dest: Path) -> bool:
    """Download a file from URL to dest. Returns True on success."""
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        dest.write_bytes(r.content)
        return True
    except Exception as e:
        print(f"    ⚠ Download failed: {e}")
        return False


def to_pdf(src: Path, dest: Path) -> bool:
    """Convert src file to PDF at dest. Returns True on success."""
    ext = src.suffix.lower()

    # Already PDF
    if ext == ".pdf":
        shutil.copy2(src, dest)
        return True

    # Images → PDF via Pillow
    if ext in (".png", ".jpg", ".jpeg", ".avif", ".bmp", ".gif", ".tiff"):
        try:
            img = Image.open(src).convert("RGB")
            img.save(str(dest), "PDF", resolution=150)
            return True
        except Exception as e:
            print(f"    ⚠ Image→PDF failed: {e}")
            return False

    # Word/Excel → PDF via docx2pdf (requires MS Word on Windows)
    if ext in (".docx", ".doc"):
        try:
            from docx2pdf import convert
            convert(str(src), str(dest))
            return True
        except Exception as e:
            print(f"    ⚠ Word→PDF failed: {e}")

    if ext in (".xlsx", ".xls"):
        try:
            import subprocess
            # Try LibreOffice if available
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
            print(f"    ⚠ Excel→PDF failed: {e}")

    # Fallback: create a simple PDF with the filename noted
    try:
        doc = SimpleDocTemplate(str(dest))
        styles = getSampleStyleSheet()
        doc.build([Paragraph(f"Original file: {src.name}<br/>Could not convert automatically.", styles["Normal"])])
        return True
    except Exception:
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    client = MongoClient(os.getenv("DATABASE_URL"))
    db     = client["Paperless_app_prod"]

    OUTPUT_DIR.mkdir(exist_ok=True)
    tmp_dir = Path("_tmp_downloads")
    tmp_dir.mkdir(exist_ok=True)

    # Build map: record_id -> [filePath, ...]
    print("Fetching attachment records from MongoDB...")
    att_map = {}
    for doc in db["Attachment"].find({}):
        for field in LINK_FIELDS:
            ref = doc.get(field)
            if ref:
                key = str(ref)
                att_map.setdefault(key, [])
                path = doc.get("filePath")
                if path:
                    att_map[key].append(path)

    print(f"Found {len(att_map)} records with attachments ({sum(len(v) for v in att_map.values())} files total)\n")

    # Download and convert
    local_map = {}   # record_id -> [local_pdf_path_str, ...]
    total_done = 0

    for rec_id, urls in att_map.items():
        rec_dir = OUTPUT_DIR / rec_id
        rec_dir.mkdir(exist_ok=True)
        local_map[rec_id] = []

        for i, url in enumerate(urls, 1):
            raw_name  = safe_filename(url, i)
            tmp_file  = tmp_dir / raw_name
            pdf_name  = Path(raw_name).stem + ".pdf"
            pdf_file  = rec_dir / pdf_name

            # Skip if already downloaded
            if pdf_file.exists():
                local_map[rec_id].append(str(pdf_file))
                total_done += 1
                continue

            print(f"  [{total_done+1}] {rec_id[:8]}... → {pdf_name}")

            if download_file(url, tmp_file):
                if to_pdf(tmp_file, pdf_file):
                    local_map[rec_id].append(str(pdf_file))
                    total_done += 1
                tmp_file.unlink(missing_ok=True)

    # Clean up tmp
    shutil.rmtree(tmp_dir, ignore_errors=True)

    print(f"\n✓ {total_done} files downloaded and converted to PDF")
    print(f"  Saved in: {OUTPUT_DIR}/\n")

    # Update segmented JSON files with local paths
    print("Updating segmented JSON files with local paths...")
    for folder in SEGMENTED_DIRS:
        for json_file in Path(folder).glob("*.json"):
            records = []
            with open(json_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))

            for rec in records:
                rec_id = rec.get("_id", {}).get("$oid") if isinstance(rec.get("_id"), dict) else str(rec.get("_id", ""))
                local_paths = local_map.get(rec_id, [])
                rec["attachments_local"] = local_paths if local_paths else None

            with open(json_file, "w", encoding="utf-8") as f:
                for rec in records:
                    f.write(json.dumps(rec) + "\n")

    print("Done. All segmented files updated with local attachment paths.")
    client.close()


if __name__ == "__main__":
    run()
