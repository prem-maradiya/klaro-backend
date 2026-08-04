<div align="center">

# Klaro Backend — Expense Tracker API

**Multi-tenant FastAPI backend for [Klaro](https://github.com/prem-maradiya/klaro-app) — the
SMS-auto-capture expense tracker — and its web dashboard.**

[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![Supabase](https://img.shields.io/badge/Postgres-Supabase-3ECF8E?logo=supabase&logoColor=white)](https://supabase.com)
[![Deploy](https://img.shields.io/badge/Deployed-Vercel-000?logo=vercel&logoColor=white)](https://klaro-api.vercel.app)
[![Tests](https://img.shields.io/badge/tests-pytest-0A9EDC?logo=pytest&logoColor=white)](#tests)

</div>

FastAPI + Supabase Postgres backend for the Klaro expense-tracking app
(SMS auto-capture on Android) and its web dashboard. Handles identity, per-user
data isolation (row-level scoping) and a source-agnostic ingestion pipeline.

> **Privacy:** SMS parsing happens entirely on the phone. This API only ever
> receives the *cleaned* transaction (merchant, amount, category, date) plus a
> one-way hash for de-duplication — never the raw message, account numbers or
> balances. No LLM is involved anywhere.

## How it fits together

```
📱 Klaro app  ──┐
                ├──HTTPS + Supabase JWT──►  THIS API (FastAPI · Vercel)
🌐 Dashboard ──┘                                  │
                                                  └──►  Supabase (Postgres + Auth + RLS)
```

Every transaction — parsed SMS, manual entry, or pasted/forwarded text — flows
through one `TxnCandidate` contract and a single `ingest()` pipeline
(*dedupe → categorize → learn → store*), so adding a new source never touches the
storage logic. Modules: `auth` (JWT/JWKS) · `ingestion` (pipeline) · `categorizer`
· `observability` (rate limit + Sentry) · `models`/`schemas`/`database`.

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

**Database: Supabase Postgres** (free tier). The app uses the **session pooler**
connection string and Supabase Auth (ES256 JWT verified against the project JWKS —
no shared secret on the backend). Tables are created automatically on first start.

Env vars (set on the Vercel project): `DATABASE_URL` (Supabase pooled string),
`SUPABASE_URL` (for JWKS verification), `SUPABASE_SERVICE_ROLE_KEY` (account
deletion), `CORS_ORIGINS`, `DB_POOL_SIZE=2`, `DB_MAX_OVERFLOW=3`, and optionally
`SENTRY_DSN`, `RATELIMIT_*`. See `.env.example` for the full list.

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
- **Rate limiting (Phase 7.5):** on by default — a global per-user/IP cap;
  `/` and `/health` are exempt. Tune via `RATELIMIT_*` (see `.env.example`).

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
| GET    | `/categorize`         | Preview a merchant's category                |
| DELETE | `/account`            | Delete the user's account and all their data |
| GET    | `/health`             | Liveness (no auth)                           |
