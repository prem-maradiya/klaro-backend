"""Budget scoping (Option A): small day-to-day spends count toward the budget
automatically; large or fee-like payments are held out and flagged for the user
to include/exclude. The budget bar uses budgeted spend; totals stay honest."""
from datetime import datetime


def _post(client, **over):
    body = {
        "merchant": "Test Merchant",
        "amount": 200.0,
        "date": datetime.now().isoformat(),
        "category": "Food",
        "is_labeled": True,
        "direction": "debit",
    }
    body.update(over)
    return client.post("/expenses", json=body)


def test_small_debit_counts_toward_budget(client):
    r = _post(client, amount=200.0)
    assert r.status_code == 201
    body = r.json()
    assert body["in_budget"] is True
    assert body["budget_review"] is False


def test_large_debit_held_for_review(client):
    # No history yet → the ₹5,000 floor applies; ₹6,000 is "large".
    r = _post(client, amount=6000.0, merchant="Big TV", category="Shopping")
    assert r.status_code == 201
    body = r.json()
    assert body["in_budget"] is False
    assert body["budget_review"] is True


def test_fee_like_text_held_even_when_small(client):
    # Below the floor, but the text mentions a fee → held out anyway.
    r = _post(
        client,
        amount=800.0,
        merchant="MIT College",
        raw_sms="Paid college fees of Rs 800",
        category="Bills",
    )
    body = r.json()
    assert body["in_budget"] is False
    assert body["budget_review"] is True


def test_credit_is_never_held(client):
    # Income never counts toward a spending budget and is never flagged.
    r = _post(client, amount=50000.0, direction="credit", category="Income")
    body = r.json()
    assert body["in_budget"] is True
    assert body["budget_review"] is False


def test_budget_status_excludes_oneoff(client):
    client.put("/budgets/overall", json={"amount": 20000.0})
    _post(client, amount=200.0, category="Food")              # counts
    _post(client, amount=6000.0, merchant="Big TV",           # held out
          category="Shopping")
    status = client.get("/budgets/status").json()
    overall = next(b for b in status["budgets"] if b["category"] == "overall")
    assert overall["spent"] == 200.0  # the ₹6,000 one-off is not counted


def test_summary_splits_budgeted_and_oneoff(client):
    _post(client, amount=200.0, category="Food")
    _post(client, amount=6000.0, merchant="Big TV", category="Shopping")
    s = client.get("/summary").json()
    assert s["expense_total"] == 6200.0      # honest total
    assert s["budgeted_total"] == 200.0      # counts toward budget
    assert s["oneoff_total"] == 6000.0       # held out


def test_review_queue_and_decision_flow(client):
    r = _post(client, amount=6000.0, merchant="Big TV", category="Shopping")
    exp_id = r.json()["id"]

    queue = client.get("/budget/review").json()
    assert [e["id"] for e in queue] == [exp_id]

    # User decides "yes, count it": include + clear the review flag.
    patched = client.patch(
        f"/expenses/{exp_id}", json={"in_budget": True, "budget_review": False}
    )
    assert patched.status_code == 200
    assert patched.json()["in_budget"] is True

    assert client.get("/budget/review").json() == []
    client.put("/budgets/overall", json={"amount": 20000.0})
    overall = next(
        b for b in client.get("/budgets/status").json()["budgets"]
        if b["category"] == "overall"
    )
    assert overall["spent"] == 6000.0  # now counted after the user included it
