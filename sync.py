"""
sync.py
-------
Incremental sync from MongoDB to local JSONL files (audit trail).
Appends only new records since the last run, then rebuilds PDF reports.

Run:  python sync.py
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
CURSOR_FILE  = "sync_cursor.json"

# collection name → output JSONL file
COLLECTIONS = {
    "Subsidiary":           "subsidiary.json",
    "Leave_Request":        "leave request.json",
    "ExpenseClaim":         "expense claim.json",
    "CashAdvance":          "cash advance.json",
    "RequestToPaySupplier": "rtps.json",
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


def serialize(doc: dict) -> dict:
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


def append_records(filepath: str, records: list):
    Path(filepath).touch()
    with open(filepath, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


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

    for col_name, json_file in COLLECTIONS.items():
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

        records = [serialize(doc) for doc in docs]
        append_records(json_file, records)
        cursor[col_name] = str(docs[-1]["_id"])
        log.info(f"  {col_name}: +{len(docs)} new records → {json_file}")

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
