# IGEZ — CBC Paperless App Backup and Analytics Platform

This repository supports the CBC Paperless App with both offline backup/reporting and live analytics capabilities.

It includes:
- A Flask-based dashboard with executive PDF export
- Incremental MongoDB data sync and audit trail
- Attachment download and PDF conversion
- Subsidiary-level consolidated reports and analytics

---
## What This Repo Does

The platform covers four CBC request modules:

- **Leave Requests**
- **Cash Advances**
- **Expense Claims**
- **Request to Pay Supplier (RTPS)**

Current functionality:

- Reads data from MongoDB Atlas
- Resolves subsidiary names for reports and file naming
- Builds an executive dashboard and a downloadable PDF export
- Generates consolidated subsidiary PDF reports with supporting attachments
- Supports incremental sync via `sync.py`

---
## Current Platform Capabilities

### Web analytics dashboard

The Flask app in `analytics_platform.py` provides:

- A dashboard view with summary KPIs and request aging
- Browser-based filtering by period and subsidiary
- An executive PDF export endpoint at `/executive-report`
- PDF filenames that include the subsidiary name
- A custom readable date range format in exported PDFs
- `NGN` fallback text for PDF currency rendering

### Backup and report generation

- `generate_pdf_reports.py` generates PDF reports per subsidiary/module
- `sync.py` performs incremental sync to local JSONL files, then runs report generation
- Attachments are downloaded to `attachments_local/`
- Final output PDFs are saved under `pdf_reports/`

---
## Setup

### Prerequisites

- Python 3.11+
- MongoDB Atlas connection string
- Optional: LibreOffice (`soffice`) for converting Office attachments to PDF

### Install dependencies

```bash
pip install pymongo python-dotenv requests Pillow reportlab pypdf
```

### Configure environment

Create a `.env` file at the project root:

```env
DATABASE_URL=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/
APP_TIMEZONE=Africa/Lagos
```

For hosted deployments, `DATABASE_URL` must also be configured in the environment.

---
## Usage

### Run the web dashboard

```bash
python analytics_platform.py
```

Open the app at `http://127.0.0.1:8050` and use the dashboard controls to view analytics or download the executive PDF.

### Generate backup PDF reports

```bash
python generate_pdf_reports.py
```

This builds consolidated subsidiary PDFs directly from MongoDB.

### Incremental sync + PDF rebuild

```bash
python sync.py
```

Syncs new records locally as JSONL and then rebuilds the PDF reports.

### MongoDB backup

```powershell
.\backup_mongo.ps1
```

---
## Output Files

| Location | Description |
|---|---|
| `attachments_local/<record_id>/` | Downloaded attachment files converted to PDF |
| `pdf_reports/<module>/<subsidiary>.pdf` | Consolidated reports by module and subsidiary |
| `sync_cursor.json` | Tracks the last synced record position across collections |
| `segmented_<module>/` | Optional JSONL files split by subsidiary for incremental processing |
| `sync.log` | Sync execution log |

---
## Report and Dashboard Notes

- Executive PDF dates are rendered as `day month year` for readability.
- Currency values in PDF exports use `NGN` to avoid font rendering issues with `₦`.
- Oldest pending request metrics are calculated only for records still in pending status.
- Approved request totals are shown separately from pending aging data.
- The `Testing` subsidiary is excluded from all reports.
- Attachments already present locally are not re-downloaded.
- If office document conversion fails, a placeholder PDF page is generated.
