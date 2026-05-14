"""
dashboard.py
------------
Analytics dashboard for CBC Paperless App.
Run:  python dashboard.py
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
from flask import Flask, render_template_string, request
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
import json

load_dotenv()

app = Flask(__name__)
DB_URL = os.getenv("DATABASE_URL")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Africa/Lagos")
LOCAL_TZ = ZoneInfo(APP_TIMEZONE)

# ── DB helpers ────────────────────────────────────────────────────────────────

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


def get_subsidiary_map(db):
    return {
        str(s["_id"]): s.get("subsidiary_name", "Unknown")
        for s in db["Subsidiary"].find({})
    }


def resolve_sub(sub_id, sub_map):
    return sub_map.get(str(sub_id), "Unknown")


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
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m")
    return "Unknown"


def app_now():
    return datetime.now(LOCAL_TZ).replace(tzinfo=None)


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
    return dt if isinstance(dt, datetime) else None


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

# ── Data loaders ──────────────────────────────────────────────────────────────

def load_all(db, sub_map, filters=None):
    data = {}
    filters = filters or {}
    start_date = filters.get("start_date")
    end_date = filters.get("end_date")
    selected_sub = filters.get("subsidiary", "All")

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

    # ── Leave Requests ────────────────────────────────────────────────────────
    all_leaves = list(db["Leave_Request"].find({}))
    leaves = apply_filters(all_leaves)
    data["leave_total"]   = len(leaves)
    data["leave_approved"]= sum(1 for r in leaves if r.get("status") == "Approved")
    data["leave_pending"] = sum(1 for r in leaves if r.get("status") == "Pending")

    leave_by_sub = defaultdict(int)
    leave_by_type = defaultdict(int)
    leave_by_month = defaultdict(int)
    for r in leaves:
        leave_by_sub[resolve_sub(r.get("subsidiary_id",""), sub_map)] += 1
        leave_by_type[r.get("leave_Details", "Unknown")] += 1
        leave_by_month[month_label(r.get("createdAt"))] += 1

    data["leave_by_sub"]   = dict(sorted(leave_by_sub.items()))
    data["leave_by_type"]  = dict(sorted(leave_by_type.items()))
    data["leave_by_month"] = dict(sorted(leave_by_month.items()))

    # ── Cash Advance ──────────────────────────────────────────────────────────
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

    # ── Expense Claims ────────────────────────────────────────────────────────
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

    # ── RTPS ──────────────────────────────────────────────────────────────────
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
        rtps_by_supplier[r.get("name_of_supplier","Unknown")] += amt
        rtps_by_mode[r.get("mode_of_payment","Unknown")] += 1

    # Top 10 suppliers
    top_suppliers = dict(sorted(rtps_by_supplier.items(),
                                key=lambda x: x[1], reverse=True)[:10])
    data["rtps_by_sub"]      = dict(sorted(rtps_by_sub.items()))
    data["rtps_by_month"]    = dict(sorted(rtps_by_month.items()))
    data["rtps_by_supplier"] = top_suppliers
    data["rtps_by_mode"]     = dict(rtps_by_mode)

    # ── Approval rates ────────────────────────────────────────────────────────
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

    # ── Executive signals ────────────────────────────────────────────────────
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

    def waiting_stage(records, field, previous_field=None):
        return sum(
            1 for r in records
            if r.get("status") == "Pending"
            and r.get(field) is not True
            and (previous_field is None or r.get(previous_field) is True)
        )

    bottlenecks = [
        {"label": "HOD - Leave", "count": waiting_stage(leaves, "is_HOD_Approved")},
        {"label": "HR - Leave", "count": waiting_stage(leaves, "is_HR_Approved", "is_HOD_Approved")},
        {"label": "HOD - Cash Adv", "count": waiting_stage(advances, "is_HOD_Approved")},
        {"label": "CFO - Cash Adv", "count": waiting_stage(advances, "is_CFO_Approved", "is_HOD_Approved")},
        {"label": "CEO - Cash Adv", "count": waiting_stage(advances, "is_CEO_Approved", "is_CFO_Approved")},
        {"label": "HOD - Expense", "count": waiting_stage(expenses, "is_HOD_Approved")},
        {"label": "CFO - Expense", "count": waiting_stage(expenses, "is_CFO_Approved", "is_HOD_Approved")},
        {"label": "CEO - Expense", "count": waiting_stage(expenses, "is_CEO_Approved", "is_CFO_Approved")},
        {"label": "HOD - RTPS", "count": waiting_stage(rtps, "is_HOD_Approved")},
        {"label": "CFO - RTPS", "count": waiting_stage(rtps, "is_CFO_Approved", "is_HOD_Approved")},
        {"label": "CEO - RTPS", "count": waiting_stage(rtps, "is_CEO_Approved", "is_CFO_Approved")},
    ]
    data["bottlenecks"] = sorted(bottlenecks, key=lambda x: x["count"], reverse=True)[:6]

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
    data["month_compare"] = {
        "requests": current_requests,
        "requests_change": format_change(amount_change(current_requests, previous_requests)),
        "spend": current_spend,
        "spend_change": format_change(amount_change(current_spend, previous_spend)),
    }

    data["insights"] = [
        f"{highest_pending_module} has the highest waiting approval spend at NGN {highest_pending_amount:,.0f}.",
        f"{top_sub} is the highest spending subsidiary in this view at NGN {top_sub_amount:,.0f}.",
        f"{lowest_approval_label} currently has the lowest approval rate at {lowest_approval_rate}%.",
    ]

    return data

# ── Chart builders ────────────────────────────────────────────────────────────

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


def bar(x, y, title, color="#1155cc", xlabel="", ylabel=""):
    fig = go.Figure(go.Bar(
        x=x, y=y,
        marker=dict(color=blue_scale(len(y)), line=dict(color="rgba(255,255,255,0.75)", width=1)),
        text=[f"{v:,.0f}" for v in y],
        textposition="outside"
    ))
    chart_layout(fig, title, 320, dict(t=50, b=40, l=40, r=20), xlabel=xlabel, ylabel=ylabel)
    return fig.to_html(full_html=False, include_plotlyjs=False)


def pie(labels, values, title):
    fig = go.Figure(go.Pie(
        labels=labels, values=values,
        hole=0.4,
        marker=dict(colors=blue_scale(len(values)), line=dict(color="white", width=2))
    ))
    chart_layout(fig, title, 320, dict(t=50, b=20, l=20, r=20))
    return fig.to_html(full_html=False, include_plotlyjs=False)


def line(x, y, title, color="#1155cc"):
    fig = go.Figure(go.Scatter(
        x=x, y=y, mode="lines+markers",
        fill="tozeroy",
        fillcolor="rgba(76,147,255,0.18)",
        line=dict(color=color, width=3, shape="spline"),
        marker=dict(size=7, color="#ffffff", line=dict(color=color, width=2))
    ))
    chart_layout(fig, title, 300, dict(t=50, b=40, l=40, r=20))
    return fig.to_html(full_html=False, include_plotlyjs=False)


def hbar(x, y, title, color="#1155cc"):
    fig = go.Figure(go.Bar(
        x=x, y=y, orientation="h",
        marker=dict(color=blue_scale(len(x)), line=dict(color="rgba(255,255,255,0.75)", width=1)),
        text=[f"₦{v:,.0f}" for v in x],
        textposition="outside"
    ))
    chart_layout(fig, title, 340, dict(t=50, b=20, l=120, r=60), font_size=10)
    return fig.to_html(full_html=False, include_plotlyjs=False)

# ── HTML template ─────────────────────────────────────────────────────────────

TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>IGEZ — Analytics Dashboard</title>
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
    .filter-bar, .executive-grid, .signal-grid, .chart-grid-2, .chart-grid-3 { grid-template-columns: 1fr; }
    .filter-hint { grid-column: 1; text-align: left; }
  }
</style>
</head>
<body>

<div class="header">
  <div>
    <div class="h1">IGEZ — Analytics Dashboard</div>
    <div class="sub">Live data from MongoDB · Last refreshed: {{ now }}</div>
  </div>
  <a href="/" class="refresh-btn">↻ Refresh</a>
</div>

<div class="container">

  <form class="filter-bar" method="get" id="filterForm">
    <label>
      Period
      <select name="period" id="periodFilter" {% if filters.date_selection_active %}class="period-disabled" disabled{% endif %}>
        {% if filters.date_selection_active %}
        <option value="date_active" selected>🚫 Date range active</option>
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
      <span class="filter-alert" id="startDateHint">{% if filters.period_filter_active %}🚫 Period active{% elif filters.date_range_incomplete and not filters.start %}Select From{% endif %}</span>
    </label>
    <label>
      To
      <input type="{% if filters.period_filter_active %}text{% else %}date{% endif %}" name="end" id="endDateFilter" value="{% if filters.period_filter_active %}Period active{% else %}{{ filters.end }}{% endif %}" data-date-value="{{ filters.end }}" {% if filters.period_filter_active %}disabled{% endif %}>
      <span class="filter-alert" id="endDateHint">{% if filters.period_filter_active %}🚫 Period active{% elif filters.date_range_incomplete and not filters.end %}Select To{% endif %}</span>
    </label>
    <button type="submit" id="applyFilters" {% if filters.date_range_incomplete or filters.date_range_invalid %}disabled{% endif %}>Apply</button>
    <div class="filter-hint" id="filterHint">{% if filters.date_range_invalid %}From date cannot be later than To date.{% elif filters.date_range_incomplete %}Select both From and To dates before applying.{% endif %}</div>
  </form>

  <div class="executive-grid">
    <div class="signal-grid">
      <div class="signal" style="--accent:#1155cc">
        <div class="label">Approved Spend</div>
        <div class="value">NGN {{ "{:,.0f}".format(d.approved_spend) }}</div>
        <div class="sub">Cleared across CA, EC and RTPS</div>
      </div>
      <div class="signal" style="--accent:#1d6ff2">
        <div class="label">Waiting Approval</div>
        <div class="value">NGN {{ "{:,.0f}".format(d.pending_spend) }}</div>
        <div class="sub">{{ d.highest_pending_module }} is the largest queue</div>
      </div>
      <div class="signal" style="--accent:#4c93ff">
        <div class="label">This Month</div>
        <div class="value">NGN {{ "{:,.0f}".format(d.month_compare.spend) }}</div>
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
        <div class="sub">NGN {{ "{:,.0f}".format(d.top_spending_subsidiary_amount) }}</div>
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
        <div class="bottleneck-row"><span>{{ item.label }}</span><strong>{{ item.count }}</strong></div>
        {% endfor %}
      </div>
      {% endif %}
    </div>
  </div>

  <!-- ── KPI OVERVIEW ── -->
  <div class="section-title">Overview</div>
  <div class="kpi-grid">
    <div class="kpi" style="--accent:#1155cc">
      <div class="label">Leave Requests</div>
      <div class="value">{{ d.leave_approved }}/{{ d.leave_total }}</div>
      <div class="sub">{{ d.leave_approved }} approved · {{ d.leave_pending }} pending</div>
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

  <!-- ── LEAVE REQUESTS ── -->
  <div class="section-title">Leave Requests</div>
  <div class="chart-grid-3">
    <div class="chart-card">{{ charts.leave_by_sub | safe }}</div>
    <div class="chart-card">{{ charts.leave_by_type | safe }}</div>
    <div class="chart-card">{{ charts.leave_status | safe }}</div>
  </div>
  <div class="chart-grid-2" style="margin-top:16px">
    <div class="chart-card full">{{ charts.leave_trend | safe }}</div>
  </div>

  <!-- ── CASH ADVANCE ── -->
  <div class="section-title">Cash Advance</div>
  <div class="chart-grid-2">
    <div class="chart-card">{{ charts.ca_by_sub | safe }}</div>
    <div class="chart-card">{{ charts.ca_status | safe }}</div>
  </div>
  <div class="chart-grid-2" style="margin-top:16px">
    <div class="chart-card full">{{ charts.ca_trend | safe }}</div>
  </div>

  <!-- ── EXPENSE CLAIMS ── -->
  <div class="section-title">Expense Claims</div>
  <div class="chart-grid-2">
    <div class="chart-card">{{ charts.ec_by_sub | safe }}</div>
    <div class="chart-card">{{ charts.ec_status | safe }}</div>
  </div>
  <div class="chart-grid-2" style="margin-top:16px">
    <div class="chart-card full">{{ charts.ec_trend | safe }}</div>
  </div>

  <!-- ── RTPS ── -->
  <div class="section-title">Request to Pay Supplier (RTPS)</div>
  <div class="chart-grid-2">
    <div class="chart-card">{{ charts.rtps_by_sub | safe }}</div>
    <div class="chart-card">{{ charts.rtps_by_mode | safe }}</div>
  </div>
  <div class="chart-grid-2" style="margin-top:16px">
    <div class="chart-card">{{ charts.rtps_suppliers | safe }}</div>
    <div class="chart-card">{{ charts.rtps_trend | safe }}</div>
  </div>

  <!-- ── APPROVAL RATES ── -->
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

<div class="footer">IGEZ Analytics Dashboard · Data live from MongoDB Atlas</div>
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
      option = new Option("🚫 Date range active", periodDisabledValue);
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

# ── Route ─────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    now = app_now().strftime("%d %b %Y, %H:%M")
    try:
        db      = get_db()
        sub_map = get_subsidiary_map(db)
    except (RuntimeError, ConfigurationError, ServerSelectionTimeoutError, PyMongoError) as exc:
        return render_template_string(
            ERROR_TEMPLATE,
            error_message=str(exc),
            now=now,
        ), 500

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
    try:
        d       = load_all(db, sub_map, filters)
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

    # Leave
    charts["leave_by_sub"]  = bar(list(d["leave_by_sub"].keys()),
                                   list(d["leave_by_sub"].values()),
                                   "Leave Requests by Subsidiary", BLUE)
    charts["leave_by_type"] = pie(list(d["leave_by_type"].keys()),
                                   list(d["leave_by_type"].values()),
                                   "Leave Type Breakdown")
    charts["leave_status"]  = pie(["Approved","Pending"],
                                   [d["leave_approved"], d["leave_pending"]],
                                   "Leave Status")
    charts["leave_trend"]   = line(list(d["leave_by_month"].keys()),
                                    list(d["leave_by_month"].values()),
                                    "Leave Requests — Monthly Trend", BLUE)

    # Cash Advance
    charts["ca_by_sub"]  = bar(list(d["ca_by_sub"].keys()),
                                list(d["ca_by_sub"].values()),
                                "Cash Advance Amount by Subsidiary (₦)", BLUE_LIGHT)
    charts["ca_status"]  = pie(["Approved","Pending"],
                                [d["ca_approved"], d["ca_pending"]],
                                "Cash Advance Status")
    charts["ca_trend"]   = line(list(d["ca_by_month"].keys()),
                                 list(d["ca_by_month"].values()),
                                 "Cash Advance — Monthly Trend (₦)", BLUE_LIGHT)

    # Expense Claims
    charts["ec_by_sub"]  = bar(list(d["ec_by_sub"].keys()),
                                list(d["ec_by_sub"].values()),
                                "Expense Claims Amount by Subsidiary (₦)", BLUE_SOFT)
    charts["ec_status"]  = pie(["Approved","Pending"],
                                [d["ec_approved"], d["ec_pending"]],
                                "Expense Claim Status")
    charts["ec_trend"]   = line(list(d["ec_by_month"].keys()),
                                 list(d["ec_by_month"].values()),
                                 "Expense Claims — Monthly Trend (₦)", BLUE_SOFT)

    # RTPS
    charts["rtps_by_sub"]   = bar(list(d["rtps_by_sub"].keys()),
                                   list(d["rtps_by_sub"].values()),
                                   "RTPS Amount by Subsidiary (₦)", BLUE_DEEP)
    charts["rtps_by_mode"]  = pie(list(d["rtps_by_mode"].keys()),
                                   list(d["rtps_by_mode"].values()),
                                   "Payment Mode")
    charts["rtps_suppliers"]= hbar(list(d["rtps_by_supplier"].values()),
                                    list(d["rtps_by_supplier"].keys()),
                                    "Top 10 Suppliers by Amount (₦)", BLUE_DEEP)
    charts["rtps_trend"]    = line(list(d["rtps_by_month"].keys()),
                                    list(d["rtps_by_month"].values()),
                                    "RTPS — Monthly Trend (₦)", BLUE_DEEP)

    return render_template_string(
        TEMPLATE, d=d, charts=charts, now=now, filters=filters, sub_options=sub_options
    )


if __name__ == "__main__":
    print("Starting dashboard...")
    print("Open in browser:  http://127.0.0.1:8050")
    app.run(debug=True, port=8050)
