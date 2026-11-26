"""Phase 7.5 ops hardening: rate limiting + Sentry wiring.

These don't touch the DB — they exercise the cross-cutting middleware/config.
"""
from fastapi import Request
from fastapi.testclient import TestClient

from sqlalchemy.exc import OperationalError

from app.database import get_db
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


# --- /health/db -------------------------------------------------------------
# "/health" is a constant, so it cannot distinguish a working deployment from
# one whose database has vanished. These pin the endpoint that can.

def test_health_db_reports_reachable(client):
    r = client.get("/health/db")
    assert r.status_code == 200
    assert r.json() == {"status": "healthy", "database": "reachable"}


def test_health_db_returns_503_when_database_is_down(client):
    """The reason this endpoint exists: a paused Supabase project must read as
    DOWN. Its hostname stops resolving, every query raises, and the old
    constant-200 /health would still report the service as healthy."""

    class _DeadSession:
        def execute(self, *args, **kwargs):
            raise OperationalError(
                "SELECT 1", {}, Exception("could not translate host name")
            )

    def _dead_db():
        yield _DeadSession()

    # The `client` fixture clears dependency_overrides on teardown.
    app.dependency_overrides[get_db] = _dead_db

    r = client.get("/health/db")
    assert r.status_code == 503
    assert "unreachable" in r.text.lower()


def test_health_db_is_exempt_from_rate_limiting(client):
    # The keep-alive pings this on a schedule; throttling it would defeat it.
    limiter.reset()
    assert all(client.get("/health/db").status_code == 200 for _ in range(50))
