"""
sync.py
-------
Incremental sync from MongoDB to local JSONL files (audit trail).
Appends only new records since the last run, downloads any new/changed
attachments, then rebuilds PDF reports.

Attachment cursor strategy
--------------------------
The Attachment collection is queried only for docs with _id > last seen _id
(new uploads).  In addition, every known attachment doc is checked against a
SHA-256 hash of its filePath stored in the cursor; if the URL changed the file
is re-downloaded so the local copy stays in sync with Cloudinary updates.

Run:  python sync.py
"""

import hashlib
import json
import os
import re
import shutil
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path

from bson import ObjectId
from pymongo import MongoClient
from dotenv import load_dotenv

# ── Logging ──────────────────────────────────────────────────────────────────
import sys

_fmt = logging.Formatter("%(asctime)s  %(levelname)s  %(message)s")
_file_handler   = logging.FileHandler("sync.log", encoding="utf-8")
_file_handler.setFormatter(_fmt)
_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setFormatter(_fmt)
# Windows cmd/PowerShell may use cp1252; fall back gracefully on encode errors
if hasattr(_console_handler.stream, "reconfigure"):
    try:
        _console_handler.stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _console_handler])
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
load_dotenv()

DATABASE_URL    = os.getenv("DATABASE_URL")
DB_NAME         = "Paperless_app_prod"
CURSOR_FILE     = "sync_cursor.json"
ATTACHMENTS_DIR = Path("attachments_local")

# collection name → output JSONL file
COLLECTIONS = {
    "Subsidiary":           "subsidiary.json",
    "Leave_Request":        "leave request.json",
    "ExpenseClaim":         "expense claim.json",
    "CashAdvance":          "cash advance.json",
    "RequestToPaySupplier": "rtps.json",
}

# Attachment collection links records via these foreign-key fields
ATTACHMENT_REF_FIELDS = [
    "request_form_id",
    "cash_advance_form_id",
    "expense_form_id",
]

# ── Cursor helpers ────────────────────────────────────────────────────────────

def load_cursor() -> dict:
    if Path(CURSOR_FILE).exists():
        with open(CURSOR_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cursor(cursor: dict):
    with open(CURSOR_FILE, "w", encoding="utf-8") as f:
        json.dump(cursor, f, indent=2, default=str)


# ── JSONL helpers ─────────────────────────────────────────────────────────────

def serialize(doc: dict) -> dict:
    out = {}
    for k, v in doc.items():
        if isinstance(v, ObjectId):
            out[k] = {"$oid": str(v)}
        elif isinstance(v, datetime):
            out[k] = {"$date": v.strftime("%Y-%m-%dT%H:%M:%S.") +
                      f"{v.microsecond // 1000:03d}Z"}
        elif isinstance(v, dict):
            out[k] = serialize(v)
        elif isinstance(v, list):
            out[k] = [serialize(i) if isinstance(i, dict) else i for i in v]
        else:
            out[k] = v
    return out


def append_records(filepath: str, records: list):
    Path(filepath).touch()
    with open(filepath, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


# ── Attachment helpers ────────────────────────────────────────────────────────

def _url_hash(url: str) -> str:
    """Short SHA-256 fingerprint of a URL — used to detect replacements."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _safe_filename(url: str, index: int) -> str:
    name = url.split("/")[-1].split("?")[0]
    name = re.sub(r"%[0-9A-Fa-f]{2}", "_", name)
    name = re.sub(r"[^\w.\-]", "_", name)
    return f"{index:02d}_{name}" if name else f"{index:02d}_file"


def _download(url: str, dest: Path) -> bool:
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        dest.write_bytes(r.content)
        return True
    except Exception as e:
        log.warning(f"    Download failed ({url}): {e}")
        return False


def _to_pdf(src: Path, dest: Path) -> bool:
    """Convert src to a PDF at dest.  Returns True on success."""
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
            log.warning(f"    Image→PDF failed: {e}")

    if ext in (".xlsx", ".xls", ".docx", ".doc"):
        try:
            import subprocess
            subprocess.run(
                ["soffice", "--headless", "--convert-to", "pdf",
                 "--outdir", str(dest.parent), str(src)],
                capture_output=True, timeout=30,
            )
            converted = dest.parent / (src.stem + ".pdf")
            if converted.exists():
                converted.rename(dest)
                return True
        except Exception as e:
            log.warning(f"    Office→PDF failed: {e}")

    # Fallback: placeholder PDF so the record isn't silently skipped
    try:
        from reportlab.platypus import SimpleDocTemplate, Paragraph as P
        from reportlab.lib.styles import getSampleStyleSheet
        doc_obj = SimpleDocTemplate(str(dest))
        doc_obj.build([P(f"Original file: {src.name} (could not convert automatically.)",
                         getSampleStyleSheet()["Normal"])])
        return True
    except Exception:
        return False


def _download_attachment(record_id: str, url: str, index: int,
                         tmp_dir: Path) -> str | None:
    """
    Download *url*, convert to PDF, save under attachments_local/<record_id>/.
    Returns the local PDF path string on success, None on failure.
    """
    rec_dir = ATTACHMENTS_DIR / record_id
    rec_dir.mkdir(parents=True, exist_ok=True)

    raw_name  = _safe_filename(url, index)
    pdf_name  = Path(raw_name).stem + ".pdf"
    pdf_file  = rec_dir / pdf_name
    tmp_file  = tmp_dir / raw_name

    if _download(url, tmp_file):
        if _to_pdf(tmp_file, pdf_file):
            tmp_file.unlink(missing_ok=True)
            return str(pdf_file)
        tmp_file.unlink(missing_ok=True)
    return None


# ── Incremental attachment sync ───────────────────────────────────────────────

def sync_attachments(db, cursor: dict) -> dict:
    """
    Incrementally sync the Attachment collection.

    Two passes:
      1. New docs  — query _id > last_attachment_id, download all.
      2. Changed   — for every known attachment doc re-check its filePath hash;
                     if the URL changed, re-download and update the hash.

    cursor keys used:
      "Attachment"          → last _id seen (ObjectId string)
      "attachment_hashes"   → {attachment_doc_id: url_hash}
    """
    ATTACHMENTS_DIR.mkdir(exist_ok=True)
    tmp_dir = Path("_tmp_dl")
    tmp_dir.mkdir(exist_ok=True)

    last_oid      = cursor.get("Attachment")
    hash_store    = cursor.get("attachment_hashes", {})   # att_doc_id → url_hash
    new_count     = 0
    changed_count = 0
    failed_count  = 0

    # ── Pass 1: brand-new attachment documents ─────────────────────────────
    query = {"_id": {"$gt": ObjectId(last_oid)}} if last_oid else {}
    try:
        new_docs = list(db["Attachment"].find(query).sort("_id", 1))
    except Exception as e:
        log.error(f"  Attachment (new): fetch error — {e}")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return cursor

    for doc in new_docs:
        att_id  = str(doc["_id"])
        url     = doc.get("filePath", "")
        if not url:
            continue

        # Find which record this attachment belongs to
        record_id = None
        for field in ATTACHMENT_REF_FIELDS:
            ref = doc.get(field)
            if ref:
                record_id = str(ref)
                break
        if not record_id:
            continue

        # Use position index 1 for new docs; generate_pdf_reports handles ordering
        result = _download_attachment(record_id, url, 1, tmp_dir)
        if result:
            hash_store[att_id] = _url_hash(url)
            new_count += 1
            log.info(f"    [new]     {att_id[:8]}... -> {Path(result).name}")
        else:
            failed_count += 1

    if new_docs:
        cursor["Attachment"] = str(new_docs[-1]["_id"])

    # ── Pass 2: changed attachments (URL replacement on existing docs) ─────
    # Only scan docs that already have a stored hash so we can detect drift.
    # We query all docs that are NOT newer than last_oid (already handled above).
    if hash_store:
        try:
            known_ids  = [ObjectId(k) for k in hash_store]
            known_docs = list(db["Attachment"].find({"_id": {"$in": known_ids}}))
        except Exception as e:
            log.error(f"  Attachment (changed): fetch error — {e}")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            cursor["attachment_hashes"] = hash_store
            return cursor

        for doc in known_docs:
            att_id = str(doc["_id"])
            url    = doc.get("filePath", "")
            if not url:
                continue

            stored_hash  = hash_store.get(att_id)
            current_hash = _url_hash(url)
            if stored_hash == current_hash:
                continue  # unchanged — skip

            # URL has changed: find the record, re-download
            record_id = None
            for field in ATTACHMENT_REF_FIELDS:
                ref = doc.get(field)
                if ref:
                    record_id = str(ref)
                    break
            if not record_id:
                continue

            # Remove stale local file before re-downloading
            rec_dir = ATTACHMENTS_DIR / record_id
            stale = list(rec_dir.glob("*.pdf")) if rec_dir.exists() else []
            # Only remove the specific file that maps to this att_id's old name
            result = _download_attachment(record_id, url, 1, tmp_dir)
            if result:
                hash_store[att_id] = current_hash
                changed_count += 1
                log.info(f"    [updated] {att_id[:8]}... -> {Path(result).name}")
            else:
                failed_count += 1

    shutil.rmtree(tmp_dir, ignore_errors=True)
    cursor["attachment_hashes"] = hash_store

    log.info(
        f"  Attachment: +{new_count} new, {changed_count} updated, "
        f"{failed_count} failed"
    )
    return cursor


# ── Main sync ─────────────────────────────────────────────────────────────────

def run_sync():
    log.info("=" * 55)
    log.info(f"Sync started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        client = MongoClient(DATABASE_URL, serverSelectionTimeoutMS=10_000)
        db     = client[DB_NAME]
        log.info("Connected to MongoDB.")
    except Exception as e:
        log.error(f"Connection failed: {e}")
        return

    cursor = load_cursor()

    # ── Sync document collections ─────────────────────────────────────────
    for col_name, json_file in COLLECTIONS.items():
        last_oid = cursor.get(col_name)
        query = {"_id": {"$gt": ObjectId(last_oid)}} if last_oid else {}

        try:
            docs = list(db[col_name].find(query).sort("_id", 1))
        except Exception as e:
            log.error(f"  {col_name}: fetch error — {e}")
            continue

        if not docs:
            log.info(f"  {col_name}: no new records")
            continue

        records = [serialize(doc) for doc in docs]
        append_records(json_file, records)
        cursor[col_name] = str(docs[-1]["_id"])
        log.info(f"  {col_name}: +{len(docs)} new records → {json_file}")

    # ── Incremental attachment sync ───────────────────────────────────────
    log.info("Syncing attachments...")
    cursor = sync_attachments(db, cursor)

    save_cursor(cursor)
    client.close()
    log.info("Sync complete.")
    log.info("=" * 55)

    # Rebuild PDF reports directly from MongoDB
    log.info("Generating PDF reports...")
    import subprocess
    subprocess.run(["python", "generate_pdf_reports.py"], check=True)
    log.info("PDF reports updated.")


if __name__ == "__main__":
    run_sync()
