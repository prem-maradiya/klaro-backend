"""Phase 7.5 ops hardening: rate limiting + Sentry wiring.

These don't touch the DB — they exercise the cross-cutting middleware/config.
"""
from fastapi import Request
from fastapi.testclient import TestClient

from app.main import app
from app.observability import init_sentry, limiter


# A throwaway route with a deliberately tiny limit, registered only in the test
# process. It uses its own named-limit bucket, independent of the (effectively
# unlimited) default the functional tests run under — so asserting a 429 here
# can't bleed into or be tripped by any other test.
@app.get("/__ratelimit_probe")
@limiter.limit("3/minute")
async def _ratelimit_probe(request: Request):
    return {"ok": True}


def test_rate_limit_returns_429_after_threshold():
    limiter.reset()
    client = TestClient(app)
    # 3 allowed, the 4th in the same window is rejected.
    assert [client.get("/__ratelimit_probe").status_code for _ in range(3)] == [200, 200, 200]
    blocked = client.get("/__ratelimit_probe")
    assert blocked.status_code == 429
    assert "rate limit exceeded" in blocked.text.lower()


def test_health_is_exempt_from_rate_limiting():
    limiter.reset()
    client = TestClient(app)
    # Uptime monitors hammer /health; it must never be throttled.
    assert all(client.get("/health").status_code == 200 for _ in range(50))
    assert all(client.get("/").status_code == 200 for _ in range(50))


def test_init_sentry_noop_without_dsn(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert init_sentry() is False  # disabled, and crucially does not raise


def test_init_sentry_enabled_with_dsn(monkeypatch):
    # Stub sentry_sdk.init so we verify init_sentry's behaviour and config
    # WITHOUT standing up a real client/transport (which would queue session
    # events and stall the process flushing them to a dummy DSN at exit).
    captured = {}
    monkeypatch.setattr("sentry_sdk.init", lambda **kw: captured.update(kw))
    monkeypatch.setenv("SENTRY_DSN", "https://examplePublicKey@o0.ingest.sentry.io/0")
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "test")

    assert init_sentry() is True
    assert captured["dsn"].endswith("/0")
    assert captured["environment"] == "test"
    # A finance app must never ship request bodies / IPs to a third party.
    assert captured["send_default_pii"] is False


def test_init_sentry_bad_dsn_is_swallowed(monkeypatch):
    monkeypatch.setenv("SENTRY_DSN", "not-a-valid-dsn")
    # A misconfigured DSN must degrade to "disabled", never crash startup.
    assert init_sentry() is False
