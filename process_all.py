import json
from pathlib import Path

SUBSIDIARY_FILE = "subsidiary.json"

TASKS = [
    {
        "input":     "leave request.json",
        "output_dir": "segmented_leave_request",
        "id_field":  "subsidiary_id",
        "id_is_oid": False,
        "already_updated": True,   # subsidiary_id already replaced with subsidiary_name
    },
    {
        "input":     "expense claim.json",
        "output_dir": "segmented_expense_claim",
        "id_field":  "subsidiary_id",
        "id_is_oid": True,
        "already_updated": True,
    },
    {
        "input":     "cash advance.json",
        "output_dir": "segmented_cash_advance",
        "id_field":  "subsidiary_id",
        "id_is_oid": True,
        "already_updated": False,
    },
    {
        "input":     "rtps.json",
        "output_dir": "segmented_rtps",
        "id_field":  "subsidiary_id",
        "id_is_oid": True,
        "already_updated": False,
    },
]

# ── Load subsidiary mapping ──────────────────────────────────────────────────
id_to_name = {}
with open(SUBSIDIARY_FILE, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        sub = json.loads(line)
        sub_id   = sub.get("_id", {}).get("$oid")
        sub_name = sub.get("subsidiary_name")
        if sub_id and sub_name:
            id_to_name[sub_id] = sub_name

print(f"Loaded {len(id_to_name)} subsidiaries.")
name_to_id = {v: k for k, v in id_to_name.items()}
print()

# ── Process each file ────────────────────────────────────────────────────────
for task in TASKS:
    input_file  = task["input"]
    output_dir  = Path(task["output_dir"])
    id_field    = task["id_field"]
    id_is_oid   = task["id_is_oid"]

    print(f"Processing: {input_file}")

    # Clear and recreate output directory
    if output_dir.exists():
        for f in output_dir.glob("*.json"):
            f.unlink()
    output_dir.mkdir(exist_ok=True)

    records_by_sub = {}   # sub_id -> [records]
    updated = 0
    skipped = 0
    already_updated = task.get("already_updated", False)

    with open(input_file, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                print(f"  Bad JSON at line {lineno}, skipping.")
                continue

            if already_updated:
                # Already has subsidiary_name — use it to determine bucket
                sub_name = record.get("subsidiary_name")
                sub_id = name_to_id.get(sub_name) if sub_name else None
            else:
                raw = record.get(id_field)
                sub_id = raw.get("$oid") if id_is_oid and isinstance(raw, dict) else raw

                if sub_id and sub_id in id_to_name:
                    record["subsidiary_name"] = id_to_name[sub_id]
                    del record[id_field]
                    updated += 1
                else:
                    skipped += 1

            bucket = sub_id if (sub_id and sub_id in id_to_name) else "__unknown__"
            records_by_sub.setdefault(bucket, []).append(record)

    # Write updated main file (only if not locked / already updated)
    try:
        with open(input_file, "w", encoding="utf-8") as f:
            for records in records_by_sub.values():
                for record in records:
                    f.write(json.dumps(record) + "\n")
        print(f"  Main file written: {input_file}")
    except PermissionError:
        # File is open in editor — write to a .new file instead
        new_file = input_file + ".new"
        with open(new_file, "w", encoding="utf-8") as f:
            for records in records_by_sub.values():
                for record in records:
                    f.write(json.dumps(record) + "\n")
        print(f"  ⚠ File locked — updated copy saved as: {new_file}")

    # Write segmented files (skip __unknown__ bucket)
    for sub_id, records in records_by_sub.items():
        if sub_id == "__unknown__":
            continue
        out_file = output_dir / f"subsidiary_{sub_id}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record) + "\n")
        print(f"  {out_file.name}: {len(records)} records")

    print(f"  → {updated} updated, {skipped} skipped\n")

print("All done.")
