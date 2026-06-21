"""Shared runtime config used across modules (kept tiny to avoid import cycles).

APP_TZ lives here so both the API layer (`main.py`, for month bucketing) and the
ingestion pipeline (`ingestion.py`, for normalizing naive client timestamps) can
use it without importing each other.
"""
import os
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

# Transactions are bucketed into months in the user's timezone, not UTC — a
# payment at 00:30 IST on the 1st belongs to the new month even though it's still
# last month in UTC.
APP_TZ_NAME = os.getenv("APP_TZ", "Asia/Kolkata")
try:
    APP_TZ = ZoneInfo(APP_TZ_NAME)
except Exception:
    # No IANA tz database on this host (slim serverless images) — fall back to a
    # fixed IST offset. APP_TZ_NAME is still valid for Postgres' own timezone().
    APP_TZ = timezone(timedelta(hours=5, minutes=30))
