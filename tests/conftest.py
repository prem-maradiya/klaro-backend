"""Pytest fixtures: run the FastAPI app against a throwaway in-memory SQLite DB
with auth stubbed to a controllable test user.

The app's real engine (Postgres) is never touched — we override the `get_db`
dependency to a SQLite session and the `get_current_user` dependency to a fixed
user id, create the schema per-test, and build the TestClient WITHOUT the
lifespan context manager (so the Postgres-specific startup never runs).
"""
import os

# Force auth open is no longer relevant (per-user auth); keep API_KEY unset.
os.environ["API_KEY"] = ""

# Rate limiting: keep the default cap effectively unlimited for the functional
# tests (the dedicated rate-limit test uses its own low-limit route). Set BEFORE
# importing the app so observability.limiter reads these.
os.environ.setdefault("RATELIMIT_DEFAULT", "100000/minute")
os.environ.setdefault("RATELIMIT_PARSE", "100000/minute")

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401 — registers tables on Base
from app.auth import get_current_user
from app.database import Base, get_db
from app.main import app
from app.observability import limiter

# Two stable test users so isolation tests can prove A can't see B's data.
USER_A = "11111111-1111-1111-1111-111111111111"
USER_B = "22222222-2222-2222-2222-222222222222"

_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_TestSession = sessionmaker(bind=_engine, autoflush=False, autocommit=False)


def _override_get_db():
    db = _TestSession()
    try:
        yield db
    finally:
        db.close()


# dependency_overrides is global to the app, so we can't bind one client to
# USER_A and another to USER_B via a closure. Instead the override reads the
# user from a per-request header, and each client sets its own default header —
# letting two clients act as different users against the same app/DB.
def _override_current_user(request: Request) -> str:
    return request.headers.get("x-test-user", USER_A)


def _make_client(user_id: str) -> TestClient:
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_current_user
    # No `with` block: skip lifespan so the real Postgres engine is never hit.
    return TestClient(app, headers={"x-test-user": user_id})


@pytest.fixture()
def client():
    """A TestClient authenticated as USER_A, with a fresh schema."""
    Base.metadata.create_all(bind=_engine)
    limiter.reset()  # clear rate-limit counters so tests don't bleed into each other
    yield _make_client(USER_A)
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=_engine)


@pytest.fixture()
def client_b():
    """A second TestClient authenticated as USER_B, sharing the same DB as
    `client` (do not drop here — the `client` fixture owns the schema lifecycle).
    Use together with `client` for cross-user isolation tests."""
    return _make_client(USER_B)


@pytest.fixture()
def unauth_client():
    """DB overridden but auth NOT — to assert protected routes reject requests
    with no/invalid token."""
    Base.metadata.create_all(bind=_engine)
    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=_engine)
