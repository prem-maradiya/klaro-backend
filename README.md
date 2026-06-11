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

## Deploy to the cloud (Render + Neon, both free)

1. **Database — Neon** (https://neon.tech): create a free project, copy the
   connection string (`postgresql://...neon.tech/neondb?sslmode=require`).
   Use the **pooled** connection string.
2. **API — Render** (https://render.com): New + → **Blueprint** → connect this
   GitHub repo. `render.yaml` defines the service. When prompted, paste:
   - `DATABASE_URL` = the Neon connection string
   - `GROQ_API_KEY` = your Groq key (free at https://console.groq.com/keys)
   - `API_KEY` is auto-generated — copy it from the service's Environment tab.
3. Tables are created automatically on first start.
4. Put the Render URL (`https://klaro-api.onrender.com`) + the API key into
   the phone app (Settings) and the dashboard (gear icon → Connection).
5. After deploying the dashboard, set `CORS_ORIGINS` on Render to its URL,
   e.g. `https://klaro-dashboard.vercel.app`.

Free-tier note: Render spins the service down after ~15 min idle; the first
request then takes ~50 s. The phone app's retry-sync handles this — a synced
expense may just take a minute to land.

## Security

- `API_KEY` env var set → every endpoint except `/` and `/health` requires
  the `X-API-Key` header. Unset → open (local dev).
- `CORS_ORIGINS` env var (comma-separated) restricts browser origins.

## Endpoints

| Method | Path                  | Purpose                                      |
|--------|-----------------------|----------------------------------------------|
| GET    | `/expenses`           | List (newest first); `?month=YYYY-MM`, `skip`/`limit`, `unlabeled_only`; `X-Total-Count` header |
| POST   | `/expenses`           | Create (dedupes by SMS hash; auto-categorizes) |
| GET    | `/expenses/{id}`      | Get one expense                              |
| PATCH  | `/expenses/{id}`      | Update label/category (learns renames)       |
| DELETE | `/expenses/{id}`      | Delete an expense                            |
| GET    | `/months`             | Months having data, with count + total       |
| GET    | `/summary`            | Category totals; `?month=YYYY-MM`            |
| POST   | `/parse-sms`          | AI fallback parser for unrecognized bank SMS |
| GET    | `/categorize`         | Preview a merchant's category                |
| GET    | `/health`             | Liveness (no auth)                           |
