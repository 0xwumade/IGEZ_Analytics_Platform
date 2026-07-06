"""
migrate_segmented.py
--------------------
One-time migration: merges old short-named segmented JSON files
(gedu.json, property.json, etc.) into their matching subsidiary_<id>.json
counterparts, deduplicates by _id, then removes the old files.

Safe to re-run — skips if old file already gone.
"""

import json
from pathlib import Path

# Map: safe_filename_stem (from generate_pdf_reports logic) -> sub_id
# Built from subsidiary.json
def load_name_to_id_map() -> dict:
    name_to_id = {}
    with open("subsidiary.json", encoding="utf-8") as f:
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
                    import re
                    safe = re.sub(r"[^\w\s-]", "", name).strip()
                    safe = re.sub(r"\s+", "_", safe).lower()
                    name_to_id[safe] = sub_id
                    # Also map common short aliases
                    short = name.lower().split()[0]  # e.g. "gedu" from "Gedu Technologies"
                    name_to_id[short] = sub_id
                    # Extra aliases for known mismatches
                    if short == "properties":
                        name_to_id["property"] = sub_id
            except Exception as e:
                print(f"  Warning: {e}")
    return name_to_id


SEGMENTED_DIRS = [
    "segmented_leave_request",
    "segmented_cash_advance",
    "segmented_expense_claim",
    "segmented_rtps",
]


def read_jsonl(path: Path) -> dict:
    """Read JSONL file, return dict keyed by record _id string."""
    records = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                raw = rec.get("_id", {})
                rid = raw.get("$oid") if isinstance(raw, dict) else str(raw)
                records[rid] = rec
            except Exception:
                pass
    return records


def write_jsonl(path: Path, records: dict):
    with open(path, "w", encoding="utf-8") as f:
        for rec in records.values():
            f.write(json.dumps(rec) + "\n")


def migrate():
    import re
    name_to_id = load_name_to_id_map()
    print("Name→ID map:")
    for k, v in sorted(name_to_id.items()):
        print(f"  {k!r} -> {v}")
    print()

    for folder in SEGMENTED_DIRS:
        folder_path = Path(folder)
        if not folder_path.exists():
            continue

        print(f"\n--- {folder} ---")

        for old_file in sorted(folder_path.glob("*.json")):
            stem = old_file.stem
            # Skip files already in subsidiary_<id> format
            if stem.startswith("subsidiary_"):
                continue

            # Find matching sub_id
            sub_id = name_to_id.get(stem.lower())
            if not sub_id:
                print(f"  SKIP {old_file.name} — no matching subsidiary ID found")
                continue

            target_file = folder_path / f"subsidiary_{sub_id}.json"

            # Read both files
            old_records = read_jsonl(old_file)
            new_records = read_jsonl(target_file) if target_file.exists() else {}

            before_count = len(new_records)
            # Merge: new_records wins on conflict (it's more recent)
            merged = {**old_records, **new_records}
            added = len(merged) - before_count

            write_jsonl(target_file, merged)
            old_file.unlink()

            print(f"  Merged {old_file.name} ({len(old_records)} records) "
                  f"into {target_file.name} (+{added} new, {len(merged)} total) — deleted old file")

    print("\nMigration complete.")


if __name__ == "__main__":
    migrate()
