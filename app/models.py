from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base

# user_id is stored/handled as a plain string UUID (as_uuid=False) so it threads
# through the code as the JWT `sub` string without uuid.UUID juggling.
_UserId = Uuid(as_uuid=False)

# Every user-owned table carries this. The FK to auth.users lives in
# supabase_schema.sql (the auth schema isn't managed by SQLAlchemy here); the
# ORM keeps user_id as a plain indexed UUID so the same models work against the
# SQLite test database too.


class Expense(Base):
    __tablename__ = "expenses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[str] = mapped_column(_UserId, nullable=False, index=True)
    merchant: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_sms: Mapped[str] = mapped_column(Text, default="")
    # SHA-256 of raw_sms — server-computed, indexed for the dedupe check on POST.
    raw_sms_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    upi_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_labeled: Mapped[bool] = mapped_column(Boolean, default=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    method: Mapped[str] = mapped_column(String(20), default="upi")  # upi | card
    # debit = money out (a spend, the default), credit = money in (income).
    direction: Mapped[str] = mapped_column(
        String(10), default="debit", server_default="debit", index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class Budget(Base):
    """Monthly spending limits, per user. One row per category; the row with
    category="overall" caps the whole month (lowercase sentinel — real
    categories are capitalized, so no collision)."""
    __tablename__ = "budgets"

    user_id: Mapped[str] = mapped_column(_UserId, primary_key=True)
    category: Mapped[str] = mapped_column(String(100), primary_key=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class RecurringDismissal(Base):
    """Recurring detections the user dismissed ("I cancelled this subscription").
    Excluded from /recurring. Keyed by (user, normalized merchant) so it matches
    the detector's grouping; the display name is kept for restore UIs."""
    __tablename__ = "recurring_dismissals"

    user_id: Mapped[str] = mapped_column(_UserId, primary_key=True)
    merchant_norm: Mapped[str] = mapped_column(String(255), primary_key=True)
    merchant_display: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class MerchantCategory(Base):
    """Learned merchant -> category mappings, per user. Grows as the user labels
    things and as the AI classifies unknown merchants (cached so each is done
    once). Keyed by (user, normalized merchant)."""
    __tablename__ = "merchant_categories"

    user_id: Mapped[str] = mapped_column(_UserId, primary_key=True)
    merchant_norm: Mapped[str] = mapped_column(String(255), primary_key=True)
    merchant_display: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="user")  # user|ai|brand
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
