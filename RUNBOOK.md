# Klaro Backend — Operations Runbook

Internal ops guide for the Klaro API (Phase 7.5). Covers deploy, configuration,
backups/restore, secret rotation, incident response, monitoring, and staging.
**No secrets live in this file** — they belong only in Vercel / Supabase env and
a local gitignored `.env`.

## At a glance

| Piece | Where |
|---|---|
| API (FastAPI, serverless) | Vercel project `klaro-api` → https://klaro-api.vercel.app |
| Code | github.com/prem-maradiya/klaro-backend (push to `master` auto-deploys) |
| Database | Supabase Postgres (session pooler host), region ap-south-1 |
| Auth | Supabase Auth (ES256 JWT; backend verifies against project JWKS) |
| Dashboard | Vercel `klaro-dashboard` → https://klaro-dashboard.vercel.app |
| Error tracking | Sentry (enabled when `SENTRY_DSN` is set) |

## Deploy

- **Normal:** push to `master`. Vercel builds `api/index.py` via the explicit
  `builds` config in `vercel.json` (do **not** switch to zero-config — it routes
  FastAPI into a framework preset that broke this deploy before).
- **Manual:** `npx vercel deploy --prod --yes` from this folder.
- **Verify:** `GET /health` → `{"status":"healthy"}` (200, no auth). Then a data
  route with a valid Bearer token returns the caller's rows only.
- Env changes do **not** take effect until the next deploy.

## Environment variables (set on the Vercel project)

| Var | Purpose |
|---|---|
| `DATABASE_URL` | Supabase session-pooler connection string (not the IPv6-only `db.*` direct host) |
| `SUPABASE_URL` | Project URL; backend fetches JWKS from `…/auth/v1/.well-known/jwks.json` |
| `CORS_ORIGINS` | Comma-separated allowed dashboard origins |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | Small on serverless (e.g. 2 / 3) |
| `GROQ_API_KEY` / `GROQ_MODEL` | AI categorization + `/parse-sms` fallback |
| `SENTRY_DSN` | Turns on error tracking; unset = off |
| `RATELIMIT_*` | Rate-limit tuning (see below); all optional |

Full list with defaults and examples: `.env.example`.

## Rate limiting

- On by default. Global cap `RATELIMIT_DEFAULT` (120/minute) on every route via
  `SlowAPIMiddleware`; the AI `/parse-sms` route has a tighter `RATELIMIT_PARSE`
  (20/minute). `/` and `/health` are exempt so uptime checks are never throttled.
- Keyed per **authenticated user** (JWT `sub`, read unverified just for bucketing),
  falling back to the client IP (first `X-Forwarded-For` hop behind Vercel).
- Over-limit → `429` with body `{"error":"Rate limit exceeded: …"}`.
- **Storage caveat:** default is in-memory, i.e. **per warm serverless instance** —
  good burst protection, but counters reset on cold start and aren't shared across
  instances. For strict global limits, create a free Upstash Redis DB and set
  `RATELIMIT_STORAGE_URI=redis://…`, then redeploy.
- **Disable fast** (incident): set `RATELIMIT_ENABLED=false` and redeploy.

## Error tracking (Sentry)

1. Create a project at sentry.io, copy the DSN.
2. Set `SENTRY_DSN` (and optionally `SENTRY_ENVIRONMENT`,
   `SENTRY_TRACES_SAMPLE_RATE`) on Vercel; redeploy.
3. `send_default_pii=False` is hard-coded — request bodies, headers and IPs are
   never sent (finance data). Keep it that way.
4. A bad/empty DSN degrades to "disabled" and never crashes startup.

## Backups & restore

- **Supabase** keeps its own automatic daily backups (verify retention for the
  current plan in the dashboard).
- **Own copy:** `python scripts/backup_db.py` → timestamped `backups/klaro-*.sql.gz`.
  Run it before any risky migration; schedule it (Task Scheduler / cron) with
  `--keep 14` to prune old dumps. Needs `pg_dump` on PATH.
- **Restore** a dump into a target database:
  ```
  gunzip -c backups/klaro-YYYYMMDD-HHMMSSZ.sql.gz | psql "<DATABASE_URL>"
  ```
  For a clean overwrite of an existing DB, drop/recreate the schema first or use a
  custom-format dump with `pg_restore --clean --if-exists --no-owner`.
- **Migration verification** (Phase 7 lesson): compare row counts and the all-time
  total between source and target before cutting over; keep the old DB until verified.

## Secret rotation

Rotate, set the new value in the appropriate env, then redeploy/restart:

- **Supabase DB password:** Supabase → Database → reset password → update
  `DATABASE_URL` on Vercel → redeploy. Update local `.env`.
- **Groq key:** console.groq.com → new key → update `GROQ_API_KEY` on Vercel →
  redeploy → revoke the old key.
- **JWT signing keys:** managed by Supabase; the backend holds no JWT secret and
  picks up rotated keys automatically via JWKS. Nothing to do here.
- **User account passwords:** via Supabase Auth (app/dashboard reset flow).
- After any leak, treat the secret as compromised immediately — rotate first,
  investigate second. Never commit secrets or paste them into memory/docs.

## Incident response (quick)

1. **Down / 500s:** check `GET /health`. Healthy but data routes 500 → likely
   `DATABASE_URL` / DB reachability. Check Sentry + Vercel runtime logs.
2. **Auth failures (401 everywhere):** verify `SUPABASE_URL` is set and the JWKS
   endpoint is reachable; confirm the project didn't rotate URLs.
3. **DB overloaded / too many connections:** lower `DB_POOL_SIZE` /
   `DB_MAX_OVERFLOW`; confirm the pooler host is in `DATABASE_URL`.
4. **Abuse / cost spike (Groq):** tighten `RATELIMIT_PARSE`, or set
   `RATELIMIT_ENABLED` aside and add a Redis store for hard global limits.
5. **Data corruption / bad migration:** restore the latest good backup into a
   fresh DB, verify counts/totals, then repoint `DATABASE_URL`.
6. Roll back code by redeploying the previous commit from Vercel's Deployments tab.

## Monitoring

- Add an UptimeRobot (or similar) HTTP monitor on `GET /health` (5-min interval);
  it's auth-free and rate-limit-exempt.
- Watch Sentry for new error groups; Vercel dashboard for function errors/latency.

## Staging (optional, recommended before big changes)

- A second Supabase project + a separate Vercel deploy (or preview) with its own
  `DATABASE_URL` / `SUPABASE_URL` / `CORS_ORIGINS`. Test migrations and risky
  changes there first; Vercel preview deploys give per-PR URLs automatically.
