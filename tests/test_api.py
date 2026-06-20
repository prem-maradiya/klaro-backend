"""End-to-end API tests covering the behaviours that protect real money data:
dedupe, income vs spending separation, budgets, recurring detection + dismissal,
learned renames, and auth. Runs against in-memory SQLite (see conftest)."""
from datetime import datetime, timedelta

import pytest

from app.categorizer import normalize


def _expense(**over):
    """A minimal valid /expenses payload for *now*, overridable per-field."""
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
# normalize() — the key every dedupe / learn / dismiss path hinges on.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Netflix", "netflix"),
        ("  NETFLIX  ", "netflix"),
        ("M Somnath Roy (IDBI BANK)", "msomnathroyidbibank"),
        ("Amazon.in*Order-123", "amazoninorder123"),
        ("", ""),
        ("!!!", ""),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw) == expected


# --------------------------------------------------------------------------- #
# Health / auth
# --------------------------------------------------------------------------- #
def test_health_open(client):
    assert client.get("/health").status_code == 200


def test_protected_routes_require_auth(unauth_client):
    # No bearer token -> 401 on a user route; /health stays open.
    assert unauth_client.get("/expenses").status_code == 401
    assert unauth_client.get("/health").status_code == 200


# --------------------------------------------------------------------------- #
# Create + dedupe
# --------------------------------------------------------------------------- #
def test_create_expense(client):
    r = client.post("/expenses", json=_expense(merchant="Zomato", category="Food"))
    assert r.status_code == 201
    body = r.json()
    assert body["merchant"] == "Zomato"
    assert body["id"] > 0


def test_brand_autocategorize_offline(client):
    # Unlabeled brand merchant should be categorized by the brand DB (no AI).
    r = client.post("/expenses", json=_expense(
        merchant="NETFLIX", category=None, is_labeled=False))
    assert r.status_code == 201
    assert r.json()["category"] == "Entertainment"


def test_dedupe_same_sms(client):
    sms = "debited Rs 199 to NETFLIX on 14-Jun"
    p = _expense(merchant="Netflix", amount=199.0, raw_sms=sms, category="Entertainment")
    first = client.post("/expenses", json=p)
    assert first.status_code == 201
    second = client.post("/expenses", json=p)
    assert second.status_code == 200                  # deduped, not created
    assert second.json()["id"] == first.json()["id"]
    assert len(client.get("/expenses").json()) == 1


def test_dedupe_is_whitespace_immune(client):
    base = _expense(merchant="Netflix", amount=199.0, category="Entertainment",
                    raw_sms="debited Rs 199 to NETFLIX")
    first = client.post("/expenses", json=base)
    spaced = dict(base, raw_sms="debited  Rs 199   to NETFLIX")  # extra spaces
    second = client.post("/expenses", json=spaced)
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_manual_entries_never_dedupe(client):
    # No raw_sms => manual entry => always created, even if identical.
    p = _expense(merchant="Cash", amount=50.0, raw_sms="", category="Cash")
    a = client.post("/expenses", json=p)
    b = client.post("/expenses", json=p)
    assert a.status_code == 201 and b.status_code == 201
    assert a.json()["id"] != b.json()["id"]


# --------------------------------------------------------------------------- #
# Income vs spending
# --------------------------------------------------------------------------- #
def test_income_excluded_from_spending(client):
    client.post("/expenses", json=_expense(amount=100.0, category="Food"))
    client.post("/expenses", json=_expense(
        merchant="Employer", amount=5000.0, direction="credit", category=None))
    s = client.get("/summary").json()
    assert s["expense_total"] == 100.0
    assert s["income_total"] == 5000.0
    assert s["net"] == 4900.0
    # Credit was filed under Income and not mixed into category spending.
    assert "Income" not in s["by_category"]


# --------------------------------------------------------------------------- #
# Budgets
# --------------------------------------------------------------------------- #
def test_budgets_crud(client):
    assert client.get("/budgets").json() == []
    put = client.put("/budgets/Food", json={"amount": 3000})
    assert put.status_code == 200 and put.json()["amount"] == 3000
    # Upsert updates in place.
    client.put("/budgets/Food", json={"amount": 3500})
    assert {b["category"]: b["amount"] for b in client.get("/budgets").json()} == {"Food": 3500.0}
    assert client.put("/budgets/Food", json={"amount": -5}).status_code == 422  # gt=0
    assert client.delete("/budgets/Food").status_code == 204
    assert client.get("/budgets").json() == []


def test_budget_status_reflects_spending(client):
    client.put("/budgets/Food", json={"amount": 1000})
    client.post("/expenses", json=_expense(amount=250.0, category="Food"))
    status = client.get("/budgets/status").json()["budgets"]
    food = next((b for b in status if b["category"] == "Food"), None)
    assert food is not None
    assert food["spent"] == 250.0
    assert food["amount"] == 1000.0


# --------------------------------------------------------------------------- #
# Recurring detect + dismiss/restore round-trip
# --------------------------------------------------------------------------- #
def _seed_netflix(client):
    now = datetime.now()
    for days_ago in (35, 5):  # two monthly charges, latest 5 days ago
        client.post("/expenses", json=_expense(
            merchant="Netflix", amount=199.0, category="Entertainment",
            raw_sms=f"NETFLIX debit {days_ago}",
            date=(now - timedelta(days=days_ago)).isoformat()))


def test_recurring_detected(client):
    _seed_netflix(client)
    data = client.get("/recurring").json()
    names = [r["merchant"] for r in data["recurring"]]
    assert "Netflix" in names
    assert data["monthly_total"] >= 199.0


def test_recurring_dismiss_and_restore(client):
    _seed_netflix(client)
    assert any(r["merchant"] == "Netflix" for r in client.get("/recurring").json()["recurring"])

    d = client.post("/recurring/dismiss", json={"merchant": "Netflix"})
    assert d.status_code == 200 and d.json()["dismissed"] is True

    after = client.get("/recurring").json()
    assert all(r["merchant"] != "Netflix" for r in after["recurring"])
    assert "Netflix" in after["dismissed"]

    assert client.delete("/recurring/dismiss/Netflix").status_code == 204
    restored = client.get("/recurring").json()
    assert any(r["merchant"] == "Netflix" for r in restored["recurring"])
    assert restored["dismissed"] == []


def test_dismiss_is_idempotent(client):
    _seed_netflix(client)
    client.post("/recurring/dismiss", json={"merchant": "Netflix"})
    # Second dismiss must not error or duplicate.
    assert client.post("/recurring/dismiss", json={"merchant": "Netflix"}).status_code == 200
    assert client.get("/recurring").json()["dismissed"].count("Netflix") == 1


def test_dismiss_requires_merchant(client):
    assert client.post("/recurring/dismiss", json={"merchant": "  "}).status_code == 400


# --------------------------------------------------------------------------- #
# Learned rename — a user correction must persist to future transactions.
# --------------------------------------------------------------------------- #
def test_learned_rename_applies_to_future(client):
    created = client.post("/expenses", json=_expense(
        merchant="PAYTM*ABCSTORE", category=None, is_labeled=False,
        raw_sms="paytm abcstore 1")).json()
    # User relabels: rename + category.
    patch = client.patch(f"/expenses/{created['id']}",
                         json={"merchant": "Corner Store", "category": "Grocery",
                               "is_labeled": True})
    assert patch.status_code == 200

    # A NEW transaction from the same raw merchant should inherit the rename.
    nxt = client.post("/expenses", json=_expense(
        merchant="PAYTM*ABCSTORE", category=None, is_labeled=False,
        raw_sms="paytm abcstore 2")).json()
    assert nxt["merchant"] == "Corner Store"
    assert nxt["category"] == "Grocery"


# --------------------------------------------------------------------------- #
# Multi-tenancy — the core Phase 7 guarantee: one user can never see another's.
# --------------------------------------------------------------------------- #
def test_users_cannot_see_each_others_expenses(client, client_b):
    a = client.post("/expenses", json=_expense(merchant="A-Shop", amount=10)).json()
    client_b.post("/expenses", json=_expense(merchant="B-Shop", amount=20))

    a_list = client.get("/expenses").json()
    b_list = client_b.get("/expenses").json()
    assert [e["merchant"] for e in a_list] == ["A-Shop"]
    assert [e["merchant"] for e in b_list] == ["B-Shop"]

    # B cannot read, edit, or delete A's row by id.
    assert client_b.get(f"/expenses/{a['id']}").status_code == 404
    assert client_b.patch(f"/expenses/{a['id']}", json={"category": "Food"}).status_code == 404
    assert client_b.delete(f"/expenses/{a['id']}").status_code == 404
    # A's row is untouched.
    assert client.get(f"/expenses/{a['id']}").status_code == 200


def test_summary_and_recurring_are_per_user(client, client_b):
    client.post("/expenses", json=_expense(amount=100, category="Food"))
    client_b.post("/expenses", json=_expense(amount=999, category="Food"))
    assert client.get("/summary").json()["expense_total"] == 100.0
    assert client_b.get("/summary").json()["expense_total"] == 999.0


def test_budgets_and_dismissals_are_per_user(client, client_b):
    client.put("/budgets/Food", json={"amount": 1000})
    assert client.get("/budgets").json()[0]["amount"] == 1000.0
    assert client_b.get("/budgets").json() == []          # B sees none of A's budgets

    client.post("/recurring/dismiss", json={"merchant": "Netflix"})
    assert client.get("/recurring").json()["dismissed"] == ["Netflix"]
    assert client_b.get("/recurring").json()["dismissed"] == []
