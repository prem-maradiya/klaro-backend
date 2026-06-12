"""One-time backfill: recompute expenses.raw_sms_hash with the new
whitespace-stripped normalization (see _sms_hash in app/main.py).

Usage: python scripts/rehash_sms.py "<database url>"
"""
import hashlib
import re
import sys

from sqlalchemy import create_engine, text

url = sys.argv[1]
if url.startswith("postgres://"):
    url = url.replace("postgres://", "postgresql+psycopg://", 1)
elif url.startswith("postgresql://"):
    url = url.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(url)
with engine.begin() as conn:
    rows = conn.execute(
        text("SELECT id, raw_sms, raw_sms_hash FROM expenses "
             "WHERE raw_sms IS NOT NULL AND raw_sms <> ''")
    ).all()
    changed = 0
    for row_id, raw, old in rows:
        h = hashlib.sha256(re.sub(r"\s+", "", raw).encode("utf-8")).hexdigest()
        if h != old:
            conn.execute(
                text("UPDATE expenses SET raw_sms_hash = :h WHERE id = :i"),
                {"h": h, "i": row_id},
            )
            changed += 1
    print(f"rows checked: {len(rows)}, hashes updated: {changed}")
