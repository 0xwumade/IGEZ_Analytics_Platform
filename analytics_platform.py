"""
analytics_platform.py
---------------------
Analytics dashboard for CBC Paperless App.
Run:  python analytics_platform.py
Then open:  http://127.0.0.1:5000
"""

import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import ConfigurationError, PyMongoError, ServerSelectionTimeoutError
from flask import Flask, render_template_string, request, send_file, redirect
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
import json

load_dotenv()

app = Flask(__name__, static_folder="static", static_url_path="/static")
DB_URL = os.getenv("DATABASE_URL")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Africa/Lagos")
LOCAL_TZ = ZoneInfo(APP_TIMEZONE)
UTC_TZ = ZoneInfo("UTC")


@app.after_request
def add_no_cache_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

# DB helpers and data processing functions for the dashboard analytics

def get_db():
    if not DB_URL:
        raise RuntimeError("DATABASE_URL is not configured in the deployment environment.")
    client = MongoClient(
        DB_URL,
        serverSelectionTimeoutMS=8000,
        connectTimeoutMS=8000,
    )
    client.admin.command("ping")
    return client["Paperless_app_prod"]


# Names that are test/placeholder subsidiaries — excluded everywhere
_EXCLUDED_SUBSIDIARIES = {"testing", "test"}


def get_subsidiary_map(db):
    return {
        str(s["_id"]): s.get("subsidiary_name", "Unknown")
        for s in db["Subsidiary"].find({})
        if s.get("subsidiary_name", "").strip().lower() not in _EXCLUDED_SUBSIDIARIES
    }


def normalize_cbc_text(value):
    return re.sub(r"\bCbc\b", "CBC", str(value or ""))


def resolve_sub(sub_id, sub_map):
    name = normalize_cbc_text(sub_map.get(str(sub_id), "Unknown"))
    
    # Treat excluded/test subsidiaries as Unknown so their records are filtered out
    if name.strip().lower() in _EXCLUDED_SUBSIDIARIES:
        return "Unknown"
    return name


def safe_amount(val):
    try:
        if val is None:
            return 0.0
        cleaned = re.sub(r"[^0-9.\-]", "", str(val).replace(",", "").strip())
        return float(cleaned) if cleaned else 0.0
    except Exception:
        return 0.0


def status_group(record):
    status = str(record.get("status", "")).strip().lower()
    if status == "approved":
        return "approved"
    if status == "pending":
        return "pending"
    return "unclassified"


def month_label(dt):
    local_dt = to_local_time(dt)
    if local_dt:
        return local_dt.strftime("%Y-%m")
    return "Unknown"


def app_now():
    return datetime.now(LOCAL_TZ).replace(tzinfo=None)


def to_local_time(dt):
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC_TZ)
    return dt.astimezone(LOCAL_TZ).replace(tzinfo=None)


def parse_date_arg(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d") if value else None
    except ValueError:
        return None


def period_bounds(period):
    today = app_now()
    start_today = datetime(today.year, today.month, today.day)
    if period == "today":
        return start_today, start_today + timedelta(days=1)
    if period == "week":
        return start_today - timedelta(days=start_today.weekday()), start_today + timedelta(days=1)
    if period == "month":
        return datetime(today.year, today.month, 1), start_today + timedelta(days=1)
    if period == "quarter":
        quarter_month = ((today.month - 1) // 3) * 3 + 1
        return datetime(today.year, quarter_month, 1), start_today + timedelta(days=1)
    if period == "year":
        return datetime(today.year, 1, 1), start_today + timedelta(days=1)
    return None, None


def record_date(record):
    dt = record.get("createdAt")
    return to_local_time(dt)


def previous_month_bounds():
    today = app_now()
    this_month = datetime(today.year, today.month, 1)
    last_day_previous = this_month - timedelta(days=1)
    previous_start = datetime(last_day_previous.year, last_day_previous.month, 1)
    return previous_start, this_month


def amount_change(current, previous):
    if previous:
        return round((current - previous) / previous * 100, 1)
    return 100.0 if current else 0.0


def format_change(value):
    direction = "up" if value >= 0 else "down"
    return f"{direction} {abs(value):,.1f}%"


def format_money(value):
    return f"NGN {value:,.0f}" if value else ""

# Data loaders and analytics computations for the dashboard

def load_all(db, sub_map, filters=None):
    data = {}
    filters = filters or {}
    start_date = filters.get("start_date")
    end_date = filters.get("end_date")
    selected_sub = filters.get("subsidiary", "All")

    # Defined early — used across all modules below
    def requester(record, *fields):
        for field in fields:
            value = str(record.get(field, "") or "").strip()
            if value:
                return normalize_cbc_text(value)
        return "Unknown"

    # Build user lookup map: ObjectId string → name  (for leave requests)
    _user_map = {}
    try:
        from bson import ObjectId as _ObjId
        for u in db["UserNotRegistered"].find({}, {"_id": 1, "name": 1, "first_name": 1, "last_name": 1, "fullName": 1}):
            uid = str(u["_id"])
            name = (str(u.get("name") or "").strip()
                    or f"{u.get('first_name','')} {u.get('last_name','')}".strip()
                    or str(u.get("fullName") or "").strip())
            if name:
                _user_map[uid] = normalize_cbc_text(name)
        # Also try the main User collection
        for u in db["User"].find({}, {"_id": 1, "name": 1, "first_name": 1, "last_name": 1, "fullName": 1}):
            uid = str(u["_id"])
            if uid not in _user_map:
                name = (str(u.get("name") or "").strip()
                        or f"{u.get('first_name','')} {u.get('last_name','')}".strip()
                        or str(u.get("fullName") or "").strip())
                if name:
                    _user_map[uid] = normalize_cbc_text(name)
    except Exception:
        pass

    def leave_name(record):
        """Resolve leave applicant name via unRegisterUser_id → UserNotRegistered collection."""
        uid = str(record.get("unRegisterUser_id") or record.get("user_id") or "").strip()
        if uid and uid in _user_map:
            return _user_map[uid]
        # Fallback: try direct name fields on the record
        return requester(record, "name", "staff_name", "employee_name",
                         "applicant_name", "full_name", "staffName", "employeeName")

    def subsidiary_matches(record):
        if selected_sub == "All":
            return True
        return resolve_sub(record.get("subsidiary_id", ""), sub_map) == selected_sub

    def date_matches(record):
        dt = record_date(record)
        if start_date and (not dt or dt < start_date):
            return False
        if end_date and (not dt or dt >= end_date):
            return False
        return True

    def apply_filters(records, include_dates=True):
        return [
            r for r in records
            if subsidiary_matches(r) and (not include_dates or date_matches(r))
        ]

    def event_date_matches(dt):
        if start_date and (not dt or dt < start_date):
            return False
        if end_date and (not dt or dt >= end_date):
            return False
        return True

    def count_between(records, start, end):
        return sum(
            1 for r in records
            if subsidiary_matches(r) and record_date(r) and start <= record_date(r) < end
        )

    def spend_between(records, amount_fn, start, end):
        return sum(
            amount_fn(r) for r in records
            if subsidiary_matches(r) and record_date(r) and start <= record_date(r) < end
        )

    # Leave Requests  (non-financial — tracked by headcount and days, never ₦)
    all_leaves = list(db["Leave_Request"].find({}))
    leaves = apply_filters(all_leaves)
    data["leave_total"]   = len(leaves)
    data["leave_approved"]= sum(1 for r in leaves if r.get("status") == "Approved")
    data["leave_pending"] = sum(1 for r in leaves if r.get("status") == "Pending")
    data["leave_rejected"]= sum(1 for r in leaves if str(r.get("status","")).strip().lower() == "rejected")

    # Day-count metrics — use integer fields from the document
    def _int(val):
        try: return int(val)
        except (TypeError, ValueError): return 0

    data["leave_days_applied"]  = sum(_int(r.get("no_days_taken", 0)) for r in leaves)
    data["leave_days_approved"] = sum(_int(r.get("no_days_taken", 0)) for r in leaves if r.get("status") == "Approved")
    data["leave_days_pending"]  = sum(_int(r.get("no_days_taken", 0)) for r in leaves if r.get("status") == "Pending")

    leave_by_sub   = defaultdict(int)   # request count per subsidiary
    leave_by_type  = defaultdict(int)   # request count per leave type
    leave_by_month = defaultdict(int)   # request count per month
    leave_days_by_type = defaultdict(int)  # total days taken per leave type
    for r in leaves:
        sub  = resolve_sub(r.get("subsidiary_id", ""), sub_map)
        ltyp = r.get("leave_Details", "Unknown")
        days = _int(r.get("no_days_taken", 0))
        leave_by_sub[sub]   += 1
        leave_by_type[ltyp] += 1
        leave_by_month[month_label(r.get("createdAt"))] += 1
        leave_days_by_type[ltyp] += days

    data["leave_by_sub"]        = dict(sorted(leave_by_sub.items()))
    data["leave_by_type"]       = dict(sorted(leave_by_type.items()))
    data["leave_by_month"]      = dict(sorted(leave_by_month.items()))
    data["leave_days_by_type"]  = dict(sorted(leave_days_by_type.items()))

    # Cash Advance
    all_advances = list(db["CashAdvance"].find({}))
    advances = apply_filters(all_advances)
    data["ca_total"]    = len(advances)
    data["ca_approved"] = sum(1 for r in advances if status_group(r) == "approved")
    data["ca_pending"]  = sum(1 for r in advances if status_group(r) == "pending")
    data["ca_unclassified"] = sum(1 for r in advances if status_group(r) == "unclassified")
    data["ca_amount"]   = sum(safe_amount(r.get("amount", 0)) for r in advances)
    data["ca_approved_amount"] = sum(
        safe_amount(r.get("amount", 0)) for r in advances if status_group(r) == "approved"
    )
    data["ca_pending_amount"] = sum(
        safe_amount(r.get("amount", 0)) for r in advances if status_group(r) == "pending"
    )
    data["ca_unclassified_amount"] = sum(
        safe_amount(r.get("amount", 0)) for r in advances if status_group(r) == "unclassified"
    )

    ca_by_sub   = defaultdict(float)
    ca_by_month = defaultdict(float)
    for r in advances:
        sub = resolve_sub(r.get("subsidiary_id",""), sub_map)
        amt = safe_amount(r.get("amount", 0))
        ca_by_sub[sub]   += amt
        ca_by_month[month_label(r.get("createdAt"))] += amt

    data["ca_by_sub"]   = dict(sorted(ca_by_sub.items()))
    data["ca_by_month"] = dict(sorted(ca_by_month.items()))

    # Expense Claims
    all_expenses = list(db["ExpenseClaim"].find({}))
    expenses = apply_filters(all_expenses)
    data["ec_total"]    = len(expenses)
    data["ec_approved"] = sum(1 for r in expenses if status_group(r) == "approved")
    data["ec_pending"]  = sum(1 for r in expenses if status_group(r) == "pending")
    data["ec_unclassified"] = sum(1 for r in expenses if status_group(r) == "unclassified")

    def ec_total_amount(r):
        items = r.get("expense_claim", [])
        if isinstance(items, list):
            return sum(safe_amount(i.get("value", 0)) for i in items if isinstance(i, dict))
        return 0.0

    data["ec_amount"] = sum(ec_total_amount(r) for r in expenses)
    data["ec_approved_amount"] = sum(
        ec_total_amount(r) for r in expenses if status_group(r) == "approved"
    )
    data["ec_pending_amount"] = sum(
        ec_total_amount(r) for r in expenses if status_group(r) == "pending"
    )
    data["ec_unclassified_amount"] = sum(
        ec_total_amount(r) for r in expenses if status_group(r) == "unclassified"
    )

    ec_by_sub   = defaultdict(float)
    ec_by_month = defaultdict(float)
    for r in expenses:
        sub = resolve_sub(r.get("subsidiary_id",""), sub_map)
        amt = ec_total_amount(r)
        ec_by_sub[sub]   += amt
        ec_by_month[month_label(r.get("createdAt"))] += amt

    data["ec_by_sub"]   = dict(sorted(ec_by_sub.items()))
    data["ec_by_month"] = dict(sorted(ec_by_month.items()))

    # Request to Pay Supplier RTPS
    all_rtps = list(db["RequestToPaySupplier"].find({}))
    rtps = apply_filters(all_rtps)
    data["rtps_total"]    = len(rtps)
    data["rtps_approved"] = sum(1 for r in rtps if status_group(r) == "approved")
    data["rtps_pending"]  = sum(1 for r in rtps if status_group(r) == "pending")
    data["rtps_unclassified"] = sum(1 for r in rtps if status_group(r) == "unclassified")
    data["rtps_amount"]   = sum(safe_amount(r.get("amount", 0)) for r in rtps)
    data["rtps_approved_amount"] = sum(
        safe_amount(r.get("amount", 0)) for r in rtps if status_group(r) == "approved"
    )
    data["rtps_pending_amount"] = sum(
        safe_amount(r.get("amount", 0)) for r in rtps if status_group(r) == "pending"
    )
    data["rtps_unclassified_amount"] = sum(
        safe_amount(r.get("amount", 0)) for r in rtps if status_group(r) == "unclassified"
    )

    rtps_by_sub      = defaultdict(float)
    rtps_by_supplier = defaultdict(float)
    rtps_by_month    = defaultdict(float)
    rtps_by_mode     = defaultdict(int)
    for r in rtps:
        sub = resolve_sub(r.get("subsidiary_id",""), sub_map)
        amt = safe_amount(r.get("amount", 0))
        rtps_by_sub[sub]   += amt
        rtps_by_month[month_label(r.get("createdAt"))] += amt
        rtps_by_supplier[normalize_cbc_text(r.get("name_of_supplier", "Unknown"))] += amt
        rtps_by_mode[r.get("mode_of_payment","Unknown")] += 1

    sorted_suppliers = sorted(rtps_by_supplier.items(), key=lambda x: x[1], reverse=True)
    # Top 10 suppliers
    top_suppliers = dict(sorted_suppliers[:10])
    data["rtps_by_sub"]      = dict(sorted(rtps_by_sub.items()))
    data["rtps_by_month"]    = dict(sorted(rtps_by_month.items()))
    data["rtps_by_supplier"] = top_suppliers
    data["rtps_by_mode"]     = dict(rtps_by_mode)

    def record_id(record):
        return str(record.get("_id", ""))

    def short_text(value, limit=92):
        text = re.sub(r"\s+", " ", normalize_cbc_text(value)).strip()
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."

    def event_stage(field):
        return field.replace("_Approved", "").replace("_Rejected", "").replace("_", " ")

    def add_activity(events, record, module, title, amount, field, action, accent):
        event_time = to_local_time(record.get(field))
        if not isinstance(event_time, datetime) or not event_date_matches(event_time):
            return
        events.append({
            "time": event_time,
            "time_label": event_time.strftime("%d %b %Y, %H:%M"),
            "module": module,
            "action": action,
            "title": short_text(title, 72),
            "subsidiary": resolve_sub(record.get("subsidiary_id", ""), sub_map),
            "status": record.get("status", "Unknown"),
            "amount": amount,
            "description": short_text(record.get("justification", ""), 110),
            "record_id": record_id(record),
            "accent": accent,
        })

    def add_record_activities(events, records, module, title_fn, amount_fn, accent):
        approval_fields = [
            "Relief_Staff_Approved", "Supervisor_Approved", "HOD_Approved",
            "HR_Approved", "GCFR_Approved", "Accountant_Approved",
            "CFO_Approved", "CEO_Approved", "COO_Approved", "Chairman_Approved",
        ]
        rejection_fields = [
            "Relief_Staff_Rejected", "Supervisor_Rejected", "HOD_Rejected",
            "HR_Rejected", "GCFR_Rejected", "Accountant_Rejected",
            "CFO_Rejected", "CEO_Rejected", "COO_Rejected", "Chairman_Rejected",
        ]
        for record in records:
            if not subsidiary_matches(record):
                continue
            title = title_fn(record)
            amount = amount_fn(record)
            add_activity(events, record, module, title, amount, "createdAt", "Created request", accent)
            created_at = record.get("createdAt")
            updated_at = record.get("updatedAt")
            if (
                isinstance(updated_at, datetime)
                and (not isinstance(created_at, datetime) or updated_at != created_at)
            ):
                add_activity(events, record, module, title, amount, "updatedAt", "Updated request", accent)
            for field in approval_fields:
                add_activity(
                    events, record, module, title, amount, field,
                    f"{event_stage(field)} approved", accent
                )
            for field in rejection_fields:
                add_activity(
                    events, record, module, title, amount, field,
                    f"{event_stage(field)} rejected", "#0b3a75"
                )

    _leave_name_fields = ("name", "staff_name", "employee_name", "applicant_name", "full_name", "employee", "user_name", "staffName", "employeeName")
    def _leave_name(r):
        for f in _leave_name_fields:
            v = str(r.get(f, "") or "").strip()
            if v:
                return normalize_cbc_text(v)
        return "Leave Request"

    activity_events = []
    add_record_activities(
        activity_events,
        all_leaves,
        "Leave",
        leave_name,
        lambda r: "",
        "#1155cc",
    )
    add_record_activities(
        activity_events,
        all_advances,
        "Cash Advance",
        lambda r: r.get("name", "Cash advance"),
        lambda r: format_money(safe_amount(r.get("amount", 0))),
        "#1d6ff2",
    )
    add_record_activities(
        activity_events,
        all_expenses,
        "Expense Claim",
        lambda r: r.get("staff_name", "Expense claim"),
        lambda r: format_money(ec_total_amount(r)),
        "#4c93ff",
    )
    add_record_activities(
        activity_events,
        all_rtps,
        "RTPS",
        lambda r: r.get("name_of_supplier", "Supplier payment"),
        lambda r: format_money(safe_amount(r.get("amount", 0))),
        "#0b3a75",
    )
    activity_events = sorted(activity_events, key=lambda item: item["time"], reverse=True)
    data["activity_total"] = len(activity_events)
    data["activity_timeline"] = activity_events[:24]

    current_time = app_now()

    aging_bucket_defs = [
        ("0-2 days", 0, 2),
        ("3-7 days", 3, 7),
        ("8-14 days", 8, 14),
        ("15+ days", 15, None),
    ]

    def pending_age_days(record):
        dt = record_date(record)
        if not dt:
            return None
        return max((current_time.date() - dt.date()).days, 0)

    def aging_bucket(days):
        if days is None:
            return "Unknown"
        for label, start, end in aging_bucket_defs:
            if days >= start and (end is None or days <= end):
                return label
        return "Unknown"

    def next_stage(record, stages):
        for label, field, previous_field in stages:
            if previous_field and record.get(previous_field) is not True:
                continue
            if record.get(field) is not True:
                return label
        return "Review"

    leave_stages = [
        ("HOD", "is_HOD_Approved", None),
        ("HR", "is_HR_Approved", "is_HOD_Approved"),
    ]
    finance_stages = [
        ("HOD", "is_HOD_Approved", None),
        ("GCFR", "is_GCFR_Approved", "is_HOD_Approved"),
        ("Accountant", "is_Accountant_Approved", "is_GCFR_Approved"),
        ("CFO", "is_CFO_Approved", "is_Accountant_Approved"),
        ("CEO", "is_CEO_Approved", "is_CFO_Approved"),
        ("COO", "is_COO_Approved", "is_CEO_Approved"),
        ("Chairman", "is_Chairman_Approved", "is_COO_Approved"),
    ]

    aging_modules = [
        ("Leave", leaves, leave_name, lambda r: 0, leave_stages),
        ("Cash Advance", advances, lambda r: r.get("name", "Cash advance"), lambda r: safe_amount(r.get("amount", 0)), finance_stages),
        ("Expense Claim", expenses, lambda r: r.get("staff_name", "Expense claim"), ec_total_amount, finance_stages),
        ("RTPS", rtps, lambda r: r.get("name_of_supplier", "Supplier payment"), lambda r: safe_amount(r.get("amount", 0)), finance_stages),
    ]
    aging_summary = {
        label: {"label": label, "count": 0, "amount": 0.0, "modules": defaultdict(int)}
        for label, _, _ in aging_bucket_defs
    }
    pending_items = []
    for module, records, title_fn, amount_fn, stages in aging_modules:
        for record in records:
            if status_group(record) != "pending":
                continue
            days = pending_age_days(record)
            bucket = aging_bucket(days)
            if bucket not in aging_summary:
                aging_summary[bucket] = {"label": bucket, "count": 0, "amount": 0.0, "modules": defaultdict(int)}
            amount = amount_fn(record)
            created = record_date(record)
            aging_summary[bucket]["count"] += 1
            aging_summary[bucket]["amount"] += amount
            aging_summary[bucket]["modules"][module] += 1
            pending_items.append({
                "module": module,
                "title": short_text(title_fn(record), 72),
                "subsidiary": resolve_sub(record.get("subsidiary_id", ""), sub_map),
                "created": created.strftime("%d %b %Y") if created else "Unknown",
                "days": days if days is not None else 0,
                "amount": "Nill" if module == "Leave" else format_money(amount),
                "amount_value": amount,
                "waiting_on": next_stage(record, stages),
                "description": short_text(record.get("justification", ""), 110),
                "leave_type":         normalize_cbc_text(record.get("leave_Details", "")) if module == "Leave" else "",
                "leave_days_applied": _int(record.get("no_days_taken", 0)) if module == "Leave" else 0,
                "leave_days_left":    _int(record.get("no_days_left", 0)) if module == "Leave" else 0,
            })

    aging_rows = []
    for label, _, _ in aging_bucket_defs:
        row = aging_summary[label]
        aging_rows.append({
            "label": label,
            "count": row["count"],
            "amount": row["amount"],
            "amount_label": format_money(row["amount"]) or "₦0",
            "leave": row["modules"]["Leave"],
            "cash_advance": row["modules"]["Cash Advance"],
            "expense_claim": row["modules"]["Expense Claim"],
            "rtps": row["modules"]["RTPS"],
            "share": 0,
        })
    total_pending_aging = sum(row["count"] for row in aging_rows)
    for row in aging_rows:
        row["share"] = round(row["count"] / total_pending_aging * 100, 1) if total_pending_aging else 0
    oldest_pending = sorted(pending_items, key=lambda item: item["days"], reverse=True)[:8]
    data["pending_aging"] = aging_rows
    data["pending_aging_total"] = total_pending_aging
    data["oldest_pending"] = oldest_pending
    data["oldest_pending_days"] = oldest_pending[0]["days"] if oldest_pending else 0
    max_pending_amount = max((item["amount_value"] for item in pending_items), default=0)
    for item in pending_items:
        amount_score = (item["amount_value"] / max_pending_amount * 45) if max_pending_amount else 0
        age_score = min(item["days"], 30) / 30 * 40
        stage_score = 15 if item["waiting_on"] in {"CFO", "CEO", "COO", "Chairman"} else 8
        item["priority_score"] = round(amount_score + age_score + stage_score, 1)
        if item["module"] == "Leave":
            day_info = f", {item['leave_days_applied']} day(s) applied" if item["leave_days_applied"] else ""
            item["why"] = f"{item['days']} days old{day_info}, waiting on {item['waiting_on']}"
        else:
            item["why"] = (
                f"{item['days']} days old"
                + (f", {item['amount']} waiting" if item["amount"] else "")
                + f", waiting on {item['waiting_on']}"
            )
    data["action_required"] = sorted(
        pending_items,
        key=lambda item: (item["priority_score"], item["amount_value"], item["days"]),
        reverse=True,
    )[:5]

    rejection_event_fields = [
        ("HOD", "HOD_Rejected"),
        ("HR", "HR_Rejected"),
        ("GCFR", "GCFR_Rejected"),
        ("Accountant", "Accountant_Rejected"),
        ("CFO", "CFO_Rejected"),
        ("CEO", "CEO_Rejected"),
        ("COO", "COO_Rejected"),
        ("Chairman", "Chairman_Rejected"),
    ]

    analytics_sources = [
        ("Leave", leaves, lambda r: 0),
        ("Cash Advance", advances, lambda r: safe_amount(r.get("amount", 0))),
        ("Expense Claim", expenses, ec_total_amount),
        ("RTPS", rtps, lambda r: safe_amount(r.get("amount", 0))),
    ]

    def is_rejected(record):
        status = str(record.get("status", "")).strip().lower()
        if status == "rejected":
            return True
        return any(isinstance(record.get(field), datetime) for _, field in rejection_event_fields)

    rejection_rows = []
    rejection_by_stage = defaultdict(int)
    rejection_by_sub = defaultdict(int)
    for module, records, amount_fn in analytics_sources:
        rejected_records = [r for r in records if is_rejected(r)]
        rejected_count = len(rejected_records)
        rejected_amount = sum(amount_fn(r) for r in rejected_records)
        if module == "Leave":
            rejected_days = sum(_int(r.get("no_days_taken", 0)) for r in rejected_records)
            metric_label = f"{rejected_count} request{'s' if rejected_count != 1 else ''} ({rejected_days} day{'s' if rejected_days != 1 else ''})"
        else:
            metric_label = format_money(rejected_amount) or "₦0"
        metric_type = "Count (days)" if module == "Leave" else "Value"
        for r in rejected_records:
            rejection_by_sub[resolve_sub(r.get("subsidiary_id", ""), sub_map)] += 1
            for stage, field in rejection_event_fields:
                if isinstance(r.get(field), datetime):
                    rejection_by_stage[f"{stage} - {module}"] += 1
        rejection_rows.append({
            "module": module,
            "count": rejected_count,
            "rate": round(rejected_count / len(records) * 100, 1) if records else 0,
            "amount": rejected_amount,
            "amount_label": format_money(rejected_amount) or "₦0",
            "metric_label": metric_label,
            "metric_type": metric_type,
        })
    data["rejection_analytics"] = rejection_rows
    data["rejection_hotspots"] = sorted(
        [{"label": label, "count": count} for label, count in rejection_by_stage.items()],
        key=lambda item: item["count"],
        reverse=True,
    )[:6]
    data["rejection_by_subsidiary"] = sorted(
        [{"subsidiary": sub, "count": count} for sub, count in rejection_by_sub.items()],
        key=lambda item: item["count"],
        reverse=True,
    )[:5]

    staff_stats = defaultdict(lambda: {"count": 0, "amount": 0.0, "pending": 0, "rejected": 0, "modules": defaultdict(int)})
    staff_sources = [
        ("Leave", leaves, leave_name, lambda r: 0),
        ("Cash Advance", advances, lambda r: requester(r, "name", "staff_name"), lambda r: safe_amount(r.get("amount", 0))),
        ("Expense Claim", expenses, lambda r: requester(r, "staff_name", "name"), ec_total_amount),
    ]
    for module, records, name_fn, amount_fn in staff_sources:
        for r in records:
            name = name_fn(r)
            staff_stats[name]["count"] += 1
            staff_stats[name]["amount"] += amount_fn(r)
            staff_stats[name]["pending"] += 1 if status_group(r) == "pending" else 0
            staff_stats[name]["rejected"] += 1 if is_rejected(r) else 0
            staff_stats[name]["modules"][module] += 1
    data["staff_behavior"] = sorted(
        [
            {
                "name": name,
                "count": row["count"],
                "amount": row["amount"],
                "amount_label": format_money(row["amount"]) or "₦0",
                "pending": row["pending"],
                "rejected": row["rejected"],
                "main_module": max(row["modules"].items(), key=lambda item: item[1])[0] if row["modules"] else "Unknown",
            }
            for name, row in staff_stats.items()
        ],
        key=lambda item: (item["amount"], item["count"]),
        reverse=True,
    )[:8]

    duplicate_groups = defaultdict(list)
    duplicate_sources = [
        ("Cash Advance", advances, lambda r: requester(r, "name", "staff_name"), lambda r: safe_amount(r.get("amount", 0))),
        ("Expense Claim", expenses, lambda r: requester(r, "staff_name", "name"), ec_total_amount),
        ("RTPS", rtps, lambda r: requester(r, "name_of_supplier"), lambda r: safe_amount(r.get("amount", 0))),
    ]
    for module, records, owner_fn, amount_fn in duplicate_sources:
        for r in records:
            dt = record_date(r)
            amount = round(amount_fn(r), 2)
            if not dt or not amount:
                continue
            key = (module, owner_fn(r).lower(), amount, dt.strftime("%Y-%m"))
            duplicate_groups[key].append(r)
    suspicious = []
    for (module, owner, amount, month), records in duplicate_groups.items():
        if len(records) < 2:
            continue
        dates = sorted([record_date(r) for r in records if record_date(r)])
        days_span = (dates[-1] - dates[0]).days if len(dates) > 1 else 0
        suspicious.append({
            "module": module,
            "owner": normalize_cbc_text(owner.title()),
            "count": len(records),
            "amount": amount,
            "amount_label": format_money(amount) or "₦0",
            "month": month,
            "reason": f"{len(records)} requests with same owner and amount in {month}; {days_span} days apart.",
        })
    data["suspicious_requests"] = sorted(
        suspicious,
        key=lambda item: (item["count"], item["amount"]),
        reverse=True,
    )[:8]

    next_month = datetime(current_time.year + (1 if current_time.month == 12 else 0), 1 if current_time.month == 12 else current_time.month + 1, 1)
    days_to_month_end = max((next_month.date() - current_time.date()).days, 0)
    close_month_start = datetime(current_time.year, current_time.month, 1)
    close_month_spend = (
        spend_between(all_advances, lambda r: safe_amount(r.get("amount", 0)), close_month_start, current_time + timedelta(days=1))
        + spend_between(all_expenses, ec_total_amount, close_month_start, current_time + timedelta(days=1))
        + spend_between(all_rtps, lambda r: safe_amount(r.get("amount", 0)), close_month_start, current_time + timedelta(days=1))
    )
    close_pending_spend = data["ca_pending_amount"] + data["ec_pending_amount"] + data["rtps_pending_amount"]
    close_unclassified_spend = data["ca_unclassified_amount"] + data["ec_unclassified_amount"] + data["rtps_unclassified_amount"]
    data["month_end_close"] = {
        "days_left": days_to_month_end,
        "pending_value": close_pending_spend,
        "pending_label": format_money(close_pending_spend) or "₦0",
        "approved_label": format_money(close_month_spend) or "₦0",
        "unclassified_label": format_money(close_unclassified_spend) or "₦0",
        "blockers": len([item for item in pending_items if item["days"] >= 8]),
    }

    recent_start = current_time - timedelta(days=7)
    recent_created = [
        item for item in activity_events
        if item["action"] == "Created request" and item["time"] >= recent_start
    ]
    recent_approved = [
        item for item in activity_events
        if "approved" in item["action"].lower() and item["time"] >= recent_start
    ]
    recent_rejected = [
        item for item in activity_events
        if "rejected" in item["action"].lower() and item["time"] >= recent_start
    ]
    data["what_changed"] = {
        "window": "Last 7 days",
        "new_requests": len(recent_created),
        "approvals": len(recent_approved),
        "rejections": len(recent_rejected),
        "latest": activity_events[:6],
    }

    # Approval rates
    def approval_rate(records, field):
        total    = len(records)
        approved = sum(1 for r in records if r.get(field) is True)
        return round(approved / total * 100, 1) if total else 0

    data["approval_rates"] = {
        "HOD (Leave)":        approval_rate(leaves,   "is_HOD_Approved"),
        "HR (Leave)":         approval_rate(leaves,   "is_HR_Approved"),
        "HOD (Cash Adv)":     approval_rate(advances, "is_HOD_Approved"),
        "CFO (Cash Adv)":     approval_rate(advances, "is_CFO_Approved"),
        "CEO (Cash Adv)":     approval_rate(advances, "is_CEO_Approved"),
        "HOD (Expense)":      approval_rate(expenses, "is_HOD_Approved"),
        "CFO (Expense)":      approval_rate(expenses, "is_CFO_Approved"),
        "CEO (Expense)":      approval_rate(expenses, "is_CEO_Approved"),
        "HOD (RTPS)":         approval_rate(rtps,     "is_HOD_Approved"),
        "CFO (RTPS)":         approval_rate(rtps,     "is_CFO_Approved"),
        "CEO (RTPS)":         approval_rate(rtps,     "is_CEO_Approved"),
    }

    # Executive signals
    total_spend = data["ca_amount"] + data["ec_amount"] + data["rtps_amount"]
    approved_spend = (
        data["ca_approved_amount"] + data["ec_approved_amount"] + data["rtps_approved_amount"]
    )
    pending_spend = (
        data["ca_pending_amount"] + data["ec_pending_amount"] + data["rtps_pending_amount"]
    )
    unclassified_spend = (
        data["ca_unclassified_amount"] + data["ec_unclassified_amount"] + data["rtps_unclassified_amount"]
    )
    data["total_requests"] = data["leave_total"] + data["ca_total"] + data["ec_total"] + data["rtps_total"]
    data["total_approved_requests"] = data["leave_approved"] + data["ca_approved"] + data["ec_approved"] + data["rtps_approved"]
    data["system_ready"] = data["total_requests"] == 0
    data["total_spend"] = total_spend
    data["approved_spend"] = approved_spend
    data["pending_spend"] = pending_spend
    data["unclassified_spend"] = unclassified_spend

    spend_by_sub = defaultdict(float)
    for source in (ca_by_sub, ec_by_sub, rtps_by_sub):
        for sub, amount in source.items():
            spend_by_sub[sub] += amount
    top_sub, top_sub_amount = ("None", 0)
    if spend_by_sub:
        top_sub, top_sub_amount = max(spend_by_sub.items(), key=lambda x: x[1])
    data["top_spending_subsidiary"] = top_sub
    data["top_spending_subsidiary_amount"] = top_sub_amount

    pending_by_module = {
        "Cash Advance": data["ca_pending_amount"],
        "Expense Claims": data["ec_pending_amount"],
        "RTPS": data["rtps_pending_amount"],
    }
    highest_pending_module, highest_pending_amount = max(pending_by_module.items(), key=lambda x: x[1])
    data["highest_pending_module"] = highest_pending_module
    data["highest_pending_amount"] = highest_pending_amount

    lowest_approval_label, lowest_approval_rate = min(
        data["approval_rates"].items(), key=lambda x: x[1]
    ) if data["approval_rates"] else ("None", 0)
    data["lowest_approval_label"] = lowest_approval_label
    data["lowest_approval_rate"] = lowest_approval_rate

    def waiting_stage_details(records, field, amount_fn, previous_field=None):
        waiting = [
            r for r in records
            if status_group(r) == "pending"
            and r.get(field) is not True
            and (previous_field is None or r.get(previous_field) is True)
        ]
        amount = sum(amount_fn(r) for r in waiting)
        ages = [pending_age_days(r) for r in waiting if pending_age_days(r) is not None]
        avg_days = round(sum(ages) / len(ages), 1) if ages else 0
        return len(waiting), amount, avg_days

    def bottleneck(label, records, field, amount_fn, previous_field=None, is_leave=False):
        count, amount, avg_days = waiting_stage_details(records, field, amount_fn, previous_field)
        if is_leave:
            waiting_label = "0"
        else:
            waiting_label = format_money(amount) or "₦0"
        return {
            "label": label,
            "count": count,
            "amount": amount,
            "amount_label": waiting_label,
            "avg_days": avg_days,
            "impact": f"{count} requests, {waiting_label} waiting, {avg_days} avg days",
        }

    bottlenecks = [
        bottleneck("HOD - Leave", leaves, "is_HOD_Approved", lambda r: 0, is_leave=True),
        bottleneck("HR - Leave",  leaves, "is_HR_Approved",  lambda r: 0, "is_HOD_Approved", is_leave=True),
        bottleneck("HOD - Cash Adv", advances, "is_HOD_Approved", lambda r: safe_amount(r.get("amount", 0))),
        bottleneck("CFO - Cash Adv", advances, "is_CFO_Approved", lambda r: safe_amount(r.get("amount", 0)), "is_HOD_Approved"),
        bottleneck("CEO - Cash Adv", advances, "is_CEO_Approved", lambda r: safe_amount(r.get("amount", 0)), "is_CFO_Approved"),
        bottleneck("HOD - Expense", expenses, "is_HOD_Approved", ec_total_amount),
        bottleneck("CFO - Expense", expenses, "is_CFO_Approved", ec_total_amount, "is_HOD_Approved"),
        bottleneck("CEO - Expense", expenses, "is_CEO_Approved", ec_total_amount, "is_CFO_Approved"),
        bottleneck("HOD - RTPS", rtps, "is_HOD_Approved", lambda r: safe_amount(r.get("amount", 0))),
        bottleneck("CFO - RTPS", rtps, "is_CFO_Approved", lambda r: safe_amount(r.get("amount", 0)), "is_HOD_Approved"),
        bottleneck("CEO - RTPS", rtps, "is_CEO_Approved", lambda r: safe_amount(r.get("amount", 0)), "is_CFO_Approved"),
    ]
    data["bottlenecks"] = sorted(
        bottlenecks,
        key=lambda x: (x["amount"], x["count"], x["avg_days"]),
        reverse=True,
    )[:6]

    current_time = app_now()
    month_start = datetime(current_time.year, current_time.month, 1)
    previous_start, previous_end = previous_month_bounds()
    current_requests = (
        count_between(all_leaves, month_start, current_time + timedelta(days=1))
        + count_between(all_advances, month_start, current_time + timedelta(days=1))
        + count_between(all_expenses, month_start, current_time + timedelta(days=1))
        + count_between(all_rtps, month_start, current_time + timedelta(days=1))
    )
    previous_requests = (
        count_between(all_leaves, previous_start, previous_end)
        + count_between(all_advances, previous_start, previous_end)
        + count_between(all_expenses, previous_start, previous_end)
        + count_between(all_rtps, previous_start, previous_end)
    )
    current_spend = (
        spend_between(all_advances, lambda r: safe_amount(r.get("amount", 0)), month_start, current_time + timedelta(days=1))
        + spend_between(all_expenses, ec_total_amount, month_start, current_time + timedelta(days=1))
        + spend_between(all_rtps, lambda r: safe_amount(r.get("amount", 0)), month_start, current_time + timedelta(days=1))
    )
    previous_spend = (
        spend_between(all_advances, lambda r: safe_amount(r.get("amount", 0)), previous_start, previous_end)
        + spend_between(all_expenses, ec_total_amount, previous_start, previous_end)
        + spend_between(all_rtps, lambda r: safe_amount(r.get("amount", 0)), previous_start, previous_end)
    )
    requests_change_value = amount_change(current_requests, previous_requests)
    spend_change_value = amount_change(current_spend, previous_spend)
    data["month_compare"] = {
        "requests": current_requests,
        "requests_change": format_change(requests_change_value),
        "requests_change_value": requests_change_value,
        "spend": current_spend,
        "spend_change": format_change(spend_change_value),
        "spend_change_value": spend_change_value,
    }

    pending_ratio = round(pending_spend / total_spend * 100, 1) if total_spend else 0
    risk_points = 0
    risk_points += 25 if pending_ratio >= 40 else 15 if pending_ratio >= 20 else 0
    risk_points += 25 if data["oldest_pending_days"] >= 15 else 12 if data["oldest_pending_days"] >= 8 else 0
    risk_points += 20 if lowest_approval_rate < 50 else 10 if lowest_approval_rate < 70 else 0
    risk_points += 15 if unclassified_spend else 0
    risk_points += 15 if spend_change_value >= 35 else 8 if spend_change_value >= 15 else 0
    health_score = max(0, 100 - risk_points)
    if health_score >= 80:
        health_status, health_tone = "Good", "Operations are moving normally."
    elif health_score >= 60:
        health_status, health_tone = "Watch", "A few queues or spending movements need attention."
    else:
        health_status, health_tone = "Needs Attention", "Approval delays or spend exposure should be reviewed today."
    data["health"] = {
        "score": health_score,
        "status": health_status,
        "tone": health_tone,
        "pending_ratio": pending_ratio,
    }

    data_quality_warnings = []
    if unclassified_spend:
        data_quality_warnings.append(
            f"Unclassified finance requests total ₦{unclassified_spend:,.0f}; totals may shift after statuses are cleaned."
        )
    if data["ca_unclassified"] or data["ec_unclassified"] or data["rtps_unclassified"]:
        data_quality_warnings.append(
            f"{data['ca_unclassified'] + data['ec_unclassified'] + data['rtps_unclassified']} finance records have unclear status."
        )
    if any(row["count"] for row in aging_rows) and data["oldest_pending_days"] >= 15:
        data_quality_warnings.append(
            f"Oldest pending request is {data['oldest_pending_days']} days old; aging should be checked with approvers."
        )
    data["data_quality_warnings"] = data_quality_warnings

    risk_alerts = []
    largest_pending = max(pending_items, key=lambda item: item["amount_value"], default=None)
    if largest_pending and largest_pending["amount_value"] > 0:
        risk_alerts.append(
            f"Largest pending request is {largest_pending['amount']} in {largest_pending['module']} for {largest_pending['subsidiary']}."
        )
    if spend_change_value >= 25:
        risk_alerts.append(f"Month-to-date spend is up {spend_change_value:,.1f}% versus last month.")
    if data["oldest_pending_days"] >= 8:
        risk_alerts.append(f"Oldest pending request has waited {data['oldest_pending_days']} days.")
    data["risk_alerts"] = risk_alerts

    scorecards = []
    for sub in sorted(set(list(spend_by_sub.keys()) + list(sub_map.values()))):
        sub_pending_items = [item for item in pending_items if item["subsidiary"] == sub]
        spend = spend_by_sub.get(sub, 0)
        pending_amount = sum(item["amount_value"] for item in sub_pending_items)
        pending_count = len(sub_pending_items)
        oldest_days = max((item["days"] for item in sub_pending_items), default=0)
        if not any((spend, pending_amount, pending_count, oldest_days)):
            continue
        scorecards.append({
            "subsidiary": sub,
            "spend": spend,
            "spend_label": format_money(spend) or "₦0",
            "pending_count": pending_count,
            "pending_amount": pending_amount,
            "pending_label": format_money(pending_amount) or "₦0",
            "oldest_days": oldest_days,
        })
    data["subsidiary_scorecards"] = sorted(
        scorecards,
        key=lambda item: (item["pending_amount"], item["spend"], item["oldest_days"]),
        reverse=True,
    )[:8]

    high_risk_count = len([
        item for item in pending_items
        if item["amount_value"] >= 5_000_000 or item["days"] >= 8 or item["waiting_on"] in {"CEO", "COO", "Chairman"}
    ])
    ceo_pending = len([item for item in pending_items if item["waiting_on"] == "CEO"])
    active_vendors = len([name for name, amount in rtps_by_supplier.items() if amount])
    turnaround_days = round(sum(item["days"] for item in pending_items) / len(pending_items), 1) if pending_items else 0

    data["executive_kpis"] = [
        {
            "label": "Total Pending Requests",
            "value": f"{len(pending_items):,}",
            "change": data["month_compare"]["requests_change"],
            "trend": "up" if data["month_compare"]["requests_change_value"] >= 0 else "down",
            "status": "warn" if len(pending_items) else "good",
            "target": "pending-approvals",
            "note": "Across approvals, escalations, and operational queues.",
        },
        {
            "label": "Total Pending Amount",
            "value": format_money(pending_spend) or "NGN 0",
            "change": f"{data['health']['pending_ratio']}% exposure",
            "trend": "up" if data["health"]["pending_ratio"] >= 20 else "flat",
            "status": "danger" if data["health"]["pending_ratio"] >= 40 else "warn" if data["health"]["pending_ratio"] >= 20 else "good",
            "target": "pending-approvals",
            "note": "Unapproved financial value waiting for action.",
        },
        {
            "label": "CEO Pending Approvals",
            "value": f"{ceo_pending:,}",
            "change": "CEO queue",
            "trend": "flat",
            "status": "danger" if ceo_pending else "good",
            "target": "pending-approvals",
            "note": "Items currently waiting at CEO level.",
        },
        {
            "label": "Longest Aging Request",
            "value": f"{data['oldest_pending_days']} days",
            "change": "oldest item",
            "trend": "up" if data["oldest_pending_days"] >= 8 else "flat",
            "status": "danger" if data["oldest_pending_days"] >= 15 else "warn" if data["oldest_pending_days"] >= 8 else "good",
            "target": "audit-risk",
            "note": "Maximum waiting time among pending records.",
        },
        {
            "label": "Approval Turnaround",
            "value": f"{turnaround_days} days",
            "change": "avg pending age",
            "trend": "down" if turnaround_days <= 3 else "up",
            "status": "warn" if turnaround_days > 5 else "good",
            "target": "audit-risk",
            "note": "Average age of currently pending items.",
        },
        {
            "label": "High-Risk Transactions",
            "value": f"{high_risk_count:,}",
            "change": "risk rules",
            "trend": "up" if high_risk_count else "flat",
            "status": "danger" if high_risk_count else "good",
            "target": "audit-risk",
            "note": "High-value, delayed, or executive-stage requests.",
        },
        {
            "label": "Total Spend by Period",
            "value": format_money(total_spend) or "NGN 0",
            "change": data["month_compare"]["spend_change"],
            "trend": "up" if data["month_compare"]["spend_change_value"] >= 0 else "down",
            "status": "warn" if data["month_compare"]["spend_change_value"] >= 25 else "good",
            "target": "rtps-analytics",
            "note": "Cash advance, expense claim, and RTPS spend.",
        },
        {
            "label": "Active Vendors",
            "value": f"{active_vendors:,}",
            "change": "top supplier set",
            "trend": "flat",
            "status": "good",
            "target": "vendor-insights",
            "note": "Suppliers represented in the current RTPS view.",
        },
        {
            "label": "Subsidiary Performance",
            "value": data["top_spending_subsidiary"],
            "change": format_money(data["top_spending_subsidiary_amount"]) or "NGN 0",
            "trend": "flat",
            "status": "warn" if data["top_spending_subsidiary_amount"] else "good",
            "target": "subsidiaries",
            "note": "Highest spend concentration in the selected view.",
        },
    ]

    def table_date(record):
        dt = record_date(record)
        return dt.strftime("%Y-%m-%d") if dt else "Unknown"

    def table_status(record):
        status = str(record.get("status", "") or "Unclassified").strip()
        return status.title() if status else "Unclassified"

    operational_rows = []

    def add_table_rows(module, records, staff_fn, vendor_fn, amount_fn, stages):
        for record in records:
            amount = amount_fn(record)
            days = pending_age_days(record) if status_group(record) == "pending" else 0
            waiting = next_stage(record, stages) if status_group(record) == "pending" else "Complete"
            priority = "High" if amount >= 5_000_000 or days >= 8 or waiting in {"CEO", "COO", "Chairman"} else "Normal"

            # Leave-specific fields — non-financial, use day counts
            if module == "Leave":
                applicant   = short_text(staff_fn(record), 38)   # person who applied
                leave_type  = normalize_cbc_text(record.get("leave_Details", "Leave"))
                days_applied = _int(record.get("no_days_taken", 0))
                days_left    = _int(record.get("no_days_left", 0))
                row_vendor  = applicant                           # use applicant name in "Vendor" column
                row_amount  = "Nill"
                row_amount_value = 0
            else:
                leave_type   = ""
                days_applied = 0
                days_left    = 0
                row_vendor   = short_text(vendor_fn(record), 38)
                row_amount   = format_money(amount) or "NGN 0"
                row_amount_value = amount

            operational_rows.append({
                "date": table_date(record),
                "request_id": record_id(record)[-8:] or "Unknown",
                "staff": short_text(staff_fn(record), 34),
                "vendor": row_vendor,
                "amount": row_amount,
                "amount_value": row_amount_value,
                "status": table_status(record),
                "aging": f"{days} days" if days else "-",
                "aging_days": days,
                "approval_level": waiting,
                "module": module,
                "subsidiary": resolve_sub(record.get("subsidiary_id", ""), sub_map),
                "priority": priority,
                "details": short_text(record.get("justification", ""), 150),
                # leave-only extras
                "leave_type":    leave_type,
                "leave_days_applied": days_applied,
                "leave_days_left":    days_left,
            })

    add_table_rows("Cash Advance", advances, lambda r: requester(r, "name", "staff_name"), lambda r: "Cash Advance", lambda r: safe_amount(r.get("amount", 0)), finance_stages)
    add_table_rows("Expense Claim", expenses, lambda r: requester(r, "staff_name", "name"), lambda r: "Expense Claim", ec_total_amount, finance_stages)
    add_table_rows("RTPS", rtps, lambda r: requester(r, "staff_name", "name"), lambda r: normalize_cbc_text(r.get("name_of_supplier", "Supplier")), lambda r: safe_amount(r.get("amount", 0)), finance_stages)
    add_table_rows("Leave", leaves, leave_name, lambda r: None, lambda r: 0, leave_stages)
    data["operational_rows"] = sorted(
        operational_rows,
        key=lambda row: (row["status"] == "Pending", row["aging_days"], row["amount_value"]),
        reverse=True,
    )[:250]

    data["alert_center"] = []
    for item in data["action_required"][:4]:
        data["alert_center"].append({
            "type": "Overdue approval" if item["days"] >= 8 else "Pending approval",
            "priority": "High" if item["priority_score"] >= 55 else "Medium",
            "message": f"{item['module']} for {item['subsidiary']} is waiting on {item['waiting_on']}.",
        })
    for alert in data["risk_alerts"][:3]:
        data["alert_center"].append({"type": "Risk anomaly", "priority": "High", "message": alert})
    for item in data["suspicious_requests"][:2]:
        data["alert_center"].append({"type": "Duplicate payment", "priority": "Medium", "message": item["reason"]})

    search_terms = set()
    for row in data["operational_rows"][:120]:
        for key in ("vendor", "request_id", "staff", "subsidiary", "module", "status", "approval_level", "priority"):
            value = str(row.get(key, "")).strip()
            if value and value != "-":
                search_terms.add(value)
    search_terms.update(["Pending approvals above NGN 10M", "CEO Attention", "Overdue approvals", "High-risk transactions", "Export reports"])
    data["smart_search_terms"] = sorted(search_terms)[:80]

    data["beginner_explainers"] = [
        {"term": "Waiting Approval", "meaning": "Money or requests submitted but not fully cleared by the approval chain."},
        {"term": "Approval Rate", "meaning": "The share of requests that have been approved at each stage."},
        {"term": "Pending Aging", "meaning": "How long requests have been waiting. Older items are usually the first to chase."},
        {"term": "RTPS", "meaning": "Request to Pay Supplier. This is supplier payment exposure."},
        {"term": "Month-End Close", "meaning": "Items that can affect finance reporting before the month closes."},
        {"term": "Suspicious Requests", "meaning": "Repeat requests with the same owner and amount that may need a second look."},
    ]

    data["insights"] = [
        f"Platform health is {health_status}: {health_tone}",
        f"{highest_pending_module} has the highest waiting approval spend at ₦{highest_pending_amount:,.0f}.",
        f"{top_sub} is the highest spending subsidiary in this view at ₦{top_sub_amount:,.0f}.",
        f"{lowest_approval_label} currently has the lowest approval rate at {lowest_approval_rate}%.",
        f"Waiting approval spend is ₦{data['pending_spend']:,.0f} across active requests.",
    ]

    return data

# Chart builders

def blue_scale(count):
    palette = ["#0b3a75", "#1155cc", "#1d6ff2", "#4c93ff", "#8fc3ff", "#c7e2ff"]
    return [palette[i % len(palette)] for i in range(max(count, 1))]


def chart_layout(fig, title, height, margin, font_size=11, xlabel="", ylabel=""):
    fig.update_layout(
        title=dict(text=title, font=dict(size=15, color="#082f63")),
        xaxis_title=xlabel,
        yaxis_title=ylabel,
        plot_bgcolor="rgba(255,255,255,0)",
        paper_bgcolor="rgba(255,255,255,0)",
        margin=margin,
        font=dict(family="Segoe UI, Arial, sans-serif", size=font_size, color="#183b68"),
        height=height,
    )
    fig.update_xaxes(showgrid=False, zeroline=False, showline=False)
    fig.update_yaxes(showgrid=False, zeroline=False, showline=False)
    return fig

# Plotly modebar: explicitly show ONLY download (toImage) and zoom2d
# Using modeBarButtons whitelist is the only reliable way
_CHART_CONFIG = {
    "modeBarButtons": [["toImage"]],
    "displaylogo": False,
    "scrollZoom": False,
}


def bar(x, y, title, color="#1155cc", xlabel="", ylabel=""):
    fig = go.Figure(go.Bar(
        x=x, y=y,
        marker=dict(color=blue_scale(len(y)), line=dict(color="rgba(255,255,255,0.75)", width=1)),
        text=[f"{v:,.0f}" for v in y],
        textposition="outside"
    ))
    chart_layout(fig, title, 320, dict(t=50, b=40, l=40, r=20), xlabel=xlabel, ylabel=ylabel)
    return fig.to_html(full_html=False, include_plotlyjs=False, config=_CHART_CONFIG)


def pie(labels, values, title):
    fig = go.Figure(go.Pie(
        labels=labels, values=values,
        hole=0.4,
        marker=dict(colors=blue_scale(len(values)), line=dict(color="white", width=2))
    ))
    chart_layout(fig, title, 320, dict(t=50, b=20, l=20, r=20))
    return fig.to_html(full_html=False, include_plotlyjs=False, config=_CHART_CONFIG)


def line(x, y, title, color="#1155cc"):
    values = [float(v or 0) for v in y]
    max_value = max(values, default=0)
    y_range = [0, max(max_value * 1.2, 1)]
    has_multiple_points = len(x) > 1
    fig = go.Figure(go.Scatter(
        x=x, y=values, mode="lines+markers" if has_multiple_points else "markers",
        fill="tozeroy" if has_multiple_points else None,
        fillcolor="rgba(76,147,255,0.18)",
        line=dict(color=color, width=3, shape="spline" if len(x) > 2 else "linear"),
        marker=dict(size=7, color="#ffffff", line=dict(color=color, width=2))
    ))
    chart_layout(fig, title, 300, dict(t=50, b=40, l=40, r=20))
    fig.update_xaxes(type="category")
    fig.update_yaxes(range=y_range, rangemode="tozero")
    return fig.to_html(full_html=False, include_plotlyjs=False, config=_CHART_CONFIG)


def hbar(x, y, title, color="#1155cc"):
    fig = go.Figure(go.Bar(
        x=x, y=y, orientation="h",
        marker=dict(color=blue_scale(len(x)), line=dict(color="rgba(255,255,255,0.75)", width=1)),
        text=[f"₦{v:,.0f}" for v in x],
        textposition="outside"
    ))
    chart_layout(fig, title, 340, dict(t=50, b=20, l=120, r=60), font_size=10)
    return fig.to_html(full_html=False, include_plotlyjs=False, config=_CHART_CONFIG)

# HTML template

TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>IGEZ - Analytics Dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Segoe UI', Arial, sans-serif;
    background:
      radial-gradient(circle at top left, rgba(76,147,255,0.20), transparent 34%),
      linear-gradient(180deg, #f8fbff 0%, #edf5ff 100%);
    color: #0b2447;
  }

  /* Header */
  .header {
    background: linear-gradient(135deg, #061b3a 0%, #082f63 55%, #1155cc 100%);
    color: white; padding: 22px 32px;
    display: flex; align-items: center; justify-content: space-between;
    box-shadow: 0 18px 40px rgba(8,47,99,0.22);
  }
  .header h1, .header .h1 { font-size: 23px; font-weight: 800; letter-spacing: 0; }
  .header .sub { font-size: 13px; opacity: 0.8; margin-top: 4px; }
  .refresh-btn {
    background: rgba(255,255,255,0.18); border: 1px solid rgba(255,255,255,0.42);
    color: white; padding: 9px 18px; border-radius: 8px;
    cursor: pointer; font-size: 13px; font-weight: 700; text-decoration: none;
    box-shadow: 0 12px 28px rgba(0,0,0,0.18);
  }
  .refresh-btn:hover { background: rgba(255,255,255,0.3); }

  /* Layout */
  .container { max-width: 1400px; margin: 0 auto; padding: 24px; }
  .section-title {
    font-size: 16px; font-weight: 800; color: #082f63;
    margin: 28px 0 14px; padding-left: 10px;
    border-left: 4px solid #1155cc;
  }
  .filter-bar {
    background: rgba(255,255,255,0.74); border: 1px solid rgba(143,195,255,0.50);
    border-radius: 12px; padding: 16px;
    display: grid; grid-template-columns: 1fr 1fr 1fr 1fr auto; gap: 12px;
    align-items: end; box-shadow: 0 18px 45px rgba(8,47,99,0.12);
    backdrop-filter: blur(18px);
  }
  .filter-bar label { display: grid; gap: 6px; font-size: 12px; color: #244a78; font-weight: 800; }
  .filter-bar select, .filter-bar input {
    width: 100%; border: 1px solid #c7ddf6; border-radius: 8px; padding: 9px 10px;
    font: inherit; color: #082f63; background: rgba(255,255,255,0.92);
  }
  .filter-bar select:disabled, .filter-bar input:disabled {
    color: #c0392b; background: #fff5f5; border-color: #e74c3c; cursor: not-allowed;
  }
  .period-cancel-option { color: #c0392b; font-weight: 700; }
  .filter-alert { display: none; }
  .filter-bar button {
    border: 0; border-radius: 8px; padding: 10px 18px; background: #1155cc;
    color: white; font-weight: 600; cursor: pointer;
    box-shadow: 0 14px 26px rgba(17,85,204,0.28);
  }
  .filter-bar button:disabled {
    background: #cbd5df; cursor: not-allowed;
  }
  .filter-hint {
    grid-column: 4 / -1; color: #c0392b; font-size: 12px; font-weight: 700;
    min-height: 16px; margin-top: -4px; text-align: right;
  }
  .executive-grid { display: grid; grid-template-columns: 1.2fr 1fr; gap: 16px; margin-top: 16px; }
  .signal-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
  .signal {
    background: linear-gradient(145deg, rgba(255,255,255,0.88), rgba(231,243,255,0.62));
    border: 1px solid rgba(143,195,255,0.56); border-radius: 12px; padding: 16px;
    box-shadow: 0 20px 46px rgba(8,47,99,0.13); border-left: 4px solid var(--accent, #1155cc);
    backdrop-filter: blur(16px);
  }
  .signal .label { font-size: 12px; color: #4b6f9b; text-transform: uppercase; letter-spacing: 0.5px; font-weight: 800; }
  .signal .value { margin-top: 8px; font-size: 21px; color: #061b3a; font-weight: 900; }
  .signal .sub { margin-top: 4px; font-size: 12px; color: #5d7899; }
  .insight-card {
    background: linear-gradient(145deg, #061b3a 0%, #082f63 58%, #0b3a75 100%);
    color: white; border-radius: 12px; padding: 18px;
    box-shadow: 0 24px 54px rgba(6,27,58,0.24);
  }
  .insight-card h3 { font-size: 15px; margin-bottom: 12px; }
  .insight-card li { margin: 10px 0 0 18px; color: #dbeafe; font-size: 13px; line-height: 1.45; }
  .ready-state {
    min-height: 250px; display: grid; place-items: center; text-align: center;
    padding: 18px 12px 10px;
  }
  .ready-orb {
    width: 118px; height: 118px; border-radius: 50%;
    background:
      radial-gradient(circle at 32% 28%, rgba(255,255,255,0.82), transparent 0 12%, transparent 26%),
      radial-gradient(circle at 50% 52%, rgba(76,147,255,0.88), rgba(17,85,204,0.42) 38%, rgba(8,47,99,0.26) 64%, rgba(255,255,255,0.10));
    box-shadow: 0 0 42px rgba(76,147,255,0.58), inset -18px -22px 34px rgba(0,0,0,0.34), inset 16px 16px 28px rgba(255,255,255,0.16);
    opacity: 0.88; margin: 2px auto 22px;
    animation: readyPulse 3.2s ease-in-out infinite;
  }
  .ready-title {
    color: #e5e7eb; font-size: 20px; font-weight: 700; letter-spacing: 0;
    text-shadow: 0 12px 30px rgba(0,0,0,0.32);
  }
  .ready-copy {
    max-width: 470px; margin-top: 10px; color: #b7c4d6; font-size: 13px; line-height: 1.55;
  }
  @keyframes readyPulse {
    0%, 100% { transform: scale(0.98); box-shadow: 0 0 30px rgba(76,147,255,0.38), inset -18px -22px 34px rgba(0,0,0,0.34), inset 16px 16px 28px rgba(255,255,255,0.12); }
    50% { transform: scale(1.02); box-shadow: 0 0 58px rgba(76,147,255,0.70), inset -18px -22px 34px rgba(0,0,0,0.30), inset 16px 16px 28px rgba(255,255,255,0.16); }
  }
  .bottleneck-list { display: grid; gap: 9px; margin-top: 12px; }
  .bottleneck-row { display: grid; grid-template-columns: 1fr auto; gap: 10px; align-items: center; font-size: 13px; }
  .bottleneck-row strong { color: #4c93ff; }
  .executive-tools {
    display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-top: 12px;
  }
  .mini-btn {
    display: inline-flex; align-items: center; justify-content: center;
    min-height: 34px; padding: 8px 12px; border-radius: 8px;
    background: rgba(255,255,255,0.16); border: 1px solid rgba(255,255,255,0.34);
    color: white; text-decoration: none; font-size: 12px; font-weight: 800;
  }
  .health-card {
    background: rgba(255,255,255,0.12); border: 1px solid rgba(255,255,255,0.22);
    border-radius: 8px; padding: 12px; margin-top: 14px;
  }
  .health-card .score { font-size: 30px; font-weight: 900; color: #ffffff; }
  .health-card .status { font-size: 13px; color: #bfdbfe; font-weight: 900; }
  .health-meter { height: 8px; background: rgba(255,255,255,0.18); border-radius: 999px; overflow: hidden; margin-top: 10px; }
  .health-fill { height: 100%; background: linear-gradient(90deg, #8fc3ff, #ffffff); border-radius: inherit; }

  .exec-panel-grid { display: grid; grid-template-columns: 1.15fr 0.85fr; gap: 16px; margin-top: 16px; }
  .exec-card {
    background: linear-gradient(145deg, rgba(255,255,255,0.92), rgba(239,247,255,0.72));
    border: 1px solid rgba(143,195,255,0.48); border-radius: 12px; padding: 16px;
    box-shadow: 0 22px 50px rgba(8,47,99,0.12);
  }
  .exec-card h3 { font-size: 15px; color: #082f63; margin-bottom: 12px; }
  .action-list, .simple-list, .scorecard-list { display: grid; gap: 10px; }
  .action-item {
    display: grid; grid-template-columns: 78px 1fr auto; gap: 12px; align-items: start;
    padding: 12px 0; border-bottom: 1px solid rgba(199,221,246,0.64);
  }
  .action-item:last-child { border-bottom: 0; }
  .priority { color: #1155cc; font-size: 12px; font-weight: 900; text-transform: uppercase; }
  .priority strong { display: block; color: #061b3a; font-size: 20px; margin-top: 2px; }
  .action-title { color: #061b3a; font-size: 14px; font-weight: 900; overflow-wrap: anywhere; }
  .action-meta { color: #5d7899; font-size: 12px; line-height: 1.45; margin-top: 4px; }
  .action-side { text-align: right; color: #244a78; font-size: 12px; font-weight: 800; white-space: nowrap; }
  .explain-item, .warning-item, .scorecard-item {
    border: 1px solid rgba(17,85,204,0.12); background: rgba(255,255,255,0.58);
    border-radius: 8px; padding: 10px;
  }
  .explain-item strong, .warning-item strong, .scorecard-item strong { color: #082f63; }
  .explain-item span, .warning-item span, .scorecard-item span { color: #5d7899; font-size: 12px; line-height: 1.45; display: block; margin-top: 3px; }
  .decision-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-top: 16px; }
  .decision-tile {
    background: linear-gradient(145deg, rgba(255,255,255,0.94), rgba(231,243,255,0.58));
    border: 1px solid rgba(143,195,255,0.46);
    border-radius: 8px; padding: 12px; min-height: 92px;
    box-shadow: 0 14px 30px rgba(8,47,99,0.08);
  }
  .decision-tile span { display: block; color: #5d7899; font-size: 11px; font-weight: 900; text-transform: uppercase; }
  .decision-tile strong { display: block; margin-top: 7px; color: #061b3a; font-size: 22px; font-weight: 900; }
  .decision-tile small { display: block; margin-top: 4px; color: #5d7899; line-height: 1.35; }
  .compact-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .compact-table th { text-align: left; color: #082f63; font-size: 11px; text-transform: uppercase; border-bottom: 1px solid #c7ddf6; padding: 8px 6px; }
  .compact-table td { border-bottom: 1px solid rgba(199,221,246,0.64); padding: 8px 6px; color: #244a78; vertical-align: top; }
  /* Activity timeline */
  .timeline-card {
    background: linear-gradient(145deg, rgba(255,255,255,0.92), rgba(239,247,255,0.72));
    border: 1px solid rgba(143,195,255,0.48); border-radius: 12px; padding: 16px;
    box-shadow: 0 22px 50px rgba(8,47,99,0.12);
    backdrop-filter: blur(14px);
  }
  .timeline-head {
    display: flex; align-items: baseline; justify-content: space-between; gap: 16px;
    border-bottom: 1px solid rgba(143,195,255,0.38); padding-bottom: 12px; margin-bottom: 4px;
  }
  .timeline-head h3 { font-size: 15px; color: #082f63; }
  .timeline-head span { color: #5d7899; font-size: 12px; font-weight: 700; }
  .timeline-list { display: grid; }
  .timeline-item {
    display: grid; grid-template-columns: 132px 1fr auto; gap: 14px;
    padding: 14px 0; border-bottom: 1px solid rgba(199,221,246,0.64);
  }
  .timeline-item:last-child { border-bottom: 0; }
  .timeline-time { color: #456489; font-size: 12px; font-weight: 800; line-height: 1.35; }
  .timeline-main { min-width: 0; border-left: 4px solid var(--accent, #1155cc); padding-left: 12px; }
  .timeline-meta { display: flex; flex-wrap: wrap; gap: 7px; align-items: center; margin-bottom: 6px; }
  .timeline-chip {
    background: rgba(17,85,204,0.09); color: #082f63; border: 1px solid rgba(17,85,204,0.16);
    border-radius: 999px; padding: 3px 8px; font-size: 11px; font-weight: 800;
  }
  .timeline-action { color: #1155cc; font-size: 12px; font-weight: 900; }
  .timeline-title { color: #061b3a; font-weight: 900; font-size: 14px; overflow-wrap: anywhere; }
  .timeline-desc { color: #5d7899; font-size: 12px; line-height: 1.45; margin-top: 4px; }
  .timeline-side { text-align: right; color: #244a78; font-size: 12px; font-weight: 800; white-space: nowrap; }
  .timeline-side .status { color: #5d7899; margin-top: 5px; }
  .timeline-empty { padding: 20px 0 8px; color: #5d7899; font-size: 13px; }

  /* Pending aging */
  .aging-grid { display: grid; grid-template-columns: 1fr 1.2fr; gap: 16px; }
  .aging-card {
    background: linear-gradient(145deg, rgba(255,255,255,0.92), rgba(239,247,255,0.72));
    border: 1px solid rgba(143,195,255,0.48); border-radius: 12px; padding: 16px;
    box-shadow: 0 22px 50px rgba(8,47,99,0.12);
    backdrop-filter: blur(14px);
  }
  .aging-card h3 { font-size: 15px; color: #082f63; margin-bottom: 12px; }
  .aging-buckets { display: grid; gap: 10px; }
  .aging-row {
    display: grid; grid-template-columns: 82px 1fr auto; gap: 12px; align-items: center;
    padding: 10px 0; border-bottom: 1px solid rgba(199,221,246,0.64);
  }
  .aging-row:last-child { border-bottom: 0; }
  .aging-label { color: #082f63; font-size: 13px; font-weight: 900; }
  .aging-bar { height: 10px; background: #dbeafe; border-radius: 999px; overflow: hidden; }
  .aging-fill { height: 100%; min-width: 3px; background: linear-gradient(90deg, #1155cc, #4c93ff); border-radius: inherit; }
  .aging-count { text-align: right; color: #244a78; font-size: 12px; font-weight: 800; }
  .aging-amount { color: #5d7899; font-size: 11px; margin-top: 3px; }
  .aging-module-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-top: 10px; }
  .aging-module {
    border: 1px solid rgba(17,85,204,0.14); background: rgba(17,85,204,0.06);
    border-radius: 8px; padding: 8px; min-height: 54px;
  }
  .aging-module span { display: block; color: #5d7899; font-size: 10px; font-weight: 800; text-transform: uppercase; }
  .aging-module strong { display: block; color: #061b3a; font-size: 17px; margin-top: 4px; }
  .oldest-list { display: grid; }
  .oldest-item {
    display: grid; grid-template-columns: 76px 1fr auto; gap: 12px;
    padding: 12px 0; border-bottom: 1px solid rgba(199,221,246,0.64);
  }
  .oldest-item:last-child { border-bottom: 0; }
  .oldest-age { color: #1155cc; font-size: 16px; font-weight: 900; }
  .oldest-age span { display: block; color: #5d7899; font-size: 10px; text-transform: uppercase; }
  .oldest-main { min-width: 0; }
  .oldest-meta { color: #1155cc; font-size: 11px; font-weight: 900; margin-bottom: 4px; }
  .oldest-title { color: #061b3a; font-size: 13px; font-weight: 900; overflow-wrap: anywhere; }
  .oldest-desc { color: #5d7899; font-size: 12px; line-height: 1.4; margin-top: 3px; }
  .oldest-side { text-align: right; color: #244a78; font-size: 12px; font-weight: 800; white-space: nowrap; }
  .oldest-side .stage { color: #1155cc; margin-top: 4px; }

  /* KPI cards */
  .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; }
  .kpi {
    background: linear-gradient(145deg, rgba(255,255,255,0.90), rgba(231,243,255,0.66));
    border: 1px solid rgba(143,195,255,0.52); border-radius: 12px; padding: 18px 20px;
    box-shadow: 0 22px 50px rgba(8,47,99,0.13);
    border-top: 4px solid var(--accent, #1155cc);
    backdrop-filter: blur(16px);
  }
  .kpi .label { font-size: 12px; color: #4b6f9b; text-transform: uppercase; letter-spacing: 0.5px; font-weight: 800; }
  .kpi .value { font-size: 30px; font-weight: 900; color: #061b3a; margin: 6px 0 2px; }
  .kpi .sub   { font-size: 12px; color: #5d7899; }
  .amount-breakdown { margin-top: 10px; display: grid; gap: 5px; font-size: 12px; color: #456489; }
  .amount-breakdown div { display: flex; justify-content: space-between; gap: 12px; }
  .amount-breakdown strong { color: #082f63; font-weight: 800; }

  /* Chart grid */
  .chart-grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .chart-grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; }
  .chart-card {
    background: linear-gradient(145deg, rgba(255,255,255,0.92), rgba(239,247,255,0.72));
    border: 1px solid rgba(143,195,255,0.48); border-radius: 12px; padding: 16px;
    box-shadow: 0 22px 50px rgba(8,47,99,0.12);
    backdrop-filter: blur(14px);
  }
  .chart-card.full { grid-column: 1 / -1; }

  /* Approval table */
  .approval-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .approval-table th {
    background: #082f63; color: white;
    padding: 10px 14px; text-align: left;
  }
  .approval-table td { padding: 9px 14px; border-bottom: 1px solid #f0f0f0; }
  .approval-table tr:hover td { background: #f8f9fa; }
  .rate-bar {
    height: 8px; border-radius: 4px; background: #e0e0e0; overflow: hidden;
  }
  .rate-fill { height: 100%; border-radius: 4px; }

  /* Footer */
  .footer { text-align: center; padding: 24px; color: #aaa; font-size: 12px; }

  @media (max-width: 900px) {
    .filter-bar, .executive-grid, .signal-grid, .exec-panel-grid, .decision-grid, .aging-grid, .chart-grid-2, .chart-grid-3 { grid-template-columns: 1fr; }
    .aging-module-grid { grid-template-columns: repeat(2, 1fr); }
    .action-item { grid-template-columns: 1fr; gap: 8px; }
    .action-side { text-align: left; white-space: normal; }
    .oldest-item { grid-template-columns: 1fr; gap: 8px; }
    .oldest-side { text-align: left; white-space: normal; }
    .timeline-item { grid-template-columns: 1fr; gap: 8px; }
    .timeline-side { text-align: left; white-space: normal; }
    .filter-hint { grid-column: 1; text-align: left; }
  }
</style>
</head>
<body>

<div class="header">
  <div style="display:flex; align-items:center; gap:18px;">
    <div style="flex-shrink:0;height:52px;display:flex;align-items:center;">
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 90 44" width="90" height="44">
        <rect x="0" y="0" width="90" height="44" rx="12" fill="#29b6f6"/>
        <text x="45" y="31" text-anchor="middle" font-family="Arial Black,Arial,sans-serif"
              font-size="22" font-weight="900" fill="#ffffff" letter-spacing="2">CBC</text>
      </svg>
    </div>
    <div>
      <div style="font-size:11px; font-weight:600; letter-spacing:2px; opacity:0.7; text-transform:uppercase; margin-bottom:2px;">CBC · Powered by IGEZ</div>
      <div class="h1">IGEZ Analytics Dashboard</div>
      <div class="sub">CBC Paperless · Live data from MongoDB · Last refreshed: {{ now }}</div>
    </div>
  </div>
  <div style="display:flex; gap:10px; align-items:center;">
    <a href="/executive-report" class="refresh-btn" style="background:rgba(255,255,255,0.12);">&#x1F4C4; Executive Report</a>
    <a href="/" class="refresh-btn">&#x21BB; Refresh</a>
  </div>
</div>

<div class="container">

  <form class="filter-bar" method="get" id="filterForm">
    <label>
      Period
      <select name="period" id="periodFilter" {% if filters.date_selection_active %}class="period-disabled" disabled{% endif %}>
        {% if filters.date_selection_active %}
        <option value="date_active" selected>Date range active</option>
        {% endif %}
        <option value="today" {% if filters.period == "today" and not filters.date_selection_active %}selected{% endif %}>Today</option>
        <option value="week" {% if filters.period == "week" and not filters.date_selection_active %}selected{% endif %}>This week</option>
        <option value="month" {% if filters.period == "month" and not filters.date_selection_active %}selected{% endif %}>This month</option>
        <option value="quarter" {% if filters.period == "quarter" and not filters.date_selection_active %}selected{% endif %}>This quarter</option>
        <option value="year" {% if filters.period == "year" and not filters.date_selection_active %}selected{% endif %}>This year</option>
        <option value="all" {% if filters.period == "all" and not filters.date_selection_active %}selected{% endif %}>All time</option>
        <option value="cancel_period" class="period-cancel-option">Cancel</option>
      </select>
    </label>
    <label>
      Subsidiary
      <select name="subsidiary">
        <option value="All">All subsidiaries</option>
        {% for sub in sub_options %}
        <option value="{{ sub }}" {% if filters.subsidiary == sub %}selected{% endif %}>{{ sub }}</option>
        {% endfor %}
      </select>
    </label>
    <label>
      From
      <input type="{% if filters.period_filter_active %}text{% else %}date{% endif %}" name="start" id="startDateFilter" value="{% if filters.period_filter_active %}Period active{% else %}{{ filters.start }}{% endif %}" data-date-value="{{ filters.start }}" {% if filters.period_filter_active %}disabled{% endif %}>
      <span class="filter-alert" id="startDateHint">{% if filters.period_filter_active %}Period active{% elif filters.date_range_incomplete and not filters.start %}Select From{% endif %}</span>
    </label>
    <label>
      To
      <input type="{% if filters.period_filter_active %}text{% else %}date{% endif %}" name="end" id="endDateFilter" value="{% if filters.period_filter_active %}Period active{% else %}{{ filters.end }}{% endif %}" data-date-value="{{ filters.end }}" {% if filters.period_filter_active %}disabled{% endif %}>
      <span class="filter-alert" id="endDateHint">{% if filters.period_filter_active %}Period active{% elif filters.date_range_incomplete and not filters.end %}Select To{% endif %}</span>
    </label>
    <button type="submit" id="applyFilters" {% if filters.date_range_incomplete or filters.date_range_invalid %}disabled{% endif %}>Apply</button>
    <div class="filter-hint" id="filterHint">{% if filters.date_range_invalid %}From date cannot be later than To date.{% elif filters.date_range_incomplete %}Select both From and To dates before applying.{% endif %}</div>
  </form>

  <div class="executive-grid">
    <div class="signal-grid">
      <div class="signal" style="--accent:#1155cc">
        <div class="label">Approved Spend</div>
        <div class="value">₦{{ "{:,.0f}".format(d.approved_spend) }}</div>
        <div class="sub">Cleared across CA, EC and RTPS</div>
      </div>
      <div class="signal" style="--accent:#1d6ff2">
        <div class="label">Waiting Approval</div>
        <div class="value">₦{{ "{:,.0f}".format(d.pending_spend) }}</div>
        <div class="sub">{{ d.highest_pending_module }} is the largest queue</div>
      </div>
      <div class="signal" style="--accent:#4c93ff">
        <div class="label">This Month</div>
        <div class="value">₦{{ "{:,.0f}".format(d.month_compare.spend) }}</div>
        <div class="sub">Spend {{ d.month_compare.spend_change }} vs last month</div>
      </div>
      <div class="signal" style="--accent:#0b3a75">
        <div class="label">This Month Requests</div>
        <div class="value">{{ d.month_compare.requests }}</div>
        <div class="sub">Volume {{ d.month_compare.requests_change }} vs last month</div>
      </div>
      <div class="signal" style="--accent:#8fc3ff">
        <div class="label">Top Subsidiary</div>
        <div class="value">{{ d.top_spending_subsidiary }}</div>
        <div class="sub">₦{{ "{:,.0f}".format(d.top_spending_subsidiary_amount) }}</div>
      </div>
      <div class="signal" style="--accent:#082f63">
        <div class="label">Approval Watch</div>
        <div class="value">{{ d.lowest_approval_label }}</div>
        <div class="sub">{{ d.lowest_approval_rate }}% approval rate</div>
      </div>
    </div>
    <div class="insight-card">
      <h3>Executive Insights</h3>
      {% if d.system_ready %}
      <div class="ready-state">
        <div>
          <div class="ready-orb" aria-hidden="true"></div>
          <div class="ready-title">Current Status: Optimal.</div>
          <div class="ready-copy">Your dashboard is up to date with no pending actions. Real-time spending and approval insights will populate here as requests arrive.</div>
        </div>
      </div>
      {% else %}
      <ul>
        {% for insight in d.insights %}
        <li>{{ insight }}</li>
        {% endfor %}
      </ul>
      <div class="bottleneck-list">
        {% for item in d.bottlenecks %}
        <div class="bottleneck-row"><span>{{ item.label }}<br><small>{{ item.impact }}</small></span><strong>{{ item.count }}</strong></div>
        {% endfor %}
      </div>
      <div class="health-card">
        <div class="status">Platform Health: {{ d.health.status }}</div>
        <div class="score">{{ d.health.score }}/100</div>
        <div class="ready-copy">{{ d.health.tone }} Pending exposure is {{ d.health.pending_ratio }}% of total spend.</div>
        <div class="health-meter"><div class="health-fill" style="width:{{ d.health.score }}%"></div></div>
      </div>
      <div class="executive-tools">
        <a class="mini-btn" href="/executive-report{% if report_query %}?{{ report_query }}{% endif %}">Download Executive PDF</a>
      </div>
      {% endif %}
    </div>
  </div>

  <!-- Executive decision layer -->
  <div class="exec-panel-grid">
    <div class="exec-card">
      <h3>Action Required Today</h3>
      {% if d.action_required %}
      <div class="action-list">
        {% for item in d.action_required %}
        <div class="action-item">
          <div class="priority">Priority<strong>{{ loop.index }}</strong></div>
          <div>
            <div class="action-title">{{ item.title }}</div>
            <div class="action-meta">{{ item.module }} - {{ item.subsidiary }} - {{ item.why }}</div>
            {% if item.description %}<div class="action-meta">{{ item.description }}</div>{% endif %}
          </div>
          <div class="action-side">
            {% if item.module == "Leave" %}
              <div>Nill</div>
              <div>{{ item.leave_type }}{% if item.leave_days_applied %} · {{ item.leave_days_applied }} day(s){% endif %}</div>
            {% elif item.amount %}<div>{{ item.amount }}</div>{% endif %}
            <div>Score {{ item.priority_score }}</div>
          </div>
        </div>
        {% endfor %}
      </div>
      {% else %}
      <div class="timeline-empty">No urgent pending actions matched the current filters.</div>
      {% endif %}
    </div>
    <div class="exec-card">
      <h3>Beginner Guide</h3>
      <div class="simple-list">
        {% for item in d.beginner_explainers %}
        <div class="explain-item"><strong>{{ item.term }}</strong><span>{{ item.meaning }}</span></div>
        {% endfor %}
      </div>
    </div>
  </div>

  <div class="exec-panel-grid">
    <div class="exec-card">
      <h3>Subsidiary Scorecards</h3>
      <div class="scorecard-list">
        {% if d.subsidiary_scorecards %}
        {% for item in d.subsidiary_scorecards %}
        <div class="scorecard-item">
          <strong>{{ item.subsidiary }}</strong>
          <span>{{ item.spend_label }} total spend - {{ item.pending_label }} waiting - {{ item.pending_count }} pending - oldest {{ item.oldest_days }} days</span>
        </div>
        {% endfor %}
        {% else %}
        <div class="scorecard-item"><strong>No active subsidiaries</strong><span>No subsidiary has spend or pending activity in this view.</span></div>
        {% endif %}
      </div>
    </div>
    <div class="exec-card">
      <h3>Spend Risk Alerts</h3>
      <div class="simple-list">
        {% if d.risk_alerts %}
          {% for alert in d.risk_alerts %}
          <div class="warning-item"><strong>Alert</strong><span>{{ alert }}</span></div>
          {% endfor %}
        {% else %}
          <div class="warning-item"><strong>Stable</strong><span>No major spend risk alerts in this view.</span></div>
        {% endif %}
      </div>
    </div>
  </div>

  <div class="exec-card" style="margin-top:16px">
      <h3>Data Quality Warnings</h3>
      <div class="simple-list">
        {% if d.data_quality_warnings %}
          {% for warning in d.data_quality_warnings %}
          <div class="warning-item"><strong>Review</strong><span>{{ warning }}</span></div>
          {% endfor %}
        {% else %}
          <div class="warning-item"><strong>Clean</strong><span>No major data-quality warnings in this view.</span></div>
        {% endif %}
      </div>
  </div>

  <div class="section-title">Decision Cockpit</div>
  <div class="exec-card">
    <h3>What changed recently</h3>
    <div class="decision-grid">
      <div class="decision-tile"><span>Window</span><strong>{{ d.what_changed.window }}</strong><small>Recent platform movement</small></div>
      <div class="decision-tile"><span>New Requests</span><strong>{{ d.what_changed.new_requests }}</strong><small>Created recently</small></div>
      <div class="decision-tile"><span>Approvals</span><strong>{{ d.what_changed.approvals }}</strong><small>Approval actions completed</small></div>
      <div class="decision-tile"><span>Rejections</span><strong>{{ d.what_changed.rejections }}</strong><small>Items sent back or declined</small></div>
    </div>
  </div>

  <div class="exec-panel-grid">
    <div class="exec-card">
      <h3>Rejection Analytics</h3>
      <table class="compact-table">
        <thead><tr><th>Module</th><th>Rate</th><th>Rejected Count / Value</th></tr></thead>
        <tbody>
          {% for row in d.rejection_analytics %}
          <tr><td>{{ row.module }}</td><td>{{ row.rate }}%</td><td>{{ row.metric_label }}</td></tr>
          {% endfor %}
        </tbody>
      </table>
      <div class="simple-list" style="margin-top:10px">
        {% if d.rejection_hotspots %}
          {% for row in d.rejection_hotspots %}
          <div class="warning-item"><strong>{{ row.label }}</strong><span>{{ row.count }} rejected items</span></div>
          {% endfor %}
        {% else %}
          <div class="warning-item"><strong>Stable</strong><span>No rejection hotspots in this view.</span></div>
        {% endif %}
      </div>
    </div>
    <div class="exec-card">
      <h3>Month-End Close View</h3>
      <div class="decision-grid" style="grid-template-columns:1fr 1fr">
        <div class="decision-tile"><span>Days Left</span><strong>{{ d.month_end_close.days_left }}</strong><small>To month end</small></div>
        <div class="decision-tile"><span>Pending</span><strong>{{ d.month_end_close.pending_label }}</strong><small>May affect close</small></div>
        <div class="decision-tile"><span>Current Spend</span><strong>{{ d.month_end_close.approved_label }}</strong><small>Month-to-date exposure</small></div>
        <div class="decision-tile"><span>Blockers</span><strong>{{ d.month_end_close.blockers }}</strong><small>Pending 8+ days</small></div>
      </div>
    </div>
  </div>

  <div class="exec-panel-grid">
    <div class="exec-card">
      <h3>Duplicate or Suspicious Requests</h3>
      <div class="simple-list">
        {% if d.suspicious_requests %}
          {% for item in d.suspicious_requests %}
          <div class="warning-item"><strong>{{ item.owner }} - {{ item.amount_label }}</strong><span>{{ item.module }} - {{ item.reason }}</span></div>
          {% endfor %}
        {% else %}
          <div class="warning-item"><strong>Clean</strong><span>No same-owner, same-amount repeat requests detected for the selected period.</span></div>
        {% endif %}
      </div>
    </div>
    <div class="exec-card">
      <h3>Staff Request Behavior</h3>
      <table class="compact-table">
        <thead><tr><th>Requester</th><th>Requests</th><th>Pending</th><th>Value</th></tr></thead>
        <tbody>
          {% for item in d.staff_behavior %}
          <tr><td>{{ item.name }}<br><small>{{ item.main_module }}</small></td><td>{{ item.count }}</td><td>{{ item.pending }}</td><td>{{ item.amount_label }}</td></tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>

  <div class="section-title">Activity Timeline</div>
  <div class="timeline-card">
    <div class="timeline-head">
      <h3>Latest platform activity</h3>
      <span>{{ d.activity_total }} matching events</span>
    </div>
    {% if d.activity_timeline %}
    <div class="timeline-list">
      {% for item in d.activity_timeline %}
      <div class="timeline-item" style="--accent:{{ item.accent }}">
        <div class="timeline-time">{{ item.time_label }}</div>
        <div class="timeline-main">
          <div class="timeline-meta">
            <span class="timeline-chip">{{ item.module }}</span>
            <span class="timeline-action">{{ item.action }}</span>
          </div>
          <div class="timeline-title">{{ item.title }}</div>
          {% if item.description %}
          <div class="timeline-desc">{{ item.description }}</div>
          {% endif %}
        </div>
        <div class="timeline-side">
          <div>{{ item.subsidiary }}</div>
          {% if item.amount %}<div>{{ item.amount }}</div>{% endif %}
          <div class="status">{{ item.status }}</div>
        </div>
      </div>
      {% endfor %}
    </div>
    {% else %}
    <div class="timeline-empty">No activity matched the current filters.</div>
    {% endif %}
  </div>

  <!-- Pending approval aging -->
  <div class="section-title">Pending Approval Aging</div>
  <div class="aging-grid">
    <div class="aging-card">
      <h3>{{ d.pending_aging_total }} pending requests by age</h3>
      <div class="aging-buckets">
        {% for row in d.pending_aging %}
        <div class="aging-row">
          <div class="aging-label">{{ row.label }}</div>
          <div>
            <div class="aging-bar"><div class="aging-fill" style="width:{{ row.share }}%"></div></div>
            <div class="aging-amount">{{ row.amount_label }}</div>
          </div>
          <div class="aging-count">{{ row.count }} - {{ row.share }}%</div>
        </div>
        <div class="aging-module-grid">
          <div class="aging-module"><span>Leave</span><strong>{{ row.leave }}</strong></div>
          <div class="aging-module"><span>Cash Adv</span><strong>{{ row.cash_advance }}</strong></div>
          <div class="aging-module"><span>Expense</span><strong>{{ row.expense_claim }}</strong></div>
          <div class="aging-module"><span>RTPS</span><strong>{{ row.rtps }}</strong></div>
        </div>
        {% endfor %}
      </div>
    </div>
    <div class="aging-card">
      <h3>Oldest pending requests{% if d.oldest_pending_days %} - {{ d.oldest_pending_days }} days max{% endif %}</h3>
      {% if d.oldest_pending %}
      <div class="oldest-list">
        {% for item in d.oldest_pending %}
        <div class="oldest-item">
          <div class="oldest-age">{{ item.days }}<span>days</span></div>
          <div class="oldest-main">
            <div class="oldest-meta">{{ item.module }} - {{ item.created }}</div>
            <div class="oldest-title">{{ item.title }}</div>
            {% if item.description %}<div class="oldest-desc">{{ item.description }}</div>{% endif %}
          </div>
          <div class="oldest-side">
            <div>{{ item.subsidiary }}</div>
            {% if item.module == "Leave" %}
              <div>Nill</div>
              {% if item.leave_days_applied %}<div>{{ item.leave_days_applied }} day(s) applied</div>{% endif %}
            {% elif item.amount %}<div>{{ item.amount }}</div>{% endif %}
            <div class="stage">Waiting: {{ item.waiting_on }}</div>
          </div>
        </div>
        {% endfor %}
      </div>
      {% else %}
      <div class="timeline-empty">No pending approvals matched the current filters.</div>
      {% endif %}
    </div>
  </div>

  <!-- KPI overview -->
  <div class="section-title">Overview</div>
  <div class="kpi-grid">
    <div class="kpi" style="--accent:#1155cc">
      <div class="label">Leave Requests</div>
      <div class="value">{{ d.leave_approved }}/{{ d.leave_total }}</div>
      <div class="sub">{{ d.leave_approved }} approved · {{ d.leave_pending }} pending · {{ d.leave_rejected }} rejected</div>
      <div class="amount-breakdown">
        <div><span>Days applied</span><strong>{{ d.leave_days_applied }}</strong></div>
        <div><span>Days approved</span><strong>{{ d.leave_days_approved }}</strong></div>
        <div><span>Days pending</span><strong>{{ d.leave_days_pending }}</strong></div>
      </div>
    </div>
    <div class="kpi" style="--accent:#1d6ff2">
      <div class="label">Cash Advances</div>
      <div class="value">{{ d.ca_approved }}/{{ d.ca_total }}</div>
      <div class="sub">₦{{ "{:,.0f}".format(d.ca_amount) }} total</div>
      <div class="amount-breakdown">
        <div><span>Approved</span><strong>₦{{ "{:,.0f}".format(d.ca_approved_amount) }}</strong></div>
        <div><span>Waiting approval</span><strong>₦{{ "{:,.0f}".format(d.ca_pending_amount) }}</strong></div>
      </div>
    </div>
    <div class="kpi" style="--accent:#4c93ff">
      <div class="label">Expense Claims</div>
      <div class="value">{{ d.ec_approved }}/{{ d.ec_total }}</div>
      <div class="sub">₦{{ "{:,.0f}".format(d.ec_amount) }} total</div>
      <div class="amount-breakdown">
        <div><span>Approved</span><strong>₦{{ "{:,.0f}".format(d.ec_approved_amount) }}</strong></div>
        <div><span>Waiting approval</span><strong>₦{{ "{:,.0f}".format(d.ec_pending_amount) }}</strong></div>
      </div>
    </div>
    <div class="kpi" style="--accent:#0b3a75">
      <div class="label">RTPS</div>
      <div class="value">{{ d.rtps_approved }}/{{ d.rtps_total }}</div>
      <div class="sub">₦{{ "{:,.0f}".format(d.rtps_amount) }} total</div>
      <div class="amount-breakdown">
        <div><span>Approved</span><strong>₦{{ "{:,.0f}".format(d.rtps_approved_amount) }}</strong></div>
        <div><span>Waiting approval</span><strong>₦{{ "{:,.0f}".format(d.rtps_pending_amount) }}</strong></div>
      </div>
    </div>
    <div class="kpi" style="--accent:#8fc3ff">
      <div class="label">Total Requests</div>
      <div class="value">{{ d.total_approved_requests }}/{{ d.total_requests }}</div>
      <div class="sub">Approved across all modules</div>
    </div>
    <div class="kpi" style="--accent:#082f63">
      <div class="label">Total Spend</div>
      <div class="value" style="font-size:20px">₦{{ "{:,.0f}".format(d.total_spend) }}</div>
      <div class="sub">CA + EC + RTPS</div>
      <div class="amount-breakdown">
        <div><span>Approved</span><strong>₦{{ "{:,.0f}".format(d.ca_approved_amount + d.ec_approved_amount + d.rtps_approved_amount) }}</strong></div>
        <div><span>Waiting approval</span><strong>₦{{ "{:,.0f}".format(d.ca_pending_amount + d.ec_pending_amount + d.rtps_pending_amount) }}</strong></div>
      </div>
    </div>
  </div>

  <!-- Leave requests -->
  <div class="section-title">Leave Requests</div>
  <div class="chart-grid-3">
    <div class="chart-card">{{ charts.leave_by_sub | safe }}</div>
    <div class="chart-card">{{ charts.leave_by_type | safe }}</div>
    <div class="chart-card">{{ charts.leave_status | safe }}</div>
  </div>
  <div class="chart-grid-2" style="margin-top:16px">
    <div class="chart-card full">{{ charts.leave_trend | safe }}</div>
  </div>
  <div class="chart-grid-2" style="margin-top:16px">
    <div class="chart-card full">{{ charts.leave_days_by_type | safe }}</div>
  </div>

  <!-- Cash advance -->
  <div class="section-title">Cash Advance</div>
  <div class="chart-grid-2">
    <div class="chart-card">{{ charts.ca_by_sub | safe }}</div>
    <div class="chart-card">{{ charts.ca_status | safe }}</div>
  </div>
  <div class="chart-grid-2" style="margin-top:16px">
    <div class="chart-card full">{{ charts.ca_trend | safe }}</div>
  </div>

  <!-- Expense claims -->
  <div class="section-title">Expense Claims</div>
  <div class="chart-grid-2">
    <div class="chart-card">{{ charts.ec_by_sub | safe }}</div>
    <div class="chart-card">{{ charts.ec_status | safe }}</div>
  </div>
  <div class="chart-grid-2" style="margin-top:16px">
    <div class="chart-card full">{{ charts.ec_trend | safe }}</div>
  </div>

  <!-- RTPS -->
  <div class="section-title">Request to Pay Supplier (RTPS)</div>
  <div class="chart-grid-2">
    <div class="chart-card">{{ charts.rtps_by_sub | safe }}</div>
    <div class="chart-card">{{ charts.rtps_by_mode | safe }}</div>
  </div>
  <div class="chart-grid-2" style="margin-top:16px">
    <div class="chart-card">{{ charts.rtps_suppliers | safe }}</div>
    <div class="chart-card">{{ charts.rtps_trend | safe }}</div>
  </div>

  <!-- Approval rates -->
  <div class="section-title">Approval Rates</div>
  <div class="chart-card">
    <table class="approval-table">
      <thead>
        <tr><th>Approver / Module</th><th>Approval Rate</th><th style="width:200px">Progress</th></tr>
      </thead>
      <tbody>
        {% for label, rate in d.approval_rates.items() %}
        <tr>
          <td>{{ label }}</td>
          <td><strong>{{ rate }}%</strong></td>
          <td>
            <div class="rate-bar">
              <div class="rate-fill" style="width:{{ rate }}%;
                background:{% if rate >= 75 %}#1155cc{% elif rate >= 50 %}#4c93ff{% else %}#8fc3ff{% endif %}">
              </div>
            </div>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

</div>

<div class="footer">IGEZ Analytics Dashboard - Data live from MongoDB Atlas</div>
<script>
  const periodFilter = document.getElementById("periodFilter");
  const filterForm = document.getElementById("filterForm");
  const applyFilters = document.getElementById("applyFilters");
  const filterHint = document.getElementById("filterHint");
  const startDateHint = document.getElementById("startDateHint");
  const endDateHint = document.getElementById("endDateHint");
  const periodDisabledValue = "date_active";
  const cancelPeriodValue = "cancel_period";
  let previousPeriodValue = periodFilter.value === periodDisabledValue ? "all" : periodFilter.value;
  const dateFilters = [
    document.getElementById("startDateFilter"),
    document.getElementById("endDateFilter")
  ];

  function getDisabledPeriodOption() {
    let option = periodFilter.querySelector(`option[value="${periodDisabledValue}"]`);
    if (!option) {
      option = new Option("Date range active", periodDisabledValue);
      periodFilter.insertBefore(option, periodFilter.firstChild);
    }
    return option;
  }

  function syncPeriodFilter() {
    if (periodFilter.value === cancelPeriodValue) {
      previousPeriodValue = "all";
      periodFilter.value = "all";
    }
    const dateSelectionActive = dateFilters.some((input) => input.dataset.dateValue);
    const dateRangeIncomplete = dateFilters.filter((input) => input.dataset.dateValue).length === 1;
    const dateRangeInvalid = dateFilters.every((input) => input.dataset.dateValue)
      && dateFilters[0].dataset.dateValue > dateFilters[1].dataset.dateValue;
    const periodFilterActive = periodFilter.value !== "all" && periodFilter.value !== periodDisabledValue;
    if (dateSelectionActive && periodFilter.value !== periodDisabledValue) {
      previousPeriodValue = periodFilter.value;
    }
    periodFilter.disabled = dateSelectionActive;
    if (dateSelectionActive) {
      getDisabledPeriodOption().selected = true;
      periodFilter.classList.add("period-disabled");
    } else {
      const disabledOption = periodFilter.querySelector(`option[value="${periodDisabledValue}"]`);
      if (disabledOption) disabledOption.remove();
      periodFilter.classList.remove("period-disabled");
      periodFilter.value = previousPeriodValue;
    }
    dateFilters.forEach((input) => {
      input.disabled = periodFilterActive;
      if (periodFilterActive) {
        input.type = "text";
        input.value = "Period active";
      } else {
        input.type = "date";
        input.value = input.dataset.dateValue;
      }
    });
    startDateHint.textContent = "";
    endDateHint.textContent = "";
    if (dateRangeInvalid) {
      filterHint.textContent = "From date cannot be later than To date.";
    } else if (dateRangeIncomplete) {
      filterHint.textContent = "Select both From and To dates before applying.";
    } else {
      filterHint.textContent = "";
    }
    applyFilters.disabled = dateRangeIncomplete || dateRangeInvalid;
  }

  dateFilters.forEach((input) => input.addEventListener("input", () => {
    input.dataset.dateValue = input.value;
    syncPeriodFilter();
  }));
  periodFilter.addEventListener("change", () => {
    if (periodFilter.value !== "all" && periodFilter.value !== periodDisabledValue && periodFilter.value !== cancelPeriodValue) {
      previousPeriodValue = periodFilter.value;
      dateFilters.forEach((input) => {
        input.dataset.dateValue = "";
        input.value = "";
      });
    } else if (periodFilter.value === "all") {
      previousPeriodValue = "all";
    }
    syncPeriodFilter();
  });
  syncPeriodFilter();
</script>
</body>
</html>
"""

EXEC_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>IGEZ Executive Analytics</title>
<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #f5f7fb; --panel: #ffffff; --ink: #132033; --muted: #667085;
    --line: #dde5ef; --brand: #174ea6; --brand-2: #0f766e;
    --warn: #b45309; --danger: #b42318; --good: #027a48;
    --shadow: 0 16px 40px rgba(16, 24, 40, 0.09);
  }
  body.dark {
    --bg: #0f172a; --panel: #182235; --ink: #f8fafc; --muted: #a8b3c7;
    --line: #2a374d; --shadow: 0 16px 40px rgba(0, 0, 0, 0.24);
  }
  body {
    font-family: "Segoe UI", Arial, sans-serif;
    background: var(--bg);
    color: var(--ink);
    min-width: 320px;
  }
  a { color: inherit; text-decoration: none; }
  button, input, select { font: inherit; }
  .app-shell { display: block; min-height: 100vh; }
  .main { min-width: 0; }
  .topbar {
    position: sticky; top: 0; z-index: 20; background: rgba(245,247,251,0.92);
    backdrop-filter: blur(18px); border-bottom: 1px solid var(--line);
  }
  body.dark .topbar { background: rgba(15,23,42,0.92); }
  .topbar-inner {
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    padding: 12px 18px;
  }
  .top-actions { display: flex; gap: 8px; align-items: center; }
  .icon-btn, .btn, .chip {
    border: 1px solid var(--line); background: var(--panel); color: var(--ink);
    border-radius: 8px; min-height: 36px; padding: 8px 11px; cursor: pointer;
    font-size: 12px; font-weight: 800;
  }
  .btn.primary { background: var(--brand); color: white; border-color: var(--brand); }
  .badge { display: inline-flex; align-items: center; justify-content: center; min-width: 21px; height: 21px; border-radius: 999px; background: var(--danger); color: white; font-size: 11px; }
  .tabs {
    display: flex; gap: 6px; overflow-x: auto; padding: 0 18px 12px; scrollbar-width: thin;
  }
  .tabs a {
    white-space: nowrap; border: 1px solid var(--line); background: var(--panel);
    border-radius: 999px; padding: 7px 11px; color: var(--muted); font-size: 12px; font-weight: 800;
  }
  .tabs a:hover, .tabs a.active { color: white; background: var(--brand); border-color: var(--brand); }
  .content { padding: 18px; max-width: 1560px; margin: 0 auto; }
  /* Executive Insights full-width card */
  .exec-insight-card {
    border-radius: 16px;
    overflow: hidden;
    box-shadow: 0 24px 60px rgba(16,24,40,0.16);
    margin-bottom: 4px;
  }
  .exec-insight-empty {
    background: linear-gradient(160deg, #061b3a 0%, #082f63 55%, #0e3f85 100%);
    display: grid; place-items: center; text-align: center;
    padding: 56px 32px 48px; min-height: 340px;
  }
  .ready-orb {
    width: 136px; height: 136px; border-radius: 50%;
    background:
      radial-gradient(circle at 32% 28%, rgba(255,255,255,0.82), transparent 0 12%, transparent 26%),
      radial-gradient(circle at 50% 52%, rgba(76,147,255,0.88), rgba(17,85,204,0.42) 38%, rgba(8,47,99,0.26) 64%, rgba(255,255,255,0.10));
    box-shadow: 0 0 52px rgba(76,147,255,0.65), inset -18px -22px 34px rgba(0,0,0,0.34), inset 16px 16px 28px rgba(255,255,255,0.16);
    opacity: 0.92; margin: 0 auto 24px;
    animation: readyPulse 3.2s ease-in-out infinite;
  }
  .ready-title {
    color: #e5e7eb; font-size: 22px; font-weight: 700; letter-spacing: 0;
    text-shadow: 0 12px 30px rgba(0,0,0,0.32); margin-bottom: 10px;
  }
  .ready-copy {
    max-width: 520px; margin: 0 auto; color: #b7c4d6; font-size: 14px; line-height: 1.6;
  }
  @keyframes readyPulse {
    0%, 100% { transform: scale(0.97); box-shadow: 0 0 32px rgba(76,147,255,0.40), inset -18px -22px 34px rgba(0,0,0,0.34), inset 16px 16px 28px rgba(255,255,255,0.12); }
    50% { transform: scale(1.03); box-shadow: 0 0 68px rgba(76,147,255,0.78), inset -18px -22px 34px rgba(0,0,0,0.28), inset 16px 16px 28px rgba(255,255,255,0.18); }
  }
  .exec-insight-active {
    background: linear-gradient(135deg, #061b3a 0%, #082f63 45%, #0e3f85 78%, #1155cc 100%);
    padding: 28px 32px 24px;
    color: white;
  }
  .ei-label {
    font-size: 11px; font-weight: 900; letter-spacing: 2px; text-transform: uppercase;
    color: #8fc3ff; margin-bottom: 6px;
  }
  .exec-insight-empty .ei-label { margin-bottom: 28px; }
  .ei-header {
    display: flex; align-items: flex-start; justify-content: space-between; gap: 16px;
    margin-bottom: 14px; flex-wrap: wrap;
  }
  .ei-health-badge {
    display: inline-block; margin-top: 6px; padding: 4px 12px; border-radius: 999px;
    font-size: 12px; font-weight: 900; letter-spacing: 0.5px;
  }
  .ei-health-good { background: rgba(2,122,72,0.28); color: #6ee7b7; border: 1px solid rgba(110,231,183,0.3); }
  .ei-health-watch { background: rgba(180,83,9,0.28); color: #fcd34d; border: 1px solid rgba(252,211,77,0.3); }
  .ei-health-needs-attention { background: rgba(180,35,24,0.28); color: #fca5a5; border: 1px solid rgba(252,165,165,0.3); }
  .ei-pdf-btn {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 9px 16px; border-radius: 8px; font-size: 12px; font-weight: 800;
    background: rgba(255,255,255,0.14); border: 1px solid rgba(255,255,255,0.28);
    color: white; text-decoration: none; white-space: nowrap;
    transition: background 0.18s;
  }
  .ei-pdf-btn:hover { background: rgba(255,255,255,0.24); }
  .ei-meter-wrap {
    display: flex; align-items: center; gap: 14px; margin-bottom: 22px; flex-wrap: wrap;
  }
  .ei-meter {
    flex: 1; min-width: 160px; height: 7px; background: rgba(255,255,255,0.14);
    border-radius: 999px; overflow: hidden;
  }
  .ei-meter-fill {
    height: 100%; border-radius: inherit;
    background: linear-gradient(90deg, #4c93ff, #8fc3ff, #ffffff);
  }
  .ei-meter-note { color: #b7c4d6; font-size: 12px; line-height: 1.4; }
  .ei-body {
    display: grid; grid-template-columns: 1.1fr 0.9fr 1fr; gap: 24px;
  }
  .ei-col-title {
    font-size: 11px; font-weight: 900; letter-spacing: 1.5px; text-transform: uppercase;
    color: #8fc3ff; margin-bottom: 12px;
  }
  .ei-insights-list {
    list-style: none; padding: 0; margin: 0; display: grid; gap: 0;
  }
  .ei-insights-list li {
    font-size: 13px; color: #dbeafe; line-height: 1.5;
    padding: 9px 0; border-bottom: 1px solid rgba(255,255,255,0.10);
    display: flex; gap: 8px; align-items: flex-start;
  }
  .ei-insights-list li::before {
    content: "›"; color: #4c93ff; font-size: 15px; flex-shrink: 0; margin-top: -1px;
  }
  .ei-insights-list li:last-child { border-bottom: 0; }
  .ei-stat-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 12px;
  }
  .ei-stat {
    background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.12);
    border-radius: 10px; padding: 12px 14px;
  }
  .ei-stat span { display: block; font-size: 10px; font-weight: 900; text-transform: uppercase; letter-spacing: 0.8px; color: #8fc3ff; margin-bottom: 6px; }
  .ei-stat strong { display: block; font-size: 16px; font-weight: 900; color: #ffffff; line-height: 1.2; overflow-wrap: anywhere; }
  .ei-bottleneck-list { display: grid; gap: 0; }
  .ei-bottleneck-row {
    display: flex; justify-content: space-between; align-items: center; gap: 12px;
    padding: 9px 0; border-bottom: 1px solid rgba(255,255,255,0.10);
  }
  .ei-bottleneck-row:last-child { border-bottom: 0; }
  .ei-bottleneck-info { min-width: 0; }
  .ei-bottleneck-label { display: block; font-size: 13px; font-weight: 800; color: #dbeafe; }
  .ei-bottleneck-impact { display: block; font-size: 11px; color: #8fc3ff; margin-top: 2px; line-height: 1.35; }
  .ei-bottleneck-count {
    flex-shrink: 0; min-width: 32px; height: 32px;
    display: flex; align-items: center; justify-content: center;
    background: rgba(76,147,255,0.22); border: 1px solid rgba(76,147,255,0.4);
    border-radius: 8px; font-size: 15px; font-weight: 900; color: #ffffff;
  }
  /* Filter context strip inside executive insights */
  .ei-filter-strip {
    display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px;
  }
  .ei-filter-chip {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 4px 11px; border-radius: 999px; font-size: 11px; font-weight: 900;
    background: rgba(255,255,255,0.14); border: 1px solid rgba(255,255,255,0.26);
    color: #dbeafe; letter-spacing: 0.3px;
  }
  /* Rich search result cards */
  .suggestion {
    padding: 9px 10px; border-radius: 7px; color: var(--ink); cursor: pointer; font-size: 13px;
  }
  .suggestion:hover { background: rgba(23,78,166,0.10); }
  .suggestion.rich-result {
    display: grid; grid-template-columns: auto 1fr auto; gap: 10px; align-items: start;
    padding: 10px 12px; border-radius: 8px; border-bottom: 1px solid var(--line);
  }
  .suggestion.rich-result:last-child { border-bottom: 0; }
  .suggestion.rich-result:hover { background: rgba(23,78,166,0.07); }
  .sr-module {
    min-width: 68px; padding: 3px 7px; border-radius: 6px; text-align: center;
    font-size: 10px; font-weight: 900; text-transform: uppercase; letter-spacing: 0.5px;
    background: rgba(23,78,166,0.12); color: var(--brand);
  }
  .sr-body { min-width: 0; }
  .sr-staff { font-size: 13px; font-weight: 800; color: var(--ink); overflow-wrap: anywhere; }
  .sr-meta { font-size: 11px; color: var(--muted); margin-top: 2px; line-height: 1.35; }
  .sr-right { text-align: right; white-space: nowrap; }
  .sr-amount { font-size: 13px; font-weight: 800; color: var(--ink); }
  .sr-status { font-size: 11px; margin-top: 3px; font-weight: 700; }
  .sr-status.pending { color: var(--warn); }
  .sr-status.approved { color: var(--good); }
  .sr-status.rejected { color: var(--danger); }
  .search-empty { padding: 14px 12px; color: var(--muted); font-size: 13px; text-align: center; }
  .search-loading { padding: 14px 12px; color: var(--muted); font-size: 13px; text-align: center; }
  /* Responsive executive insights */
  @media (max-width: 900px) {
    .ei-body { grid-template-columns: 1fr; gap: 18px; }
    .exec-insight-active { padding: 20px 18px; }
  }
  .hero-panel, .panel, details.analytics {
    background: var(--panel); border: 1px solid var(--line); border-radius: 8px; box-shadow: var(--shadow);
  }
  .eyebrow { color: var(--brand); font-size: 12px; font-weight: 900; text-transform: uppercase; }
  h1 { margin-top: 6px; font-size: 28px; line-height: 1.15; letter-spacing: 0; }
  .subtle { color: var(--muted); font-size: 13px; line-height: 1.45; }
  .filter-dock {
    position: sticky; top: 101px; z-index: 15; margin-top: 0; margin-bottom: 14px; padding: 12px;
    display: grid; grid-template-columns: 1fr 1fr 1fr 1fr auto; gap: 10px;
    background: rgba(255,255,255,0.92); border: 1px solid var(--line); border-radius: 8px; box-shadow: var(--shadow);
  }
  body.dark .filter-dock { background: rgba(24,34,53,0.94); }
  .filter-dock label { display: grid; gap: 5px; color: var(--muted); font-size: 11px; font-weight: 900; text-transform: uppercase; }
  .filter-dock select, .filter-dock input {
    width: 100%; border: 1px solid var(--line); border-radius: 8px; padding: 8px 9px; background: var(--panel); color: var(--ink);
  }
  .filter-hint { grid-column: 1 / -1; min-height: 16px; color: var(--danger); font-size: 12px; font-weight: 700; }
  .chip.active { background: var(--brand); color: white; border-color: var(--brand); }
  .section { scroll-margin-top: 172px; margin-top: 18px; }
  .section-head { display: flex; align-items: end; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
  .section-head h2 { font-size: 16px; }
  .kpi-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-top: 14px; }
  .kpi {
    cursor: pointer; background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
    padding: 14px; min-height: 142px; box-shadow: var(--shadow); display: grid; gap: 7px;
  }
  .kpi:hover { transform: translateY(-1px); border-color: rgba(23,78,166,0.45); }
  .kpi .label { color: var(--muted); font-size: 11px; font-weight: 900; text-transform: uppercase; }
  .kpi .value { font-size: 26px; font-weight: 900; line-height: 1.05; overflow-wrap: anywhere; }
  .trend { display: inline-flex; width: fit-content; border-radius: 999px; padding: 4px 8px; font-size: 11px; font-weight: 900; }
  .status-good .trend { background: rgba(2,122,72,0.12); color: var(--good); }
  .status-warn .trend { background: rgba(180,83,9,0.14); color: var(--warn); }
  .status-danger .trend { background: rgba(180,35,24,0.12); color: var(--danger); }
  .panel { padding: 14px; }
  .grid-2 { display: grid; grid-template-columns: 1.15fr 0.85fr; gap: 12px; }
  .grid-3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
  .list { display: grid; gap: 8px; }
  .item { border: 1px solid var(--line); border-radius: 8px; padding: 10px; background: rgba(23,78,166,0.03); }
  .item strong { display: block; font-size: 13px; }
  .item span { display: block; margin-top: 3px; color: var(--muted); font-size: 12px; line-height: 1.4; }
  details.analytics { overflow: hidden; margin-top: 12px; }
  details.analytics > summary {
    list-style: none; cursor: pointer; padding: 14px; display: flex; justify-content: space-between; gap: 12px; align-items: center;
  }
  details.analytics > summary::-webkit-details-marker { display: none; }
  .analytics-body { padding: 0 14px 14px; border-top: 1px solid var(--line); }
  .chart-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 12px; }
  .chart-box { min-height: 340px; border: 1px solid var(--line); border-radius: 8px; padding: 8px; overflow: hidden; }
  .table-tools { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; margin-bottom: 10px; }
  .table-wrap { overflow: auto; max-height: 560px; border: 1px solid var(--line); border-radius: 8px; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; min-width: 980px; }
  th { position: sticky; top: 0; background: #101828; color: white; padding: 10px; text-align: left; cursor: pointer; z-index: 1; }
  td { border-bottom: 1px solid var(--line); padding: 9px 10px; color: var(--ink); vertical-align: top; }
  tr.hidden-row, tr.page-hidden { display: none; }
  .row-detail { display: none; background: rgba(23,78,166,0.05); }
  tr.expanded + .row-detail { display: table-row; }
  .pill { border-radius: 999px; padding: 4px 8px; font-weight: 900; font-size: 11px; display: inline-block; background: rgba(23,78,166,0.10); color: var(--brand); }
  .pill.high { background: rgba(180,35,24,0.12); color: var(--danger); }
  .pager { display: flex; justify-content: flex-end; gap: 8px; align-items: center; margin-top: 10px; }
  .alert-drawer, .drilldown {
    display: none; position: fixed; inset: 0; z-index: 60; background: rgba(15,23,42,0.45); padding: 6vh 18px;
  }
  .alert-drawer.open, .drilldown.open { display: block; }
  .drawer-card, .drill-card {
    max-width: 720px; margin: 0 auto; background: var(--panel); color: var(--ink);
    border: 1px solid var(--line); border-radius: 8px; box-shadow: var(--shadow); padding: 14px;
  }
  @media (max-width: 1180px) {
    .hero, .grid-2 { grid-template-columns: 1fr; }
    .kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .filter-dock { top: 98px; grid-template-columns: 1fr 1fr; }
  }
  @media (max-width: 720px) {
    .content { padding: 12px; }
    .kpi-grid, .grid-3, .chart-grid, .filter-dock { grid-template-columns: 1fr; }
    h1 { font-size: 24px; }
  }
</style>
</head>
<body>
<div class="app-shell">
  <main class="main">
    <header class="topbar">
      <div class="topbar-inner">
        <div style="display:flex;align-items:center;gap:10px;">
          <div style="flex-shrink:0;height:38px;display:flex;align-items:center;">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 72 34" width="72" height="34">
              <rect x="0" y="0" width="72" height="34" rx="9" fill="#29b6f6"/>
              <text x="36" y="24" text-anchor="middle" font-family="Arial Black,Arial,sans-serif"
                    font-size="17" font-weight="900" fill="#ffffff" letter-spacing="2">CBC</text>
            </svg>
          </div>
          <div>
            <div style="font-size:10px;font-weight:700;letter-spacing:2px;opacity:0.55;text-transform:uppercase;">CBC · IGEZ Analytics</div>
            <div style="font-size:12px;font-weight:600;color:var(--muted);">Refreshed at {{ now }}</div>
          </div>
        </div>
        <div class="top-actions">
          <button class="icon-btn" id="themeToggle" title="Toggle dark/light mode">☀️ Day</button>
        </div>
      </div>
      <nav class="tabs" id="topTabs">
        <a class="{% if active_tab == 'overview' %}active{% endif %}" href="/page/overview{% if nav_query %}?{{ nav_query }}{% endif %}">Overview</a><a class="{% if active_tab == 'pending-approvals' %}active{% endif %}" href="/page/pending-approvals{% if nav_query %}?{{ nav_query }}{% endif %}">Pending Approvals</a><a class="{% if active_tab == 'rtps-analytics' %}active{% endif %}" href="/page/rtps-analytics{% if nav_query %}?{{ nav_query }}{% endif %}">RTPS Analytics</a><a class="{% if active_tab == 'cash-advance' %}active{% endif %}" href="/page/cash-advance{% if nav_query %}?{{ nav_query }}{% endif %}">Cash Advance</a><a class="{% if active_tab == 'subsidiaries' %}active{% endif %}" href="/page/subsidiaries{% if nav_query %}?{{ nav_query }}{% endif %}">Subsidiaries</a><a class="{% if active_tab == 'audit-risk' %}active{% endif %}" href="/page/audit-risk{% if nav_query %}?{{ nav_query }}{% endif %}">Audit &amp; Risk</a><a class="{% if active_tab == 'vendor-insights' %}active{% endif %}" href="/page/vendor-insights{% if nav_query %}?{{ nav_query }}{% endif %}">Vendor Insights</a><a class="{% if active_tab == 'reports' %}active{% endif %}" href="/page/reports{% if nav_query %}?{{ nav_query }}{% endif %}">Reports</a>
      </nav>
    </header>

    <div class="content">
      <form class="filter-dock" method="get" id="filterForm" action="/page/{{ active_tab }}">
        <label>Period
          <select name="period" id="periodFilter" {% if filters.date_selection_active %}disabled{% endif %}>
            {% if filters.date_selection_active %}<option value="date_active" selected>Date range active</option>{% endif %}
            <option value="today" {% if filters.period == "today" and not filters.date_selection_active %}selected{% endif %}>Today</option>
            <option value="week" {% if filters.period == "week" and not filters.date_selection_active %}selected{% endif %}>This week</option>
            <option value="month" {% if filters.period == "month" and not filters.date_selection_active %}selected{% endif %}>This month</option>
            <option value="quarter" {% if filters.period == "quarter" and not filters.date_selection_active %}selected{% endif %}>This quarter</option>
            <option value="year" {% if filters.period == "year" and not filters.date_selection_active %}selected{% endif %}>This year</option>
            <option value="all" {% if filters.period == "all" and not filters.date_selection_active %}selected{% endif %}>All time</option>
            <option value="cancel_period">Cancel</option>
          </select>
        </label>
        <label>Subsidiary
          <select name="subsidiary">
            <option value="All">All subsidiaries</option>
            {% for sub in sub_options %}<option value="{{ sub }}" {% if filters.subsidiary == sub %}selected{% endif %}>{{ sub }}</option>{% endfor %}
          </select>
        </label>
        <label>From
          <input type="{% if filters.period_filter_active %}text{% else %}date{% endif %}" name="start" id="startDateFilter" value="{% if filters.period_filter_active %}Period active{% else %}{{ filters.start }}{% endif %}" data-date-value="{{ filters.start }}" {% if filters.period_filter_active %}disabled{% endif %}>
        </label>
        <label>To
          <input type="{% if filters.period_filter_active %}text{% else %}date{% endif %}" name="end" id="endDateFilter" value="{% if filters.period_filter_active %}Period active{% else %}{{ filters.end }}{% endif %}" data-date-value="{{ filters.end }}" {% if filters.period_filter_active %}disabled{% endif %}>
        </label>
        <button class="btn primary" type="submit" id="applyFilters" {% if filters.date_range_incomplete or filters.date_range_invalid %}disabled{% endif %}>Apply</button>
        <div class="filter-hint" id="filterHint">{% if filters.date_range_invalid %}From date cannot be later than To date.{% elif filters.date_range_incomplete %}Select both From and To dates before applying.{% endif %}</div>
      </form>

      {% if active_tab == "overview" %}
      <section id="overview" class="section">
        {% if d.system_ready %}
        <!-- Empty state: full-width orb animation -->
        <div class="exec-insight-card exec-insight-empty">
          <div class="ei-filter-strip">
            <span class="ei-filter-chip">&#x1F4C5; {{ filters.period | capitalize if filters.period != 'all' else 'All time' }}</span>
            <span class="ei-filter-chip">&#x1F3E2; {{ filters.subsidiary }}</span>
            {% if filters.start %}<span class="ei-filter-chip">{{ filters.start }} → {{ filters.end }}</span>{% endif %}
          </div>
          <div class="ei-label">Executive Insights</div>
          <div class="ready-orb" aria-hidden="true"></div>
          <div class="ready-title">Current Status: Optimal.</div>
          <p class="ready-copy">Your dashboard is up to date with no pending actions. Real-time spending and approval insights will populate here as requests arrive.</p>
        </div>
        {% else %}
        <!-- Active state: full-width rich executive insights -->
        <div class="exec-insight-card exec-insight-active">
          <div class="ei-filter-strip">
            <span class="ei-filter-chip">&#x1F4C5; {{ filters.period | capitalize if filters.period != 'all' else 'All time' }}</span>
            <span class="ei-filter-chip">&#x1F3E2; {{ filters.subsidiary }}</span>
            {% if filters.start %}<span class="ei-filter-chip">{{ filters.start }} → {{ filters.end }}</span>{% endif %}
          </div>
          <div class="ei-header">
            <div>
              <div class="ei-label">Executive Insights</div>
              <div class="ei-health-badge ei-health-{{ d.health.status | lower | replace(' ', '-') }}">
                {{ d.health.status }} &nbsp;·&nbsp; {{ d.health.score }}/100
              </div>
            </div>
            <a class="ei-pdf-btn" href="/executive-report{% if report_query %}?{{ report_query }}{% endif %}">&#x1F4C4; Executive PDF</a>
          </div>
          <div class="ei-meter-wrap">
            <div class="ei-meter"><div class="ei-meter-fill" style="width:{{ d.health.score }}%"></div></div>
            <span class="ei-meter-note">{{ d.health.tone }} Pending exposure {{ d.health.pending_ratio }}% of total spend.</span>
          </div>
          <div class="ei-body">
            <div class="ei-insights-col">
              <div class="ei-col-title">Key Signals</div>
              <ul class="ei-insights-list">
                {% for insight in d.insights %}
                <li>{{ insight }}</li>
                {% endfor %}
              </ul>
            </div>
            <div class="ei-stats-col">
              <div class="ei-col-title">Spend Snapshot</div>
              <div class="ei-stat-grid">
                <div class="ei-stat">
                  <span>Approved Spend</span>
                  <strong>&#x20A6;{{ "{:,.0f}".format(d.approved_spend) }}</strong>
                </div>
                <div class="ei-stat">
                  <span>Waiting Approval</span>
                  <strong>&#x20A6;{{ "{:,.0f}".format(d.pending_spend) }}</strong>
                </div>
                <div class="ei-stat">
                  <span>This Month</span>
                  <strong>&#x20A6;{{ "{:,.0f}".format(d.month_compare.spend) }}</strong>
                </div>
                <div class="ei-stat">
                  <span>Total Requests</span>
                  <strong>{{ d.total_approved_requests }}/{{ d.total_requests }}</strong>
                </div>
              </div>
            </div>
            <div class="ei-bottleneck-col">
              <div class="ei-col-title">Approval Bottlenecks</div>
              <div class="ei-bottleneck-list">
                {% for item in d.bottlenecks %}
                <div class="ei-bottleneck-row">
                  <div class="ei-bottleneck-info">
                    <span class="ei-bottleneck-label">{{ item.label }}</span>
                    <span class="ei-bottleneck-impact">{{ item.impact }}</span>
                  </div>
                  <span class="ei-bottleneck-count">{{ item.count }}</span>
                </div>
                {% endfor %}
              </div>
            </div>
          </div>
        </div>
        {% endif %}
      </section>
      {% endif %}

      {% if active_tab == "overview" %}
      <section class="section">
        <div class="kpi-grid">
          {% for kpi in d.executive_kpis %}
          <article class="kpi status-{{ kpi.status }}" role="button" tabindex="0">
            <div class="label">{{ kpi.label }}</div>
            <div class="value">{{ kpi.value }}</div>
            <span class="trend">{{ kpi.change }}</span>
            <p class="subtle">{{ kpi.note }}</p>
          </article>
          {% endfor %}
        </div>
      </section>
      {% endif %}

      {% if active_tab == "pending-approvals" %}
      <section class="section grid-2">
        <div class="panel">
          <div class="section-head"><h2>Urgent Approvals</h2><span class="subtle">One-click executive action queue</span></div>
          <div class="list">
            {% if d.action_required %}
              {% for item in d.action_required %}
              <div class="item">
                <strong>{{ item.title }} <span class="pill {% if item.priority_score >= 55 %}high{% endif %}">Score {{ item.priority_score }}</span></strong>
                <span>{{ item.module }} - {{ item.subsidiary }} - {{ item.why }}</span>
              </div>
              {% endfor %}
            {% else %}
              <div class="item"><strong>No urgent approvals</strong><span>No urgent pending actions matched the current filters.</span></div>
            {% endif %}
          </div>
        </div>
        <div class="panel">
          <div class="section-head"><h2>Executive Alerts</h2><button class="chip" id="openAlertsInline">Open alert center</button></div>
          <div class="list">
            {% for alert in d.alert_center[:5] %}
            <div class="item"><strong>{{ alert.type }} <span class="pill {% if alert.priority == 'High' %}high{% endif %}">{{ alert.priority }}</span></strong><span>{{ alert.message }}</span></div>
            {% else %}
            <div class="item"><strong>Stable</strong><span>No high-priority alerts in this view.</span></div>
            {% endfor %}
          </div>
        </div>
      </section>
      {% endif %}

      {% if active_tab == "audit-risk" %}
      <section class="section grid-3">
        <div class="panel"><h2>Risk Monitoring</h2><div class="list" style="margin-top:10px">{% for alert in d.risk_alerts %}<div class="item"><strong>Risk Alert</strong><span>{{ alert }}</span></div>{% else %}<div class="item"><strong>Stable</strong><span>No major spend risk alerts in this view.</span></div>{% endfor %}</div></div>
        <div class="panel"><h2>Data Quality</h2><div class="list" style="margin-top:10px">{% for warning in d.data_quality_warnings %}<div class="item"><strong>Review</strong><span>{{ warning }}</span></div>{% else %}<div class="item"><strong>Clean</strong><span>No major data-quality warnings in this view.</span></div>{% endfor %}</div></div>
        <div class="panel"><h2>Role View</h2><div id="roleNarrative" class="list" style="margin-top:10px"></div></div>
      </section>
      {% endif %}

      {% if active_tab == "subsidiaries" %}
      <section class="section panel">
        <div class="section-head"><h2>Subsidiary Performance</h2><span class="subtle">Spend, pending value, and aging by entity</span></div>
        <div class="grid-3">
          {% for item in d.subsidiary_scorecards[:6] %}
          <div class="item"><strong>{{ item.subsidiary }}</strong><span>{{ item.spend_label }} spend - {{ item.pending_label }} waiting - oldest {{ item.oldest_days }} days.</span></div>
          {% else %}
          <div class="item"><strong>No active subsidiaries</strong><span>No subsidiary has spend or pending activity in this view.</span></div>
          {% endfor %}
        </div>
      </section>
      {% endif %}

      {% if active_tab == "rtps-analytics" %}
      <section class="section panel">
        <div class="section-head"><h2>RTPS Analytics</h2><span class="subtle">{{ d.rtps_total }} supplier payment requests - {{ d.rtps_pending }} pending</span></div>
        <div class="chart-grid"><div class="chart-box">{{ charts.rtps_by_sub | safe }}</div><div class="chart-box">{{ charts.rtps_by_mode | safe }}</div><div class="chart-box">{{ charts.rtps_suppliers | safe }}</div><div class="chart-box">{{ charts.rtps_trend | safe }}</div></div>
      </section>
      {% endif %}

      {% if active_tab == "cash-advance" %}
      <section class="section panel">
        <div class="section-head"><h2>Cash Advance Analytics</h2><span class="subtle">{{ d.ca_total }} requests - {{ d.ca_pending }} pending</span></div>
        <div class="chart-grid"><div class="chart-box">{{ charts.ca_by_sub | safe }}</div><div class="chart-box">{{ charts.ca_status | safe }}</div><div class="chart-box">{{ charts.ca_trend | safe }}</div></div>
        <div class="chart-grid" style="margin-top:12px"><div class="chart-box">{{ charts.ec_by_sub | safe }}</div><div class="chart-box">{{ charts.ec_status | safe }}</div></div>
        <div class="chart-grid" style="margin-top:12px"><div class="chart-box" style="grid-column:1/-1">{{ charts.ec_trend | safe }}</div></div>
      </section>
      {% endif %}

      {% if active_tab == "vendor-insights" %}
      <section class="section panel">
        <div class="section-head"><h2>Vendor Performance</h2><span class="subtle">Supplier concentration and duplicate-payment checks</span></div>
        <div class="grid-2" style="margin-top:12px">
          <div class="chart-box">{{ charts.rtps_suppliers | safe }}</div>
          <div class="panel">
            <h2>Duplicate or Suspicious Requests</h2>
            <div class="list" style="margin-top:10px">{% for item in d.suspicious_requests %}<div class="item"><strong>{{ item.owner }} - {{ item.amount_label }}</strong><span>{{ item.module }} - {{ item.reason }}</span></div>{% else %}<div class="item"><strong>Clean</strong><span>No same-owner, same-amount repeat requests detected.</span></div>{% endfor %}</div>
          </div>
        </div>
      </section>
      {% endif %}

      {% if active_tab == "reports" %}
      <section class="section panel">
        <div class="section-head"><h2>Operational Details</h2><span class="subtle">Interactive table with sorting, filtering, pagination, export, and expandable rows</span></div>
        <div class="table-tools">
          <input class="smart-search" id="tableSearch" style="max-width:420px;padding-left:12px" placeholder="Inline table search">
          <div class="top-actions">
            <select id="statusFilter"><option value="">All statuses</option><option>Pending</option><option>Approved</option><option>Rejected</option><option>Unclassified</option></select>
            <select id="priorityFilter"><option value="">All priority</option><option>High</option><option>Normal</option></select>
            <button class="btn" id="exportCsv" type="button">Export CSV</button>
          </div>
        </div>
        <div class="table-wrap">
          <table id="opsTable">
            <thead><tr><th data-sort="date">Date</th><th data-sort="request">Request ID</th><th data-sort="staff">Staff</th><th data-sort="vendor">Vendor</th><th data-sort="amount">Amount</th><th data-sort="status">Status</th><th data-sort="aging">Aging</th><th data-sort="level">Approval Level</th><th>Action Buttons</th></tr></thead>
            <tbody>
            {% for row in d.operational_rows %}
              <tr class="data-row" data-search="{{ row.date }} {{ row.request_id }} {{ row.staff }} {{ row.vendor }} {{ row.amount }} {{ row.status }} {{ row.aging }} {{ row.approval_level }} {{ row.module }} {{ row.subsidiary }} {{ row.priority }}" data-status="{{ row.status }}" data-priority="{{ row.priority }}" data-amount="{{ row.amount_value }}" data-aging="{{ row.aging_days }}">
                <td>{{ row.date }}</td><td>{{ row.request_id }}</td><td>{{ row.staff }}</td><td>{{ row.vendor }}</td><td>{{ row.amount }}</td><td><span class="pill">{{ row.status }}</span></td><td>{{ row.aging }}</td><td>{{ row.approval_level }}</td><td><button class="chip expand-row" type="button">Open</button></td>
              </tr>
              <tr class="row-detail"><td colspan="9">
                <strong>{{ row.module }} - {{ row.subsidiary }}</strong><br>
                {% if row.module == "Leave" %}
                  <span class="subtle">
                    <strong>Leave Type:</strong> {{ row.leave_type or "—" }} &nbsp;|&nbsp;
                    <strong>Days Applied:</strong> {{ row.leave_days_applied }} &nbsp;|&nbsp;
                    <strong>Days Remaining:</strong> {{ row.leave_days_left }}
                  </span>
                {% else %}
                  <span class="subtle">{{ row.details or "No additional request narrative captured." }}</span>
                {% endif %}
              </td></tr>
            {% endfor %}
            </tbody>
          </table>
        </div>
        <div class="pager"><button class="btn" id="prevPage" type="button">Prev</button><span class="subtle" id="pageInfo"></span><button class="btn" id="nextPage" type="button">Next</button></div>
      </section>
      {% endif %}

    </div>
  </main>
</div>

<div class="alert-drawer" id="alertDrawer"><div class="drawer-card"><div class="section-head"><h2>Notification &amp; Alert Center</h2><button class="chip" data-close="alertDrawer">Close</button></div><div class="list">{% for alert in d.alert_center %}<div class="item"><strong>{{ alert.type }} <span class="pill {% if alert.priority == 'High' %}high{% endif %}">{{ alert.priority }}</span></strong><span>{{ alert.message }}</span><button class="chip dismiss-alert" type="button" style="margin-top:8px">Dismiss</button></div>{% else %}<div class="item"><strong>No active alerts</strong><span>Nothing requires executive attention in this view.</span></div>{% endfor %}</div></div></div>
<div class="drilldown" id="drilldown"><div class="drill-card"><div class="section-head"><h2 id="drillTitle">Drill-down Analytics</h2><button class="chip" data-close="drilldown">Close</button></div><div id="drillBody" class="grid-3"></div></div></div>

<script>
  const searchTerms = {{ d.smart_search_terms | tojson }};
  const commands = [
    {label:"Search approvals", action:() => window.location.href="/page/pending-approvals{% if report_query %}?{{ report_query }}{% endif %}"},
    {label:"Open reports", action:() => window.location.href="/page/reports{% if report_query %}?{{ report_query }}{% endif %}"},
    {label:"Export analytics", action:() => window.location.href="/executive-report{% if report_query %}?{{ report_query }}{% endif %}"},
    {label:"View vendor data", action:() => window.location.href="/page/vendor-insights{% if report_query %}?{{ report_query }}{% endif %}"},
    {label:"Open subsidiary analytics", action:() => window.location.href="/page/subsidiaries{% if report_query %}?{{ report_query }}{% endif %}"},
    {label:"View high-risk transactions", action:() => window.location.href="/page/reports{% if report_query %}?{{ report_query }}{% endif %}"},
    {label:"Open audit trail", action:() => window.location.href="/page/audit-risk{% if report_query %}?{{ report_query }}{% endif %}"}
  ];
  const roleCopy = {
    ceo: [["Financial exposure", "{{ d.pending_spend|round|int }} pending value needs executive visibility."], ["High-value approvals", "{{ d.action_required|length }} urgent items are prioritized by age, amount, and approval level."], ["Risk alerts", "{{ d.alert_center|length }} executive alerts are active."]],
    finance: [["Spend analysis", "{{ d.total_spend|round|int }} total tracked spend in this view."], ["Budget performance", "{{ d.month_compare.spend_change }} movement versus the previous month."], ["Vendor concentration", "Top supplier and subsidiary charts are available under RTPS Analytics."]],
    operations: [["Daily workflow", "{{ d.what_changed.new_requests }} new requests in the recent window."], ["Queue health", "{{ d.pending_aging_total }} requests are currently pending."], ["Processing", "Use quick filters for overdue and CEO attention queues."]],
    admin: [["Audit logs", "{{ d.activity_total }} activity events match this view."], ["User activity", "Staff request behavior and rejection analytics remain available below."], ["System management", "Filters, exports, and PDF reports are available from the sticky header."]]
  };

  function renderRole(role) {
    const el = document.getElementById("roleNarrative");
    if (el) el.innerHTML = roleCopy[role].map(x => `<div class="item"><strong>${x[0]}</strong><span>${x[1]}</span></div>`).join("");
  }
  renderRole("ceo");

  document.getElementById("themeToggle").addEventListener("click", () => {
    const isDark = document.body.classList.toggle("dark");
    document.getElementById("themeToggle").textContent = isDark ? "🌙 Night" : "☀️ Day";
    localStorage.setItem("igezTheme", isDark ? "dark" : "light");
  });
  // Restore saved theme on load
  if (localStorage.getItem("igezTheme") === "dark") {
    document.body.classList.add("dark");
    const btn = document.getElementById("themeToggle");
    if (btn) btn.textContent = "🌙 Night";
  }
  document.querySelectorAll("[data-close]").forEach(btn => btn.addEventListener("click", () => document.getElementById(btn.dataset.close).classList.remove("open")));
  document.querySelectorAll(".dismiss-alert").forEach(btn => btn.addEventListener("click", () => btn.closest(".item").remove()));

  document.addEventListener("keydown", e => {
    if (e.key === "Escape") document.querySelectorAll(".open").forEach(x => x.classList.remove("open"));
  });

  const tableSearch = document.getElementById("tableSearch");
  const statusFilter = document.getElementById("statusFilter");
  const priorityFilter = document.getElementById("priorityFilter");
  const dataRows = [...document.querySelectorAll("#opsTable .data-row")];
  let page = 1, pageSize = 20;
  function setTableFilter(value) { if (priorityFilter) { priorityFilter.value = value; filterRows(); } }
  function filterRows() {
    if (!tableSearch) return;
    const q = tableSearch.value.toLowerCase().trim();
    const status = statusFilter ? statusFilter.value : "";
    const priority = priorityFilter ? priorityFilter.value : "";
    dataRows.forEach(row => {
      const match = (!q || row.dataset.search.toLowerCase().includes(q)) && (!status || row.dataset.status === status) && (!priority || row.dataset.priority === priority);
      row.classList.toggle("hidden-row", !match);
      row.nextElementSibling.classList.toggle("hidden-row", !match);
    });
    page = 1; paginate();
  }
  if (tableSearch) [tableSearch, statusFilter, priorityFilter].forEach(el => el && el.addEventListener("input", filterRows));
  function paginate() {
    if (!dataRows.length) return;
    const visible = dataRows.filter(r => !r.classList.contains("hidden-row"));
    const pages = Math.max(1, Math.ceil(visible.length / pageSize));
    page = Math.min(page, pages);
    dataRows.forEach(row => {
      row.classList.add("page-hidden");
      row.nextElementSibling.classList.add("page-hidden");
    });
    visible.slice((page - 1) * pageSize, page * pageSize).forEach(row => {
      row.classList.remove("page-hidden");
      row.nextElementSibling.classList.remove("page-hidden");
    });
    const pi = document.getElementById("pageInfo");
    if (pi) pi.textContent = `Page ${page} of ${pages} - ${visible.length} rows`;
  }
  const prevPage = document.getElementById("prevPage");
  const nextPage = document.getElementById("nextPage");
  if (prevPage) prevPage.addEventListener("click", () => { page = Math.max(1, page - 1); paginate(); });
  if (nextPage) nextPage.addEventListener("click", () => { page += 1; paginate(); });
  document.querySelectorAll(".expand-row").forEach(btn => btn.addEventListener("click", () => btn.closest("tr").classList.toggle("expanded")));
  document.querySelectorAll("#opsTable th[data-sort]").forEach((th, index) => th.addEventListener("click", () => {
    const tbody = document.querySelector("#opsTable tbody");
    const pairs = dataRows.map(row => [row, row.nextElementSibling]);
    pairs.sort((a,b) => a[0].children[index].textContent.localeCompare(b[0].children[index].textContent, undefined, {numeric:true}));
    pairs.forEach(pair => { tbody.appendChild(pair[0]); tbody.appendChild(pair[1]); });
    paginate();
  }));
  const exportCsv = document.getElementById("exportCsv");
  if (exportCsv) exportCsv.addEventListener("click", () => {
    const rows = dataRows.filter(r => !r.classList.contains("hidden-row")).map(r => [...r.children].slice(0, 8).map(td => `"${td.textContent.replaceAll('"','""').trim()}"`).join(","));
    const csv = ["Date,Request ID,Staff,Vendor,Amount,Status,Aging,Approval Level", ...rows].join("\\n");
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([csv], {type:"text/csv"}));
    link.download = "igez-executive-dashboard.csv"; link.click();
  });

  document.querySelectorAll("#quickFilters .chip").forEach(btn => btn.addEventListener("click", () => {
    const value = btn.dataset.filter;
    if (value === "today" || value === "week") { document.getElementById("periodFilter").value = value; document.getElementById("filterForm").submit(); return; }
    window.location.href = "/page/reports?filter=" + encodeURIComponent(value);
  }));

  const periodFilter = document.getElementById("periodFilter");
  const applyFilters = document.getElementById("applyFilters");
  const filterHint = document.getElementById("filterHint");
  const periodDisabledValue = "date_active";
  const cancelPeriodValue = "cancel_period";
  let previousPeriodValue = periodFilter.value === periodDisabledValue ? "all" : periodFilter.value;
  const dateFilters = [
    document.getElementById("startDateFilter"),
    document.getElementById("endDateFilter")
  ];

  function getDisabledPeriodOption() {
    let option = periodFilter.querySelector(`option[value="${periodDisabledValue}"]`);
    if (!option) {
      option = new Option("Date range active", periodDisabledValue);
      periodFilter.insertBefore(option, periodFilter.firstChild);
    }
    return option;
  }

  function syncPeriodFilter() {
    if (periodFilter.value === cancelPeriodValue) {
      previousPeriodValue = "all";
      periodFilter.value = "all";
    }

    const dateSelectionActive = dateFilters.some((input) => input.dataset.dateValue);
    const dateRangeIncomplete = dateFilters.filter((input) => input.dataset.dateValue).length === 1;
    const dateRangeInvalid = dateFilters.every((input) => input.dataset.dateValue)
      && dateFilters[0].dataset.dateValue > dateFilters[1].dataset.dateValue;
    const periodFilterActive = periodFilter.value !== "all" && periodFilter.value !== periodDisabledValue;

    if (dateSelectionActive && periodFilter.value !== periodDisabledValue) {
      previousPeriodValue = periodFilter.value;
    }

    periodFilter.disabled = dateSelectionActive;
    if (dateSelectionActive) {
      getDisabledPeriodOption().selected = true;
    } else {
      const disabledOption = periodFilter.querySelector(`option[value="${periodDisabledValue}"]`);
      if (disabledOption) disabledOption.remove();
      periodFilter.value = previousPeriodValue;
    }

    dateFilters.forEach((input) => {
      input.disabled = periodFilterActive;
      if (periodFilterActive) {
        input.type = "text";
        input.value = "Period active";
      } else {
        input.type = "date";
        input.value = input.dataset.dateValue;
      }
    });

    if (dateRangeInvalid) {
      filterHint.textContent = "From date cannot be later than To date.";
    } else if (dateRangeIncomplete) {
      filterHint.textContent = "Select both From and To dates before applying.";
    } else {
      filterHint.textContent = "";
    }
    applyFilters.disabled = dateRangeIncomplete || dateRangeInvalid;
  }
  dateFilters.forEach((input) => input.addEventListener("input", () => {
    input.dataset.dateValue = input.value;
    syncPeriodFilter();
  }));
  periodFilter.addEventListener("change", () => {
    if (periodFilter.value !== "all" && periodFilter.value !== periodDisabledValue && periodFilter.value !== cancelPeriodValue) {
      previousPeriodValue = periodFilter.value;
      dateFilters.forEach((input) => {
        input.dataset.dateValue = "";
        input.value = "";
      });
    } else if (periodFilter.value === "all") {
      previousPeriodValue = "all";
    }
    syncPeriodFilter();
  });
  syncPeriodFilter();
  if (dataRows && dataRows.length) paginate();
</script>
</body>
</html>
"""


ERROR_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>IGEZ Dashboard - Configuration Required</title>
<style>
  body { margin: 0; font-family: 'Segoe UI', sans-serif; background: #f0f2f5; color: #2c3e50; }
  .wrap { max-width: 900px; margin: 56px auto; padding: 0 24px; }
  .panel { background: white; border-radius: 10px; padding: 28px; box-shadow: 0 1px 8px rgba(0,0,0,0.10); border-top: 5px solid #e74c3c; }
  h1 { font-size: 24px; margin-bottom: 10px; }
  p { line-height: 1.6; color: #556; }
  .error { margin: 18px 0; padding: 14px; background: #fff5f5; border: 1px solid #ffd5d5; border-radius: 8px; color: #9f1d1d; font-family: Consolas, monospace; font-size: 13px; overflow-wrap: anywhere; }
  .steps { display: grid; gap: 10px; margin-top: 18px; }
  .step { padding: 12px 14px; border: 1px solid #e5e9f0; border-radius: 8px; background: #fbfcfe; }
  .muted { margin-top: 20px; color: #889; font-size: 12px; }
</style>
</head>
<body>
  <div class="wrap">
    <div class="panel">
      <h1>Dashboard configuration needs attention</h1>
      <p>The app deployed successfully, but it could not load the MongoDB data needed to render the dashboard.</p>
      <div class="error">{{ error_message }}</div>
      <div class="steps">
        <div class="step"><strong>1. Vercel environment variable:</strong> Add <code>DATABASE_URL</code> in Project Settings &gt; Environment Variables.</div>
        <div class="step"><strong>2. MongoDB Atlas network access:</strong> Allow Vercel to connect. For testing, add <code>0.0.0.0/0</code> to Atlas Network Access, then tighten it later if needed.</div>
        <div class="step"><strong>3. Redeploy:</strong> Trigger a new deployment after saving the environment variable or Atlas network rule.</div>
      </div>
      <div class="muted">Last checked: {{ now }}</div>
    </div>
  </div>
</body>
</html>
"""

# -- Cache & Route helpers -----------------------------------------------------

import time as _time
_DATA_CACHE: dict = {}          # key → (timestamp, payload)
_CACHE_TTL = 90                 # seconds before cache expires

def _cache_key(filters: dict) -> str:
    return "|".join([
        filters.get("period", "all"),
        filters.get("subsidiary", "All"),
        str(filters.get("start", "")),
        str(filters.get("end", "")),
    ])

def _build_nav_query(filters: dict) -> str:
    """Build a query string that carries filter state across nav links."""
    from urllib.parse import urlencode
    params: dict = {}
    period = filters.get("period", "all")
    subsidiary = filters.get("subsidiary", "All")
    start = filters.get("start", "")
    end   = filters.get("end", "")
    if period and period not in ("all", "date_active"):
        params["period"] = period
    if subsidiary and subsidiary != "All":
        params["subsidiary"] = subsidiary
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    return urlencode(params) if params else ""


def dashboard_payload():
    now = app_now().strftime("%d %b %Y, %H:%M")
    db = get_db()
    sub_map = get_subsidiary_map(db)
    requested_period = request.args.get("period", "all")
    if requested_period not in {"all", "today", "week", "month", "quarter", "year", "date_active"}:
        requested_period = "all"
    raw_start = request.args.get("start", "")
    raw_end = request.args.get("end", "")
    start_date = parse_date_arg(raw_start)
    end_date = parse_date_arg(raw_end)
    date_selection_active = bool(raw_start or raw_end)
    date_range_complete = bool(start_date and end_date)
    date_range_invalid = bool(date_range_complete and start_date > end_date)
    date_range_valid = date_range_complete and not date_range_invalid
    date_range_incomplete = date_selection_active and not date_range_complete
    period = "all" if date_selection_active else requested_period
    period_filter_active = period not in ("all", "date_active")
    period_start, period_end = period_bounds(period)
    filters = {
        "period": period,
        "start": raw_start,
        "end": raw_end,
        "start_date": start_date if date_range_valid else period_start,
        "end_date": (end_date + timedelta(days=1)) if date_range_valid else period_end,
        "subsidiary": request.args.get("subsidiary", "All"),
        "date_selection_active": date_selection_active,
        "date_range_incomplete": date_range_incomplete,
        "date_range_invalid": date_range_invalid,
        "period_filter_active": period_filter_active,
    }
    sub_options = sorted(set(sub_map.values()))

    # --- Cache: skip re-running load_all if same filters were used recently ---
    ck = _cache_key(filters)
    cached = _DATA_CACHE.get(ck)
    if cached and (_time.monotonic() - cached[0]) < _CACHE_TTL:
        d = cached[1]
    else:
        d = load_all(db, sub_map, filters)
        _DATA_CACHE[ck] = (_time.monotonic(), d)
        # Prune old entries to avoid unbounded growth
        if len(_DATA_CACHE) > 30:
            oldest = min(_DATA_CACHE, key=lambda k: _DATA_CACHE[k][0])
            _DATA_CACHE.pop(oldest, None)

    return now, filters, sub_options, d


def build_executive_pdf(d, filters, now):
    import io
    from xml.sax.saxutils import escape
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_LEFT
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

    def pdf_text(value):
        # Replace Naira symbol with NGN for font compatibility in PDF
        text = escape(str(value or ""))
        text = text.replace("₦", "NGN ")
        return text

    def format_date_range(filters):
        """Generate a human-readable date range description in day, month, year format."""
        from datetime import datetime
        
        raw_start = filters.get("start", "").strip() if filters.get("start") else ""
        raw_end = filters.get("end", "").strip() if filters.get("end") else ""
        period = filters.get("period", "all")
        
        def format_date_str(date_str):
            """Convert YYYY-MM-DD to day month year format (e.g., 1 February 2026)."""
            if not date_str:
                return ""
            
            date_str_clean = str(date_str).strip()
            if not date_str_clean:
                return ""
                
            try:
                dt = datetime.strptime(date_str_clean, "%Y-%m-%d")
                day = dt.day
                month = dt.strftime("%B")
                year = dt.year
                return f"{day} {month} {year}"
            except (ValueError, AttributeError) as e:
                # If parsing fails, return original string
                return date_str_clean
        
        # Check for custom date range
        if raw_start and raw_end:
            start_fmt = format_date_str(raw_start)
            end_fmt = format_date_str(raw_end)
            return f"Custom: {start_fmt} to {end_fmt}"
        elif raw_start:
            start_fmt = format_date_str(raw_start)
            return f"From {start_fmt} to Present"
        elif raw_end:
            end_fmt = format_date_str(raw_end)
            return f"Until {end_fmt}"
        
        # Fall back to period names
        period_names = {
            "all": "All time",
            "today": "Today",
            "week": "This week",
            "month": "This month",
            "quarter": "This quarter",
            "year": "This year",
        }
        return period_names.get(period, period)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("ExecTitle", parent=styles["Title"], fontSize=18, textColor=colors.HexColor("#082f63"))
    section = ParagraphStyle("ExecSection", parent=styles["Heading2"], fontSize=12, textColor=colors.HexColor("#082f63"), spaceBefore=12)
    body = ParagraphStyle("ExecBody", parent=styles["BodyText"], fontSize=9, leading=12)
    small = ParagraphStyle("ExecSmall", parent=styles["BodyText"], fontSize=8, leading=10, textColor=colors.HexColor("#456489"))
    date_range_style = ParagraphStyle("DateRange", parent=styles["Heading3"], fontSize=11, textColor=colors.HexColor("#0b3a75"), spaceBefore=6, alignment=TA_LEFT, fontName="Helvetica-Bold")

    date_range_str = format_date_range(filters)
    subsidiary_str = filters.get("subsidiary", "All")

    story = [
        Paragraph("IGEZ Executive Analytics Brief", title),
        Paragraph(f"Generated: {pdf_text(now)}", small),
        Paragraph(f"Data Period: {date_range_str} | Subsidiary: {subsidiary_str}", date_range_style),
        Spacer(1, 12),
    ]
    kpis = [
        ["Health", f"{d['health']['status']} ({d['health']['score']}/100)"],
        ["Approved Spend", f"NGN {d['approved_spend']:,.0f}"],
        ["Waiting Approval", f"NGN {d['pending_spend']:,.0f}"],
        ["This Month Spend", f"NGN {d['month_compare']['spend']:,.0f}"],
        ["Total Requests", f"{d['total_approved_requests']}/{d['total_requests']} approved"],
        ["Oldest Pending", f"{d['oldest_pending_days']} days"],
    ]
    table = Table(kpis, colWidths=[150, 330])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#082f63")),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.white),
        ("BACKGROUND", (1, 0), (1, -1), colors.HexColor("#f3f8ff")),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c7ddf6")),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("PADDING", (0, 0), (-1, -1), 7),
    ]))
    story.extend([table, Spacer(1, 10), Paragraph("Executive Summary", section)])
    for insight in d["insights"]:
        story.append(Paragraph(f"- {pdf_text(insight)}", body))

    story.append(Paragraph("Action Required Today", section))
    if d["action_required"]:
        action_rows = [["Item", "Why", "Owner"]]
        for item in d["action_required"]:
            action_rows.append([pdf_text(item["title"]), pdf_text(item["why"]), pdf_text(item["waiting_on"])])
        actions = Table(action_rows, colWidths=[170, 230, 80])
        actions.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0b3a75")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c7ddf6")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("PADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(actions)
    else:
        story.append(Paragraph("No urgent pending actions matched the current filters.", body))

    story.append(Paragraph("Rejection Analytics", section))
    for item in d["rejection_analytics"]:
        metric_note = "rejected requests" if item["metric_type"] == "Count" else "rejected value"
        story.append(Paragraph(
            f"- {pdf_text(item['module'])}: {item['rate']}% rejected; {pdf_text(item['metric_label'])} {metric_note}.",
            body,
        ))

    story.append(Paragraph("Month-End Close View", section))
    close = d["month_end_close"]
    story.append(Paragraph(
        f"{close['days_left']} days left; {pdf_text(close['pending_label'])} pending; {close['blockers']} stale blockers; {pdf_text(close['unclassified_label'])} unclassified.",
        body,
    ))

    story.append(Paragraph("Suspicious Requests", section))
    suspicious = d["suspicious_requests"] or [{"owner": "None", "reason": "No same-owner, same-amount repeat requests detected."}]
    for item in suspicious[:5]:
        story.append(Paragraph(f"- {pdf_text(item['owner'])}: {pdf_text(item['reason'])}", body))

    story.append(Paragraph("Data Quality Notes", section))
    warnings = d["data_quality_warnings"] or ["No major data-quality warnings in this view."]
    for warning in warnings:
        story.append(Paragraph(f"- {pdf_text(warning)}", body))

    story.append(Paragraph("Spend Risk Alerts", section))
    alerts = d["risk_alerts"] or ["No major spend risk alerts in this view."]
    for alert in alerts:
        story.append(Paragraph(f"- {pdf_text(alert)}", body))

    story.append(Paragraph("Top Subsidiary Scorecards", section))
    for item in d["subsidiary_scorecards"][:5]:
        story.append(Paragraph(
            f"{pdf_text(item['subsidiary'])}: {pdf_text(item['spend_label'])} total spend; {pdf_text(item['pending_label'])} waiting; oldest pending {item['oldest_days']} days.",
            body,
        ))

    doc.build(story)
    buf.seek(0)
    return buf


@app.route("/")
def dashboard():
    return redirect("/page/overview")


@app.route("/api/search")
def api_search():
    from flask import jsonify
    q = request.args.get("q", "").strip().lower()
    if len(q) < 2:
        return jsonify(results=[])
    try:
        db = get_db()
        sub_map = get_subsidiary_map(db)
        results = []

        def _add(module, record, staff_field, vendor_field, amount_fn):
            staff = normalize_cbc_text(str(record.get(staff_field) or record.get("name") or record.get("staff_name") or "")).strip()
            vendor = normalize_cbc_text(str(record.get(vendor_field) or "")).strip()
            rid = str(record.get("_id", ""))[-8:]
            sub = resolve_sub(record.get("subsidiary_id", ""), sub_map)
            combined = " ".join([staff, vendor, rid, sub, module]).lower()
            if q not in combined:
                return
            dt = record_date(record)
            amt = amount_fn(record)
            results.append({
                "module": module,
                "staff": staff or "—",
                "vendor": vendor or "—",
                "subsidiary": sub,
                "amount": format_money(amt) if amt else "—",
                "status": str(record.get("status") or "").title() or "Unknown",
                "date": dt.strftime("%d %b %Y") if dt else "—",
                "request_id": rid,
            })

        def ec_amt(r):
            items = r.get("expense_claim", [])
            if isinstance(items, list):
                return sum(safe_amount(i.get("value", 0)) for i in items if isinstance(i, dict))
            return 0.0

        # Build user map for leave name resolution in search
        _search_user_map = {}
        try:
            from bson import ObjectId as _ObjId2
            for u in db["UserNotRegistered"].find({}, {"_id": 1, "name": 1, "first_name": 1, "last_name": 1}):
                uid = str(u["_id"])
                nm = (str(u.get("name") or "").strip()
                      or f"{u.get('first_name','')} {u.get('last_name','')}".strip())
                if nm:
                    _search_user_map[uid] = normalize_cbc_text(nm)
        except Exception:
            pass

        for r in db["Leave_Request"].find({}):
            # Resolve staff name via unRegisterUser_id
            uid = str(r.get("unRegisterUser_id") or "").strip()
            staff = _search_user_map.get(uid) or normalize_cbc_text(
                str(r.get("name") or r.get("staff_name") or r.get("employee_name") or "")).strip() or "—"
            rid = str(r.get("_id", ""))[-8:]
            sub = resolve_sub(r.get("subsidiary_id", ""), sub_map)
            vendor = normalize_cbc_text(str(r.get("leave_Details") or "Leave")).strip()
            combined = " ".join([staff, vendor, rid, sub, "Leave"]).lower()
            if q in combined:
                dt = record_date(r)
                results.append({
                    "module": "Leave",
                    "staff": staff,
                    "vendor": staff,   # show applicant name in vendor column
                    "subsidiary": sub,
                    "amount": "Nill",
                    "status": str(r.get("status") or "").title() or "Unknown",
                    "date": dt.strftime("%d %b %Y") if dt else "—",
                    "request_id": rid,
                })
        for r in db["CashAdvance"].find({}):
            _add("Cash Advance", r, "name", "name", lambda rec: safe_amount(rec.get("amount", 0)))
        for r in db["ExpenseClaim"].find({}):
            _add("Expense Claim", r, "staff_name", "staff_name", ec_amt)
        for r in db["RequestToPaySupplier"].find({}):
            _add("RTPS", r, "staff_name", "name_of_supplier", lambda rec: safe_amount(rec.get("amount", 0)))

        results.sort(key=lambda x: (x["status"] == "Pending"), reverse=True)
        return jsonify(results=results[:12])
    except Exception as exc:
        return jsonify(results=[], error=str(exc)), 500


@app.route("/page/<tab>")
def dashboard_page(tab):
    valid_tabs = {"overview", "pending-approvals", "rtps-analytics", "cash-advance", "subsidiaries", "audit-risk", "vendor-insights", "reports"}
    if tab not in valid_tabs:
        tab = "overview"
    now = app_now().strftime("%d %b %Y, %H:%M")
    try:
        now, filters, sub_options, d = dashboard_payload()
    except (PyMongoError, RuntimeError, ValueError) as exc:
        return render_template_string(
            ERROR_TEMPLATE,
            error_message=str(exc),
            now=now,
        ), 500

    BLUE = "#1155cc"
    BLUE_LIGHT = "#1d6ff2"
    BLUE_SOFT = "#4c93ff"
    BLUE_DEEP = "#0b3a75"

    charts = {}
    if tab in {"overview", "rtps-analytics", "vendor-insights", "cash-advance", "subsidiaries", "audit-risk", "reports", "pending-approvals"}:
        # Leave charts — always built (shown on overview and any tab that needs them)
        charts["leave_by_sub"]  = bar(
            list(d["leave_by_sub"].keys()), list(d["leave_by_sub"].values()),
            "Leave Requests by Subsidiary (count)", BLUE)
        charts["leave_by_type"] = bar(
            list(d["leave_by_type"].keys()), list(d["leave_by_type"].values()),
            "Leave Requests by Type (count)", BLUE_LIGHT)
        charts["leave_status"]  = pie(
            ["Approved", "Pending", "Rejected"],
            [d["leave_approved"], d["leave_pending"], d["leave_rejected"]],
            "Leave Request Status")
        charts["leave_trend"]   = line(
            list(d["leave_by_month"].keys()), list(d["leave_by_month"].values()),
            "Leave Requests — Monthly Trend (count)", BLUE)
        charts["leave_days_by_type"] = bar(
            list(d["leave_days_by_type"].keys()), list(d["leave_days_by_type"].values()),
            "Total Days Applied by Leave Type", BLUE_SOFT)
    if tab in {"rtps-analytics", "vendor-insights"}:
        charts["rtps_by_sub"]    = bar(list(d["rtps_by_sub"].keys()), list(d["rtps_by_sub"].values()), "RTPS Amount by Subsidiary (₦)", BLUE_DEEP)
        charts["rtps_by_mode"]   = pie(list(d["rtps_by_mode"].keys()), list(d["rtps_by_mode"].values()), "Payment Mode")
        charts["rtps_suppliers"] = hbar(list(d["rtps_by_supplier"].values()), list(d["rtps_by_supplier"].keys()), "Top 10 Suppliers by Amount (₦)", BLUE_DEEP)
        charts["rtps_trend"]     = line(list(d["rtps_by_month"].keys()), list(d["rtps_by_month"].values()), "RTPS - Monthly Trend (₦)", BLUE_DEEP)
    if tab == "cash-advance":
        charts["ca_by_sub"]  = bar(list(d["ca_by_sub"].keys()), list(d["ca_by_sub"].values()), "Cash Advance Amount by Subsidiary (₦)", BLUE_LIGHT)
        charts["ca_status"]  = pie(["Approved","Pending"], [d["ca_approved"], d["ca_pending"]], "Cash Advance Status")
        charts["ca_trend"]   = line(list(d["ca_by_month"].keys()), list(d["ca_by_month"].values()), "Cash Advance - Monthly Trend (₦)", BLUE_LIGHT)
        charts["ec_by_sub"]  = bar(list(d["ec_by_sub"].keys()), list(d["ec_by_sub"].values()), "Expense Claims Amount by Subsidiary (₦)", BLUE_SOFT)
        charts["ec_status"]  = pie(["Approved","Pending"], [d["ec_approved"], d["ec_pending"]], "Expense Claim Status")
        charts["ec_trend"]   = line(list(d["ec_by_month"].keys()), list(d["ec_by_month"].values()), "Expense Claims - Monthly Trend (₦)", BLUE_SOFT)

    return render_template_string(
        EXEC_TEMPLATE,
        d=d,
        charts=charts,
        now=now,
        filters=filters,
        sub_options=sub_options,
        report_query=request.query_string.decode("utf-8"),
        nav_query=_build_nav_query(filters),
        active_tab=tab,
    )


@app.route("/executive-report")
def executive_report():
    now = app_now().strftime("%d %b %Y, %H:%M")
    try:
        now, filters, _sub_options, d = dashboard_payload()
        pdf = build_executive_pdf(d, filters, now)
        subsidiary = filters.get("subsidiary", "All").lower().replace(" ", "-")
        return send_file(
            pdf,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"{subsidiary}-igez-executive-{app_now().strftime('%Y-%m-%d')}.pdf",
        )
    except (PyMongoError, RuntimeError, ValueError, ImportError) as exc:
        return render_template_string(
            ERROR_TEMPLATE,
            error_message=str(exc),
            now=now,
        ), 500


if __name__ == "__main__":
    print("Starting dashboard...")
    print("Open in browser:  http://127.0.0.1:8050")
    app.run(debug=False, port=8050, use_reloader=False)
