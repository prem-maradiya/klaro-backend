import csv
import hashlib
import io
import logging
import os
import re
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import case, func, select, text
from sqlalchemy.orm import Session

from pydantic import BaseModel

from . import models, schemas
from .database import Base, engine, get_db
from .categorizer import CATEGORIES, categorize, normalize, ai_parse_sms
from .auth import get_current_user
from .observability import RATE_PARSE, init_sentry, limiter

# Error tracking — enabled only when SENTRY_DSN is set (no-op otherwise). Called
# before the app is built so it instruments request handling from the first call.
init_sentry()

# Auth is per-user now (Phase 7.1): data routes depend on get_current_user,
# which verifies a Supabase JWT and yields the user's id. The old global
# X-API-Key gate is gone; "/" and "/health" stay open for uptime checks.

# Comma-separated allowed origins for the browser dashboard
# (e.g. "https://klaro-dashboard.vercel.app,http://localhost:5173").
# Default stays wide-open for local dev.
CORS_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()
]

# Months are bucketed in the user's timezone, not UTC — a payment at 00:30 IST
# on the 1st belongs to the new month even though it's still last month in UTC.
APP_TZ_NAME = os.getenv("APP_TZ", "Asia/Kolkata")
try:
    APP_TZ = ZoneInfo(APP_TZ_NAME)
except Exception:
    # No IANA tz database on this host (slim serverless images) — fall back to
    # a fixed IST offset. APP_TZ_NAME is still used (and valid) in Postgres.
    APP_TZ = timezone(timedelta(hours=5, minutes=30))


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    """'YYYY-MM' -> [start, end) as UTC instants of that local-tz month."""
    try:
        year_s, mon_s = month.split("-")
        year, mon = int(year_s), int(mon_s)
        if not 1 <= mon <= 12:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    start = datetime(year, mon, 1, tzinfo=APP_TZ)
    end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=APP_TZ)
    return start, end


def _sms_hash(raw_sms: str | None) -> str | None:
    if not raw_sms or not raw_sms.strip():
        return None
    # Hash with ALL whitespace removed: the same SMS arrives with slightly
    # different spacing depending on the capture path (live broadcast vs inbox
    # read — observed "<Mob.No>" vs "<Mob. No>" in one IDBI message), and that
    # must not defeat dedupe.
    return hashlib.sha256(re.sub(r"\s+", "", raw_sms).encode("utf-8")).hexdigest()


def _learned_category(db: Session, user_id: str, merchant: str) -> str | None:
    norm = normalize(merchant)
    if not norm:
        return None
    row = db.get(models.MerchantCategory, (user_id, norm))
    return row.category if row else None


def _remember(db: Session, user_id: str, merchant: str, category: str | None = None,
              source: str = "user", display: str | None = None):
    """Learn a merchant for this user. `merchant` is the key (normalized).
    `display` is the preferred name to show (defaults to merchant) — used to
    learn RENAMES so a parsed name maps to the user's chosen name on future
    transactions."""
    norm = normalize(merchant)
    if not norm:
        return
    row = db.get(models.MerchantCategory, (user_id, norm))
    if row:
        if category:
            row.category = category
        if display:
            row.merchant_display = display
        row.source = source
    else:
        db.add(models.MerchantCategory(
            user_id=user_id,
            merchant_norm=norm,
            merchant_display=display or merchant,
            category=category or "Other",
            source=source,
        ))


def _learned_mapping(db: Session, user_id: str, merchant: str):
    norm = normalize(merchant)
    return db.get(models.MerchantCategory, (user_id, norm)) if norm else None


def _ensure_schema() -> None:
    """Idempotent column adds that create_all can't apply to an existing table
    (it only creates whole missing tables). Runs on every startup so a deploy
    self-migrates both local dev and the cloud DB — no manual step. Postgres
    `IF NOT EXISTS` makes each a no-op once applied."""
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE expenses ADD COLUMN IF NOT EXISTS direction "
            "VARCHAR(10) NOT NULL DEFAULT 'debit'"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_expenses_direction ON expenses (direction)"
        ))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables / apply additive migrations on startup. Never fatal: with the
    # DB unreachable (e.g. DATABASE_URL not configured yet in the cloud), /health
    # must still work — data endpoints will surface the real error.
    try:
        Base.metadata.create_all(bind=engine)
        _ensure_schema()
    except Exception:
        logging.exception("schema setup failed (database unreachable?) — continuing")
    yield


app = FastAPI(title="Expense Tracker API", version="1.0.0", lifespan=lifespan)

# Rate limiting (Phase 7.5). The default cap from observability.limiter applies
# to every route via SlowAPIMiddleware; /health and / are exempted below so
# uptime monitors are never throttled. Added BEFORE CORS so CORS stays the
# outermost middleware and 429 responses still carry CORS headers for the
# browser dashboard.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)


# Allow the Flutter app (mobile + web dashboard) to call the API.
# Added after the auth middleware so it runs first and answers preflights.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials="*" not in CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Total-Count"],
)


@app.get("/")
@limiter.exempt
def root(request: Request):
    return {"status": "ok", "service": "expense-tracker-api"}


@app.get("/health")
@limiter.exempt
def health(request: Request):
    return {"status": "healthy"}


@app.post("/expenses", response_model=schemas.ExpenseOut, status_code=201)
async def create_expense(
    payload: schemas.ExpenseCreate,
    response: Response,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    data = payload.model_dump()
    data["user_id"] = user_id
    merchant = data.get("merchant") or ""

    # The phone sends timezone-naive local timestamps (IST wall-clock). Treat
    # naive datetimes as APP_TZ — never as server time, which is UTC in the
    # cloud and would shift every transaction by +5:30.
    if data["date"].tzinfo is None:
        data["date"] = data["date"].replace(tzinfo=APP_TZ)

    # Server-side dedupe: same SMS text + amount within a ±5 min window means a
    # retry or re-import, never a new transaction — return the existing row
    # (200) so clients can never double-count. Manual entries (no raw_sms) skip
    # this and are always created.
    sms_hash = _sms_hash(data.get("raw_sms"))
    data["raw_sms_hash"] = sms_hash
    if sms_hash:
        window = timedelta(minutes=5)
        existing = db.scalars(
            select(models.Expense)
            .where(
                models.Expense.user_id == user_id,
                models.Expense.raw_sms_hash == sms_hash,
                models.Expense.amount == data["amount"],
                models.Expense.date >= data["date"] - window,
                models.Expense.date <= data["date"] + window,
            )
            .limit(1)
        ).first()
        if existing:
            response.status_code = 200
            return existing

    if data.get("direction") == "credit":
        # Income: the "merchant" is the payer, not a vendor — so don't run spend
        # categorization or learn a merchant->category mapping from it (that would
        # poison future debits). Just file it under the Income bucket.
        if not data.get("category"):
            data["category"] = "Income"
        data["is_labeled"] = True
    else:
        # Apply any learned mapping for this merchant: a remembered RENAME (preferred
        # display name) and/or category. This makes the user's corrections persist,
        # apply to ALL future transactions, and survive re-imports.
        mapping = _learned_mapping(db, user_id, merchant)
        if mapping:
            if mapping.merchant_display and normalize(mapping.merchant_display) != normalize(merchant):
                data["merchant"] = mapping.merchant_display
            if mapping.category:
                data["category"] = mapping.category
                data["is_labeled"] = True

        # Auto-categorize anything still unlabeled (brand -> keyword -> AI).
        if not data.get("is_labeled") or not data.get("category"):
            cat, source = await categorize(data.get("merchant") or "", data.get("raw_sms"))
            if cat:
                data["category"] = cat
                data["is_labeled"] = True
                if source in ("ai", "brand", "keyword"):
                    _remember(db, user_id, merchant, cat, source)

    expense = models.Expense(**data)
    db.add(expense)
    db.commit()
    db.refresh(expense)
    return expense


@app.get("/expenses", response_model=list[schemas.ExpenseOut])
def list_expenses(
    response: Response,
    skip: int = 0,
    limit: int = 200,
    unlabeled_only: bool = False,
    month: str | None = None,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    stmt = select(models.Expense).where(models.Expense.user_id == user_id)
    if unlabeled_only:
        stmt = stmt.where(models.Expense.is_labeled.is_(False))
    if month:
        start, end = _month_bounds(month)
        stmt = stmt.where(models.Expense.date >= start, models.Expense.date < end)

    # Total matching rows (pre-pagination) so clients know when to fetch more.
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    response.headers["X-Total-Count"] = str(total or 0)

    stmt = stmt.order_by(models.Expense.date.desc()).offset(skip).limit(limit)
    return db.scalars(stmt).all()


@app.get("/months")
def list_months(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    """Distinct months that have expenses (newest first) with count + total —
    drives the dashboard's month selector without fetching every row."""
    month_expr = func.to_char(
        func.timezone(APP_TZ_NAME, models.Expense.date), "YYYY-MM"
    ).label("month")
    # `total` is spending (debits) only, so the selector matches "spent". `count`
    # still includes every transaction in the month (income + spends).
    spend_sum = func.sum(
        case((models.Expense.direction == "debit", models.Expense.amount), else_=0.0)
    )
    rows = db.execute(
        select(
            month_expr,
            func.count(models.Expense.id),
            spend_sum,
        )
        .where(models.Expense.user_id == user_id)
        .group_by(month_expr)
        .order_by(month_expr.desc())
    ).all()
    return [
        {"month": m, "count": c, "total": round(float(t or 0), 2)}
        for m, c, t in rows
    ]


def _owned_expense(db: Session, expense_id: int, user_id: str) -> models.Expense:
    """Fetch an expense and assert it belongs to the caller. 404 (not 403) on a
    miss so we never reveal that someone else's id exists."""
    expense = db.get(models.Expense, expense_id)
    if expense is None or expense.user_id != user_id:
        raise HTTPException(status_code=404, detail="Expense not found")
    return expense


@app.get("/expenses/{expense_id}", response_model=schemas.ExpenseOut)
def get_expense(
    expense_id: int,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    return _owned_expense(db, expense_id, user_id)


@app.patch("/expenses/{expense_id}", response_model=schemas.ExpenseOut)
def update_expense(
    expense_id: int,
    payload: schemas.ExpenseUpdate,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    expense = _owned_expense(db, expense_id, user_id)

    old_merchant = expense.merchant
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(expense, field, value)

    # Learn the user's correction so it auto-applies to future transactions.
    if expense.merchant and expense.category:
        _remember(db, user_id, expense.merchant, expense.category, source="user")
        # Rename learning: map the ORIGINAL parsed name -> the user's chosen
        # name, so future SMS that parse to the original show the user's name.
        if normalize(old_merchant) and normalize(old_merchant) != normalize(expense.merchant):
            _remember(db, user_id, old_merchant, expense.category, source="user", display=expense.merchant)

    db.commit()
    db.refresh(expense)
    return expense


@app.delete("/expenses/{expense_id}", status_code=204)
def delete_expense(
    expense_id: int,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    db.delete(_owned_expense(db, expense_id, user_id))
    db.commit()


class ParseSmsIn(BaseModel):
    body: str
    sender: str = ""


@app.post("/parse-sms")
@limiter.limit(RATE_PARSE)
async def parse_sms(
    request: Request,
    payload: ParseSmsIn,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    """AI fallback parser for bank SMS the on-device regex couldn't handle.
    Returns a structured transaction (or is_transaction=false)."""
    result = await ai_parse_sms(payload.body, payload.sender)
    # Cache the merchant->category so future identical merchants are instant.
    if result.get("is_transaction") and result.get("category") and result.get("merchant"):
        _remember(db, user_id, result["merchant"], result["category"], source="ai")
        db.commit()
    return result


@app.get("/categorize")
async def categorize_merchant(
    merchant: str,
    raw_sms: str | None = None,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    """Classify a merchant name. Used by the app/dashboard to preview a category."""
    cat = _learned_category(db, user_id, merchant)
    source = "learned"
    if not cat:
        cat, source = await categorize(merchant, raw_sms)
    return {"merchant": merchant, "category": cat, "source": source}


# ---------------------------------------------------------------------------
# Budgets — monthly limits shared by the app and the dashboard. Keyed by
# category name; "overall" caps the whole month.
# ---------------------------------------------------------------------------

def _valid_budget_category(category: str) -> str:
    if category == "overall" or category in CATEGORIES:
        return category
    raise HTTPException(
        status_code=422,
        detail=f"category must be 'overall' or one of {CATEGORIES}",
    )


@app.get("/budgets", response_model=list[schemas.BudgetOut])
def list_budgets(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    return db.scalars(
        select(models.Budget)
        .where(models.Budget.user_id == user_id)
        .order_by(models.Budget.category)
    ).all()


@app.put("/budgets/{category}", response_model=schemas.BudgetOut)
def upsert_budget(
    category: str,
    payload: schemas.BudgetIn,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    category = _valid_budget_category(category)
    row = db.get(models.Budget, (user_id, category))
    if row:
        row.amount = payload.amount
        row.updated_at = datetime.now(timezone.utc)
    else:
        row = models.Budget(user_id=user_id, category=category, amount=payload.amount)
        db.add(row)
    db.commit()
    db.refresh(row)
    return row


@app.delete("/budgets/{category}", status_code=204)
def delete_budget(
    category: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    row = db.get(models.Budget, (user_id, category))
    if row is None:
        raise HTTPException(status_code=404, detail="Budget not found")
    db.delete(row)
    db.commit()


@app.get("/budgets/status")
def budgets_status(
    month: str | None = None,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    """Each budget with how much of it is spent in the month (default: current
    month, app timezone) — one call for progress bars."""
    if month is None:
        now = datetime.now(APP_TZ)
        month = f"{now.year:04d}-{now.month:02d}"
    start, end = _month_bounds(month)

    spent_rows = db.execute(
        select(
            func.coalesce(models.Expense.category, "Other"),
            func.sum(models.Expense.amount),
        )
        .where(
            models.Expense.user_id == user_id,
            models.Expense.is_labeled.is_(True),
            models.Expense.direction == "debit",  # budgets cap spending, not income
            models.Expense.date >= start,
            models.Expense.date < end,
        )
        .group_by(models.Expense.category)
    ).all()
    spent: dict[str, float] = {}
    for cat, amt in spent_rows:
        spent[cat] = spent.get(cat, 0.0) + float(amt or 0)
    overall_spent = sum(spent.values())

    out = []
    for b in db.scalars(
        select(models.Budget)
        .where(models.Budget.user_id == user_id)
        .order_by(models.Budget.category)
    ):
        used = overall_spent if b.category == "overall" else spent.get(b.category, 0.0)
        out.append({
            "category": b.category,
            "amount": b.amount,
            "spent": round(used, 2),
            "pct": round(used / b.amount * 100, 1) if b.amount else 0.0,
        })
    return {"month": month, "budgets": out}


@app.get("/summary")
def summary(
    month: str | None = None,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    """Category totals for a month (default: current month, app timezone).
    Aggregated in SQL so it stays fast no matter how much history exists."""
    if month is None:
        now = datetime.now(APP_TZ)
        month = f"{now.year:04d}-{now.month:02d}"
    start, end = _month_bounds(month)

    rows = db.execute(
        select(
            func.coalesce(models.Expense.category, "Other"),
            func.sum(models.Expense.amount),
        )
        .where(
            models.Expense.user_id == user_id,
            models.Expense.is_labeled.is_(True),
            models.Expense.direction == "debit",  # spending only; income is separate
            models.Expense.date >= start,
            models.Expense.date < end,
        )
        .group_by(models.Expense.category)
    ).all()

    totals: dict[str, float] = {}
    for cat, amt in rows:  # NULL and literal "Other" both land on "Other"
        totals[cat] = round(totals.get(cat, 0.0) + float(amt or 0), 2)
    expense_total = round(sum(totals.values()), 2)

    # Income (credits) in the same month — drives the in/out net-flow view.
    income_total = round(float(db.scalar(
        select(func.coalesce(func.sum(models.Expense.amount), 0.0)).where(
            models.Expense.user_id == user_id,
            models.Expense.direction == "credit",
            models.Expense.date >= start,
            models.Expense.date < end,
        )
    ) or 0.0), 2)

    return {
        "month": month,
        "month_total": expense_total,  # spending only — kept for back-compat
        "by_category": totals,
        "income_total": income_total,
        "expense_total": expense_total,
        "net": round(income_total - expense_total, 2),
    }


@app.get("/export")
def export_csv(
    month: str | None = None,
    year: int | None = None,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    """Download transactions as CSV (opens cleanly in Excel/Sheets). Scope:
    `?month=YYYY-MM` -> that month, `?year=YYYY` -> that whole year, neither ->
    everything. Rows are newest-first; dates are rendered in the app timezone."""
    stmt = select(models.Expense).where(models.Expense.user_id == user_id)
    scope = "all"
    if month:
        start, end = _month_bounds(month)
        stmt = stmt.where(models.Expense.date >= start, models.Expense.date < end)
        scope = month
    elif year is not None:
        if not 2000 <= year <= 2100:
            raise HTTPException(status_code=422, detail="year out of range")
        start = datetime(year, 1, 1, tzinfo=APP_TZ)
        end = datetime(year + 1, 1, 1, tzinfo=APP_TZ)
        stmt = stmt.where(models.Expense.date >= start, models.Expense.date < end)
        scope = str(year)

    rows = db.scalars(stmt.order_by(models.Expense.date.desc())).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["Date", "Merchant", "Category", "Amount", "Direction", "Method", "UPI ID", "Labeled", "Raw SMS"]
    )
    for e in rows:
        d = e.date
        if d is not None:
            # Naive timestamps are app-local wall-clock; aware ones are UTC in
            # the DB — normalize both to the app timezone for a sensible export.
            d = (d.replace(tzinfo=APP_TZ) if d.tzinfo is None else d).astimezone(APP_TZ)
            date_str = d.strftime("%Y-%m-%d %H:%M")
        else:
            date_str = ""
        writer.writerow([
            date_str,
            e.merchant or "",
            e.category or "",
            f"{e.amount:.2f}",
            e.direction or "debit",
            e.method or "",
            e.upi_id or "",
            "yes" if e.is_labeled else "no",
            # Keep each transaction on one CSV row even if the SMS had newlines.
            (e.raw_sms or "").replace("\r", " ").replace("\n", " "),
        ])

    # Lead with a UTF-8 BOM so Excel renders the ₹ sign and other unicode right.
    body = "﻿" + buf.getvalue()
    filename = f"klaro-expenses-{scope}.csv"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Recurring payments — subscriptions, EMIs, regular bills. Detected from spend
# history (a stable amount on a regular cadence) or a known subscription brand,
# with the next due date predicted forward.
# ---------------------------------------------------------------------------

# Known subscriptions (normalized merchant fragment -> cadence days). Recurring
# even from a single charge, since we know what they are.
_RECURRING_BRANDS = {
    "netflix": 30,
    "spotify": 30,
    "hotstar": 30,
    "disneyhotstar": 30,
    "amazonprime": 30,
    "primevideo": 30,
    "jiocinema": 30,
    "youtubepremium": 30,
}

# (label, canonical period in days, low gap, high gap)
_CADENCE_BANDS = [
    ("Weekly", 7, 6, 9),
    ("Fortnightly", 14, 12, 16),
    ("Monthly", 30, 26, 34),
    ("Bi-monthly", 61, 56, 66),
    ("Quarterly", 91, 84, 98),
    ("Half-yearly", 182, 172, 196),
    ("Yearly", 365, 350, 385),
]

_MONTHLY_FACTOR = {
    "Weekly": 30 / 7,
    "Fortnightly": 30 / 14,
    "Monthly": 1.0,
    "Bi-monthly": 0.5,
    "Quarterly": 1 / 3,
    "Half-yearly": 1 / 6,
    "Yearly": 1 / 12,
}

# Categories that are people, transfers or variable discretionary spend — repeats
# there are coincidence (two payments to a friend, two food orders), not a
# subscription. Pattern detection skips them; known brands are still caught.
_RECURRING_EXCLUDE_CATS = {"Personal", "Transfer", "Cash", "Food", "Grocery"}

# Words in a bank SMS that all but guarantee a recurring debit (EMI instalment,
# SIP, an executed UPI mandate / NACH auto-debit). High precision, so a single
# charge carrying one of these is treated as recurring (assumed monthly).
_RECURRING_RE = re.compile(
    r"\b(?:emi|sip|nach|mandate|auto\s*-?\s*pay|auto\s*-?\s*debit|"
    r"standing\s+instruction|subscription|recurring)\b",
    re.IGNORECASE,
)


def _cadence_for(days: float):
    for label, period, lo, hi in _CADENCE_BANDS:
        if lo <= days <= hi:
            return label, period
    return None


def _to_app_tz(d: datetime) -> datetime:
    return (d.replace(tzinfo=APP_TZ) if d.tzinfo is None else d).astimezone(APP_TZ)


def _detect_recurring(items: list, today: datetime) -> dict | None:
    """`items`: one merchant's debits, oldest-first. Returns a recurring
    descriptor (merchant, typical amount, cadence, last/next dates) or None."""
    last = items[-1]
    merchant = last.merchant or ""
    amounts = [e.amount for e in items]
    typical = round(median(amounts), 2)
    last_d = _to_app_tz(last.date)

    label = period = source = None

    if len(items) >= 2 and last.category not in _RECURRING_EXCLUDE_CATS:
        dates = [_to_app_tz(e.date) for e in items]
        gaps = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
        gaps = [g for g in gaps if g >= 3]  # ignore same-day repeats
        if gaps:
            band = _cadence_for(median(gaps))
            # Subscriptions/EMIs hold a steady amount — require all within 15%.
            stable = typical > 0 and all(
                abs(a - typical) <= max(0.15 * typical, 1.0) for a in amounts
            )
            if band and stable:
                label, period = band
                source = "pattern"

    if source is None:
        key = normalize(merchant)
        for brand, days in _RECURRING_BRANDS.items():
            if brand in key:
                label, period, source = "Monthly", days, "brand"
                break

    if source is None and any(_RECURRING_RE.search(e.raw_sms or "") for e in items):
        # EMI / SIP / autopay mandate — recurring even from a single charge.
        label, period, source = "Monthly", 30, "keyword"

    if source is None:
        return None

    # Drop ones that look dead — nothing charged for well over two cycles.
    if (today.date() - last_d.date()).days > period * 2 + 7:
        return None

    # Roll the prediction forward to the next future occurrence.
    next_due = last_d
    while next_due.date() <= today.date():
        next_due += timedelta(days=period)

    return {
        "merchant": merchant,
        "amount": typical,
        "category": last.category,
        "cadence": label,
        "occurrences": len(items),
        "last_paid": last_d.date().isoformat(),
        "next_due": next_due.date().isoformat(),
        "source": source,
    }


@app.get("/recurring")
def recurring(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    """Detected recurring payments (subscriptions, EMIs, regular bills) with the
    next due date predicted for each — soonest first, plus the monthly-equivalent
    total they add up to."""
    rows = db.scalars(
        select(models.Expense)
        .where(
            models.Expense.user_id == user_id,
            models.Expense.direction == "debit",
        )
        .order_by(models.Expense.date.asc())
    ).all()

    groups: dict[str, list] = defaultdict(list)
    for e in rows:
        key = normalize(e.merchant or "")
        if key:
            groups[key].append(e)

    # Detections the user has dismissed ("I cancelled this") — keyed by the same
    # normalized merchant the detector groups on, so they're skipped here.
    dismissed_rows = db.scalars(
        select(models.RecurringDismissal)
        .where(models.RecurringDismissal.user_id == user_id)
    ).all()
    dismissed = {d.merchant_norm for d in dismissed_rows}

    today = datetime.now(APP_TZ)
    out = [
        d
        for key, items in groups.items()
        if key not in dismissed and (d := _detect_recurring(items, today))
    ]
    out.sort(key=lambda r: r["next_due"])

    monthly_total = round(
        sum(r["amount"] * _MONTHLY_FACTOR.get(r["cadence"], 1.0) for r in out), 2
    )
    return {
        "count": len(out),
        "monthly_total": monthly_total,
        "recurring": out,
        "dismissed": [d.merchant_display for d in dismissed_rows],
    }


class DismissIn(BaseModel):
    merchant: str


@app.post("/recurring/dismiss")
def dismiss_recurring(
    payload: DismissIn,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    """Hide a detected recurring payment (the user cancelled the subscription /
    doesn't want it tracked). Idempotent; reversible via DELETE."""
    norm = normalize(payload.merchant)
    if not norm:
        raise HTTPException(status_code=400, detail="merchant required")
    if db.get(models.RecurringDismissal, (user_id, norm)) is None:
        db.add(models.RecurringDismissal(
            user_id=user_id, merchant_norm=norm, merchant_display=payload.merchant))
        db.commit()
    return {"merchant": payload.merchant, "dismissed": True}


@app.delete("/recurring/dismiss/{merchant}", status_code=204)
def restore_recurring(
    merchant: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_current_user),
):
    """Un-dismiss a recurring payment so it can be detected again (undo)."""
    row = db.get(models.RecurringDismissal, (user_id, normalize(merchant)))
    if row is not None:
        db.delete(row)
        db.commit()
