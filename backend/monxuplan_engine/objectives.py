"""Objective components, weighted and lexicographic scalarisation, presets.

The same :func:`components` function evaluates every schedule, whatever provider produced it, so
objective values are comparable across providers, runs and scenarios.
"""

from __future__ import annotations

from collections.abc import Sequence

from .contract import ObjectiveSpec

# What each component measures exactly (minutes are real elapsed minutes unless stated):
#   unscheduled         number of operations not placed
#   late_orders         Σ weight of orders finishing after (due − safety time)
#   tardiness           Σ weight × minutes late (vs due − safety time)
#   critical_tardiness  same, critical orders only
#   setup               Σ setup minutes (working time)
#   wip                 Σ order flow time, minutes from the first operation's setup start to the order's
#                       completion — a proxy of work in process (time-weighted, not a count of units)
#   inventory           Σ quantity × hours the order is finished before (due − safety time): finished-goods
#                       holding caused by early completion (unit-hours). Raw-material and in-process stock
#                       are not included; the name is kept for compatibility
#   makespan            minutes from as-of to the last completion
#   overtime            Σ overtime minutes used
#   stability           Σ |start shift| vs the baseline plan + a penalty per resource change
#   preference          Σ minutes on less-preferred alternative resources × preference rank
#   cost                Σ resource, overtime, setup and subcontracting cost of the placements
COMPONENTS = (
    "unscheduled",
    "late_orders",
    "tardiness",
    "critical_tardiness",
    "setup",
    "wip",
    "inventory",
    "makespan",
    "overtime",
    "stability",
    "preference",
    "cost",
)

# minimum scale used when normalising (avoid division by ~0 and over-amplification)
SCALE_FLOOR = {
    "unscheduled": 1.0,
    "late_orders": 1.0,
    "tardiness": 60.0,
    "critical_tardiness": 60.0,
    "setup": 60.0,
    "wip": 600.0,
    "inventory": 60.0,
    "makespan": 60.0,
    "overtime": 60.0,
    "stability": 60.0,
    "preference": 60.0,
    "cost": 1.0,
}

DEFAULT_LEVELS: list[list[str]] = [
    ["critical_tardiness"],
    ["late_orders"],
    ["tardiness"],
    ["setup"],
    ["wip"],
    ["inventory"],
    ["makespan"],
    ["overtime"],
    ["stability"],
]

PRESETS: dict[str, dict] = {
    "OTIF_FIRST": {
        "label": "OTIF First",
        "mode": "LEXICOGRAPHIC",
        "levels": [["critical_tardiness"], ["late_orders"], ["tardiness"], ["setup"], ["makespan"], ["stability"]],
        "weights": {"late_orders": 40, "tardiness": 25, "critical_tardiness": 20, "setup": 5, "makespan": 5, "stability": 5},
    },
    "MINIMIZE_SETUP": {
        "label": "Minimize Setup",
        "mode": "WEIGHTED",
        "weights": {"setup": 45, "late_orders": 20, "tardiness": 15, "critical_tardiness": 10, "makespan": 10},
    },
    "MAX_THROUGHPUT": {
        "label": "Max Throughput",
        "mode": "WEIGHTED",
        "weights": {"makespan": 40, "setup": 20, "late_orders": 20, "tardiness": 10, "wip": 10},
    },
    "MINIMIZE_INVENTORY": {
        "label": "Minimize Inventory",
        "mode": "WEIGHTED",
        "weights": {"inventory": 35, "wip": 25, "late_orders": 20, "tardiness": 15, "setup": 5},
    },
    "STABLE_PLAN": {
        "label": "Stable Plan",
        "mode": "WEIGHTED",
        "weights": {"stability": 45, "late_orders": 20, "tardiness": 15, "setup": 10, "critical_tardiness": 10},
    },
    "BALANCED": {
        "label": "Balanced",
        "mode": "WEIGHTED",
        "weights": {
            "late_orders": 15,
            "tardiness": 12,
            "critical_tardiness": 8,
            "setup": 20,
            "makespan": 15,
            "inventory": 10,
            "overtime": 10,
            "stability": 10,
        },
    },
}


def apply_preset(spec: ObjectiveSpec, preset: str) -> ObjectiveSpec:
    p = PRESETS[preset]
    data = spec.model_dump()
    data["preset"] = preset
    data["mode"] = p["mode"]
    data["weights"] = dict(p["weights"])
    data["levels"] = [list(lvl) for lvl in p.get("levels", [])]
    return ObjectiveSpec.model_validate(data)


def components(cp, placements, unscheduled, baseline: dict | None = None) -> dict[str, float]:
    """Objective components of a schedule (see OPTIMIZATION.md §2)."""
    out = dict.fromkeys(COMPONENTS, 0.0)
    out["unscheduled"] = float(len(unscheduled))
    max_end = cp.as_of
    for o in cp.orders:
        if not o.ops:
            continue
        target = o.due - o.safety
        ends = []
        starts = []
        missing = False
        for i in o.ops:
            p = placements[i]
            if p is None:
                missing = True
                continue
            starts.append(p.setup_start)
        for i in o.last_ops:
            p = placements[i]
            if p is None:
                missing = True
            else:
                ends.append(p.end)
        if missing or not ends:
            c = cp.hi
        else:
            c = max(ends)
            max_end = max(max_end, c)
        late = max(0, c - target)
        if late > 0:
            out["late_orders"] += o.weight
            out["tardiness"] += o.weight * late
            if o.critical:
                out["critical_tardiness"] += o.weight * late
        else:
            out["inventory"] += o.qty * (target - c) / 60.0
        if starts and not missing:
            out["wip"] += c - min(starts)
    stab_pen = cp.objectives.stability_resource_change_minutes
    base = baseline if baseline is not None else cp.baseline
    for p in placements:
        if p is None:
            continue
        op = cp.ops[p.op]
        m = op.modes[p.mode]
        out["setup"] += p.setup
        out["overtime"] += p.overtime
        out["cost"] += p.cost
        if m.pref > 0:
            out["preference"] += m.pref * max(p.end - p.setup_start, 0)
        b = base.get(p.op) if base else None
        if b is not None:
            b_res, _b_ss, b_start, _b_end = b
            out["stability"] += abs(p.start - b_start) + (stab_pen if b_res != p.res else 0)
    out["makespan"] = float(max_end - cp.as_of)
    return out


def scales(ref: dict[str, float]) -> dict[str, float]:
    return {k: max(abs(ref.get(k, 0.0)), SCALE_FLOOR[k]) for k in COMPONENTS}


def weighted(spec: ObjectiveSpec, comp: dict[str, float], scale: dict[str, float]) -> float:
    total = 0.0
    for k, w in spec.weights.items():
        if w:
            total += w * comp.get(k, 0.0) / scale[k]
    return total


def levels_of(spec: ObjectiveSpec) -> list[list[str]]:
    return [list(lvl) for lvl in spec.levels] if spec.levels else DEFAULT_LEVELS


def vector(spec: ObjectiveSpec, comp: dict[str, float], scale: dict[str, float]) -> list[float]:
    """Objective vector. Position 0 is always the number of unscheduled operations."""
    if spec.mode == "LEXICOGRAPHIC":
        return [comp["unscheduled"]] + [sum(comp[c] for c in lvl) for lvl in levels_of(spec)]
    return [comp["unscheduled"], weighted(spec, comp, scale)]


def better(a: Sequence[float], b: Sequence[float], tol: float = 0.0) -> bool:
    """True if vector a is strictly better than b (lexicographic, relative tolerance per level)."""
    for x, y in zip(a, b, strict=False):
        margin = max(abs(y) * tol, 1e-9)
        if x < y - margin:
            return True
        if x > y + margin:
            return False
    return False


def scalar(vec: Sequence[float]) -> float:
    """Single number for reporting (unscheduled dominates)."""
    if not vec:
        return 0.0
    if len(vec) == 2:
        return vec[0] * 1e6 + vec[1]
    return vec[0] * 1e6 + vec[1]
