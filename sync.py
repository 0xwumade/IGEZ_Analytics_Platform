"""
sync.py
-------
Connects to MongoDB, fetches NEW records since the last run (incremental),
replaces subsidiary_id with subsidiary_name, appends to the main JSONL files,
and updates the segmented folders.

Run manually anytime:  python sync.py
"""

import json
import os
import logging
from datetime import datetime, timezone
from pathlib import Path

from pymongo import MongoClient
from dotenv import load_dotenv

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.FileHandler("sync.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
DB_NAME      = "Paperless_app_prod"

# Tracks the last-fetched timestamp per collection
CURSOR_FILE  = "sync_cursor.json"

# collection name  →  (output json file, segmented folder, id_is_oid)
COLLECTIONS = {
    "Subsidiary":          ("subsidiary.json",    None,                       False),
    "Leave_Request":       ("leave request.json", "segmented_leave_request",  False),  # plain string
    "ExpenseClaim":        ("expense claim.json",  "segmented_expense_claim", True),   # ObjectId
    "CashAdvance":         ("cash advance.json",   "segmented_cash_advance",  True),   # ObjectId
    "RequestToPaySupplier":("rtps.json",           "segmented_rtps",          True),   # ObjectId
}

# Maps each collection to the attachment link field
ATTACHMENT_LINK_FIELD = {
    "Leave_Request":        None,                  # no attachment link field
    "ExpenseClaim":         "expense_form_id",
    "CashAdvance":          "cash_advance_form_id",
    "RequestToPaySupplier": "request_form_id",
}

# ── Helpers ──────────────────────────────────────────────────────────────────

def load_cursor() -> dict:
    if Path(CURSOR_FILE).exists():
        with open(CURSOR_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cursor(cursor: dict):
    with open(CURSOR_FILE, "w", encoding="utf-8") as f:
        json.dump(cursor, f, indent=2, default=str)


def load_subsidiary_map() -> dict:
    """Build id -> name map from the local subsidiary.json."""
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
                sub_id   = sub.get("_id", {}).get("$oid") or str(sub.get("_id", ""))
                sub_name = sub.get("subsidiary_name")
                if sub_id and sub_name:
                    id_to_name[sub_id] = sub_name
            except json.JSONDecodeError:
                pass
    return id_to_name


def serialize(doc: dict) -> dict:
    """Convert MongoDB ObjectId / datetime to JSON-serialisable types."""
    from bson import ObjectId
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


def resolve_sub_id(record: dict, id_is_oid: bool) -> str | None:
    raw = record.get("subsidiary_id")
    if raw is None:
        return None
    if id_is_oid and isinstance(raw, dict):
        return raw.get("$oid")   # already serialized
    return str(raw)              # plain string or already stringified ObjectId


def append_to_main_file(filepath: str, records: list):
    with open(filepath, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def build_attachment_map(db) -> dict:
    """
    Returns a dict:  record_id (str) -> [filePath, filePath, ...]
    covering all link fields: request_form_id, cash_advance_form_id, expense_form_id
    """
    att_map = {}
    for doc in db["Attachment"].find({}):
        for field in ["request_form_id", "cash_advance_form_id", "expense_form_id"]:
            ref = doc.get(field)
            if ref:
                key = str(ref)
                att_map.setdefault(key, [])
                path = doc.get("filePath")
                if path:
                    att_map[key].append(path)
    log.info(f"  Attachment map: {len(att_map)} records with attachments")
    return att_map


def update_segmented_file(seg_dir: str, sub_id: str, records: list):
    Path(seg_dir).mkdir(exist_ok=True)
    seg_file = Path(seg_dir) / f"subsidiary_{sub_id}.json"
    with open(seg_file, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def sync_subsidiaries(col, cursor: dict):
    """Sync Subsidiary collection and return updated id->name map."""
    from bson import ObjectId
    last_oid = cursor.get("Subsidiary")
    query = {"_id": {"$gt": ObjectId(last_oid)}} if last_oid else {}

    docs = list(col.find(query).sort("_id", 1))
    if not docs:
        log.info("  Subsidiary: no new records")
        return load_subsidiary_map()

    sub_file = Path("subsidiary.json")
    with open(sub_file, "a", encoding="utf-8") as f:
        for doc in docs:
            rec = serialize(doc)
            f.write(json.dumps(rec) + "\n")

    cursor["Subsidiary"] = str(docs[-1]["_id"])
    log.info(f"  Subsidiary: +{len(docs)} new records")
    return load_subsidiary_map()


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

    # Always sync subsidiaries first so the map is fresh
    id_to_name = sync_subsidiaries(db["Subsidiary"], cursor)
    log.info(f"  Subsidiary map: {len(id_to_name)} entries")

    # Build attachment map once
    att_map = build_attachment_map(db)

    for col_name, (json_file, seg_dir, id_is_oid) in COLLECTIONS.items():
        if col_name == "Subsidiary":
            continue

        from bson import ObjectId
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

        # Group new records by subsidiary
        by_sub   = {}
        no_match = 0

        for doc in docs:
            rec    = serialize(doc)
            sub_id = resolve_sub_id(rec, id_is_oid)

            if sub_id and sub_id in id_to_name:
                rec["subsidiary_name"] = id_to_name[sub_id]
                rec.pop("subsidiary_id", None)
                by_sub.setdefault(sub_id, []).append(rec)
            else:
                no_match += 1
                by_sub.setdefault("__unknown__", []).append(rec)

            # Attach file links
            rec_id = str(doc["_id"])
            links  = att_map.get(rec_id, [])
            rec["attachments"] = links if links else None

        # Append to main file
        all_recs = [r for recs in by_sub.values() for r in recs]
        Path(json_file).touch()
        append_to_main_file(json_file, all_recs)

        # Append to segmented files
        if seg_dir:
            for sub_id, recs in by_sub.items():
                if sub_id != "__unknown__":
                    update_segmented_file(seg_dir, sub_id, recs)

        cursor[col_name] = str(docs[-1]["_id"])
        log.info(f"  {col_name}: +{len(docs)} new  |  {no_match} unmatched subsidiary")

    save_cursor(cursor)
    client.close()
    log.info("Sync complete.")
    log.info("=" * 55)

    # Step 1: Enrich all segmented files with latest attachment URLs from MongoDB
    log.info("Enriching attachment links...")
    import subprocess
    subprocess.run(["python", "enrich_attachments.py"], check=True)
    log.info("Attachment links updated.")

    # Step 2: Download any new/missing attachments and set attachments_local paths
    log.info("Downloading new attachments...")
    subprocess.run(["python", "download_attachments.py"], check=True)
    log.info("Attachments downloaded.")

    # Step 3: Rebuild PDF reports with all current attachments
    log.info("Generating PDF reports...")
    subprocess.run(["python", "generate_pdf_reports.py"], check=True)
    log.info("PDF reports updated.")


if __name__ == "__main__":
    run_sync()
