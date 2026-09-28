# MonxuPlan — Testing

| Level | Where | What it proves | Run |
|---|---|---|---|
| Engine unit & optimisation | `backend/tests/engine` (69 tests) | Calendars incl. DST, breaks and holidays; durations; setup matrices; material ledger (no stock creation); every hard constraint in the schedule builder; independent validator; known-answer optimisation cases; rescheduling; MRP; aggregate MIP; Monte Carlo | `pytest tests/engine` |
| Critical scenarios of the specification | same | §196 CNC-03 8 h breakdown → only affected operations move, explained; §197 MAT-A 100 available vs 300 needed → shortage, never "magic inventory"; §198 machine + operator + tool + material must coincide or the operation is explained as infeasible; §199 A/B setups 5/60/60/5 → campaigns (heuristic, CP-SAT and hybrid reach 105 min) | `pytest tests/engine -k "196 or 197 or 198 or 199"` |
| Fuzz | `tests/engine/test_fuzz.py` | Random problems (alternatives, labour, tools, materials, detached setups, calendars) solved by every provider: the independent validator finds **zero** hard violations | `pytest tests/engine/test_fuzz.py` |
| Platform / API | `backend/tests/platform/test_api.py` (17 tests) | Health, login/lockout, CSRF, bearer tokens, RBAC, tenant isolation (404 across tenants), optimistic locking (409), run → Gantt → explain, move preview → apply → undo → redo → publish → dispatch, Excel/CSV export, import validation blocks bad files, unknown references, export→import round trip, idempotent MES events, audit | `pytest tests/platform` |
| Acceptance (§195) | `backend/tests/platform/test_acceptance.py` | A new company imports 20 machines, 20 products, BOMs, routings, stock, POs and 100 orders (300 operations) from customer-style Excel/CSV files → data quality → plan (all 300 operations, no hard violation, no overlapping machine) → Gantt → bottleneck (GRD-1) → KPIs → explanation → scenario (bottleneck at 70 %) → replan → compare → publish | `pytest tests/platform/test_acceptance.py` |
| PostgreSQL | same suites | Migrations apply with no drift; SKIP LOCKED worker; all platform tests | `MONXU_TEST_DATABASE_URL=postgresql+psycopg://… pytest tests/platform` |
| Browser walkthrough | `tests/e2e/ui_walkthrough.py` | Logs in, runs the planner from the UI, opens every screen (EN/ES, desktop and phone width), takes screenshots (`docs/screenshots`) and fails on any console error, page error or failed API call | start API + web, then `python tests/e2e/ui_walkthrough.py` |
| Benchmarks | `backend/benchmarks/run_benchmarks.py` | Runtime, feasibility, objective, gap and memory on 10/2, 50/5, 500/20 and 5000-operation instances | `python benchmarks/run_benchmarks.py --markdown` |

CI (`.github/workflows/ci.yml`) runs ruff, the full backend suite on SQLite, migrations + platform
tests on PostgreSQL 16, and the frontend typecheck and production build.

Regression policy: every defect found (e.g. the OR-Tools abort with a single worker and hint repair,
labour-pool capacity during breaks, detached setups, MRP demand beyond the horizon) is fixed at the
root cause and covered by a test or by the acceptance dataset.
