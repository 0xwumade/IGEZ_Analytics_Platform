# IGEZ — CBC Paperless App Backup System

A record-keeping and backup pipeline for the CBC Paperless application. It connects to MongoDB, syncs all request records, downloads supporting documents, and generates structured PDF reports per subsidiary — all in one run.

---

## What This Repo Does

This system handles four core modules from the CBC Paperless platform:

- **Leave Requests**
- **Cash Advances**
- **Expense Claims**
- **Request to Pay Supplier (RTPS)**

For each module, it:
1. Pulls new records from MongoDB (incremental — only fetches what's new since the last run)
2. Resolves subsidiary names from their IDs
3. Downloads all supporting attachment files from Cloudinary and converts them to PDF
4. Generates a consolidated PDF report per subsidiary, with the summary table followed by all attached documents embedded inline

---

## How It Works — End to End

```
MongoDB Atlas
     │
     │  generate_pdf_reports.py
     │  (queries all collections directly)
     ▼
Downloads attachments → attachments_local/<record_id>/<file>.pdf
     │
     │  builds PDF per subsidiary
     ▼
pdf_reports/
  ├── cash_advance/
  │     ├── emea.pdf
  │     ├── gedu_technologies.pdf
  │     ├── properties.pdf
  │     └── ...
  ├── expense_claim/
  ├── leave_request/
  └── rtps/
```

`sync.py` handles incremental record syncing to local JSONL files (used as a lightweight cache and audit trail), then calls `generate_pdf_reports.py` automatically.

---

## Setup

### 1. Prerequisites

- Python 3.11+
- MongoDB connection string (from CBC Atlas account)

### 2. Install dependencies

```bash
pip install pymongo python-dotenv requests Pillow reportlab pypdf
```

### 3. Configure environment

Create a `.env` file in the project root:

```
DATABASE_URL=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/
APP_TIMEZONE=Africa/Lagos
```

---

## Running the Pipeline

### Full run (recommended)

```bash
python generate_pdf_reports.py
```

This connects to MongoDB directly, downloads any missing attachments, and rebuilds all PDF reports. No intermediate files needed.

### Incremental sync + PDF rebuild

```bash
python sync.py
```

Syncs new records to local JSONL files (audit trail), then automatically runs `generate_pdf_reports.py`.

### MongoDB backup

```powershell
.\backup_mongo.ps1
```

---

## Output Files

| Location | Description |
|---|---|
| `segmented_<module>/` | JSONL records split by subsidiary — source data for PDFs |
| `attachments_local/<id>/` | Downloaded attachment PDFs per request record |
| `pdf_reports/<module>/<subsidiary>.pdf` | Final consolidated PDF reports |
| `sync_cursor.json` | Tracks last-synced record ID per collection |
| `sync.log` | Log of every sync run |

---

## PDF Report Structure

Each PDF contains:
1. A summary table of all records for that subsidiary and module (date, staff, amount, status, approval stages)
2. The actual attachment documents for each record embedded page-by-page after its table row

---

## Notes

- The sync is **incremental** — it only fetches records newer than the last run. Re-running is safe and fast.
- Attachment download is **skipped for already-downloaded files** — only new files are fetched.
- Excel attachments (`.xlsx`) require LibreOffice installed to convert to PDF. Without it, a placeholder page is inserted.
- The `Testing` subsidiary is automatically excluded from all data and reports.
