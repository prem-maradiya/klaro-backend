import hashlib
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pydantic import BaseModel

from . import models, schemas
from .database import Base, engine, get_db
from .categorizer import CATEGORIES, categorize, normalize, ai_parse_sms

# When set, every endpoint except / and /health requires this value in the
# X-API-Key header. Leave unset for open local development.
API_KEY = os.getenv("API_KEY", "").strip()

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
    return hashlib.sha256(raw_sms.strip().encode("utf-8")).hexdigest()


def _learned_category(db: Session, merchant: str) -> str | None:
    norm = normalize(merchant)
    if not norm:
        return None
    row = db.get(models.MerchantCategory, norm)
    return row.category if row else None


def _remember(db: Session, merchant: str, category: str | None = None,
              source: str = "user", display: str | None = None):
    """Learn a merchant. `merchant` is the key (normalized). `display` is the
    preferred name to show (defaults to merchant) — used to learn RENAMES so a
    parsed name maps to the user's chosen name on future transactions."""
    norm = normalize(merchant)
    if not norm:
        return
    row = db.get(models.MerchantCategory, norm)
    if row:
        if category:
            row.category = category
        if display:
            row.merchant_display = display
        row.source = source
    else:
        db.add(models.MerchantCategory(
            merchant_norm=norm,
            merchant_display=display or merchant,
            category=category or "Other",
            source=source,
        ))


def _learned_mapping(db: Session, merchant: str):
    norm = normalize(merchant)
    return db.get(models.MerchantCategory, norm) if norm else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables on startup if they don't exist. Never fatal: with the DB
    # unreachable (e.g. DATABASE_URL not configured yet in the cloud), /health
    # must still work — data endpoints will surface the real error.
    try:
        Base.metadata.create_all(bind=engine)
    except Exception:
        logging.exception("create_all failed (database unreachable?) — continuing")
    yield


app = FastAPI(title="Expense Tracker API", version="1.0.0", lifespan=lifespan)


# Paths that stay open so uptime checks and the app's connection test work
# without credentials.
_AUTH_EXEMPT = {"/", "/health"}


@app.middleware("http")
async def require_api_key(request, call_next):
    if (
        API_KEY
        and request.method != "OPTIONS"  # CORS preflight carries no custom headers
        and request.url.path not in _AUTH_EXEMPT
        and request.headers.get("x-api-key") != API_KEY
    ):
        return JSONResponse(
            status_code=401, content={"detail": "Invalid or missing API key"}
        )
    return await call_next(request)


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
def root():
    return {"status": "ok", "service": "expense-tracker-api"}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.post("/expenses", response_model=schemas.ExpenseOut, status_code=201)
async def create_expense(
    payload: schemas.ExpenseCreate,
    response: Response,
    db: Session = Depends(get_db),
):
    data = payload.model_dump()
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

    # Apply any learned mapping for this merchant: a remembered RENAME (preferred
    # display name) and/or category. This makes the user's corrections persist,
    # apply to ALL future transactions, and survive re-imports.
    mapping = _learned_mapping(db, merchant)
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
                _remember(db, merchant, cat, source)

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
):
    stmt = select(models.Expense)
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
def list_months(db: Session = Depends(get_db)):
    """Distinct months that have expenses (newest first) with count + total —
    drives the dashboard's month selector without fetching every row."""
    month_expr = func.to_char(
        func.timezone(APP_TZ_NAME, models.Expense.date), "YYYY-MM"
    ).label("month")
    rows = db.execute(
        select(
            month_expr,
            func.count(models.Expense.id),
            func.sum(models.Expense.amount),
        )
        .group_by(month_expr)
        .order_by(month_expr.desc())
    ).all()
    return [
        {"month": m, "count": c, "total": round(float(t or 0), 2)}
        for m, c, t in rows
    ]


@app.get("/expenses/{expense_id}", response_model=schemas.ExpenseOut)
def get_expense(expense_id: int, db: Session = Depends(get_db)):
    expense = db.get(models.Expense, expense_id)
    if expense is None:
        raise HTTPException(status_code=404, detail="Expense not found")
    return expense


@app.patch("/expenses/{expense_id}", response_model=schemas.ExpenseOut)
def update_expense(
    expense_id: int,
    payload: schemas.ExpenseUpdate,
    db: Session = Depends(get_db),
):
    expense = db.get(models.Expense, expense_id)
    if expense is None:
        raise HTTPException(status_code=404, detail="Expense not found")

    old_merchant = expense.merchant
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(expense, field, value)

    # Learn the user's correction so it auto-applies to future transactions.
    if expense.merchant and expense.category:
        _remember(db, expense.merchant, expense.category, source="user")
        # Rename learning: map the ORIGINAL parsed name -> the user's chosen
        # name, so future SMS that parse to the original show the user's name.
        if normalize(old_merchant) and normalize(old_merchant) != normalize(expense.merchant):
            _remember(db, old_merchant, expense.category, source="user", display=expense.merchant)

    db.commit()
    db.refresh(expense)
    return expense


@app.delete("/expenses/{expense_id}", status_code=204)
def delete_expense(expense_id: int, db: Session = Depends(get_db)):
    expense = db.get(models.Expense, expense_id)
    if expense is None:
        raise HTTPException(status_code=404, detail="Expense not found")
    db.delete(expense)
    db.commit()


class ParseSmsIn(BaseModel):
    body: str
    sender: str = ""


@app.post("/parse-sms")
async def parse_sms(payload: ParseSmsIn, db: Session = Depends(get_db)):
    """AI fallback parser for bank SMS the on-device regex couldn't handle.
    Returns a structured transaction (or is_transaction=false)."""
    result = await ai_parse_sms(payload.body, payload.sender)
    # Cache the merchant->category so future identical merchants are instant.
    if result.get("is_transaction") and result.get("category") and result.get("merchant"):
        _remember(db, result["merchant"], result["category"], source="ai")
        db.commit()
    return result


@app.get("/categorize")
async def categorize_merchant(merchant: str, raw_sms: str | None = None, db: Session = Depends(get_db)):
    """Classify a merchant name. Used by the app/dashboard to preview a category."""
    cat = _learned_category(db, merchant)
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
def list_budgets(db: Session = Depends(get_db)):
    return db.scalars(select(models.Budget).order_by(models.Budget.category)).all()


@app.put("/budgets/{category}", response_model=schemas.BudgetOut)
def upsert_budget(
    category: str, payload: schemas.BudgetIn, db: Session = Depends(get_db)
):
    category = _valid_budget_category(category)
    row = db.get(models.Budget, category)
    if row:
        row.amount = payload.amount
        row.updated_at = datetime.now(timezone.utc)
    else:
        row = models.Budget(category=category, amount=payload.amount)
        db.add(row)
    db.commit()
    db.refresh(row)
    return row


@app.delete("/budgets/{category}", status_code=204)
def delete_budget(category: str, db: Session = Depends(get_db)):
    row = db.get(models.Budget, category)
    if row is None:
        raise HTTPException(status_code=404, detail="Budget not found")
    db.delete(row)
    db.commit()


@app.get("/budgets/status")
def budgets_status(month: str | None = None, db: Session = Depends(get_db)):
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
            models.Expense.is_labeled.is_(True),
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
    for b in db.scalars(select(models.Budget).order_by(models.Budget.category)):
        used = overall_spent if b.category == "overall" else spent.get(b.category, 0.0)
        out.append({
            "category": b.category,
            "amount": b.amount,
            "spent": round(used, 2),
            "pct": round(used / b.amount * 100, 1) if b.amount else 0.0,
        })
    return {"month": month, "budgets": out}


@app.get("/summary")
def summary(month: str | None = None, db: Session = Depends(get_db)):
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
            models.Expense.is_labeled.is_(True),
            models.Expense.date >= start,
            models.Expense.date < end,
        )
        .group_by(models.Expense.category)
    ).all()

    totals: dict[str, float] = {}
    for cat, amt in rows:  # NULL and literal "Other" both land on "Other"
        totals[cat] = round(totals.get(cat, 0.0) + float(amt or 0), 2)
    return {
        "month": month,
        "month_total": round(sum(totals.values()), 2),
        "by_category": totals,
    }
