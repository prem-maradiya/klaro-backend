"""One-time migration: add expenses.direction ('debit' | 'credit') for income
tracking. Every existing row is a spend, so the column defaults to 'debit'.
Idempotent — safe to run more than once, on local dev and on Neon.

Usage: python scripts/add_direction_column.py ["<database url>"]
       (with no argument it reads DATABASE_URL from the environment / .env)
"""
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
url = sys.argv[1] if len(sys.argv) > 1 else os.getenv("DATABASE_URL", "")
if not url:
    sys.exit("No database URL given and DATABASE_URL is not set")
if url.startswith("postgres://"):
    url = url.replace("postgres://", "postgresql+psycopg://", 1)
elif url.startswith("postgresql://"):
    url = url.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(url)
with engine.begin() as conn:
    conn.execute(text(
        "ALTER TABLE expenses ADD COLUMN IF NOT EXISTS direction "
        "VARCHAR(10) NOT NULL DEFAULT 'debit'"
    ))
    # Matches the name SQLAlchemy gives the model's index=True column, so a
    # fresh create_all and this migration converge on the same schema.
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_expenses_direction ON expenses (direction)"
    ))
    total = conn.execute(text("SELECT count(*) FROM expenses")).scalar()
    debit = conn.execute(
        text("SELECT count(*) FROM expenses WHERE direction = 'debit'")
    ).scalar()
    print(f"direction column ready — {debit}/{total} rows = 'debit'")
