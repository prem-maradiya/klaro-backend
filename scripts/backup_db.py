"""Off-site database backup (Phase 7.5 ops).

Runs `pg_dump` against DATABASE_URL and writes a timestamped, gzip-compressed
custom-format dump under ./backups/ (override with --out-dir). Supabase keeps
its own daily backups; this is a belt-and-suspenders copy you control and can
restore anywhere — run it before risky migrations and on a schedule.

Usage (from the backend folder, with the venv active or via its python):
    python scripts/backup_db.py                  # uses DATABASE_URL from env/.env
    python scripts/backup_db.py --out-dir D:/klaro-backups
    python scripts/backup_db.py --keep 14         # prune backups older than N days
    DATABASE_URL=... python scripts/backup_db.py  # explicit target

Requires the `pg_dump` client binary on PATH (ships with PostgreSQL; on Windows
it's in e.g. C:\\Program Files\\PostgreSQL\\17\\bin). Restore a dump with:
    pg_restore --clean --if-exists --no-owner -d "<DATABASE_URL>" <file>
See RUNBOOK.md for the full restore procedure.
"""
import argparse
import gzip
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make `from dotenv import load_dotenv` optional — the script also works with a
# plain DATABASE_URL in the environment.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass


def _libpq_url(url: str) -> str:
    """SQLAlchemy uses a `postgresql+psycopg://` driver prefix; pg_dump wants a
    plain libpq URI. Strip the driver, leave everything else (user/host/query)."""
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


def main() -> int:
    ap = argparse.ArgumentParser(description="Back up the Klaro Postgres database.")
    ap.add_argument("--out-dir", default="backups", help="directory to write dumps to")
    ap.add_argument("--keep", type=int, default=0,
                    help="delete .sql.gz backups older than N days (0 = keep all)")
    args = ap.parse_args()

    url = os.getenv("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set (check .env or the environment).", file=sys.stderr)
        return 2
    if shutil.which("pg_dump") is None:
        print("pg_dump not found on PATH. Install the PostgreSQL client tools "
              "(e.g. C:\\Program Files\\PostgreSQL\\17\\bin).", file=sys.stderr)
        return 3

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    target = out_dir / f"klaro-{stamp}.sql.gz"

    # Plain-SQL dump piped straight into gzip so no uncompressed copy hits disk.
    print(f"Dumping database -> {target} ...")
    started = time.time()
    proc = subprocess.Popen(
        ["pg_dump", "--no-owner", "--no-privileges", _libpq_url(url)],
        stdout=subprocess.PIPE,
    )
    assert proc.stdout is not None
    with gzip.open(target, "wb") as gz:
        shutil.copyfileobj(proc.stdout, gz)
    rc = proc.wait()
    if rc != 0:
        target.unlink(missing_ok=True)
        print(f"pg_dump failed (exit {rc}).", file=sys.stderr)
        return rc

    size_mb = target.stat().st_size / 1_048_576
    print(f"OK — {size_mb:.2f} MB in {time.time() - started:.1f}s")

    if args.keep > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.keep)
        removed = 0
        for f in out_dir.glob("klaro-*.sql.gz"):
            if datetime.fromtimestamp(f.stat().st_mtime, timezone.utc) < cutoff:
                f.unlink()
                removed += 1
        if removed:
            print(f"Pruned {removed} backup(s) older than {args.keep} days.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
