"""Account deletion (Play Store / DPDP-Act requirement): DELETE /account wipes
every user-owned table, deletes the auth record (best-effort), and is scoped to
the caller so one user can never erase another's data."""
from datetime import datetime


def _seed(client):
    """Give the caller one expense, one budget, one learned category (via a
    label edit) and one recurring dismissal, so deletion has something in every
    table to clear."""
    r = client.post("/expenses", json={
        "merchant": "Cafe Coffee Day",
        "amount": 180.0,
        "date": datetime.now().isoformat(),
        "category": "Food",
        "is_labeled": True,
        "direction": "debit",
    })
    exp_id = r.json()["id"]
    # A label edit writes a learned merchant->category row.
    client.patch(f"/expenses/{exp_id}", json={"merchant": "CCD", "category": "Food"})
    client.put("/budgets/overall", json={"amount": 20000.0})
    client.post("/recurring/dismiss", json={"merchant": "Netflix"})


def test_delete_account_wipes_all_data(client):
    _seed(client)
    assert client.get("/expenses").json()  # non-empty before

    r = client.delete("/account")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "deleted"
    assert body["rows"]["expenses"] == 1
    assert body["rows"]["budgets"] == 1
    # A label rename writes both the new name and an original-name mapping, so
    # this is >=1 rather than exactly 1 — the point is the table is cleared.
    assert body["rows"]["merchant_categories"] >= 1
    assert body["rows"]["recurring_dismissals"] == 1

    # Everything is gone afterward.
    assert client.get("/expenses").json() == []
    assert client.get("/budgets").json() == []
    assert client.get("/recurring").json()["dismissed"] == []


def test_delete_account_auth_record_skipped_without_key(client):
    # No SUPABASE_SERVICE_ROLE_KEY in tests → auth-record delete is a no-op, but
    # the data wipe (the actual guarantee) still succeeds and is reported.
    _seed(client)
    body = client.delete("/account").json()
    assert body["auth_account_deleted"] is False


def test_delete_account_is_scoped_to_caller(client, client_b):
    _seed(client)     # USER_A
    _seed(client_b)   # USER_B, same DB

    client.delete("/account")  # A deletes their account

    # B's data is untouched.
    assert len(client_b.get("/expenses").json()) == 1
    assert len(client_b.get("/budgets").json()) == 1


def test_delete_account_requires_auth(unauth_client):
    assert unauth_client.delete("/account").status_code == 401
