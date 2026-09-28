"""Sensitivity analysis — "what is limiting the system?".

Marginal experiments on copies of the problem, each re-planned with the same fast heuristic as the
reference run (so differences are attributable to the change, not to optimisation noise):

* +1 extra 8-hour shift per week on each of the top bottleneck resources,
* +1 unit of capacity on each labour pool / tool group that induced waiting,
* +10 % supply on each material that delayed or blocked operations.

For each experiment: Δ completed quantity in the horizon, Δ late orders, Δ weighted tardiness and
the throughput gained per added hour.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .changes import apply_changes
from .contract import Problem, Solution


def _fast(problem: Problem) -> Problem:
    data = problem.model_dump(mode="json", by_alias=True)
    data["solver"].update({"provider": "heuristic", "local_search": False, "multi_start": False, "explain": False, "time_limit_s": 30})
    return Problem.model_validate(data)


def _summ(sol: Solution) -> dict[str, float]:
    return {
        "throughput_units": float(sol.kpis.get("throughput_units") or 0.0),
        "late_orders": float(sol.kpis.get("late_orders") or 0.0) + float(sol.kpis.get("orders_unscheduled") or 0.0),
        "tardiness_h": float(sol.solver_metadata.objective_breakdown.get("tardiness", 0.0)) / 60.0,
    }


def analyse(problem: Problem, solution: Solution, max_resources: int = 3) -> dict[str, Any]:
    from .pipeline import solve

    base_p = _fast(problem)
    base = _summ(solve(base_p))
    experiments: list[dict[str, Any]] = []
    weeks = max((problem.horizon.end - problem.horizon.start).total_seconds() / (7 * 86400), 1.0)
    res_by_id = {r.id: r for r in problem.resources}
    machines = [b for b in solution.bottlenecks if b.resource_id and b.kind in ("OVERLOADED", "HIGH_UTILIZATION", "QUEUE")][:max_resources]
    for b in machines:
        r = res_by_id.get(b.resource_id)
        if r is None or r.calendar_id is None:
            continue
        change = {"type": "ADD_SHIFT", "payload": {"resource_ids": [r.id], "label": "sensitivity", "shifts": [{"weekday": 5, "start": "06:00", "end": "14:00"}]}}
        experiments.append(_run(solve, base_p, base, change, f"+8 h/week on {r.code}", "RESOURCE", r.code, 8 * weeks))
    pools = [b for b in solution.bottlenecks if b.kind in ("LABOR", "TOOL") and b.resource_id][:max_resources]
    for b in pools:
        r = res_by_id.get(b.resource_id)
        if r is None:
            continue
        change = {"type": "CHANGE_CAPACITY", "payload": {"resource_id": r.id, "capacity": r.capacity + 1}}
        experiments.append(_run(solve, base_p, base, change, f"+1 unit of {r.code}", b.kind, r.code, None))
    mats = [b for b in solution.bottlenecks if b.kind == "MATERIAL"][:max_resources]
    mat_by_code = {m.code: m for m in problem.materials}
    for b in mats:
        m = mat_by_code.get(b.ref or "")
        if m is None:
            continue
        total = sum(s.quantity for s in m.supplies) or 1.0
        change = {"type": "MATERIAL_ADJUST", "payload": {"material_id": m.id, "quantity": round(total * 0.1, 3)}}
        experiments.append(_run(solve, base_p, base, change, f"+10 % supply of {m.code}", "MATERIAL", m.code, None))
    experiments.sort(key=lambda e: (-e["delta_throughput_units"], e["delta_late_orders"]))
    return {"reference": base, "experiments": experiments, "generated_at": datetime.now().isoformat(timespec="seconds")}


def _run(solve, base_p: Problem, base: dict, change: dict, label: str, kind: str, ref: str, added_hours: float | None) -> dict[str, Any]:
    p, _log = apply_changes(base_p, [change])
    s = _summ(solve(p))
    d_units = s["throughput_units"] - base["throughput_units"]
    return {
        "label": label,
        "kind": kind,
        "ref": ref,
        "added_hours": round(added_hours, 1) if added_hours else None,
        "delta_throughput_units": round(d_units, 3),
        "delta_late_orders": round(s["late_orders"] - base["late_orders"], 3),
        "delta_tardiness_h": round(s["tardiness_h"] - base["tardiness_h"], 2),
        "units_per_added_hour": round(d_units / added_hours, 3) if added_hours else None,
    }
