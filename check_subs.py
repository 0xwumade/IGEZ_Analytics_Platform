import json
from pathlib import Path

id_to_name = {}
with open("subsidiary.json", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            sub = json.loads(line)
            raw_id = sub.get("_id", {})
            sub_id = raw_id.get("$oid") if isinstance(raw_id, dict) else str(raw_id)
            sub_name = sub.get("subsidiary_name", "")
            if sub_id and sub_name:
                id_to_name[sub_id] = sub_name
        except Exception:
            pass

for k, v in sorted(id_to_name.items(), key=lambda x: x[1]):
    print(f"{k}  ->  {v}")
