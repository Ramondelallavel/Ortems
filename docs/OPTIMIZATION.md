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
