"""Privacy sync (on-device parsing): the app uploads only the cleaned
transaction plus a client-computed dedupe hash — never the raw SMS or VPA. These
tests assert the backend stores nothing sensitive and still dedupes re-imports
via the client hash (so retries can't double-count)."""
from datetime import datetime


def _sms_payload(**over):
    body = {
        "merchant": "Zomato",
        "amount": 285.0,
        "date": datetime.now().isoformat(),
        "category": "Food",
        "is_labeled": True,
        "direction": "debit",
        "source": "sms",
        "raw_sms_hash": "a" * 64,  # one-way token; no raw_sms / upi_id sent
    }
    body.update(over)
    return body


def test_nothing_sensitive_stored(client):
    r = client.post("/expenses", json=_sms_payload())
    assert r.status_code == 201
    body = r.json()
    # The raw message, VPA and balance never reached the server.
    assert body["raw_sms"] == ""
    assert body["upi_id"] is None
    # Source was taken from the explicit field (no longer inferred from raw_sms).
    assert body["source"] == "sms"


def test_client_hash_dedupes_reimport(client):
    p = _sms_payload()
    r1 = client.post("/expenses", json=p)
    assert r1.status_code == 201
    # Same hash + amount within the dedupe window -> existing row, 200 not 201.
    r2 = client.post("/expenses", json=p)
    assert r2.status_code == 200
    assert r2.json()["id"] == r1.json()["id"]


def test_different_hash_is_a_distinct_txn(client):
    # Two genuinely different SMS (different hashes), same amount/time -> two rows.
    # This is why a hash beats a natural key: identical-looking spends aren't merged.
    a = client.post("/expenses", json=_sms_payload(raw_sms_hash="b" * 64))
    b = client.post("/expenses", json=_sms_payload(raw_sms_hash="c" * 64))
    assert a.status_code == 201 and b.status_code == 201
    assert a.json()["id"] != b.json()["id"]


def test_manual_entry_no_hash_always_creates(client):
    # A manual add (no raw_sms, no hash) skips dedupe -> never silently merged.
    p = {
        "merchant": "Chai", "amount": 20.0,
        "date": datetime.now().isoformat(),
        "category": "Food", "is_labeled": True,
        "direction": "debit", "source": "manual",
    }
    a = client.post("/expenses", json=p)
    b = client.post("/expenses", json=p)
    assert a.json()["id"] != b.json()["id"]
