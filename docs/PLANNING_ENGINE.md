# MonxuPlan — Planning Engine

The Planning Engine (`backend/monxuplan_engine`) is a pure, deterministic function

```
solve(Problem) -> Solution
```

It has no dependency on the database or the API. Its input and output are the JSON contract in
`monxuplan_engine/contract.py` (JSON Schema available at `GET /api/v1/planning/contract`).

```jsonc
// Problem (abridged)
{
  "scenario_id": "SC-001",
  "horizon": {"start": "2026-09-28T04:00:00Z", "end": "2026-11-09T04:00:00Z",
               "frozen_until": "2026-09-29T04:00:00Z", "flexible_until": "2026-10-05T04:00:00Z",
               "timezone": "Europe/Madrid"},
  "calendars": [...], "resources": [...], "setup_matrices": [...], "setup_rules": [...],
  "materials": [...], "orders": [...], "operations": [...], "precedences": [...],
  "sequence_constraints": [...], "rules": [...], "constraints": {...},
  "objectives": {...}, "solver": {...}, "baseline": [...]
}
// Solution (abridged)
{
  "schedule": [...], "unscheduled": [...], "violations": [...], "orders": [...],
  "kpis": {...}, "bottlenecks": [...], "explanations": {...}, "pegging": [...],
  "solver_metadata": {"provider": "hybrid", "status": "FEASIBLE", "objective": ..., "best_bound": ...,
                      "gap": 0.042, "runtime_s": 9.8, "input_hash": "…", "engine_version": "…"}
}
```

## 1. Time model

* Internal time is **integer minutes since `horizon.start`** (UTC). All input/output datetimes are
  timezone-aware ISO-8601.
* Calendars are defined in **plant local time** (shift patterns, breaks, exceptions) and expanded
  with `zoneinfo`, so DST transitions are exact (a 22:00–06:00 shift on the night the clocks go back
  lasts 9 real hours).
* A calendar becomes a sorted list of disjoint working windows `[a_i, b_i)`, each tagged `REGULAR`
  or `OVERTIME`. Overtime windows are usable only when the scenario allows overtime; their use is
  measured and penalised.

### Working-time arithmetic

`W(t)` = cumulative working minutes of a calendar up to `t`. For an operation needing `d` working
minutes that starts at working instant `s`:

```
end(s, d) = min { t : W(t) − W(s) = d }          (interruptible: pauses over non-working time)
end(s, d) = s + d   and   [s, s+d) ⊂ one window  (non-interruptible, e.g. curing, heat treatment)
```

`end(s, d)` is piecewise linear in `s` with slope 1; the CP-SAT model uses exactly these segments
(see OPTIMIZATION.md), and the schedule builder evaluates them with binary search.

The **effective calendar of a mode** is the intersection of the calendars of every resource the
mode needs (machine ∩ labour pool shifts ∩ tool availability) minus maintenance and downtime.

## 2. Entities in the engine

| Concept | Meaning |
|---|---|
| Order `j` | A production order: item, quantity, release `r_j`, due `d_j`, priority weight `w_j`, customer flags. |
| Operation `o` | A step of an order: quantity, duration model, candidate modes, material uses, status, fixed assignment (frozen / in progress / locked). |
| Mode `m ∈ M(o)` | One feasible way to execute `o`: primary resource + secondary requirements (labour pool units, tool units) + speed factor + preference + optional subcontracting. |
| Resource `k` | Machine, human, labour pool, tool, work centre, subcontractor, storage, transport. Capacity `c_k` (1 = unary/disjunctive), calendar, efficiency, costs, attributes, unavailability. |
| Precedence | `(pred, succ, type ∈ {FS, SS, FF, SF}, lag)`; routing steps generate FS with move/queue/wait times, overlap generates SS + FF. |
| Material `i` | Item with a supply timeline (on-hand available, firm receipts, projected receipts, production outputs) and uses (consumption at operation start). |

### Duration model

```
run(q)   = Σ tiers:  q × minutes_per_unit(q)         (tiered: e.g. 3.4 min/u, 3.1 min/u if q > 1000)
         + fixed_minutes
         + ceil(q / batch_size) × minutes_per_batch   (ovens, batch processes)
run_eff  = ceil( run(q) / (efficiency_k × speed_factor_m) )
setup    = setup(prev_state_k, next_item, k)          (sequence dependent, see §4)
duration = setup + run_eff + teardown                 (working minutes on the effective calendar)
```

In-progress operations use their remaining quantity; completed operations are not scheduled.

## 3. Constraints

### HARD (never violated unless the scenario explicitly relaxes that category)

| Code | Constraint |
|---|---|
| `COMPATIBILITY` | An operation can only run on one of its modes (resource must exist, be active and compatible). |
| `CALENDAR` | Work only inside the effective calendar; non-interruptible operations inside one window. |
| `CAPACITY` | Unary resources: no overlap (setup included). Cumulative resources (labour pools, tools, ovens): Σ units ≤ capacity(t) at every t. |
| `MAINTENANCE` | Maintenance / downtime blocks are removed from calendars. |
| `PRECEDENCE` | Routing order with lags, overlap (transfer batch), move / queue / wait times, buffers. |
| `MATERIAL` | A consumption at time t requires projected available stock ≥ 0 at every instant ≥ t (no magic inventory). Projected receipts count only if the scenario allows it. |
| `SKILL` / `TOOL` | Labour pool with the required skill and the required tool must be available simultaneously for the whole operation. |
| `RELEASE` | No start before the order release / operation release. |
| `FROZEN` | Operations inside the frozen zone keep resource and times unless the planner authorises changes. |
| `LOCK` | Locked operations and locked sequences are respected. |
| `SEQUENCE` | Configured sequence rules: *A before B*, *C cannot immediately follow D*, forbidden transitions. |
| `LOT` | Quantities respect min / max / multiple / integer rules (checked by the validator). |

### SOFT (penalised in the objective)

`DUE_DATE` (tardiness, late orders), `PREFERRED_RESOURCE`, `PREFERRED_SHIFT`, `SETUP`,
`FAMILY_GROUPING`, `OVERTIME`, `WIP`, `INVENTORY` (earliness), `STABILITY` (changes vs baseline),
`COST`.

HARD and SOFT are separate code paths: HARD constraints are enforced structurally by the schedule
builder and the CP-SAT model and verified by the validator; SOFT constraints only appear as terms of
the objective function. Relaxation (e.g. `materials: "ALLOW_SHORTAGE"`) turns a HARD category into
reported violations — it never silently drops it.

## 4. Setup modelling

Setup time of operation `o` on unary resource `k` depends on the state left by the previous
operation on `k` (or the resource's initial state):

```
setup(prev, next, k) = combine_{M ∈ matrices(k)}  M[prev.attr_M][next.attr_M]
                       + Σ rules r matching (prev, next)       (e.g. cleaning after family Y → X)
combine ∈ {max, sum}     same value → M.same_minutes ;  missing pair → M.default_minutes
```

Matrices can be keyed by any attribute: item, family, colour, diameter, thickness, tool, mould,
temperature, format. A matrix can be global, per resource group or per resource.

## 5. Schedule builder (the "decoder")

The builder is an exact serial schedule-generation scheme:

1. Place fixed operations (in progress, frozen, locked) and reserve their capacity and materials.
2. Repeatedly select the eligible operation (all predecessors placed) with the best dispatch key
   (EDD, SPT, CR, slack, setup, family, material, bottleneck, customer priority, or a weighted /
   lexicographic combination), or follow an explicit per-resource sequence (from CP-SAT / manual).
3. For every mode of the operation compute the earliest feasible start:
   `t ≥ max(release, predecessors + lags, material ready, frozen boundary)`, then iterate until the
   primary resource is free for `setup + run + teardown` on the effective calendar, every cumulative
   resource has capacity during the whole interval and materials are covered.
4. Choose the mode by the resource strategy (earliest finish, preferred, lowest cost, least setup)
   and record **why**: binding constraint (resource busy with …, material ready at …, predecessor,
   calendar, release, labour, tool) and the evaluated alternatives with their finish times or
   blocking reasons.
5. If no mode is feasible within the horizon the operation (and its successors) are reported as
   **unscheduled with an explicit reason** — never placed in an impossible position.

## 6. Material synchronisation and pegging

* Raw / bought materials: a ledger per material holds supplies (on-hand available at t=0, receipts at
  their expected date) and consumptions. A consumption of `q` at `t` is allowed iff
  `min_{t' ≥ t} stock(t') ≥ q`; the earliest material availability is the smallest such `t`.
* Make items: static pegging (FIFO by need date) links component production orders to the parent
  operations that consume them, adding finish-to-start synchronisation; the ledger enforces quantity.
* After scheduling, FIFO pegging reconstructs `supply → consumption` links, giving the full chain
  `Sales order → Production order → Sub-assembly order → Raw material → Purchase order` and the reverse
  impact query *"which customer orders are affected if this receipt is late?"*.

## 7. Replanning modes

| Mode | Operations re-timed |
|---|---|
| No replan | Only the moved operation; the validator reports resulting violations. |
| This order | The moved operation and its order's successors. |
| Downstream | Everything transitively affected (routing successors + later operations on touched resources). |
| Resource | All operations of the resources touched. |
| Area | All operations of the planning area. |
| Scenario | Full re-optimisation. |

Operations outside the affected set keep resource, sequence and (when still feasible) time;
stability metrics (operations moved, average shift, resource changes, sequence changes) are reported
against the baseline.

## 8. Explainability

Every scheduled operation carries an explanation built from facts computed by the engine:

* chosen resource, start, end, binding constraint;
* reasons: compatibility, tool / labour available, material ready at t, family kept, setup saved vs
  the best alternative, due date met, preferred resource, rules that fired;
* alternatives evaluated: finish time delta or the blocking constraint (maintenance, material,
  tool, labour, incompatible, calendar).

For solutions produced by CP-SAT the same explanation is computed post-hoc by evaluating every
alternative mode against the final schedule of all other operations (counterfactual placement).
The **Constraint Explorer** uses the same routine for "why can't I schedule this operation here?".

Root cause of lateness walks the binding constraints backwards from the order's last operation:
order late → operation late → resource busy (with which orders) / material ready at (which receipt,
which supplier) / predecessor late → …

## 9. Determinism and reproducibility

* Same `Problem` + same `solver` settings ⇒ same schedule (stable sort keys, seeded RNG; CP-SAT in
  reproducible mode uses a deterministic time limit and interleaved search).
* The problem snapshot and its SHA-256 are stored with every plan version.
