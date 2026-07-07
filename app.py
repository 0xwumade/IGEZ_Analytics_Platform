# app.py — Vercel entry point
# Vercel looks for a WSGI callable named `app` in this file.
# All routes and logic live in dashboard.py; we simply re-export the Flask app.

from analytics_platform import app  # noqa: F401  (re-exported as the WSGI handler)
