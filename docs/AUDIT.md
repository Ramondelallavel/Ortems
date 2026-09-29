# MonxuPlan — Audit and risk matrix

Audit of the code (the code, not the documentation, is the source of truth). Each finding: location,
problem, root cause, impact, reproduction, fix, protecting test. Status is updated as findings are fixed;
"Open" findings are listed in *Remaining risks* at the end.

Severity: **P0** data / security / industrial-integrity / wrong publication · **P1** incorrect behaviour or
serious production risk · **P2** maintainability, performance, relevant UX · **P3** desirable.

## P0

| # | Area | Location | Problem · root cause | Impact · reproduction | Fix | Test | Status |
|---|---|---|---|---|---|---|---|
| P0-1 | Publish gate | `services/planning.publish` | Any version could be published (not only the scenario head); `UNSCHEDULED` excluded from the hard count; critical data issues ignored; `force` without reason when there were no hard violations; no final validation; setting `publish_requires_validation` read nowhere. Root cause: the gate counted stored violations only. | An old or incomplete plan (operations missing) becomes the plant's live plan, silently. Repro: publish a superseded version; publish a plan with unscheduled operations. | Formal gate (see PLANNING_ENGINE.md §Publication): head only, plant policy, independent validator re-run on the stored schedule of the plan's own snapshot, unscheduled and critical data issues as explicit blockers, `force` only with reason and permission, recorded overrides, plant row locked. | `test_publish_gate.py` | Fixed |
| P0-2 | Plant isolation | `services/events_in` (all event types, `report_execution`) | No `require_plant` on the resource / order / PO / plant an event touches. | A MES identity (or user) scoped to plant A reports downtime, production, inventory or maintenance on plant B. | Plant check at every entity resolution; lookups by code resolved inside the caller's plants. | `test_security_scope.py` | Fixed |
| P0-3 | RBAC escalation | `routers/admin._check_roles/_set_roles`, `change_password`, `services/auth.create_api_key` | Only `SUPER_ADMIN` grant was guarded. A plant-scoped admin could create/modify users with any role and **all-plant** scope, reset the password of a higher admin, and create API keys (tenant-wide, any role). | Privilege escalation / account takeover inside a tenant. | Delegation rule: a grant (role permissions, plant scope) must be a subset of the grantor's; users with more privileges than the actor cannot be modified; API keys only by unscoped admins, role ⊆ own permissions. | `test_security_scope.py` | Fixed |
| P0-4 | OIDC account linking | `services/auth.user_from_oidc` | Matched `external_subject == sub OR email == email` across all tenants, no `email_verified`, no issuer binding; accepted tokens without audience when none configured. | Account takeover by anyone able to obtain an IdP token carrying the victim's e-mail. | Match by subject only; link by e-mail only if `email_verified` is true, the account has no subject yet, and linking is enabled (`MONXU_OIDC_LINK_BY_EMAIL`); refuse OIDC without an audience. | `test_security_scope.py::test_oidc_*` | Fixed |
| P0-5 | Duplicate planning runs | `services/planning.enqueue_run` | "No other QUEUED/RUNNING run" was a count-then-insert. | Two concurrent requests queue two runs for one scenario; both promote results. | Partial unique index on `planning_run(scenario_id)` for active statuses (migration 0004) + IntegrityError → 409. | `test_concurrency.py::test_one_active_run` | Fixed |
| P0-6 | Stale planning results | `services/planning._execute_run`, `persist_solution` | A long run always moved the scenario head to its result, even if the head or the data changed meanwhile (or failed with a generic engine error through optimistic locking). | A newer manual edit or newer master data silently overwritten, or a lost run with a misleading error. | Inputs recorded at start (head plan, tenant data revision); at promotion the scenario row is locked and compared; a stale result is stored as a non-head version with status `STALE` and the run says why. Data revision bumped on every ORM write of planning inputs. | `test_concurrency.py::test_stale_*` | Fixed |

## P1

| # | Area | Location | Problem · root cause | Impact | Fix | Test | Status |
|---|---|---|---|---|---|---|---|
| P1-1 | Worker ownership | `worker.requeue_stale`, `_execute_run` | A run re-queued after a missed heartbeat could be finished by two workers; the first one still persisted. | Duplicate versions. | Result promoted only if the run still belongs to this worker (`worker` column checked under lock). | `test_concurrency.py` | Fixed |
| P1-2 | Webhook delivery | `services/webhooks.deliver_pending` | No claim: every API replica's thread selected the same PENDING rows; HTTP calls inside an open transaction; back-off from creation time. | Duplicate deliveries, long locks. | Outbox with lease (`locked_until`, `locked_by`, `next_attempt_at`), atomic claim (`FOR UPDATE SKIP LOCKED` / conditional UPDATE), HTTP outside the transaction, exponential back-off from the last attempt, final state `FAILED`. Migration 0004. | `test_webhooks.py` | Fixed |
| P1-3 | SSRF | webhooks, database connectors | No policy for private, loopback, link-local or metadata addresses; DNS resolved at send time without re-check. | Requests to internal services / cloud metadata. | `core/netpolicy.py`: resolve and check every address (IPv4/IPv6), deny private/loopback/link-local/multicast/reserved and metadata unless allow-listed (`MONXU_EGRESS_ALLOW`), HTTPS required in production, redirects not followed, connection pinned to the checked address for webhooks. | `test_netpolicy.py` | Fixed |
| P1-4 | Connector SQL guard | `services/dbconnect._clean_sql` | `SELECT … INTO` (creates tables), `INTO OUTFILE`, `pg_read_file`, `pg_sleep`, `xp_cmdshell`, `utl_http`, `dblink`, `openrowset` allowed. | Writes / file reads / SSRF from the DB server. | Forbidden list extended (statements and dangerous functions); read-only transaction failure is no longer swallowed on PostgreSQL/MySQL. | `test_dbconnect.py` | Fixed |
| P1-5 | Import/sync plant default | `imports._plant`, `dbconnect.sync` | Without a plant, the *first plant by code* was used; `sync` did not check plant scope. | Data written into the wrong plant. | Plant required when the tenant has more than one plant; `require_plant` on sync. | `test_security_scope.py` | Fixed |
| P1-6 | Auto-reschedule on event | `events_in._maybe_auto_reschedule` | Exceptions swallowed without a savepoint: a failed reschedule could leave half-written rows committed with the event. | Partial plan versions. | Savepoint around the reschedule; failure rolled back and recorded. | `test_security_scope.py` (event path) | Fixed |
| P1-7 | Material quantities | `monxuplan_engine/materials.py`, `validator.py` | Ledger levels summed in floats compared with an absolute `EPS = 1e-9` (5 copies). At large magnitudes accumulated error exceeds EPS. | False shortages / false availability. | Ledger in exact integer micro-units (`quantities.py`, one policy); no EPS in material feasibility. | `test_quantities.py`, property tests | Fixed |
| P1-8 | Locks lost on replan | `repair._baseline_overrides` | Locked operations placed as `KEPT` (override wins over the lock). | Locks silently dropped by any move/repair. | Fixed operations placed by their own fixing. | `test_plan_store.py::test_move_session_matches_a_fresh_load` | Fixed (previous commit) |
| P1-9 | Feasibility semantics | `monxuplan_engine/feasibility.py` | Returned `YES` for "no lower bound violated". | Read as "the detailed schedule is feasible". | Explicit `check: LOWER_BOUND`, `detailed_schedule_feasible: null`, status `NO_BOUND_VIOLATED` instead of `YES`. | `test_optimization.py` | Fixed (breaking) |
| P1-10 | Readiness leak | `api/app.readiness` | Database exception text returned to anonymous callers. | Host / user names disclosed. | Generic body; detail logged. | `test_api.py` | Fixed |

## P2

| # | Area | Location | Problem | Fix | Status |
|---|---|---|---|---|---|
| P2-1 | Secondary reservation when a setup shrinks | `builder._commit` | A later insertion that shortens the next job's setup left the secondary reservation of the removed setup time. Conservative (phantom load), never infeasible. | Release the no-longer-needed pieces. | Fixed |
| P2-2 | Objective naming | `objectives.py` | `inventory` is finished-goods earliness (quantity × hours before target), `wip` is order flow time (minutes). | Documented exactly (OPTIMIZATION.md) and in the objective catalogue. | Fixed (doc) |
| P2-3 | Excel decompression | `imports.parse_file` | No limit on the uncompressed size of an .xlsx. | Uncompressed-size and ratio limit before openpyxl. | Fixed |
| P2-4 | Metrics | `/metrics` | Public. Labels are route templates (no tenant data). | Optional bearer token `MONXU_METRICS_TOKEN`. | Fixed |
| P2-5 | CP-SAT `OPTIMAL` | `providers/cpsat.py` | `OPTIMAL` requires model optimality, exact decode and exact overtime — sound. Coefficients are rounded (×100 000): optimality is with respect to that model. | Documented. | Doc |

## Remaining risks

See the end of this file after the fix phases (kept current).
