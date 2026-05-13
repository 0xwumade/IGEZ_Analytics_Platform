import json
import os
from pathlib import Path

# ==========================
INPUT_FILE = "expense claim.json"
SUBSIDIARY_FILE = "subsidiary.json"
OUTPUT_DIR = "segmented_expense_claim"
# ==========================

# Create output directory
Path(OUTPUT_DIR).mkdir(exist_ok=True)

# -------- Load Subsidiary Mapping --------
id_to_name = {}

with open(SUBSIDIARY_FILE, "r", encoding="utf-8") as f:
    for line_number, line in enumerate(f, start=1):
        line = line.strip()
        if not line:
            continue

        try:
            sub = json.loads(line)
            sub_id = sub.get("_id", {}).get("$oid")
            sub_name = sub.get("subsidiary_name")

            if sub_id and sub_name:
                id_to_name[sub_id] = sub_name

        except json.JSONDecodeError:
            print(f"Invalid JSON in subsidiary file at line {line_number}")

print(f"Loaded {len(id_to_name)} subsidiaries.")

# -------- Process and Segment File --------
records_by_subsidiary = {}
replaced_count = 0
not_found_count = 0

with open(INPUT_FILE, "r", encoding="utf-8") as infile:
    for line_number, line in enumerate(infile, start=1):
        line = line.strip()
        if not line:
            continue

        try:
            record = json.loads(line)
            subsidiary_id_obj = record.get("subsidiary_id")
            
            # Extract the $oid value
            if isinstance(subsidiary_id_obj, dict):
                subsidiary_id = subsidiary_id_obj.get("$oid")
            else:
                subsidiary_id = subsidiary_id_obj

            if subsidiary_id and subsidiary_id in id_to_name:
                subsidiary_name = id_to_name[subsidiary_id]
                record["subsidiary_name"] = subsidiary_name
                del record["subsidiary_id"]
                replaced_count += 1
                
                # Group by subsidiary
                if subsidiary_id not in records_by_subsidiary:
                    records_by_subsidiary[subsidiary_id] = []
                records_by_subsidiary[subsidiary_id].append(record)
            elif subsidiary_id:
                not_found_count += 1

        except json.JSONDecodeError:
            print(f"Invalid JSON at line {line_number}")

# -------- Write Updated Main File --------
with open(INPUT_FILE, "w", encoding="utf-8") as outfile:
    for subsidiary_id, records in records_by_subsidiary.items():
        for record in records:
            outfile.write(json.dumps(record))
            outfile.write("\n")

# -------- Write Segmented Files --------
for subsidiary_id, records in records_by_subsidiary.items():
    output_file = Path(OUTPUT_DIR) / f"subsidiary_{subsidiary_id}.json"
    with open(output_file, "w", encoding="utf-8") as outfile:
        for record in records:
            outfile.write(json.dumps(record))
            outfile.write("\n")
    print(f"  Created: {output_file.name} ({len(records)} records)")

print("\n========== SUMMARY ==========")
print(f"Records updated: {replaced_count}")
print(f"IDs not found: {not_found_count}")
print(f"Segmented files created in: {OUTPUT_DIR}")
print(f"Main file updated: {INPUT_FILE}")
