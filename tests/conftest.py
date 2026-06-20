"""Pytest fixtures: run the FastAPI app against a throwaway in-memory SQLite DB.

The app's real engine (Postgres) is never touched — we set DATABASE_URL to
SQLite *before* importing the app, override the `get_db` dependency to a test
session, create the schema per-test, and build the TestClient WITHOUT the
lifespan context manager (so the Postgres-specific `_ensure_schema` never runs).
"""
import os

# The app's real engine (Postgres, built at import time) is never connected:
# we override get_db to a SQLite session and skip the lifespan. We only force
# auth open here; DATABASE_URL keeps its Postgres default so create_engine
# accepts the prod pool settings.
os.environ["API_KEY"] = ""  # open auth by default; auth test sets it explicitly

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401 — registers tables on Base
from app.database import Base, get_db
from app.main import app

# One shared in-memory database for the whole test session. StaticPool keeps a
# single connection so every session sees the same tables/rows.
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


@pytest.fixture()
def client():
    """A TestClient with a fresh schema. Tables are dropped after each test so
    cases stay isolated."""
    Base.metadata.create_all(bind=_engine)
    app.dependency_overrides[get_db] = _override_get_db
    # No `with` block: skip lifespan so the real Postgres engine is never hit.
    yield TestClient(app)
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=_engine)


@pytest.fixture()
def db():
    """Direct DB session for tests that seed rows without going through the API."""
    Base.metadata.create_all(bind=_engine)
    session = _TestSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=_engine)
