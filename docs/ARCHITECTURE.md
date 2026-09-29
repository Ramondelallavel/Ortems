# MonxuPlan — Architecture

MonxuPlan is an Advanced Planning & Scheduling (APS) platform. It complements the ERP: the ERP stays
the transactional system of record (items, BOMs, routings, orders, inventory, purchasing), MonxuPlan
is the specialist system for planning, finite-capacity scheduling, optimisation, simulation and
analysis.

## 1. Guiding decisions

| Decision | Choice | Why |
|---|---|---|
| Deployment shape | **Modular monolith** + separate worker processes | One codebase, clear module boundaries, services can be extracted when there is a technical reason (not on day one). |
| Planning engine | Separate Python package `monxuplan_engine`, no import of the platform | The engine is a pure function `Problem → Solution` behind a JSON contract. It can run in-process, in a worker, or as its own HTTP service (`python -m monxuplan_engine.service`). |
| Solver | `OptimizationProvider` abstraction: Heuristic, CP-SAT (OR-Tools), Hybrid (heuristic + CP-SAT LNS), MIP (aggregate planning) | No lock-in to one solver; Gurobi/CPLEX providers can be added behind the same interface. |
| Feasibility authority | **Backend only.** Every schedule — solver output or manual edit — goes through `PlanValidator` | The frontend never decides that a plan is feasible. |
| Database | PostgreSQL (SQLite supported for tests / local dev) via SQLAlchemy 2 + Alembic | Relational integrity, transactions, JSONB for flexible payloads. |
| Jobs | DB-backed job table (`planning_run`, `import_job`) claimed with `FOR UPDATE SKIP LOCKED` by worker processes | Survives restarts, no extra broker required; Redis is optional. |
| Real time | Event bus (in-process, or Redis pub/sub when `MONXU_REDIS_URL` is set) → Server-Sent Events | Plan progress, plan published, machine down, alerts. |
| Tenancy | `tenant_id` on every business row, enforced by the repository layer; per-request tenant context | SaaS-ready isolation. |
| Frontend | Next.js (App Router) + React + TypeScript + Tailwind, TanStack Query/Table/Virtual, Apache ECharts, own canvas Gantt | Dense, fast, keyboard-driven planner UI. No business logic in React. |

## 2. Component view

```
┌──────────────────────────── Frontend (Next.js, TypeScript) ─────────────────────────────┐
│ Command Center · Planning Board (canvas Gantt) · Orders · Capacity · Materials · MPS     │
│ Scenarios & comparison · Alerts · Analytics · Master Data · Import wizard · Admin        │
│ Dispatch / Supervisor / Operator views · Data Quality · Assistant                        │
└──────────────────────────────┬──────────────────────────────────────────────────────────┘
                               │ REST /api/v1 (OpenAPI) · SSE /api/v1/events/stream
┌──────────────────────────────▼──────────────────────────────────────────────────────────┐
│ API layer (FastAPI) — auth (session cookie + CSRF, bearer JWT/API key), RBAC, rate limit │
├───────────────┬───────────────┬───────────────┬───────────────┬─────────────────────────┤
│ Master Data   │ Scenario      │ Planning      │ KPI/Analytics │ Integration             │
│ service       │ service       │ service       │ service       │ service                 │
│ (CRUD, rules, │ (copy-on-write│ (problem      │ (KPIs, drill, │ (Excel/CSV/JSON import  │
│  data quality)│  overlays,    │  builder, run │  bottlenecks, │  wizard, exports,       │
│               │  locking)     │  jobs, plans, │  alerts)      │  webhooks, ERP/MES      │
│               │               │  moves, publish)              │  adapters, events)      │
├───────────────┴───────────────┴───────┬───────┴───────────────┴─────────────────────────┤
│ Audit service · Notification/Event bus · Auth service                                    │
└───────────────────────────────┬───────┴──────────────────────────────────────────────────┘
                                │ Problem JSON  ↓   ↑ Solution JSON
┌───────────────────────────────▼──────────────────────────────────────────────────────────┐
│ Planning Engine (monxuplan_engine) — pure Python, no DB, no HTTP                          │
│ contract · calendars · durations · setups · rules · BOM/MRP · material ledger · timelines │
│ schedule builder (decoder) · providers {heuristic, cpsat, hybrid, mip} · validator        │
│ feasibility · capacity · bottlenecks · KPIs · explanations · stability · repair · sim    │
└──────────────────────────────────────────────────────────────────────────────────────────┘
        ▲                                   ▲
┌───────┴──────────┐              ┌─────────┴─────────────┐        ┌───────────────────┐
│ Worker process(es)│              │ PostgreSQL             │        │ Redis (optional)  │
│ python -m          │◄────────────►│ tenants · master data  │        │ event fan-out,    │
│ monxuplan.worker   │  SKIP LOCKED │ scenarios · plans ·    │        │ rate limiting     │
└────────────────────┘              │ audit · events · jobs  │        └───────────────────┘
                                    └────────────────────────┘
```

### Module map (backend)

| Package | Responsibility |
|---|---|
| `monxuplan_engine.contract` | Pydantic models of the engine input/output (the stable contract). |
| `monxuplan_engine.calendars` | Shift patterns → working windows in UTC minutes; DST-correct via `zoneinfo`; intersections; stretch arithmetic. |
| `monxuplan_engine.compile` | Contract → compiled, index-based problem (fast structures for algorithms). Applies planning rules with trace. |
| `monxuplan_engine.builder` | Exact, calendar-aware schedule builder (serial schedule-generation scheme). Guarantees HARD constraints. Records binding constraint and evaluated alternatives for every operation. |
| `monxuplan_engine.providers` | Optimisation providers. |
| `monxuplan_engine.validator` | Independent `PlanValidator` for any schedule (solver output, manual edits, imported plans). |
| `monxuplan_engine.analysis` | KPIs, capacity load, bottlenecks, explanations, root cause, stability, feasibility, sensitivity. |
| `monxuplan_engine.mrp` | BOM explosion, low-level codes, netting, lot sizing, planned orders, pegging, MPS. |
| `monxuplan.core` | Settings, DB session, tenancy context, security, errors, logging, metrics, event bus. |
| `monxuplan.models` | SQLAlchemy ORM (data model, see DATA_MODEL.md). |
| `monxuplan.services` | Application services (transactions, audit, authorisation checks). |
| `monxuplan.api` | FastAPI routers (`/api/v1`). |
| `monxuplan.worker` | Job runner for planning runs and imports. |
| `monxuplan.seed` | Demo factory "Monxu Manufacturing". |

## 3. Key flows

### 3.1 Planning run

```
POST /api/v1/planning/run
  → PlanningRun(QUEUED) + audit        (API returns 202 + run id immediately)
worker claims run (SKIP LOCKED)
  1 Load scenario (base data + copy-on-write overlay of scenario changes)
  2 Validate master data  ── critical data errors block the run (Data Quality)
  3 Validate constraints
  4 Explode BOM / pegging of make-items
  5 Material availability (ledger of on-hand, receipts, production outputs)
  6 Generate operations (remaining work of open production orders)
  7 Feasible resource alternatives (modes) per operation
  8 Build calendars (resource ∩ labour ∩ tool, minus maintenance/downtime)
  9 Build setup matrices
 10 Identify bottlenecks (infinite-capacity load vs capacity)
 11 Initial feasible schedule (dispatching rules)
 12 Optimise (provider: CP-SAT / hybrid LNS / local search)
 13 Repair / re-time with the exact builder
 14 KPIs      15 Explanations      16 Save plan version      17 Publish result (event)
  progress of every step → planning_run.progress + event bus → SSE → UI progress panel
```

The run never blocks the API; the solver always has a time limit and returns the best solution
found together with its status (`OPTIMAL`, `FEASIBLE` + gap, `HEURISTIC`, `INFEASIBLE`).

### 3.2 Manual change with impact preview

```
drag in Gantt → POST /plans/{id}/moves/preview {op, resource, start, replan_mode}
  engine: apply move on a copy → re-time according to replan mode → PlanValidator → KPIs → diff
  ← impact: orders delayed/advanced, OTIF before/after, setup Δ, overtime Δ, violations
planner confirms → POST /plans/{id}/moves → new immutable plan version (parent = previous) + audit
undo / redo = move the scenario's head pointer along the version chain
```

### 3.3 Event-driven rescheduling

```
MES/ERP/UI event (MachineDown CNC-03 8h) → event store → scenario change (downtime)
  → impact analysis: affected operations, orders, customers
  → repair run (local / regional / global) with baseline = current plan
  → comparison vs baseline (stability metrics, KPIs) → planner accepts / rejects
```

## 4. Scenarios, versions and publication

* A **scenario** is a base (the *live* scenario reads current master and transactional data) plus an
  ordered list of **scenario changes** (copy-on-write overlay: add resource, add shift, downtime,
  rush order, supplier delay, priority change…). Cloning a scenario copies only the change list →
  instantaneous regardless of factory size.
* Every planning run produces an immutable **plan version** (`PLAN-2026-09-28-V003`) storing the
  parameters, solver, status, KPIs, violations, the exact problem snapshot (gzip JSON, hash) and the
  schedule rows. Plans are never overwritten.
* Lifecycle: `DRAFT → VALIDATED → PUBLISHED → SUPERSEDED`; publishing supersedes the previous published
  plan of the plant (kept for history) and feeds dispatch lists and exports to ERP/MES. A run whose
  inputs changed while it computed leaves a `STALE` version. Publication goes through the gate described
  in PLANNING_ENGINE.md §10. Undo/redo/restore move the scenario's head pointer; version contents are
  never modified (the only in-place change is the lock flag of operations, which invalidates caches).

## 5. Concurrency

* Optimistic locking (`version` column) on all editable entities; stale writes return **409**.
* Plan edits reference the plan version they were computed on; editing a non-head version returns 409
  (`PLAN_VERSION_CONFLICT`) — nothing is ever silently overwritten.
* Scenario edit lease (`locked_by`, `lock_expires_at`) for collaborative work; viewers are unaffected.
* **Planning runs**: at most one QUEUED/RUNNING run per scenario, enforced by a partial unique index
  (no count-then-insert race); workers claim with `FOR UPDATE SKIP LOCKED`; a run re-queued after a
  missed heartbeat is only promoted by the worker that owns it now.
* **Stale results**: `data_revision` (per tenant) is incremented in the transaction of every ORM change
  to a planning input; runs compare it (and the scenario head) under a row lock before promotion.
* **Publication**: the plant row is locked (`SELECT … FOR UPDATE`) and plant/plan/scenario rows carry
  optimistic versions.
* **Webhooks**: transactional outbox; deliveries are claimed with a lease (`locked_until`, `locked_by`)
  by any API replica or worker, sent outside transactions, and only the lease holder records the result
  (at-least-once; receivers deduplicate on `X-Monxu-Delivery`).

## 6. Security

* Local users (scrypt password hashes) and OIDC bearer tokens (JWKS validation) — `AuthProvider`
  abstraction. Browser sessions use an httpOnly `SameSite=Lax` cookie plus double-submit CSRF token;
  integrations use API keys (hashed at rest) or bearer tokens.
* RBAC: roles → permissions (`plan:run`, `plan:publish`, `masterdata:write`, …), scoped per tenant and
  optionally per plant. Delegation rule: a grant (roles, plants) must be within the grantor's rights; users
  with more rights cannot be modified; API keys (tenant-wide) only by unscoped administrators.
* Plant isolation: every service resolving a plan, scenario, run, resource, order, purchase order or
  plant checks it against the caller's plants (`ctx.require_plant`), inbound events included; lookups by
  business code are limited to the caller's plants.
* OIDC: issuer and audience verified; accounts matched by subject; e-mail linking only when verified and
  enabled.
* Outbound network policy (`core/netpolicy.py`): webhook and connector destinations are resolved and
  every address checked (loopback, link-local, metadata always refused; private networks by setting);
  the connection is pinned to the checked address.
* Tenant isolation enforced in the repository layer (every query filtered by `tenant_id`).
* Secrets for connectors/webhooks encrypted at rest (Fernet-compatible AES via `MONXU_SECRET_KEY`).
* Security headers, rate limiting, input validation (Pydantic), SQLAlchemy parameter binding only.

## 7. Observability

Structured JSON logs with request id and tenant; Prometheus metrics at `/metrics`
(HTTP latency, planning runs, solver duration, queue depth; optional bearer token); `/health`
(liveness), `/readiness` (database; failure details go to the log, not to the anonymous caller); every planning run logs planning id, scenario, input hash, algorithm, parameters,
solver status, duration, objective, gap, violations and KPIs.

## 8. Scaling path

* Engine is stateless → horizontal workers.
* Large instances: dispatching heuristic + CP-SAT LNS over time windows / resource subsets;
  incremental (local/regional) rescheduling; Gantt queries by time window and resource page;
  virtualised tables and canvas Gantt in the UI.
* Extraction candidates when load requires it: Planning Engine service (already has its HTTP
  wrapper), Integration service, KPI service.
