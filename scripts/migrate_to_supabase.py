"""One-shot data migration: copy Klaro's existing single-tenant rows from the
old database (Neon) into the new multi-tenant Supabase schema, tagging every
row with the owner's user_id.

Run AFTER:
  1. supabase_schema.sql has been applied to the Supabase project, and
  2. the owner has signed up (so their auth.users row + UUID exist).

Usage (PowerShell):
  $env:SOURCE_DATABASE_URL = "postgresql://...neon..."        # old DB (read)
  $env:DEST_DATABASE_URL   = "postgresql://...supabase..."    # new DB (write)
  $env:KLARO_USER_ID       = "<your-supabase-auth-user-uuid>"
  ./.venv/Scripts/python.exe scripts/migrate_to_supabase.py

It is idempotent-ish: expenses are de-duplicated by (raw_sms_hash, amount, date)
so re-running won't double-insert SMS-sourced rows. Budgets / learned categories
/ dismissals upsert on their natural keys.
"""
import os
import sys

import psycopg

SOURCE = os.environ.get("SOURCE_DATABASE_URL")
DEST = os.environ.get("DEST_DATABASE_URL")
USER_ID = os.environ.get("KLARO_USER_ID")


def _norm(url: str | None) -> str | None:
    # psycopg wants a plain postg: URL, not the SQLAlchemy +psycopg variant.
    if not url:
        return url
    return url.replace("postgresql+psycopg://", "postgresql://")


def main() -> int:
    if not (SOURCE and DEST and USER_ID):
        print("Set SOURCE_DATABASE_URL, DEST_DATABASE_URL, KLARO_USER_ID.")
        return 2

    src = psycopg.connect(_norm(SOURCE))
    dst = psycopg.connect(_norm(DEST))
    dst.autocommit = False

    with src.cursor() as sc, dst.cursor() as dc:
        # ---- expenses ----
        sc.execute(
            "select merchant, amount, date, raw_sms, raw_sms_hash, upi_id, "
            "is_labeled, category, method, direction, created_at from expenses"
        )
        rows = sc.fetchall()
        inserted = 0
        for r in rows:
            (merchant, amount, date, raw_sms, raw_sms_hash, upi_id,
             is_labeled, category, method, direction, created_at) = r
            # Skip if an identical SMS-sourced row already exists for this user.
            if raw_sms_hash:
                dc.execute(
                    "select 1 from expenses where user_id=%s and raw_sms_hash=%s "
                    "and amount=%s and date=%s limit 1",
                    (USER_ID, raw_sms_hash, amount, date),
                )
                if dc.fetchone():
                    continue
            dc.execute(
                "insert into expenses (user_id, merchant, amount, date, raw_sms, "
                "raw_sms_hash, upi_id, is_labeled, category, method, direction, created_at) "
                "values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (USER_ID, merchant, amount, date, raw_sms, raw_sms_hash, upi_id,
                 is_labeled, category, method, direction, created_at),
            )
            inserted += 1
        print(f"expenses: {inserted} inserted / {len(rows)} source rows")

        # ---- budgets ----
        sc.execute("select category, amount, updated_at from budgets")
        b = sc.fetchall()
        for category, amount, updated_at in b:
            dc.execute(
                "insert into budgets (user_id, category, amount, updated_at) "
                "values (%s,%s,%s,%s) on conflict (user_id, category) do update "
                "set amount=excluded.amount, updated_at=excluded.updated_at",
                (USER_ID, category, amount, updated_at),
            )
        print(f"budgets: {len(b)} upserted")

        # ---- merchant_categories (learned labels/renames) ----
        sc.execute("select merchant_norm, merchant_display, category, source, "
                   "updated_at from merchant_categories")
        m = sc.fetchall()
        for merchant_norm, merchant_display, category, source, updated_at in m:
            dc.execute(
                "insert into merchant_categories (user_id, merchant_norm, "
                "merchant_display, category, source, updated_at) values "
                "(%s,%s,%s,%s,%s,%s) on conflict (user_id, merchant_norm) do update "
                "set merchant_display=excluded.merchant_display, "
                "category=excluded.category, source=excluded.source, "
                "updated_at=excluded.updated_at",
                (USER_ID, merchant_norm, merchant_display, category, source, updated_at),
            )
        print(f"merchant_categories: {len(m)} upserted")

        # ---- recurring_dismissals ----
        try:
            sc.execute("select merchant_norm, merchant_display, created_at "
                       "from recurring_dismissals")
            d = sc.fetchall()
            for merchant_norm, merchant_display, created_at in d:
                dc.execute(
                    "insert into recurring_dismissals (user_id, merchant_norm, "
                    "merchant_display, created_at) values (%s,%s,%s,%s) on conflict "
                    "(user_id, merchant_norm) do nothing",
                    (USER_ID, merchant_norm, merchant_display, created_at),
                )
            print(f"recurring_dismissals: {len(d)} upserted")
        except psycopg.errors.UndefinedTable:
            dst.rollback()
            print("recurring_dismissals: source table absent, skipped")

    dst.commit()
    src.close()
    dst.close()
    print("Migration complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
