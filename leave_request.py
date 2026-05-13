import json
import os
from pathlib import Path

# ==========================
# CONFIG
# ==========================
MAIN_INPUT_FILE = "leave-request.json"
SUBSIDIARY_FILE = "subsidiary.json"
SEGMENTED_DIR = "segmented_leave_request"
# ==========================

# -------- Step 1: Load Subsidiary Mapping (JSONL) --------
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

# -------- Step 2: Process Main File (JSONL) --------
def process_file(input_file, output_file):
    replaced_count = 0
    not_found_count = 0
    
    with open(input_file, "r", encoding="utf-8") as infile, \
         open(output_file, "w", encoding="utf-8") as outfile:

        for line_number, line in enumerate(infile, start=1):
            line = line.strip()
            if not line:
                continue

            try:
                record = json.loads(line)
                subsidiary_id = record.get("subsidiary_id")

                if subsidiary_id and subsidiary_id in id_to_name:
                    record["subsidiary_name"] = id_to_name[subsidiary_id]
                    del record["subsidiary_id"]
                    replaced_count += 1
                elif subsidiary_id:
                    not_found_count += 1

                outfile.write(json.dumps(record))
                outfile.write("\n")

            except json.JSONDecodeError:
                print(f"Invalid JSON at line {line_number} in {input_file}")
    
    return replaced_count, not_found_count

# Process main file
print("\n========== Processing Main File ==========")
replaced, not_found = process_file(MAIN_INPUT_FILE, MAIN_INPUT_FILE + ".tmp")
os.replace(MAIN_INPUT_FILE + ".tmp", MAIN_INPUT_FILE)
print(f"Records updated: {replaced}")
print(f"IDs not found: {not_found}")

# -------- Step 3: Process Segmented Files --------
print("\n========== Processing Segmented Files ==========")
segmented_path = Path(SEGMENTED_DIR)
if segmented_path.exists() and segmented_path.is_dir():
    total_replaced = 0
    total_not_found = 0
    
    for file_path in segmented_path.glob("*.json"):
        replaced, not_found = process_file(str(file_path), str(file_path) + ".tmp")
        os.replace(str(file_path) + ".tmp", str(file_path))
        total_replaced += replaced
        total_not_found += not_found
        print(f"  {file_path.name}: {replaced} updated, {not_found} not found")
    
    print(f"\nTotal segmented records updated: {total_replaced}")
    print(f"Total segmented IDs not found: {total_not_found}")
else:
    print(f"Segmented directory not found: {SEGMENTED_DIR}")

print("\n========== COMPLETE ==========")
print("All files have been updated with subsidiary names.")