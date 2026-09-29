# MonxuPlan — Optimisation

## 1. Provider abstraction

```python
class OptimizationProvider(Protocol):
    name: str
    def capabilities(self) -> ProviderCapabilities: ...
    def solve(self, problem: CompiledProblem, ctx: SolveContext) -> ProviderResult: ...
```

| Provider | Use | Guarantees |
|---|---|---|
| `heuristic` | Dispatching rules + local search on the priority list / mode choice, evaluated by the exact schedule builder. Scales to 10k+ operations. | Feasible (all HARD constraints) by construction. Status `HEURISTIC` — never claims optimality. |
| `cpsat` | OR-Tools CP-SAT model of the whole problem (small / medium instances). | Returns `OPTIMAL` only when CP-SAT proves it; otherwise `FEASIBLE` with the proven bound and gap. |
| `hybrid` (default) | Heuristic initial solution → CP-SAT on the full model if small enough, otherwise Large Neighbourhood Search: repeatedly free a neighbourhood (time window, bottleneck resource, late orders), fix the rest, re-optimise with CP-SAT with solution hints, re-time with the exact builder, accept if the objective improves. | Feasible at every step; reports the best objective, the neighbourhoods explored and the gap of the last sub-problem. |
| `mip` | OR-Tools linear solver (SCIP) for **aggregate planning**: family × period production quantities under resource-group capacity, backlog, inventory and overtime costs. | Optimal/feasible status from the MIP solver. Not used for detailed sequencing. |

Commercial solvers (Gurobi, CPLEX) plug in by adding a provider — the problem compiler, the
builder, the validator and the KPIs are shared.

## 2. Objective components

All components are computed from a schedule by `objectives.evaluate()` — the same code for every
provider, so objective values are comparable across providers and scenarios.

| Code | Definition | Unit |
|---|---|---|
| `unscheduled` | Σ operations that could not be placed (always optimised first, implicit level 0) | ops |
| `late_orders` | Σ_j w_j · [C_j > d_j] | weighted orders |
| `tardiness` | Σ_j w_j · max(0, C_j − d_j) | weighted minutes |
| `critical_tardiness` | tardiness restricted to expedite / strategic / priority ≥ 8 orders | minutes |
| `setup` | Σ setup minutes | minutes |
| `wip` | Σ_j (C_j − S_j) order flow time | order-minutes |
| `inventory` | Σ_j q_j · max(0, d_j − C_j) finished-goods earliness | unit-minutes |
| `makespan` | max_j C_j (throughput / utilisation proxy) | minutes |
| `overtime` | minutes of work inside overtime windows | minutes |
| `stability` | Σ_o |S_o − S_o^base| + 480 · [resource changed] over operations of the baseline | minutes |
| `preference` | Σ minutes executed on non-preferred modes (preference rank > 0) | minutes |
| `cost` | machine + labour + overtime + setup + subcontract + lateness cost | currency |

### Weighted mode

```
F = Σ_k  weight_k × f_k / scale_k
```
`scale_k` = value of component k in the reference solution (initial heuristic), floored at 1, so
weights express relative importance (e.g. Customer service 35 %, Setup 20 %, Throughput 15 %,
Inventory 10 %, Overtime 10 %, Stability 10 %). Weights live in the scenario's optimisation profile —
nothing is hard-coded. Presets: *OTIF First, Minimize Setup, Max Throughput, Minimize Inventory,
Stable Plan, Balanced*.

### Lexicographic mode

Levels are ordered lists of components, e.g.
`[[unscheduled], [critical_tardiness], [late_orders], [setup], [wip], [inventory], [makespan], [overtime], [stability]]`.
Heuristic/local search compares objective vectors lexicographically (with a relative tolerance per
level). CP-SAT solves level by level: optimise level i, then add `f_i ≤ best_i × (1 + tol_i)` and
continue with level i+1, splitting the time budget.

## 3. CP-SAT model

Indices: operations `o`, modes `m ∈ M(o)`, unary resources `k`, cumulative resources `r`,
materials `i`, orders `j`.

**Variables**

* `x_{o,m} ∈ {0,1}` mode choice, `Σ_m x_{o,m} = 1` (fixed operations: a single fixed mode).
* `S_o, E_o` integer minutes; optional intervals `I_{o,m} = [S_{o,m}, E_{o,m})` present iff `x_{o,m}`.
* Calendar segments `y_{o,m,s} ∈ {0,1}`, `Σ_s y_{o,m,s} = x_{o,m}`:
  `y ⇒ lo_s ≤ S_{o,m} ≤ hi_s ∧ E_{o,m} = S_{o,m} + off_s` — the exact piecewise-linear
  working-time stretch of §1 of PLANNING_ENGINE.md for the mode's effective calendar.
* Sequencing literals `z_{a,b,k}` (arc a → b on resource k) when the resource has sequence-dependent
  setups and at most `circuit_max_ops` operations in the sub-problem.

**Constraints**

```
Unary k:        AddCircuit(arcs(k) ∪ {0→a, a→0, a→a (¬present)})
                z_{a,b,k} ⇒ S_b ≥ E_a + setup(a,b,k)            (setup as a gap)
                or NoOverlap(I_{o,m} : resource(m)=k) when no sequence-dependent setup
Cumulative r:   AddCumulative({I_{o,m} : r ∈ req(m)}, units, cap_r_max)
                + fixed blocker intervals for periods with reduced capacity (shifts, absences)
Precedence:     S_succ ≥ E_pred + lag        (FS; SS/FF/SF analogous)
Materials:      AddReservoirConstraint per material:
                +supply at fixed time, −q at S_o (active iff operation present), level ≥ 0
Release/frozen: S_o ≥ r_o ; fixed operations have fixed S, E, m
Forbidden:      z_{a,b,k} = 0 for forbidden immediate transitions; A-before-B precedences
```

**Objective** — the weighted sum (integer coefficients `round(1000 × weight_k / scale_k)`) or the
current lexicographic level. Tardiness `T_j ≥ E_last(j) − d_j, T_j ≥ 0`; lateness indicators
`L_j ⇒` otherwise `E_last(j) ≤ d_j`; setup = Σ z · setup; WIP = Σ (E_last − S_first);
stability = Σ |S_o − S_o^base| + penalty · [mode changed]; overtime is approximated per calendar
segment and measured exactly afterwards.

**Decoding.** The CP-SAT assignment and per-resource sequences are re-timed by the exact schedule
builder (setup inside working time, overlap/transfer batches). Reported KPIs are those of the
decoded schedule; the solver metadata keeps the CP objective, best bound and gap separately.
MonxuPlan never reports `OPTIMAL` unless CP-SAT proved it for the full (not LNS) model.

**Limits.** Every solve has a time limit (profiles *Quick* 10 s, *Normal* 60 s, *Deep* 600 s — all
configurable). When the limit is hit the best solution found is returned with
`status = FEASIBLE` and `gap = (objective − bound) / objective`. If CP-SAT finds nothing in time
the heuristic solution is returned and labelled as such.

**Reproducible mode** uses `max_deterministic_time`, `interleave_search` and a fixed seed, so two runs
with the same input return the same plan.

## 4. Dispatching rules

`EDD`, `EDD_PRIORITY`, `SPT`, `LPT`, `FIFO`, `CRITICAL_RATIO`, `MIN_SLACK`, `SHORTEST_SETUP`,
`FAMILY_GROUPING`, `MATERIAL_AVAILABILITY`, `BOTTLENECK_FIRST`, `DUE_DATE_FIRST`,
`CUSTOMER_PRIORITY`, `HYBRID_APS`. Rules combine lexicographically
(`["CUSTOMER_PRIORITY", "MIN_SLACK", "EDD", "SHORTEST_SETUP", "MATERIAL_AVAILABILITY"]`) or as a
weighted composite of normalised keys.

## 5. Scheduling direction

* **Forward** — as soon as possible from availability.
* **Backward** — as late as possible to finish at the due date (just-in-time), then validated.
* **Hybrid** — backward to obtain latest start times (used as release targets and slack), then
  forward finite-capacity placement.

## 6. Robust planning, simulation and sensitivity

* **Monte Carlo evaluation**: the schedule builder is re-run N times with sampled run-time variance,
  supplier delays and breakdowns (seeded) keeping the plan's sequence → on-time probability per order,
  expected lead time, WIP and throughput distributions.
* **Sensitivity**: marginal analysis "what is limiting the system?" — +1 hour / +1 shift on the top
  bottleneck candidates, +1 operator per pool, +10 % material → change in completed quantity and late
  orders.

## Benchmarks

Measured with `python backend/benchmarks/run_benchmarks.py --providers heuristic hybrid cpsat --time-limit 30 --markdown`
(one process per run; 30 s budget; reproducible mode; seeded instances with two-shift calendars, family setup
matrices, alternative resources, labour pools, tools and late material receipts; 4-core cloud container).
"Hard violations" are counted by the independent validator on the returned schedule.

| Instance | Ops | Resources | Provider | Runtime (s) | Status | Hard violations | Unscheduled | Late | Setup (h) | Gap | Peak RSS (MB) |
|---|---:|---:|---|---:|---|---:|---:|---:|---:|---:|---:|
| 10 orders / 2 machines | 16 | 4 | heuristic | 0.48 | HEURISTIC | 0 | 0 | 0 | 5.83 | — | 98 |
| 10 orders / 2 machines | 16 | 4 | hybrid | 28.48 | FEASIBLE | 0 | 0 | 0 | 4.33 | 55.6% | 129 |
| 10 orders / 2 machines | 16 | 4 | cpsat | 28.49 | FEASIBLE | 0 | 0 | 0 | 4.33 | 55.6% | 129 |
| 50 orders / 5 machines | 91 | 7 | heuristic | 1.8 | HEURISTIC | 0 | 0 | 0 | 34.08 | — | 101 |
| 50 orders / 5 machines | 91 | 7 | hybrid | 28.6 | FEASIBLE | 0 | 0 | 0 | 34.58 | 69.4% | 207 |
| 50 orders / 5 machines | 91 | 7 | cpsat | 28.53 | FEASIBLE | 0 | 0 | 0 | 34.58 | 69.4% | 207 |
| 500 orders / 20 machines | 1511 | 22 | heuristic | 16.25 | HEURISTIC | 0 | 0 | 0 | 519.42 | — | 126 |
| 500 orders / 20 machines | 1511 | 22 | hybrid | 18.71 | FEASIBLE | 0 | 0 | 0 | 519.42 | — | 211 |
| 500 orders / 20 machines | 1511 | 22 | cpsat | 16.35 | FEASIBLE | 0 | 0 | 0 | 519.42 | — | 210 |
| 5000 operations / 100 resources | 5117 | 100 | heuristic | 21.49 | HEURISTIC | 0 | 0 | 0 | 1883.92 | — | 212 |
| 5000 operations / 100 resources | 5117 | 100 | hybrid | 15.26 | FEASIBLE | 0 | 0 | 0 | 1883.92 | — | 329 |
| 5000 operations / 100 resources | 5117 | 100 | cpsat | 15.36 | FEASIBLE | 0 | 0 | 0 | 1883.92 | — | 330 |

Reading the table honestly:

* Every run returns a complete schedule with **no hard violation**, within the time budget (the budget
  covers the search; model building and explanations are reserved inside it).
* The **gap** is reported only when it is meaningful: CP-SAT on the whole model (up to `cpsat_max_ops`
  = 250 operations). On the small instances the bound is weak (calendar gaps and the inventory/flow terms
  relax poorly), so CP-SAT improves the schedule (setup 5.8 h → 4.3 h on S) but cannot *prove* optimality in
  30 s; the status stays `FEASIBLE`, never `OPTIMAL`. `OPTIMAL` is reported only when proven and exactly
  reproduced by the schedule builder (e.g. the known-answer tests).
* Above 250 operations the hybrid provider runs large-neighbourhood search with a deterministic number of
  neighbourhoods; LNS reports no global gap. On L and XL it did not find an improvement over the heuristic within
  its iteration budget (identical KPIs), which is reported as such rather than hidden.
* Peak RSS is the whole Python process including OR-Tools native memory.

## Large plants: 100 000 orders a day

Measured on the Scale Plant (`python -m monxuplan.seed.scale --orders 100000`: 100 000 open orders due
over one day, 198 753 operations, 1 000 machines in cells of 8, 600 products with 2–3 alternative
machines per operation, family changeovers, raw materials with stock) on PostgreSQL 16, 4-core cloud
container (Xeon 2.8 GHz), one Python process. Commands: `benchmarks/platform_scale.py --run --views`
(planning run through the worker path, then the screens' service calls with JSON encoding) and the move
timings from the same database.

**Planning run** (heuristic, 120 s budget):

| Phase | Time |
|---|---:|
| Build the problem from the database | 11.4 s |
| Solve (one construction ≈ 49 s; explanations recorded during the first one) | 133 s |
| Store the version (COPY of 200 000 operations, 100 000 order results, pegging, read models) | 42.9 s |
| Load previous version + compare | 10.1 s |
| **Total** | **203 s** (was 401 s) · peak memory 6.9 GB (was 10.8 GB) |

The time budget only allows the first construction: the run says so in its messages ("alternative
dispatching strategies skipped", "local search skipped … raise the time limit"). It is not an optimised
plan in the sense of the small instances above.

**Screens** (service call + JSON encoding; response size):

| Call | Time | Size |
|---|---:|---:|
| Dashboard | 0.75 s | 14 kB |
| Order book page (first / last of 100 000, search, late by lateness) | 0.23–0.36 s | ≤ 174 kB |
| Gantt rows (1 000 resources, calendars) | 0.22 s | 420 kB |
| Gantt viewport: 50 rows × 16 h of operations | 1.42 s | 5.6 MB |
| Gantt viewport: 50 rows × 2 weeks as busy blocks | 0.52 s | 8 kB |
| Operation search | 0.24 s | < 1 kB |
| Operation / order detail, order chain | 0.00–0.40 s | ≤ 10 kB |
| Capacity heat-map (day buckets / hour buckets, 100 rows) | 0.02 / 0.04 s | 1.0 / 1.5 MB |
| Material availability / projection | 0.62 / 0.01 s | 339 / 286 kB |
| Dispatch list of one machine | 0.14 s | 82 kB |
| Supervisor view (whole plant) | 4.4 s | 831 kB |
| Legacy full-window Gantt (first day, all resources) | 6.5 s | 48 MB — not used by the planning board for large plans |

**Manual moves** (DOWNSTREAM replan, 60–300 operations re-placed):

| Step | Before | Now |
|---|---:|---:|
| Impact preview, first on a plan version (loads and compiles the version) | 182 s | 100 s |
| Impact preview, following ones on the same version | 182 s | 30 s |
| Apply (re-place, explain the re-placed operations, store the new version) | ~230 s | 63 s |
| Impact preview on the version created by the move | 182 s | 31 s |

How: the compiled plan of the current version is kept in memory and the next version's is derived from
the move instead of being read and compiled again; the preview skips explanations, bottlenecks and
pegging; the new version copies the parent's unchanged rows and read models inside the database and
keeps the explanations of the operations that did not move.

**Limits, stated plainly.** A move still re-validates, re-assembles and stores the whole 200 000-operation
version (validation alone ≈ 14 s, writing a full version ≈ 6 s per 200 000 rows even server-side), so it
takes tens of seconds, not the sub-second feel of a small plan. Getting there needs incremental validation
and KPIs over the affected region and versions stored as differences from their parent; those are not
implemented. The first preview after a new version is loaded (or after a restart) pays the full load.
