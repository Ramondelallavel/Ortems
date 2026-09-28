# MonxuPlan — Data Model

PostgreSQL is the primary database (SQLite is accepted for tests and local development). The ORM
lives in `backend/monxuplan/models/`; migrations in `backend/alembic/`.

## Conventions

* Primary keys: `UUID`. Business codes (`code`, `number`) are unique **per tenant**.
* Every business table has `tenant_id` (FK `tenant`, indexed, part of unique constraints),
  `created_at`, `updated_at`, `created_by`, `updated_by` and `version` (optimistic locking — a stale
  update fails with 409).
* Timestamps are stored in UTC (`timestamptz`); plants carry an IANA `timezone` used for calendars
  and display.
* Quantities are `numeric(18,6)`; the item's `quantity_type` (INTEGER / DECIMAL) and lot rules govern
  rounding. Durations are minutes (`numeric`).
* Planning history is never deleted: plans, schedules, actuals, scenarios and audit are append-only;
  master data uses `is_active` instead of hard deletes when referenced.
* Flexible, type-specific payloads (item attributes, rule conditions, scenario changes, KPI details)
  are JSON columns (`JSONB` on PostgreSQL).

## Entity overview

```
Tenant ─┬─ Company ─┬─ Site ── Plant ─┬─ PlanningArea ── WorkCenter ── Resource (machine…)
        │           │                 ├─ Calendar ─┬─ CalendarShift (weekly pattern, overtime)
        │           │                 │            └─ CalendarException (holiday, closure, extra work)
        │           │                 └─ Scenario ─┬─ ScenarioChange (copy-on-write overlay)
        │           │                              ├─ PlanningRun (job, progress, solver log)
        │           │                              └─ Plan (version) ─┬─ ScheduledOperation
        │           │                                                 ├─ ConstraintViolation
        │           │                                                 └─ KPIValue
        ├─ User ── UserRole ── Role ── RolePermission ── Permission
        ├─ Item (product / material) ── ProductFamily, UnitOfMeasure, ItemPlant (sourcing)
        │    ├─ BOM ── BOMLine (component, qty, scrap, consumed at operation)
        │    └─ Routing ── RoutingOperation ─┬─ OperationResource (primary / alternative / secondary)
        │                                    └─ OperationPrecedence (FS/SS/FF/SF, lag)
        ├─ Resource ─┬─ ResourceGroup / ResourceGroupMember
        │            ├─ Maintenance, Downtime
        │            └─ ToolCompatibility
        ├─ LaborPool ── Operator ── OperatorSkill ── Skill ; OperatorAbsence
        ├─ SetupMatrix ── SetupMatrixEntry ; SetupRule
        ├─ PlanningRule (rule engine) ; ConstraintDefinition ; OptimizationProfile
        ├─ Customer ; Supplier ; SalesOrder ── SalesOrderLine ; Demand (forecast…)
        ├─ PurchaseOrder ── PurchaseOrderLine
        ├─ ProductionOrder ── ProductionOrderOperation ── ActualProduction
        ├─ Inventory ; MaterialLot ; InventoryTransaction ; MaterialReservation
        ├─ TransferLane (plant ↔ plant / warehouse lead times)
        ├─ Event (industrial event store) ; Alert
        ├─ Integration ; ImportJob ; ExportJob ; WebhookSubscription ; WebhookDelivery ; ApiKey
        └─ AuditLog ; SavedView
```

## Main tables

| Table | Key columns |
|---|---|
| `tenant` | name, slug (unique), settings |
| `company` | name, currency, locale |
| `site` / `plant` | code, name, timezone, country, default calendar |
| `planning_area` | plant, code, name (Cutting, Machining, …) |
| `work_center` | area, code, name, calendar |
| `calendar` | code, name, timezone, parent calendar (inheritance) |
| `calendar_shift` | calendar, weekday, shift code, start/end local time, kind REGULAR/OVERTIME, breaks |
| `calendar_exception` | calendar, start/end, kind HOLIDAY/CLOSURE/EXTRA_WORK/OVERTIME, reason |
| `resource` | plant, area, work center, code, name, kind (MACHINE, HUMAN, TOOL, LABOR_POOL, WORK_CENTER, SUBCONTRACTOR, STORAGE, TRANSPORT), capacity, calendar, efficiency, speed, costs (hour, overtime, setup), energy kW, CO₂ factor, attributes, finite flag, setup state |
| `resource_group` / `resource_group_member` | e.g. FRESADO → CNC-01..04 |
| `maintenance` | resource, start, end, kind PREVENTIVE/CORRECTIVE/PLANNED/UNPLANNED, status |
| `downtime` | resource, start, end, reason, source (MES/manual) |
| `labor_pool` | code, name, resource (capacity profile derived from members), machines per operator |
| `operator` | code, name, pool, calendar, cost; `operator_skill` (skill, level, certified until); `operator_absence` |
| `skill` | code, name |
| `tool` | resource (kind TOOL, capacity = copies), tool family; `tool_compatibility` (tool ↔ machine) |
| `product_family` | code, name, attributes (colour group…) |
| `unit_of_measure` / `uom_conversion` | code, dimension; factor |
| `item` | code, name, type FINISHED/SEMI_FINISHED/RAW/PACKAGING, make_or_buy, uom, quantity_type, family, attributes, lot policy (min, max, multiple, fixed, economic), safety stock, purchase lead time, unit cost, price |
| `item_plant` | item, plant, sourcing MAKE/BUY/TRANSFER/SUBCONTRACT, priority |
| `bom` / `bom_line` | item, version, validity; component, qty per, scrap %, consumed at operation seq |
| `routing` / `routing_operation` | item, version; seq, code, name, setup, run per unit, run tiers, fixed, batch size/time, teardown, queue, move, wait, overlap %, transfer batch, split rules, interruptible, labour pool + units, tool + units, buffers |
| `operation_resource` | routing operation, resource or group, role PRIMARY/ALTERNATIVE/SECONDARY/SUBCONTRACT, preference, speed factor, subcontract lead time and cost |
| `operation_precedence` | routing, pred seq, succ seq, type FS/SS/FF/SF, lag |
| `setup_matrix` / `setup_matrix_entry` | scope (resource / group / global), attribute, same/default minutes; from, to, minutes, cost |
| `setup_rule` | resource/group scope, condition (from/to attributes), extra minutes (cleaning) |
| `planning_rule` | code, name, condition JSON, actions JSON, priority, active |
| `optimization_profile` | preset, objective mode, weights, levels, solver, time limits, strategies |
| `customer` / `supplier` | priority, strategic flag / lead time, reliability |
| `sales_order` / `sales_order_line` | customer, item, qty, requested, promised, due dates, priority, revenue |
| `demand` | item, plant, period, qty, type FORECAST/CUSTOMER_ORDER/FIRM/EXPECTED/PROMOTIONAL/SAFETY_STOCK |
| `production_order` | number, item, qty, status, release/due/requested/promised/need dates, priorities, expedite, customer, sales order line, routing, BOM, material/planning status, source |
| `production_order_operation` | order, seq, routing operation snapshot (times, resources), status, completed/scrap qty, actual start/end, locked resource |
| `purchase_order` / `purchase_order_line` | supplier; item, qty, expected date, confirmed, received qty |
| `inventory` | item, plant, location, on hand, reserved, blocked, quality hold |
| `material_lot` / `inventory_transaction` / `material_reservation` | lot, qty, status; movements; reservations to order operations |
| `transfer_lane` | from plant/location, to plant/location, lead time |
| `scenario` | plant, name, parent, is_live, status, owner, config (horizon, frozen, objectives, strategies), head plan, baseline plan, lock |
| `scenario_change` | scenario, seq, type (ADD_RESOURCE, DOWNTIME, ADD_SHIFT, RUSH_ORDER, MATERIAL_DELAY, …), payload |
| `planning_run` | scenario, status, mode, params, progress steps, solver, status, objective, bound, gap, input hash, duration, error, log |
| `plan` | scenario, run, number (`PLAN-YYYY-MM-DD-Vnnn`), version, parent, kind, status DRAFT/VALIDATED/PUBLISHED/SUPERSEDED, KPIs, problem snapshot (gzip) + hash, solver metadata, published by/at |
| `scheduled_operation` | plan, order, order operation, op key, resource, secondary allocations, setup start, start, end, setup/run minutes, qty, flags (frozen, locked, late), binding constraint, explanation |
| `constraint_violation` | plan, severity, type, order, operation, resource, message, details |
| `kpi_value` | plan, code, value, details |
| `actual_production` | order operation, resource, start, end, good qty, scrap qty, operator, source |
| `event` | type, payload, occurred at, source, correlation id, processed |
| `alert` | type, severity, title, message, context refs, plan, status, acknowledged by |
| `integration` / `import_job` / `export_job` | connector config (secrets encrypted) / file, mapping, status, stats, errors |
| `webhook_subscription` / `webhook_delivery` | url, events, encrypted secret / attempts, status |
| `api_key` | name, prefix, hash, role, last used |
| `audit_log` | who, when, entity, action, before, after, reason, request id, IP |
| `saved_view` | user, page, name, filters, columns, shared flag |

## Indexing highlights

* `(tenant_id, code)` unique on master data; `(tenant_id, number)` unique on orders and plans.
* `scheduled_operation (plan_id, resource_id, start)` for Gantt window queries;
  `(plan_id, order_id)` for order views.
* `production_order (tenant_id, plant_id, status, due_date)`; `planning_run (status, created_at)` for
  the job queue; `audit_log (tenant_id, entity_type, entity_id, at)`.
