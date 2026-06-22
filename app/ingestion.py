"""Source-agnostic ingestion pipeline (Phase 7.3).

Every way a transaction can enter Klaro — manual entry, an on-device SMS parse,
a UPI/receipt-email import, and (Phase 8) an Account Aggregator feed — is first
normalized into a single `TxnCandidate`. The pipeline below then does the same
work regardless of where it came from: dedupe → categorize → learn → store. So
adding a new source later is "just another adapter that produces a TxnCandidate";
none of the dedupe/categorize/learn/store logic has to change.
"""
import hashlib
import re
from statistics import median

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models, schemas
from .categorizer import categorize, normalize
from .config import APP_TZ
from datetime import datetime, timedelta

# Where a transaction came from. The pipeline is identical for all of them; the
# value is recorded on the row for analytics and so the Play (no-SMS) vs power
# (SMS) builds, and a future Account Aggregator feed, are distinguishable.
VALID_SOURCES = {"manual", "sms", "email", "upi", "aa"}

# A retry / re-import is the same SMS text + amount within this window, not a new
# transaction (the same payment SMS can arrive with slightly different spacing).
DEDUPE_WINDOW = timedelta(minutes=5)

# --- Budget scoping heuristic ------------------------------------------------ #
# A monthly budget is for day-to-day spending. A large one-off / fixed payment
# (college fees, rent, a laptop) would blow it up, so those are held OUT of the
# budget and surfaced for the user to decide. Normal small spends flow in
# automatically — the user is only ever interrupted for the rare big one.
#
# A debit is treated as "large / fee-like" (→ held out, flagged for review) when:
#   • its amount ≥ max(FLOOR, MULTIPLE × the user's typical spend), or
#   • the merchant / SMS text mentions a fee-like word (fees, tuition, rent…).
BUDGET_LARGE_FLOOR = 5000.0    # ₹ — "large" for a user with little/no history yet
BUDGET_LARGE_MULTIPLE = 3.0    # …or this many times the user's typical spend
BUDGET_MIN_HISTORY = 8         # need at least this many debits to trust a median
BUDGET_TYPICAL_WINDOW = 100    # median over the most recent N budgeted debits
BUDGET_FEE_RE = re.compile(
    r"\b(fees?|tuition|admission|semester|hostel|college|university|"
    r"exam|rent|deposit|insurance|premium)\b",
    re.IGNORECASE,
)


def _typical_debit(db: Session, user_id: str) -> float | None:
    """Median of the user's recent budgeted debits — the baseline a new spend is
    compared against. None until there's enough history to be meaningful (so a
    brand-new user falls back to the absolute floor instead of a noisy median)."""
    amounts = db.scalars(
        select(models.Expense.amount)
        .where(
            models.Expense.user_id == user_id,
            models.Expense.direction == "debit",
            models.Expense.in_budget.is_(True),
        )
        .order_by(models.Expense.created_at.desc())
        .limit(BUDGET_TYPICAL_WINDOW)
    ).all()
    if len(amounts) < BUDGET_MIN_HISTORY:
        return None
    return float(median(amounts))


def assess_budget(db: Session, user_id: str, cand: "TxnCandidate") -> tuple[bool, bool]:
    """Decide (in_budget, budget_review) for a candidate. Credits/income never
    count toward a spending budget. A large or fee-like debit is held out
    (in_budget=False) and flagged for review (budget_review=True); everything else
    flows in automatically."""
    if cand.direction != "debit":
        return True, False
    blob = f"{cand.merchant or ''} {cand.raw_ref or ''}"
    fee_like = bool(BUDGET_FEE_RE.search(blob))
    typical = _typical_debit(db, user_id)
    threshold = max(BUDGET_LARGE_FLOOR, (typical or 0.0) * BUDGET_LARGE_MULTIPLE)
    large = cand.amount >= threshold
    if fee_like or large:
        return False, True
    return True, False


class TxnCandidate(BaseModel):
    """A normalized transaction, regardless of its source. Adapters build one of
    these; `ingest()` consumes it."""
    merchant: str = ""
    amount: float
    date: datetime
    direction: str = "debit"          # debit = money out | credit = money in
    source: str = "manual"            # manual | sms | email | upi | aa
    method: str = "upi"               # upi | card
    upi_id: str | None = None
    # Original text/id this candidate came from, used for dedupe. For SMS it's the
    # message body; empty => no dedupe (manual entries always create).
    raw_ref: str = ""
    # A caller-supplied label, if any (e.g. a manual entry the user categorized,
    # or a credit already known to be Income). Unlabeled debits get auto-filed.
    category: str | None = None
    is_labeled: bool = False


# --------------------------------------------------------------------------- #
# Merchant learning — the user's corrections + AI/brand classifications, cached
# per user so they auto-apply to and survive future imports. Lives here because
# the ingest pipeline both reads (apply learned mapping) and writes (remember
# auto-classifications) it; the API layer reuses these for PATCH/categorize.
# --------------------------------------------------------------------------- #
def sms_hash(raw_ref: str | None) -> str | None:
    if not raw_ref or not raw_ref.strip():
        return None
    # Hash with ALL whitespace removed: the same SMS arrives with slightly
    # different spacing depending on the capture path (live broadcast vs inbox
    # read — observed "<Mob.No>" vs "<Mob. No>"), and that must not defeat dedupe.
    return hashlib.sha256(re.sub(r"\s+", "", raw_ref).encode("utf-8")).hexdigest()


def learned_category(db: Session, user_id: str, merchant: str) -> str | None:
    norm = normalize(merchant)
    if not norm:
        return None
    row = db.get(models.MerchantCategory, (user_id, norm))
    return row.category if row else None


def learned_mapping(db: Session, user_id: str, merchant: str):
    norm = normalize(merchant)
    return db.get(models.MerchantCategory, (user_id, norm)) if norm else None


def remember(db: Session, user_id: str, merchant: str, category: str | None = None,
             source: str = "user", display: str | None = None):
    """Learn a merchant for this user. `merchant` is the key (normalized).
    `display` is the preferred name to show (defaults to merchant) — used to learn
    RENAMES so a parsed name maps to the user's chosen name on future txns."""
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


# --------------------------------------------------------------------------- #
# Adapters: raw input -> TxnCandidate.
# --------------------------------------------------------------------------- #
def candidate_from_expense_create(payload: "schemas.ExpenseCreate") -> TxnCandidate:
    """Adapt the client's create payload into a candidate. The phone already does
    the SMS parsing on-device, so this is mostly a field map — but it also infers
    the source when the client doesn't send one (older app builds)."""
    src = (payload.source or "").strip().lower()
    if src not in VALID_SOURCES:
        # No/invalid source: a non-empty raw_sms means it came from an SMS parse;
        # otherwise it's a manual entry.
        src = "sms" if (payload.raw_sms or "").strip() else "manual"
    return TxnCandidate(
        merchant=payload.merchant or "",
        amount=payload.amount,
        date=payload.date,
        direction=payload.direction,
        source=src,
        method=payload.method,
        upi_id=payload.upi_id,
        raw_ref=payload.raw_sms or "",
        category=payload.category,
        is_labeled=payload.is_labeled,
    )


# --------------------------------------------------------------------------- #
# The pipeline.
# --------------------------------------------------------------------------- #
def _find_duplicate(db: Session, user_id: str, amount: float, date: datetime,
                    hash_: str) -> models.Expense | None:
    return db.scalars(
        select(models.Expense)
        .where(
            models.Expense.user_id == user_id,
            models.Expense.raw_sms_hash == hash_,
            models.Expense.amount == amount,
            models.Expense.date >= date - DEDUPE_WINDOW,
            models.Expense.date <= date + DEDUPE_WINDOW,
        )
        .limit(1)
    ).first()


async def ingest(db: Session, user_id: str, cand: TxnCandidate) -> tuple[models.Expense, bool]:
    """Run a candidate through dedupe -> categorize -> learn -> store.

    Returns `(expense, created)` — `created` is False when an existing row was
    returned because the candidate was a duplicate (so the caller can answer 200
    instead of 201). Commits on success.
    """
    # The phone sends timezone-naive local timestamps (IST wall-clock). Treat
    # naive datetimes as APP_TZ — never as server time (UTC in the cloud), which
    # would shift every transaction by the offset.
    date = cand.date if cand.date.tzinfo else cand.date.replace(tzinfo=APP_TZ)
    merchant = cand.merchant or ""

    # Dedupe: a matching hash + amount within the window is a retry/re-import.
    hash_ = sms_hash(cand.raw_ref)
    if hash_:
        existing = _find_duplicate(db, user_id, cand.amount, date, hash_)
        if existing is not None:
            return existing, False

    category = cand.category
    is_labeled = cand.is_labeled

    if cand.direction == "credit":
        # Income: the "merchant" is the payer, not a vendor — don't run spend
        # categorization or learn a merchant->category from it (that would poison
        # future debits). Just file it under Income.
        category = category or "Income"
        is_labeled = True
    else:
        # Apply any learned mapping for this merchant: a remembered RENAME and/or
        # category, so the user's corrections persist and survive re-imports.
        mapping = learned_mapping(db, user_id, merchant)
        if mapping:
            if mapping.merchant_display and normalize(mapping.merchant_display) != normalize(merchant):
                merchant = mapping.merchant_display
            if mapping.category:
                category = mapping.category
                is_labeled = True
        # Auto-categorize anything still unlabeled (brand -> keyword -> AI).
        if not is_labeled or not category:
            cat, csource = await categorize(merchant or "", cand.raw_ref or None)
            if cat:
                category = cat
                is_labeled = True
                if csource in ("ai", "brand", "keyword"):
                    remember(db, user_id, cand.merchant or "", cat, csource)

    # Budget scoping: small day-to-day spends count automatically; a large or
    # fee-like one is held out and flagged for the user to include/exclude.
    in_budget, budget_review = assess_budget(db, user_id, cand)

    expense = models.Expense(
        user_id=user_id,
        merchant=merchant,
        amount=cand.amount,
        date=date,
        raw_sms=cand.raw_ref,
        raw_sms_hash=hash_,
        upi_id=cand.upi_id,
        is_labeled=is_labeled,
        category=category,
        method=cand.method,
        direction=cand.direction,
        source=cand.source,
        in_budget=in_budget,
        budget_review=budget_review,
    )
    db.add(expense)
    db.commit()
    db.refresh(expense)
    return expense, True
