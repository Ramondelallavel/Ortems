# MonxuPlan — Deployment & operations

## Components

| Process | Image / command | Scales | Notes |
|---|---|---|---|
| **api** | `backend/Dockerfile` · `uvicorn monxuplan.api.app:app` | horizontally (stateless) | REST API, SSE stream, webhooks delivery thread |
| **worker** | same image · `python -m monxuplan.worker` | horizontally | Claims planning runs with `SELECT … FOR UPDATE SKIP LOCKED`; stale runs (no heartbeat 10 min) are re-queued |
| **web** | `frontend/Dockerfile` · Next.js standalone | horizontally | Serves the UI and proxies `/api/*` to the API (first-party cookies) |
| **PostgreSQL 16** | `postgres:16-alpine` | primary + replicas | Single source of truth, tenant-scoped rows |
| **Redis 7** | `redis:7-alpine` | single | Event fan-out between API/worker processes (SSE); optional for a single process |

Development mode needs none of the infrastructure: SQLite + in-process worker threads + in-memory
event bus (`MONXU_WORKER_IN_PROCESS=1`, the default).

## Docker Compose

```bash
cd deploy
cp .env.example .env            # change POSTGRES_PASSWORD and MONXU_SECRET_KEY
docker compose up -d --build
open http://localhost:3000      # demo users when MONXU_SEED_DEMO=1: planner / Monxu-Demo-2026
```

The API container applies migrations on start (`MONXU_MIGRATE=1`), then loads the demo tenant if
`MONXU_SEED_DEMO=1` (never in production). Two worker replicas run planning jobs.

## Configuration (environment)

| Variable | Default | Purpose |
|---|---|---|
| `MONXU_ENV` | `development` | `production` enables start-up checks (below) |
| `MONXU_DATABASE_URL` | SQLite in `backend/data` | `postgresql+psycopg://user:pass@host:5432/db` |
| `MONXU_SECRET_KEY` | generated file (dev only) | Signs sessions/CSRF, derives the AES-GCM key for connector and webhook secrets. ≥ 32 random chars; identical on every API instance |
| `MONXU_REDIS_URL` | — | Enables multi-process event fan-out |
| `MONXU_WORKER_IN_PROCESS` / `MONXU_WORKER_THREADS` | `1` / `2` | Run planning jobs inside the API process (dev) |
| `MONXU_COOKIE_SECURE` | `0` | `1` behind HTTPS (Secure cookies + HSTS header) |
| `MONXU_CORS_ORIGINS` | `http://localhost:3000` | Only needed when the UI is served from another origin |
| `MONXU_SESSION_HOURS` | `12` | Session lifetime |
| `MONXU_RATE_LIMIT_PER_MINUTE` / `MONXU_LOGIN_RATE_LIMIT_PER_MINUTE` | `600` / `20` | Per client IP |
| `MONXU_MAX_UPLOAD_MB` | `25` | Import file size limit |
| `MONXU_OIDC_ISSUER`, `MONXU_OIDC_AUDIENCE`, `MONXU_OIDC_JWKS_URL` | — | Single sign-on (OAuth2/OIDC bearer tokens) |
| `MONXU_LOG_JSON`, `MONXU_LOG_LEVEL` | `1`, `INFO` | Structured logs |
| `MONXU_WEBHOOK_TIMEOUT_S` | `5` | Outbound webhook timeout |
| `MONXU_ASSISTANT_LLM`, `ANTHROPIC_API_KEY`, `MONXU_ASSISTANT_MODEL` | off | Optional language-model mode of the assistant (read-only tools over plan data) |
| `MONXU_NOW` | — | Freeze the clock (demos, tests) |
| `MONXU_API_URL` (web build arg) | `http://127.0.0.1:8000` | Where the web server proxies `/api` |

With `MONXU_ENV=production` the API refuses to start if the secret key is missing/weak, cookies are
not secure, or the database is SQLite.

## Database migrations

```bash
cd backend
alembic -c monxuplan/alembic.ini upgrade head     # apply
alembic -c monxuplan/alembic.ini check            # verify the models and the schema match
alembic -c monxuplan/alembic.ini revision --autogenerate -m "…"   # after model changes (review the file)
```

Revisions `0001` (schema) and `0002` (circular foreign keys, PostgreSQL only) are verified on
SQLite and PostgreSQL 16 with no drift.

## Onboarding a new company

```python
from monxuplan.services.tenants import create_tenant
create_tenant("ACME Industries", "acme", "acme.admin", "it@acme.example", "<initial password>",
              plant_code="P1", plant_name="Main plant", timezone="Europe/Madrid")
```

Then import master data with the wizard (Integrations → Import) in this order: calendars, customers,
suppliers, items, resources, BOMs, routings, inventory, purchase orders, production orders — the
acceptance dataset (`python -m monxuplan.seed.acceptance ./files`) is a complete example.

## Health, metrics, logs

* `GET /health` (liveness), `GET /readiness` (database reachable), `GET /metrics` (Prometheus:
  HTTP requests/latency, planning runs by status/provider, solver seconds, queue depth, SSE clients).
* JSON logs with request id, tenant, user, planning run id; unhandled errors log a stack trace with an
  `error_id` that is also returned to the client.
* Every business change is in the audit log (Administration → Audit log / `GET /audit`).

## Security checklist

HTTPS termination in front of web and API · `MONXU_COOKIE_SECURE=1` · strong `MONXU_SECRET_KEY` shared
by all API instances and kept in a secret store · PostgreSQL credentials rotated · API keys per
integration with expiry · webhook URLs HTTPS only (enforced in production) · backups of PostgreSQL
(plans are immutable versions: a restore loses nothing that was published).

## Backups

`pg_dump` of the database is sufficient: problem snapshots of every plan version are stored in the
database (gzip, content-addressed), so any published plan can be reproduced from the backup.
