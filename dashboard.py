"""
dashboard.py
------------
Analytics dashboard for CBC Paperless App.
Run:  python dashboard.py
Then open:  http://127.0.0.1:5000
"""

import os
from datetime import datetime
from collections import defaultdict
from dotenv import load_dotenv
from pymongo import MongoClient
from flask import Flask, render_template_string
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
import json

load_dotenv()

app = Flask(__name__)
DB_URL = os.getenv("DATABASE_URL")

# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db():
    client = MongoClient(DB_URL, serverSelectionTimeoutMS=8000)
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
        return float(str(val).replace(",", "").strip())
    except Exception:
        return 0.0


def month_label(dt):
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m")
    return "Unknown"

# ── Data loaders ──────────────────────────────────────────────────────────────

def load_all(db, sub_map):
    data = {}

    # ── Leave Requests ────────────────────────────────────────────────────────
    leaves = list(db["Leave_Request"].find({}))
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
    advances = list(db["CashAdvance"].find({}))
    data["ca_total"]    = len(advances)
    data["ca_approved"] = sum(1 for r in advances if r.get("status") == "Approved")
    data["ca_pending"]  = sum(1 for r in advances if r.get("status") == "Pending")
    data["ca_amount"]   = sum(safe_amount(r.get("amount", 0)) for r in advances)
    data["ca_approved_amount"] = sum(
        safe_amount(r.get("amount", 0)) for r in advances if r.get("status") == "Approved"
    )
    data["ca_pending_amount"] = sum(
        safe_amount(r.get("amount", 0)) for r in advances if r.get("status") == "Pending"
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
    expenses = list(db["ExpenseClaim"].find({}))
    data["ec_total"]    = len(expenses)
    data["ec_approved"] = sum(1 for r in expenses if r.get("status") == "Approved")
    data["ec_pending"]  = sum(1 for r in expenses if r.get("status") == "Pending")

    def ec_total_amount(r):
        items = r.get("expense_claim", [])
        if isinstance(items, list):
            return sum(safe_amount(i.get("value", 0)) for i in items if isinstance(i, dict))
        return 0.0

    data["ec_amount"] = sum(ec_total_amount(r) for r in expenses)
    data["ec_approved_amount"] = sum(
        ec_total_amount(r) for r in expenses if r.get("status") == "Approved"
    )
    data["ec_pending_amount"] = sum(
        ec_total_amount(r) for r in expenses if r.get("status") == "Pending"
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
    rtps = list(db["RequestToPaySupplier"].find({}))
    data["rtps_total"]    = len(rtps)
    data["rtps_approved"] = sum(1 for r in rtps if r.get("status") == "Approved")
    data["rtps_pending"]  = sum(1 for r in rtps if r.get("status") == "Pending")
    data["rtps_amount"]   = sum(safe_amount(r.get("amount", 0)) for r in rtps)
    data["rtps_approved_amount"] = sum(
        safe_amount(r.get("amount", 0)) for r in rtps if r.get("status") == "Approved"
    )
    data["rtps_pending_amount"] = sum(
        safe_amount(r.get("amount", 0)) for r in rtps if r.get("status") == "Pending"
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

    return data

# ── Chart builders ────────────────────────────────────────────────────────────

def bar(x, y, title, color="#2c3e50", xlabel="", ylabel=""):
    fig = go.Figure(go.Bar(
        x=x, y=y,
        marker_color=color,
        text=[f"{v:,.0f}" for v in y],
        textposition="outside"
    ))
    fig.update_layout(
        title=title, xaxis_title=xlabel, yaxis_title=ylabel,
        plot_bgcolor="white", paper_bgcolor="white",
        margin=dict(t=50, b=40, l=40, r=20),
        font=dict(family="Segoe UI", size=11),
        height=320
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


def pie(labels, values, title):
    fig = go.Figure(go.Pie(
        labels=labels, values=values,
        hole=0.4,
        marker=dict(colors=px.colors.qualitative.Set2)
    ))
    fig.update_layout(
        title=title,
        paper_bgcolor="white",
        margin=dict(t=50, b=20, l=20, r=20),
        font=dict(family="Segoe UI", size=11),
        height=320
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


def line(x, y, title, color="#2980b9"):
    fig = go.Figure(go.Scatter(
        x=x, y=y, mode="lines+markers",
        line=dict(color=color, width=2),
        marker=dict(size=6)
    ))
    fig.update_layout(
        title=title,
        plot_bgcolor="white", paper_bgcolor="white",
        margin=dict(t=50, b=40, l=40, r=20),
        font=dict(family="Segoe UI", size=11),
        height=300
    )
    return fig.to_html(full_html=False, include_plotlyjs=False)


def hbar(x, y, title, color="#27ae60"):
    fig = go.Figure(go.Bar(
        x=x, y=y, orientation="h",
        marker_color=color,
        text=[f"₦{v:,.0f}" for v in x],
        textposition="outside"
    ))
    fig.update_layout(
        title=title,
        plot_bgcolor="white", paper_bgcolor="white",
        margin=dict(t=50, b=20, l=120, r=60),
        font=dict(family="Segoe UI", size=10),
        height=340
    )
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
  body { font-family: 'Segoe UI', sans-serif; background: #f0f2f5; color: #333; }

  /* Header */
  .header {
    background: linear-gradient(135deg, #2c3e50, #3498db);
    color: white; padding: 20px 32px;
    display: flex; align-items: center; justify-content: space-between;
  }
  .header h1 { font-size: 22px; font-weight: 600; }
  .header .sub { font-size: 13px; opacity: 0.8; margin-top: 4px; }
  .refresh-btn {
    background: rgba(255,255,255,0.2); border: 1px solid rgba(255,255,255,0.4);
    color: white; padding: 8px 18px; border-radius: 6px;
    cursor: pointer; font-size: 13px; text-decoration: none;
  }
  .refresh-btn:hover { background: rgba(255,255,255,0.3); }

  /* Layout */
  .container { max-width: 1400px; margin: 0 auto; padding: 24px; }
  .section-title {
    font-size: 16px; font-weight: 600; color: #2c3e50;
    margin: 28px 0 14px; padding-left: 10px;
    border-left: 4px solid #3498db;
  }

  /* KPI cards */
  .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; }
  .kpi {
    background: white; border-radius: 10px; padding: 18px 20px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.08);
    border-top: 4px solid var(--accent, #3498db);
  }
  .kpi .label { font-size: 12px; color: #888; text-transform: uppercase; letter-spacing: 0.5px; }
  .kpi .value { font-size: 28px; font-weight: 700; color: #2c3e50; margin: 6px 0 2px; }
  .kpi .sub   { font-size: 12px; color: #aaa; }
  .amount-breakdown { margin-top: 10px; display: grid; gap: 5px; font-size: 12px; color: #555; }
  .amount-breakdown div { display: flex; justify-content: space-between; gap: 12px; }
  .amount-breakdown strong { color: #2c3e50; font-weight: 600; }

  /* Chart grid */
  .chart-grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .chart-grid-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; }
  .chart-card {
    background: white; border-radius: 10px; padding: 16px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.08);
  }
  .chart-card.full { grid-column: 1 / -1; }

  /* Approval table */
  .approval-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .approval-table th {
    background: #2c3e50; color: white;
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
    .chart-grid-2, .chart-grid-3 { grid-template-columns: 1fr; }
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

  <!-- ── KPI OVERVIEW ── -->
  <div class="section-title">Overview</div>
  <div class="kpi-grid">
    <div class="kpi" style="--accent:#3498db">
      <div class="label">Leave Requests</div>
      <div class="value">{{ d.leave_total }}</div>
      <div class="sub">{{ d.leave_approved }} approved · {{ d.leave_pending }} pending</div>
    </div>
    <div class="kpi" style="--accent:#e67e22">
      <div class="label">Cash Advances</div>
      <div class="value">{{ d.ca_total }}</div>
      <div class="sub">₦{{ "{:,.0f}".format(d.ca_amount) }} total</div>
      <div class="amount-breakdown">
        <div><span>Approved</span><strong>₦{{ "{:,.0f}".format(d.ca_approved_amount) }}</strong></div>
        <div><span>Waiting approval</span><strong>₦{{ "{:,.0f}".format(d.ca_pending_amount) }}</strong></div>
      </div>
    </div>
    <div class="kpi" style="--accent:#9b59b6">
      <div class="label">Expense Claims</div>
      <div class="value">{{ d.ec_total }}</div>
      <div class="sub">₦{{ "{:,.0f}".format(d.ec_amount) }} total</div>
      <div class="amount-breakdown">
        <div><span>Approved</span><strong>₦{{ "{:,.0f}".format(d.ec_approved_amount) }}</strong></div>
        <div><span>Waiting approval</span><strong>₦{{ "{:,.0f}".format(d.ec_pending_amount) }}</strong></div>
      </div>
    </div>
    <div class="kpi" style="--accent:#27ae60">
      <div class="label">RTPS</div>
      <div class="value">{{ d.rtps_total }}</div>
      <div class="sub">₦{{ "{:,.0f}".format(d.rtps_amount) }} total</div>
      <div class="amount-breakdown">
        <div><span>Approved</span><strong>₦{{ "{:,.0f}".format(d.rtps_approved_amount) }}</strong></div>
        <div><span>Waiting approval</span><strong>₦{{ "{:,.0f}".format(d.rtps_pending_amount) }}</strong></div>
      </div>
    </div>
    <div class="kpi" style="--accent:#e74c3c">
      <div class="label">Total Requests</div>
      <div class="value">{{ d.leave_total + d.ca_total + d.ec_total + d.rtps_total }}</div>
      <div class="sub">Across all modules</div>
    </div>
    <div class="kpi" style="--accent:#1abc9c">
      <div class="label">Total Spend</div>
      <div class="value" style="font-size:20px">₦{{ "{:,.0f}".format(d.ca_amount + d.ec_amount + d.rtps_amount) }}</div>
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
                background:{% if rate >= 75 %}#27ae60{% elif rate >= 50 %}#f39c12{% else %}#e74c3c{% endif %}">
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
</body>
</html>
"""

# ── Route ─────────────────────────────────────────────────────────────────────

@app.route("/")
def dashboard():
    db      = get_db()
    sub_map = get_subsidiary_map(db)
    d       = load_all(db, sub_map)

    BLUE   = "#3498db"
    ORANGE = "#e67e22"
    PURPLE = "#9b59b6"
    GREEN  = "#27ae60"

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
                                "Cash Advance Amount by Subsidiary (₦)", ORANGE)
    charts["ca_status"]  = pie(["Approved","Pending"],
                                [d["ca_approved"], d["ca_pending"]],
                                "Cash Advance Status")
    charts["ca_trend"]   = line(list(d["ca_by_month"].keys()),
                                 list(d["ca_by_month"].values()),
                                 "Cash Advance — Monthly Trend (₦)", ORANGE)

    # Expense Claims
    charts["ec_by_sub"]  = bar(list(d["ec_by_sub"].keys()),
                                list(d["ec_by_sub"].values()),
                                "Expense Claims Amount by Subsidiary (₦)", PURPLE)
    charts["ec_status"]  = pie(["Approved","Pending"],
                                [d["ec_approved"], d["ec_pending"]],
                                "Expense Claim Status")
    charts["ec_trend"]   = line(list(d["ec_by_month"].keys()),
                                 list(d["ec_by_month"].values()),
                                 "Expense Claims — Monthly Trend (₦)", PURPLE)

    # RTPS
    charts["rtps_by_sub"]   = bar(list(d["rtps_by_sub"].keys()),
                                   list(d["rtps_by_sub"].values()),
                                   "RTPS Amount by Subsidiary (₦)", GREEN)
    charts["rtps_by_mode"]  = pie(list(d["rtps_by_mode"].keys()),
                                   list(d["rtps_by_mode"].values()),
                                   "Payment Mode")
    charts["rtps_suppliers"]= hbar(list(d["rtps_by_supplier"].values()),
                                    list(d["rtps_by_supplier"].keys()),
                                    "Top 10 Suppliers by Amount (₦)", GREEN)
    charts["rtps_trend"]    = line(list(d["rtps_by_month"].keys()),
                                    list(d["rtps_by_month"].values()),
                                    "RTPS — Monthly Trend (₦)", GREEN)

    now = datetime.now().strftime("%d %b %Y, %H:%M")
    return render_template_string(TEMPLATE, d=d, charts=charts, now=now)


if __name__ == "__main__":
    print("Starting dashboard...")
    print("Open in browser:  http://127.0.0.1:8050")
    app.run(debug=True, port=8050)
