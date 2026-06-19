from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base


class Expense(Base):
    __tablename__ = "expenses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
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
    # Indexed because every spending aggregate filters on it.
    direction: Mapped[str] = mapped_column(
        String(10), default="debit", server_default="debit", index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class Budget(Base):
    """Monthly spending limits, shared by the phone app and the dashboard.
    One row per category; the row with category="overall" caps the whole month
    (lowercase sentinel — real categories are capitalized, so no collision)."""
    __tablename__ = "budgets"

    category: Mapped[str] = mapped_column(String(100), primary_key=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class RecurringDismissal(Base):
    """Recurring detections the user dismissed ("I cancelled this subscription").
    Excluded from /recurring. Keyed by the normalized merchant so it matches the
    detector's grouping; the display name is kept for restore UIs. A whole new
    table, so create_all provisions it on startup (no column migration needed)."""
    __tablename__ = "recurring_dismissals"

    merchant_norm: Mapped[str] = mapped_column(String(255), primary_key=True)
    merchant_display: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class MerchantCategory(Base):
    """Learned merchant -> category mappings. Grows as the user labels things
    and as the AI classifies unknown merchants (cached so each is done once)."""
    __tablename__ = "merchant_categories"

    merchant_norm: Mapped[str] = mapped_column(String(255), primary_key=True)
    merchant_display: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="user")  # user|ai|brand
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
