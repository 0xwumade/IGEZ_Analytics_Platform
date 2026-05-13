"""
enrich_attachments.py
---------------------
One-time script: adds 'attachments' field to all existing segmented JSON files
by looking up the Attachment collection in MongoDB.
"""
import json
from pathlib import Path
from pymongo import MongoClient
from dotenv import load_dotenv
import os

load_dotenv()
client = MongoClient(os.getenv("DATABASE_URL"))
db     = client["Paperless_app_prod"]

# Build attachment map: record_id -> [filePath, ...]
print("Building attachment map...")
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

print(f"Found {len(att_map)} records with attachments.")

# Enrich each segmented folder
DIRS = [
    "segmented_leave_request",
    "segmented_cash_advance",
    "segmented_expense_claim",
    "segmented_rtps",
]

for folder in DIRS:
    for json_file in Path(folder).glob("*.json"):
        records = []
        with open(json_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))

        for rec in records:
            rec_id = rec.get("_id", {}).get("$oid") if isinstance(rec.get("_id"), dict) else str(rec.get("_id", ""))
            links  = att_map.get(rec_id, [])
            rec["attachments"] = links if links else None

        with open(json_file, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

        print(f"  Updated: {json_file}")

print("\nDone. All files enriched with attachment links.")
