"""Phase 7.3 ingestion abstraction: the source-agnostic TxnCandidate pipeline.

Confirms the adapter infers/records the source and that dedupe/categorize/learn
still behave the same through the new pipeline (the API-level behaviour is also
covered by test_api.py, which all still passes).
"""
import asyncio
from datetime import datetime

import pytest

from app import schemas
from app.ingestion import (
    TxnCandidate,
    candidate_from_expense_create,
    ingest,
)


def _session():
    """A DB session bound to the same in-memory SQLite the `client` fixture set
    up (via the get_db override), for testing the pipeline without HTTP."""
    from app.main import app
    from app.database import get_db

    return next(app.dependency_overrides[get_db]())


def _payload(**over):
    base = {
        "merchant": "Test Merchant",
        "amount": 100.0,
        "date": datetime.now().isoformat(),
        "raw_sms": "",
        "category": "Food",
        "is_labeled": True,
        "direction": "debit",
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# Adapter: source inference + explicit source.
# --------------------------------------------------------------------------- #
def test_source_inferred_sms_when_raw_sms_present():
    c = candidate_from_expense_create(
        schemas.ExpenseCreate(**_payload(raw_sms="Dr INR 100 UPI/DR/x/Shop"))
    )
    assert c.source == "sms"
    assert c.raw_ref.startswith("Dr INR")


def test_source_inferred_manual_when_no_raw_sms():
    c = candidate_from_expense_create(schemas.ExpenseCreate(**_payload(raw_sms="")))
    assert c.source == "manual"


@pytest.mark.parametrize("given,expected", [
    ("email", "email"),
    ("aa", "aa"),
    ("UPI", "upi"),          # normalized to lowercase
    ("bogus", "manual"),     # invalid -> inferred (no raw_sms here -> manual)
])
def test_explicit_source_validated(given, expected):
    c = candidate_from_expense_create(
        schemas.ExpenseCreate(**_payload(source=given, raw_sms=""))
    )
    assert c.source == expected


# --------------------------------------------------------------------------- #
# Round-trip through the API: source is persisted and returned.
# --------------------------------------------------------------------------- #
def test_source_persisted_and_returned(client):
    sms = client.post("/expenses", json=_payload(raw_sms="Dr INR 100 to Shop")).json()
    assert sms["source"] == "sms"

    manual = client.post("/expenses", json=_payload(raw_sms="")).json()
    assert manual["source"] == "manual"

    email = client.post("/expenses", json=_payload(source="email", raw_sms="")).json()
    assert email["source"] == "email"


# --------------------------------------------------------------------------- #
# Pipeline directly: dedupe + credit handling are source-agnostic.
# --------------------------------------------------------------------------- #
def test_ingest_dedupes_same_raw_ref(client):
    db = _session()
    cand = TxnCandidate(
        merchant="Shop", amount=50.0, date=datetime.now(),
        source="sms", raw_ref="Dr INR 50 to Shop ref 12345",
        category="Food", is_labeled=True,
    )
    uid = "33333333-3333-3333-3333-333333333333"
    e1, created1 = asyncio.run(ingest(db, uid, cand))
    e2, created2 = asyncio.run(ingest(db, uid, cand))
    assert created1 is True and created2 is False
    assert e1.id == e2.id  # the duplicate returned the same row


def test_ingest_credit_files_as_income(client):
    db = _session()
    cand = TxnCandidate(
        merchant="ACME Payroll", amount=5000.0, date=datetime.now(),
        direction="credit", source="sms", raw_ref="credited by Rs 5000 salary",
    )
    e, created = asyncio.run(ingest(db, "44444444-4444-4444-4444-444444444444", cand))
    assert created is True
    assert e.direction == "credit"
    assert e.category == "Income"
    assert e.is_labeled is True
