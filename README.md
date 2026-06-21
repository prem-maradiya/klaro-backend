# Klaro Backend (Expense Tracker API)

FastAPI + PostgreSQL backend for the Klaro expense-tracking app
(SMS auto-capture on Android) and its web dashboard.

## Local setup

```powershell
# From this folder
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env   # then edit
```

Make sure PostgreSQL is running and the `expense_tracker` database exists
(see `.env` for the connection string).

## Run (local)

```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

- API docs: http://localhost:8000/docs
- Phone over USB: `adb reverse tcp:8000 tcp:8000`, app backend URL `http://localhost:8000`
- Phone over Wi-Fi: set the laptop's LAN IP in the app's Settings screen

## Tests

```powershell
pip install -r requirements-dev.txt
pytest -q
```

The suite runs against in-memory SQLite (no Postgres needed) and covers dedupe,
income/spending separation, budgets, recurring detection + dismiss/restore, and
learned renames. CI runs it on every push (`.github/workflows/tests.yml`).

## Cloud deployment (live)

**Deployed on Vercel: https://klaro-api.vercel.app** (project `klaro-api`,
auto-deploys on push to this repo's master). Serverless entrypoint is
`api/index.py`; `vercel.json` uses an explicit `builds` config on purpose —
zero-config detection routes FastAPI apps into Vercel's framework preset,
which broke this deploy in several ways (see `.vercelignore` note about
render.yaml too).

Env vars (set on the Vercel project): `API_KEY`, `GROQ_API_KEY`, `GROQ_MODEL`,
`CORS_ORIGINS`, `DB_POOL_SIZE=2`, `DB_MAX_OVERFLOW=3`, and — once the database
exists — `DATABASE_URL`.

**Remaining one-time step — database (Neon, free, ~5 min):**
1. Sign up at https://neon.tech (free tier, no card), create a project.
2. Copy the **pooled** connection string (`postgresql://...-pooler...neon.tech/neondb?sslmode=require`).
3. `printf '%s' "<that string>" | npx vercel env add DATABASE_URL production`
   (run in this folder), then `npx vercel deploy --prod --yes`.
4. Tables are created automatically on first start.

To migrate existing local data to Neon:
`pg_dump -d expense_tracker | psql "<neon connection string>"`.

### Alternative: Render (render.yaml)

`render.yaml` is kept for deploying on https://render.com instead (New + →
Blueprint → connect this repo). Note Render's free tier spins down after
~15 min idle (~50 s cold start).

## Security

- **Auth (Phase 7.1+):** every endpoint except `/` and `/health` requires a
  valid Supabase JWT (`Authorization: Bearer …`). The backend verifies the
  ES256 signature against the project JWKS (`SUPABASE_URL`) — no shared secret —
  and scopes every query to the token's `user_id`. In unit tests auth is
  overridden (see `tests/conftest.py`).
- **CORS:** `CORS_ORIGINS` (comma-separated) restricts browser origins.
- **Rate limiting (Phase 7.5):** on by default — a global per-user/IP cap plus a
  tighter limit on the AI `/parse-sms` route; `/` and `/health` are exempt.
  Tune via `RATELIMIT_*` (see `.env.example`).

## Ops & observability (Phase 7.5)

- **Error tracking:** set `SENTRY_DSN` to enable Sentry (no-op when unset; never
  sends PII).
- **Backups:** `python scripts/backup_db.py` writes a gzipped `pg_dump` under
  `backups/` (Supabase also keeps daily backups).
- **Runbook:** deploy, secret rotation, restore, incident steps and monitoring
  live in [`RUNBOOK.md`](RUNBOOK.md).

## Endpoints

| Method | Path                  | Purpose                                      |
|--------|-----------------------|----------------------------------------------|
| GET    | `/expenses`           | List (newest first); `?month=YYYY-MM`, `skip`/`limit`, `unlabeled_only`; `X-Total-Count` header |
| POST   | `/expenses`           | Create via the source-agnostic ingestion pipeline (dedupes by SMS hash; auto-categorizes; records `source`) |
| GET    | `/expenses/{id}`      | Get one expense                              |
| PATCH  | `/expenses/{id}`      | Update label/category (learns renames)       |
| DELETE | `/expenses/{id}`      | Delete an expense                            |
| GET    | `/months`             | Months having data, with count + total       |
| GET    | `/summary`            | Category totals; `?month=YYYY-MM`            |
| POST   | `/parse-sms`          | AI fallback parser for unrecognized bank SMS |
| GET    | `/categorize`         | Preview a merchant's category                |
| GET    | `/health`             | Liveness (no auth)                           |
