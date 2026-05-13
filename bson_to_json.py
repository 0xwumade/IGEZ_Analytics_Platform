"""
bson_to_json.py  <source_dir>  <dest_dir>
Converts all .bson files in source_dir to .json files in dest_dir.
"""
import sys
import json
from pathlib import Path
from datetime import datetime, timezone
from bson import decode_all, ObjectId

def serialize(obj):
    if isinstance(obj, ObjectId):
        return {"$oid": str(obj)}
    if isinstance(obj, datetime):
        return {"$date": obj.strftime("%Y-%m-%dT%H:%M:%S.") + f"{obj.microsecond // 1000:03d}Z"}
    if isinstance(obj, dict):
        return {k: serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [serialize(i) for i in obj]
    if isinstance(obj, bytes):
        return obj.hex()
    return obj

source = Path(sys.argv[1])
dest   = Path(sys.argv[2])
dest.mkdir(parents=True, exist_ok=True)

for bson_file in sorted(source.glob("*.bson")):
    out_file = dest / (bson_file.stem + ".json")
    with open(bson_file, "rb") as f:
        docs = decode_all(f.read())
    with open(out_file, "w", encoding="utf-8") as f:
        for doc in docs:
            f.write(json.dumps(serialize(doc)) + "\n")
    print(f"  {bson_file.name} -> {out_file.name}  ({len(docs)} records)")

print(f"\nDone. {len(list(source.glob('*.bson')))} files converted.")
