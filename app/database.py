import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5432/expense_tracker",
)

# Managed Postgres providers (Neon, Render, Railway) hand out URLs as
# postgres:// or postgresql:// — rewrite to the psycopg3 driver SQLAlchemy needs.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgres://"):]
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgresql://"):]

engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    # Handle bursts: the app POSTs many expenses at once on import, and each
    # request can hold a connection while the AI fallback runs. Cloud free
    # tiers cap connections (Neon free ~100 via pooler) — size down via env.
    pool_size=int(os.getenv("DB_POOL_SIZE", "25")),
    max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "50")),
    pool_timeout=30,
    pool_recycle=300,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency that yields a DB session and closes it afterward."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
