"""Rescheduling: event-driven repair and manual moves with impact preview.

Both work on a problem whose ``baseline`` holds the current plan. The current plan is first
*re-validated* under the new conditions (a breakdown, a late receipt, a rush order, a manual move…):
the operations that violate a HARD constraint are the **affected operations**. Depending on the
scope, a set of operations is freed and re-placed by the exact builder in their previous order
(stability), everything else keeps resource, sequence and time.

Scopes (rescheduling) / replan modes (manual moves):

``LOCAL``   / ``THIS_ORDER``  affected operations + their successors
``REGIONAL``/ ``DOWNSTREAM``  + later operations on the touched resources (right shift)
``RESOURCE``                  all operations of the touched resources
``AREA``                      all operations of the touched planning areas
``GLOBAL``  / ``SCENARIO``    everything that is not frozen, then re-optimised
``NO_REPLAN``                 nothing moves except the moved operation (violations are reported)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .assemble import assemble
from .builder import BuildConfig, BuildResult, ScheduleBuilder
from .compile import CompiledProblem, compile_problem
from .contract import Problem, Solution, SolverMetadata
from .diff import compare_solutions
from .timing import compute_timing
from .validator import validate

HARD_CONFLICTS = {
    "CALENDAR",
    "CALENDAR_INTERRUPTED",
    "CAPACITY_OVERLAP",
    "CAPACITY_EXCEEDED",
    "SETUP_INSUFFICIENT",
    "PRECEDENCE",
    "MATERIAL_SHORTAGE",
    "INCOMPATIBLE_RESOURCE",
    "SEQUENCE_RULE",
    "RELEASE",
}


@dataclass
class RepairOutcome:
    solution: Solution
    affected_ops: list[str]
    affected_orders: list[str]
    freed_ops: list[str]
    scope: str
    comparison: dict[str, Any] | None = None
    messages: list[str] = field(default_factory=list)


def _baseline_overrides(cp: CompiledProblem) -> dict[int, tuple[int, int, int | None, str]]:
    out = {}
    for i, (res, ss, _s, e) in cp.baseline.items():
        op = cp.ops[i]
        mi = next((m.idx for m in op.modes if m.res == res), None)
        if mi is None:
            continue
        out[i] = (mi, ss, e, "KEPT")
    return out


def replay_baseline(cp: CompiledProblem) -> BuildResult:
    """Place every operation exactly where the baseline has it (new operations are appended)."""
    tm = compute_timing(cp)
    cfg = BuildConfig(overrides=_baseline_overrides(cp), explain=False)
    return ScheduleBuilder(cp, cfg, tm).build()


def affected_operations(cp: CompiledProblem, replay: BuildResult | None = None) -> tuple[set[int], list]:
    """Operations of the current plan that violate a HARD constraint under the new conditions."""
    replay = replay or replay_baseline(cp)
    val = validate(cp, replay.placements, {}, include_data_issues=False)
    hit: set[int] = set()
    for v in val.violations:
        if v.hardness != "HARD" or v.type not in HARD_CONFLICTS:
            continue
        if v.op is not None:
            hit.add(v.op)
        for oid in v.details.get("operations", []) or []:
            if oid in cp.op_index:
                hit.add(cp.op_index[oid])
        if v.type == "CAPACITY_OVERLAP" and v.details.get("other_op") in cp.op_index:
            hit.add(cp.op_index[v.details["other_op"]])
    # operations without a baseline position (new orders, new operations)
    for op in cp.ops:
        if op.idx not in cp.baseline and op.fixed is None:
            hit.add(op.idx)
    # baseline positions that are no longer possible at all (resource removed from the routing)
    for i, (res, *_r) in cp.baseline.items():
        if not any(m.res == res for m in cp.ops[i].modes):
            hit.add(i)
    return hit, val.violations


def _successors(cp: CompiledProblem, seeds: set[int]) -> set[int]:
    out = set(seeds)
    stack = list(seeds)
    while stack:
        i = stack.pop()
        for s, *_ in cp.ops[i].succs:
            if s not in out:
                out.add(s)
                stack.append(s)
    return out


def _is_frozen(cp: CompiledProblem, i: int) -> bool:
    op = cp.ops[i]
    if op.fixed is not None:
        return True
    b = cp.baseline.get(i)
    return b is not None and cp.frozen_until is not None and b[1] < cp.frozen_until


def freed_set(cp: CompiledProblem, seeds: set[int], scope: str, since: int | None = None, resources: set[int] | None = None, allow_frozen: bool = False) -> set[int]:
    scope = scope.upper()
    if scope in ("GLOBAL", "SCENARIO"):
        freed = {op.idx for op in cp.ops}
    else:
        freed = set(seeds)
        touched = set(resources or set())
        new_ops_lb = []
        for i in seeds:
            b = cp.baseline.get(i)
            if b is not None:
                touched.add(b[0])
            else:
                # a new operation (rush order…) competes on every resource it can use
                touched.update(m.res for m in cp.ops[i].modes)
                o = cp.orders[cp.ops[i].order]
                new_ops_lb.append(max(cp.work_lb, o.release or cp.work_lb))
        t0 = since
        if t0 is None:
            starts = [cp.baseline[i][1] for i in seeds if i in cp.baseline] + new_ops_lb
            t0 = min(starts) if starts else cp.as_of
        if scope in ("REGIONAL", "DOWNSTREAM"):
            for i, (res, ss, _s, _e) in cp.baseline.items():
                if res in touched and ss >= t0 - 1:
                    freed.add(i)
        elif scope == "RESOURCE":
            for i, (res, *_r) in cp.baseline.items():
                if res in touched:
                    freed.add(i)
        elif scope == "AREA":
            areas = {cp.resources[r].area for r in touched if cp.resources[r].area}
            for i, (res, ss, _s, _e) in cp.baseline.items():
                if cp.resources[res].area in areas and ss >= t0 - 1:
                    freed.add(i)
        freed = _successors(cp, freed)
    if not allow_frozen:
        freed = {i for i in freed if not _is_frozen(cp, i)}
    # operations fixed in the problem itself (in progress, locked, frozen) never move here
    return {i for i in freed if cp.ops[i].fixed is None}


INSERTION_STRATEGIES = ("LATEST_START", "BEFORE_LATEST_START", "FRONT")


def rebuild(cp: CompiledProblem, freed: set[int], extra_overrides: dict | None = None, explain: bool = True, messages: list[str] | None = None) -> BuildResult:
    """Keep every non-freed baseline operation, re-place the freed ones in their previous order.

    Operations without a baseline position (rush orders, new operations) are inserted by urgency.
    Three insertion priorities are evaluated — at the latest start time, one duration before it, and
    in front of the queue — and the one with the best objective (all orders, weighted) is kept, so an
    expedited order is not blindly put ahead of everything: its cost for the rest of the plan counts.
    """
    from .objectives import components, scales, vector

    tm = compute_timing(cp)
    overrides = {i: ov for i, ov in _baseline_overrides(cp).items() if i not in freed}
    if extra_overrides:
        overrides.update(extra_overrides)
    new_ops = [i for i in freed if i not in cp.baseline]
    base_prio: dict[int, float] = {}
    for op in cp.ops:
        b = cp.baseline.get(op.idx)
        if b is not None:
            base_prio[op.idx] = float(b[1])

    def lst_of(i: int) -> float:
        v = tm.lst[i]
        return float(v if v < (1 << 49) else cp.hi)

    strategies = INSERTION_STRATEGIES if new_ops else INSERTION_STRATEGIES[:1]
    best = None
    best_vec = None
    scale = None
    chosen = strategies[0]
    for strat in strategies:
        prio = dict(base_prio)
        for i in (op.idx for op in cp.ops if op.idx not in base_prio):
            if strat == "LATEST_START":
                prio[i] = lst_of(i)
            elif strat == "BEFORE_LATEST_START":
                prio[i] = lst_of(i) - tm.nominal[i]
            else:
                prio[i] = float(cp.work_lb) - 1.0
        cfg = BuildConfig(priority=prio, overrides=overrides, explain=False, mode_selection=cp.solver.mode_selection)
        res = ScheduleBuilder(cp, cfg, tm).build()
        comp = components(cp, res.placements, res.unscheduled)
        if scale is None:
            scale = scales(comp)
        vec = vector(cp.objectives, comp, scale)
        if best_vec is None or vec < best_vec:
            best, best_vec, chosen = (prio, cfg), vec, strat
    assert best is not None
    if new_ops and messages is not None:
        messages.append(f"new operations inserted with strategy {chosen} (best of {', '.join(strategies)} by the scenario objective)")
    prio, cfg = best
    cfg = BuildConfig(priority=prio, overrides=overrides, explain=explain, mode_selection=cp.solver.mode_selection)
    return ScheduleBuilder(cp, cfg, tm).build()


def _meta(provider: str, cp: CompiledProblem, t0: float, messages: list[str]) -> SolverMetadata:
    from .compile import ENGINE_VERSION

    return SolverMetadata(provider=provider, status="HEURISTIC", runtime_s=round(time.monotonic() - t0, 3), engine_version=ENGINE_VERSION, messages=messages, input_hash=cp.input_hash)


def repair(problem: Problem, scope: str = "LOCAL", allow_frozen: bool = False, baseline_solution: Solution | None = None, optimize_global: bool = True) -> RepairOutcome:
    """Reschedule after a change of conditions (the change is already applied to ``problem``)."""
    t0 = time.monotonic()
    cp = compile_problem(problem)
    replay = replay_baseline(cp)
    seeds, conflicts = affected_operations(cp, replay)
    messages: list[str] = []
    frozen_hit = [cp.ops[i].id for i in seeds if _is_frozen(cp, i)]
    if frozen_hit and not allow_frozen:
        messages.append(f"{len(frozen_hit)} affected operation(s) are in the frozen zone and were kept; authorise frozen changes to move them")
    if scope.upper() in ("GLOBAL", "SCENARIO") and optimize_global:
        from .pipeline import solve

        # frozen operations stay fixed; stability versus the current plan is part of the objective
        sol = solve(problem)
        outcome = RepairOutcome(sol, [cp.ops[i].id for i in sorted(seeds)], sorted({cp.orders[cp.ops[i].order].id for i in seeds}), [op.id for op in cp.ops], scope, messages=messages)
    else:
        freed = freed_set(cp, seeds, scope, allow_frozen=allow_frozen)
        res = rebuild(cp, freed, messages=messages)
        meta = _meta("repair-" + scope.lower(), cp, t0, messages)
        sol = assemble(res, meta)
        outcome = RepairOutcome(
            sol,
            [cp.ops[i].id for i in sorted(seeds)],
            sorted({cp.orders[cp.ops[i].order].id for i in seeds}),
            [cp.ops[i].id for i in sorted(freed)],
            scope,
            messages=messages,
        )
    if baseline_solution is not None:
        outcome.comparison = compare_solutions(baseline_solution, outcome.solution)
    return outcome


def move(problem: Problem, op_id: str, resource_id: str, start: datetime, replan: str = "DOWNSTREAM", allow_frozen: bool = False, baseline_solution: Solution | None = None) -> RepairOutcome:
    """Manual move of one operation with the chosen replan mode (impact preview or apply)."""
    t0 = time.monotonic()
    cp = compile_problem(problem)
    i = cp.op_index.get(op_id)
    if i is None:
        raise ValueError(f"unknown operation {op_id}")
    ri = cp.res_index.get(resource_id)
    if ri is None:
        raise ValueError(f"unknown resource {resource_id}")
    op = cp.ops[i]
    mode = next((m for m in op.modes if m.res == ri), None)
    messages: list[str] = []
    if mode is None:
        raise ValueError(f"{cp.resources[ri].code} is not a resource of operation {op_id}")
    if _is_frozen(cp, i) and not allow_frozen:
        raise PermissionError(f"{op_id} is in the frozen zone; authorise frozen changes to move it")
    s = cp.axis.to_min(start)
    snapped = mode.cal.next_work(s) if op.interruptible else mode.cal.next_uninterrupted_start(s, mode.nominal)
    if snapped is None:
        raise ValueError(f"{cp.resources[ri].code} has no working time after {start.isoformat()}")
    if snapped != s:
        messages.append(f"start snapped to the next working time {cp.dt(snapped).isoformat()}")
    replan = replan.upper()
    old = cp.baseline.get(i)
    touched = {ri} | ({old[0]} if old else set())
    since = min(snapped, old[1]) if old else snapped
    if replan == "NO_REPLAN":
        freed: set[int] = set()
    elif replan == "THIS_ORDER":
        freed = _successors(cp, {i}) - {i}
    elif replan in ("DOWNSTREAM", "RESOURCE", "AREA"):
        freed = freed_set(cp, {i}, replan, since=since, resources=touched, allow_frozen=allow_frozen) - {i}
    elif replan in ("SCENARIO", "GLOBAL"):
        freed = freed_set(cp, {i}, "GLOBAL", allow_frozen=allow_frozen) - {i}
    else:
        raise ValueError(f"unknown replan mode {replan}")
    res = rebuild(cp, freed, extra_overrides={i: (mode.idx, snapped, None, "MANUAL")}, messages=messages)
    if replan in ("SCENARIO", "GLOBAL") and len(freed) > 0:
        messages.append("scenario-wide replan re-places every non-frozen operation around the moved one in its previous order")
    meta = _meta(f"move-{replan.lower()}", cp, t0, messages)
    sol = assemble(res, meta)
    outcome = RepairOutcome(sol, [op_id], [cp.orders[op.order].id], [cp.ops[k].id for k in sorted(freed)], replan, messages=messages)
    if baseline_solution is not None:
        outcome.comparison = compare_solutions(baseline_solution, sol)
    return outcome
