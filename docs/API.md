# MonxuPlan — REST API

Base path `/api/v1`. The live OpenAPI 3 schema is served at `/api/openapi.json`, with interactive
documentation at `/api/docs` (Swagger UI) and `/api/redoc`. Everything the web application does goes
through this API — there is no private back door.

## Authentication

| Client | How | CSRF |
|---|---|---|
| Browser | `POST /auth/login` sets an httpOnly `mx_session` cookie (JWT, HS256) and a readable `mx_csrf` cookie | Send the `mx_csrf` value in `X-CSRF-Token` on every non-GET request |
| Script / service account | `POST /auth/token` → `{"access_token": …}`, then `Authorization: Bearer <token>` | Not needed |
| Integration | API key created in Administration → API keys: `Authorization: Bearer mxk_<prefix>_<secret>` (only a hash is stored; role-scoped, optional expiry) | Not needed |
| Single sign-on | `Authorization: Bearer <OIDC id/access token>` verified against `MONXU_OIDC_JWKS_URL`; users are matched by subject or e-mail | Not needed |

Every request runs inside the caller's tenant: rows of other tenants are invisible (404, not 403),
and plant-scoped users only see their plants. Permissions are checked per action (see the role matrix
in Administration or `GET /roles`).

```bash
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/token -H 'content-type: application/json' \
  -d '{"username":"planner","password":"Monxu-Demo-2026"}' | jq -r .access_token)
curl -s localhost:8000/api/v1/plants -H "Authorization: Bearer $TOKEN"
```

## Conventions

* JSON in and out; timestamps are ISO 8601 with offset (UTC from the server). Calendars, shifts and
  import files use the plant's local time.
* Optimistic locking: editable records carry `version`; send it back on update. A stale version →
  `409 VERSION_CONFLICT`.
* Errors have one shape, with a stable machine code and a message meant for a planner:

```json
{"error": {"code": "PLANNING_BLOCKED", "message": "Planning is blocked by 2 critical data problems…", "context": {"issues": [...]}}}
```

| Status | Typical codes |
|---|---|
| 400/422 | `VALIDATION_FAILED` (with `context.fields`), `INVALID_MOVE`, `IMPORT_BLOCKED` |
| 401 | `NOT_AUTHENTICATED`, `SESSION_EXPIRED`, `INVALID_TOKEN`, `INVALID_API_KEY` |
| 403 | `NO_PERMISSION`, `NO_PLANT_ACCESS`, `CSRF`, `FROZEN_OPERATION` |
| 404 | `NOT_FOUND`, `PLAN_NOT_FOUND` |
| 409 | `VERSION_CONFLICT`, `PLAN_VERSION_CONFLICT`, `INTEGRITY_ERROR`, `SCENARIO_LOCKED`, `PLANNING_BLOCKED` (critical data errors) |
| 429 | `RATE_LIMITED` (with `Retry-After`) |
| 500 | `INTERNAL_ERROR` with an `error_id` that appears in the server log |

* Every response carries `X-Request-ID` (send your own to correlate logs).
* Rate limits per client IP: `MONXU_RATE_LIMIT_PER_MINUTE` (default 600) and a stricter login limit.

## Planning in four calls

```bash
# 1. start an optimisation run of the live scenario (asynchronous, returns 202)
curl -s -X POST $API/planning/run -H "$AUTH" -H 'content-type: application/json' \
  -d '{"scenario_id":"<live scenario>","solver":{"provider":"hybrid","profile":"NORMAL"},"objectives":{"preset":"OTIF_FIRST"}}'
# → {"run_id": "…", "status": "QUEUED", "steps": [17 pipeline steps]}

# 2. follow progress (or subscribe to GET /events/stream, Server-Sent Events)
curl -s $API/planning/runs/<run_id> -H "$AUTH"      # status, current_step, progress[], solver_status, gap, plan_id

# 3. read the result
curl -s "$API/plans/<plan_id>" -H "$AUTH"            # KPIs, solver metadata, health, bottlenecks
curl -s "$API/plans/<plan_id>/schedule" -H "$AUTH"   # every operation with resource, times, binding constraint
curl -s "$API/plans/<plan_id>/export?format=xlsx" -H "$AUTH" -o plan.xlsx

# 4. publish it to the shop floor
curl -s -X POST $API/plans/<plan_id>/publish -H "$AUTH" -H 'content-type: application/json' -d '{"reason":"weekly plan"}'
```

A manual move is previewed first (nothing changes), then applied as a new plan version:
`POST /plans/{id}/moves/preview` → `POST /plans/{id}/moves` with the same body
(`op_id`, `resource_id`, `start`, `replan`: `NO_REPLAN | THIS_ORDER | DOWNSTREAM | RESOURCE | AREA | SCENARIO`).
Undo / redo: `POST /scenarios/{id}/undo|redo`.

The planning engine itself can also be called without the platform (`POST /solve` of
`python -m monxuplan_engine.service`, or the Python function `monxuplan_engine.solve(problem)`), with the
input/output contract documented in [PLANNING_ENGINE.md](PLANNING_ENGINE.md) (`GET /planning/contract`
returns its JSON schema).

## Inbound events (MES / ERP / IoT)

`POST /events` with `{"type", "payload", "occurred_at"?, "correlation_id"?, "source"?}`. Types:
`OrderCreated, OrderUpdated, MachineDown, MachineAvailable, MaterialReceived, MaterialDelayed,
OperationStarted, OperationFinished, QuantityProduced, Scrap, InventoryChanged, MaintenanceCreated`.
Events are stored first (idempotent per `correlation_id`), then applied to operational data — never to a
plan directly. A plant can opt into automatic rescheduling for chosen event types
(`PUT /plants/{id}/settings`, `auto_reschedule`).

```bash
curl -s -X POST $API/events -H "$AUTH" -H 'content-type: application/json' \
  -d '{"type":"MachineDown","payload":{"resource":"CNC-03","reason":"spindle alarm","expected_end":"2026-09-29T14:00:00+02:00"},"correlation_id":"mes-4711"}'
```

## Outbound webhooks

Subscriptions (`/webhooks`) receive `POST` JSON for `plan.published, plan.created,
planning.run.finished, alert.created, order.updated, machine.down, machine.available,
material.delayed, import.completed`. Headers: `X-Monxu-Event`, `X-Monxu-Delivery`, `X-Monxu-Timestamp`,
`X-Monxu-Signature: sha256=HMAC(secret, timestamp + "." + body)`. Failed deliveries are retried with
back-off (5 attempts) and listed under `/webhooks/{id}/deliveries`.

## Imports

`POST /imports` (multipart: `entity`, `file`, `plant_id`) → mapping suggestion →
`PUT /imports/{id}/mapping` → `POST /imports/{id}/validate` → `POST /imports/{id}/commit`.
Entities: items, resources, calendars, customers, suppliers, boms, routings, production-orders,
inventory, purchase-orders, demand, downtimes. Templates: `GET /imports/templates/{entity}?format=xlsx|csv`.
A file with errors cannot be committed unless `skip_invalid_rows` is chosen explicitly; commits are a
single transaction; imports never delete data.

## GraphQL

Not provided in this version (REST + OpenAPI cover every use case of the UI). *Coming soon.*

## Endpoints

### health

| Method | Path |
|---|---|
| GET | `/health` |
| GET | `/readiness` |
| GET | `/metrics` |

### auth

| Method | Path |
|---|---|
| POST | `/api/v1/auth/login` |
| POST | `/api/v1/auth/token` |
| POST | `/api/v1/auth/logout` |
| GET | `/api/v1/auth/me` |
| POST | `/api/v1/auth/password` |

### master-data

| Method | Path |
|---|---|
| GET | `/api/v1/master-data` |
| GET | `/api/v1/master-data/{entity}` |
| POST | `/api/v1/master-data/{entity}` |
| GET | `/api/v1/master-data/{entity}/schema` |
| GET | `/api/v1/master-data/{entity}/{id_}` |
| PATCH | `/api/v1/master-data/{entity}/{id_}` |
| PUT | `/api/v1/master-data/{entity}/{id_}` |
| DELETE | `/api/v1/master-data/{entity}/{id_}` |
| GET | `/api/v1/orders` |
| POST | `/api/v1/orders` |
| PUT | `/api/v1/orders/{id_}` |
| GET | `/api/v1/resources` |
| GET | `/api/v1/items/{id_}/bom-tree` |

### scenarios

| Method | Path |
|---|---|
| GET | `/api/v1/scenarios` |
| POST | `/api/v1/scenarios` |
| GET | `/api/v1/scenarios/{scenario_id}` |
| PATCH | `/api/v1/scenarios/{scenario_id}` |
| POST | `/api/v1/scenarios/{scenario_id}/clone` |
| POST | `/api/v1/scenarios/{scenario_id}/changes` |
| DELETE | `/api/v1/scenarios/{scenario_id}/changes/{change_id}` |
| POST | `/api/v1/scenarios/{scenario_id}/lock` |
| POST | `/api/v1/scenarios/{scenario_id}/unlock` |
| POST | `/api/v1/scenarios/{scenario_id}/archive` |
| GET | `/api/v1/scenarios/{scenario_id}/plans` |
| POST | `/api/v1/scenarios/{scenario_id}/undo` |
| POST | `/api/v1/scenarios/{scenario_id}/redo` |
| POST | `/api/v1/scenarios/{scenario_id}/reschedule` |
| POST | `/api/v1/scenarios/what-if` |

### planning

| Method | Path |
|---|---|
| POST | `/api/v1/planning/run` |
| GET | `/api/v1/planning/runs/{run_id}` |
| GET | `/api/v1/planning/runs` |
| POST | `/api/v1/planning/runs/{run_id}/cancel` |
| POST | `/api/v1/planning/feasibility` |
| GET | `/api/v1/planning/contract` |
| GET | `/api/v1/planning/presets` |
| GET | `/api/v1/planning/providers` |
| POST | `/api/v1/planning/mrp` |
| POST | `/api/v1/planning/mrp/firm` |
| POST | `/api/v1/planning/aggregate` |

### plans

| Method | Path |
|---|---|
| GET | `/api/v1/plans/{plan_id}` |
| GET | `/api/v1/plans/{plan_id}/gantt` |
| GET | `/api/v1/plans/{plan_id}/schedule` |
| GET | `/api/v1/plans/{plan_id}/violations` |
| GET | `/api/v1/plans/{plan_id}/unscheduled` |
| GET | `/api/v1/plans/{plan_id}/orders` |
| GET | `/api/v1/plans/{plan_id}/operations/{op_key}/explore` |
| POST | `/api/v1/plans/{plan_id}/operations/{op_key}/check` |
| GET | `/api/v1/plans/{plan_id}/operations/{op_key}` |
| GET | `/api/v1/plans/{plan_id}/order-detail/{order_key}` |
| GET | `/api/v1/plans/{plan_id}/order-chain/{order_key}` |
| POST | `/api/v1/plans/{plan_id}/moves/preview` |
| POST | `/api/v1/plans/{plan_id}/moves` |
| POST | `/api/v1/plans/{plan_id}/locks` |
| POST | `/api/v1/plans/{plan_id}/lock-sequence` |
| POST | `/api/v1/plans/{plan_id}/validate` |
| POST | `/api/v1/plans/{plan_id}/publish` |
| POST | `/api/v1/plans/{plan_id}/restore` |
| GET | `/api/v1/plans/{plan_id}/export` |
| POST | `/api/v1/plans/{plan_id}/simulate` |
| POST | `/api/v1/plans/{plan_id}/sensitivity` |
| GET | `/api/v1/plans/{plan_id}/diff/{other_id}` |

### analytics

| Method | Path |
|---|---|
| GET | `/api/v1/kpis` |
| GET | `/api/v1/kpis/{code}/drilldown` |
| GET | `/api/v1/bottlenecks` |
| GET | `/api/v1/capacity/load` |
| GET | `/api/v1/analytics/compare` |
| GET | `/api/v1/analytics/trends` |
| GET | `/api/v1/analytics/plan-vs-actual` |

### materials

| Method | Path |
|---|---|
| GET | `/api/v1/materials/availability` |
| GET | `/api/v1/materials/{material_id}/projection` |
| GET | `/api/v1/materials/{material_id}/impact` |
| GET | `/api/v1/pegging/order/{order_id}` |
| GET | `/api/v1/receipts` |
| GET | `/api/v1/mps` |

### shop-floor

| Method | Path |
|---|---|
| GET | `/api/v1/dispatch` |
| GET | `/api/v1/supervisor` |
| GET | `/api/v1/operator` |
| POST | `/api/v1/execution/report` |

### integrations

| Method | Path |
|---|---|
| GET | `/api/v1/imports/templates` |
| GET | `/api/v1/imports/templates/{entity}` |
| POST | `/api/v1/imports` |
| GET | `/api/v1/imports` |
| GET | `/api/v1/imports/{job_id}` |
| PUT | `/api/v1/imports/{job_id}/mapping` |
| POST | `/api/v1/imports/{job_id}/validate` |
| POST | `/api/v1/imports/{job_id}/commit` |
| GET | `/api/v1/exports/{entity}` |
| POST | `/api/v1/events` |
| GET | `/api/v1/events` |
| GET | `/api/v1/events/types` |
| GET | `/api/v1/webhooks` |
| POST | `/api/v1/webhooks` |
| DELETE | `/api/v1/webhooks/{webhook_id}` |
| GET | `/api/v1/webhooks/{webhook_id}/deliveries` |
| GET | `/api/v1/connectors` |
| POST | `/api/v1/connectors` |

### admin

| Method | Path |
|---|---|
| GET | `/api/v1/plants` |
| GET | `/api/v1/dashboard` |
| GET | `/api/v1/alerts` |
| POST | `/api/v1/alerts/{alert_id}/acknowledge` |
| GET | `/api/v1/data-quality` |
| POST | `/api/v1/assistant/ask` |
| GET | `/api/v1/assistant/status` |
| GET | `/api/v1/roles` |
| GET | `/api/v1/users` |
| POST | `/api/v1/users` |
| PATCH | `/api/v1/users/{user_id}` |
| POST | `/api/v1/users/{user_id}/password` |
| GET | `/api/v1/api-keys` |
| POST | `/api/v1/api-keys` |
| DELETE | `/api/v1/api-keys/{key_id}` |
| GET | `/api/v1/audit` |
| GET | `/api/v1/views` |
| POST | `/api/v1/views` |
| DELETE | `/api/v1/views/{view_id}` |
| GET | `/api/v1/plants/{plant_id}/settings` |
| PUT | `/api/v1/plants/{plant_id}/settings` |
| POST | `/api/v1/demo/reset` |

### events

| Method | Path |
|---|---|
| GET | `/api/v1/events/stream` |
