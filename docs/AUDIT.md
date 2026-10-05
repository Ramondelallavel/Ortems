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
| P1-11 | Validator independence (materials) | `validator._validate_materials` | Re-used the builder's ledger class (a ledger bug would be invisible). | Own exact balance and FIFO coverage. | `test_properties.py`, `test_industrial_cases.py` | Fixed |
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

## Found during the fix phases

| # | Sev. | Location | Problem · root cause | Fix | Test | Status |
|---|---|---|---|---|---|---|
| F-1 | P1 | `frontend/components/gantt/Gantt.tsx` (drop) | A drag sent *operation start* (left edge + setup) while the API reads the *setup start*: every move landed one setup duration later than dropped. | Send the bar's left edge; API field documented. | manual + preview shows "earliest valid position" only when snapped | Fixed |
| F-2 | P1 | `frontend/app/(app)/planning/page.tsx` (publish) | The UI set `force: true` automatically whenever there were violations or unscheduled operations: overriding was the normal path. | Publish without force; on `PUBLISH_BLOCKED` show the server's blockers and ask for an explicit override with reason. | typecheck; gate tests server-side | Fixed |
| F-3 | P1 | `routers/planning.get_run/list_runs`, `planning.cancel_run` | No plant check: runs of another plant could be read or cancelled by id. | `get_scenario` (plant + private scenario) on each. | `test_security_scope.py::test_runs_of_another_plant_are_not_visible` | Fixed |
| F-4 | P1 | `frontend/components/planning/MovePreview.tsx` | Older preview responses could overwrite newer ones (no abort). | AbortController per request. | typecheck | Fixed |
| F-5 | P2 | webhooks | All threads of a process shared one lease identity. | One identity per delivery round. | `test_webhooks.py` | Fixed |
| F-6 | P2 | `mrp.py`, `providers/mip.py` | Bucketed plans not labelled as such. | `planning_level`, `capacity_model`, note. | — | Fixed |
| F-7 | P1 | `builder._commit` | The cost/overtime of a placement was not recomputed when a later insertion changed its setup (previous commit). | `_set_cost`. | `test_plan_store.py::test_incremental_storage_matches_a_full_write` | Fixed |
| F-8 | P1 | `services/masterdata.py` | Master-data rows of another plant could be listed, read, changed and deleted by id (plant scope was only checked on create). | `_in_scope` on list/get/update/delete (404 outside scope), plant re-checked after applying fields. | `test_security_scope.py::test_master_data_rows_of_another_plant_are_out_of_reach` | Fixed |
| F-9 | P1 | `services/orders.py`, `services/views.py` | The order book listed orders of every plant of the tenant; the operator view accepted a resource of another plant. | Plant scoping of the list and of the plan; resource must belong to the plant. | `…::test_order_book_and_operator_view_stay_in_the_users_plants` | Fixed |
| F-10 | P1 | `services/alerts.acknowledge` | Alerts of another plant could be acknowledged by id. | 404 outside the caller's plants. | `…::test_alerts_of_another_plant_cannot_be_acknowledged` | Fixed |
| F-11 | P1 | `routers/masterdata /resources` | Operators and supervisors got 403 on their own screens (the resource list needed master-data read). | Plan readers get identity fields of their plant's resources. | `…::test_shop_floor_roles_list_their_plant_resources` | Fixed |
| F-12 | P1 | `services/planning.enqueue_run`, `routers/scenarios` what-if | Forcing past critical data problems needed no reason; what-ifs always forced. | Blockers always computed; `force` needs `force_reason`, stored with the overridden issues and audited (`PLANNING_RUN_FORCED`); what-ifs report `run_blocked` instead of forcing. | `test_publish_gate.py::test_planning_past_critical_data_problems_needs_a_reason_and_is_audited` | Fixed |
| F-13 | P1 | `providers/cpsat.py` (warm start) | With a full-model warm start the heuristic's local search was skipped: CP-SAT and hybrid could return a worse plan than the heuristic. | Local search kept on the warm start. | `engine/test_optimization.py::test_exact_providers_never_return_a_worse_plan_than_the_heuristic` (failed before the fix) | Fixed |
| F-14 | P1 | `frontend/components/gantt/Gantt.tsx` | Drag-and-drop never opened the move preview (the drag state was read from a stale closure). | Drag state in a ref. | `tests/e2e/flows.py` (drag → preview → apply) | Fixed |
| F-15 | P2 | `services/auth.change_password` | A wrong current password answered 401: the interface signed the user out. | `422 INVALID_CURRENT_PASSWORD`. | `test_account.py::test_wrong_current_password_is_not_a_session_error` | Fixed |
| F-16 | P2 | sessions | Changing a password, deactivating a user or "sign out everywhere" did not end existing sessions and tokens. | `user.tokens_valid_after` (migration `0005`) checked on every request. | `test_account.py` | Fixed |
| F-17 | P2 | `services/scenarios` what-if | Missing or foreign-plant parameters answered 500. | Validated per what-if kind, plant-scoped resources (422/404). | `…::test_what_if_inputs_are_validated_and_plant_scoped` | Fixed |
| F-18 | P2 | `services/dataquality.py` | Invalid-routing examples only carried an internal id, hidden by the interface: the routing could not be identified. | Product code and routing version in each example. | `…::test_invalid_routing_examples_name_the_routing` | Fixed |
| F-19 | P2 | `monxuplan_engine/kpis.py` | Delay causes printed raw UTC ISO timestamps and "(start)"-style limits. | Local time and a named limiting factor; template + values for translation. | e2e language audit | Fixed |
| F-20 | P2 | frontend | Several controls did nothing or misled: Find did not open the operation panel, Publish did not refresh the header, the run panel showed the previous run, dialogs stacked Escape/focus wrongly, unsaved master-data edits were lost on navigation, the audit filter offered actions that never occur, the sensitivity text described experiments the engine does not run, surrogate glyphs rendered as "�". | Fixed individually. | `tests/e2e/flows.py`, `tests/e2e/crawl.py` | Fixed |
| F-22 | P2 | `services/engine_view.replay` | Rebuilding a stored plan marked every operation as merely "kept", losing that frozen, in-progress or locked work was fixed: views computed from the replay (capacity requirement) placed that work elsewhere than the views stored with the plan. | The stored `is_fixed`/`fixed_reason` are kept in the replay. | `test_plan_store.py::test_read_models_match_the_engine_replay` (was failing) | Fixed |
| F-23 | P2 | Operator screen, operation panel | After choosing another machine (or operation) the previous one's job stayed on screen until the new answer arrived, with its Start/Report/Lock buttons active. | Only the answer for the selected machine/operation is shown; otherwise a loading state. | `tests/e2e/flows.py` (operator; the flow now waits for each machine's own answer) | Fixed |
| F-21 | P3 | CLIs | `python -m monxuplan_engine` printed tracebacks for bad input; `seed.acceptance --help` created a directory. | Argument validation, clean errors (exit 2), argparse. | manual runs | Fixed |

## Remaining risks (not fixed in this pass, stated plainly)

* **Manual moves on very large plans** take tens of seconds (OPTIMIZATION.md): the whole version is
  re-validated and stored. Incremental validation and versions stored as differences are not implemented.
* **Input revision counts ORM writes.** Bulk Core writes to input tables (today only the scale seeder)
  do not bump it; any future bulk import path must call `models.revision.bump`.
* **Connector SQL guard is lexical** (defence in depth on top of read-only transactions where the
  database supports them — PostgreSQL, MySQL, Oracle). SQL Server has no read-only transaction here:
  connectors must use a read-only database account (documented).
* **DNS pinning for connectors** replaces the host by the checked address except for PostgreSQL
  (`hostaddr`); drivers verifying TLS certificates by host name against an IP may need an allow-listed
  host name.
* **Validator independence** is at the placement level: builder and validator share the compiled
  problem (calendars, modes). The property-test oracle covers the output level, not compilation.
* **CP-SAT `OPTIMAL`** is optimality of the model with objective coefficients rounded to 10⁻⁵ of the
  normalised weights, exactly re-timed; it is not a proof for the unrounded weighted objective.
* **Accessibility** of the new override dialog follows the existing dialog component; no screen-reader
  audit was run in this pass.
* **Engine explanations in English.** "Why here?", the constraint explorer, the order chain and the
  free-text part of unscheduled reasons (evaluated alternatives) are generated as English text by the
  schedule builder; the interface states it in the account dialog. Localising them needs templates in
  the builder.
* **The Playwright suites** (`tests/e2e/crawl.py`, `flows.py`, `i18n_audit.py`) need a running stack
  and are not part of CI; they are run before each delivery.
