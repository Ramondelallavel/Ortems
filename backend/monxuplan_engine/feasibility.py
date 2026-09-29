"""Fast feasibility check before optimising: "Can we meet all orders?".

Two lower bounds, both cheap and both honest about what they are:

1. **Lead-time bound** (per order): earliest completion at infinite capacity, respecting calendars,
   releases, precedences and material supply → orders whose due date is impossible whatever the
   sequence (reason: lead time / material / calendar).
2. **Rough-cut capacity bound** (per resource group of interchangeable resources): for every due-date
   checkpoint, the work that must be done before it (EDD cumulative requirement) versus the capacity
   available until then → capacity shortages, the critical resource and the additional hours needed.

Result (``check: LOWER_BOUND``): ``NO_BOUND_VIOLATED`` (no bound rules the orders out — this is *not*
a statement that the detailed schedule is feasible or on time; ``detailed_schedule_feasible`` is
always null here), ``PARTIALLY`` or ``NO`` (proofs: those orders cannot be on time) with the primary
and secondary reasons.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .compile import CompiledProblem
from .timing import Timing, compute_timing


def check(cp: CompiledProblem, timing: Timing | None = None) -> dict[str, Any]:
    tm = timing or compute_timing(cp)
    impossible: list[dict[str, Any]] = []
    for o in cp.orders:
        eft = tm.order_eft[o.idx]
        if not o.ops:
            continue
        if eft is None:
            impossible.append(
                {
                    "order_id": o.id,
                    "number": o.number,
                    "requested": cp.dt(o.due).isoformat(),
                    "earliest_achievable": None,
                    "reason": "NO_FEASIBLE_ROUTE",
                    "constraint": _no_route_reason(cp, o),
                    "required_additional_capacity_h": None,
                }
            )
        elif eft > o.due:
            limit = tm.order_limit[o.idx] or "LEAD_TIME"
            impossible.append(
                {
                    "order_id": o.id,
                    "number": o.number,
                    "requested": cp.dt(o.due).isoformat(),
                    "earliest_achievable": cp.dt(eft).isoformat(),
                    "delay_minutes": eft - o.due,
                    "reason": {"MATERIAL": "MATERIAL", "RELEASE": "RELEASE_DATE", "START": "LEAD_TIME", "PREDECESSOR": "LEAD_TIME", "FIXED": "FROZEN"}.get(limit, limit),
                    "constraint": _critical_constraint(cp, tm, o),
                    "required_additional_capacity_h": None,
                }
            )

    # ---- rough-cut capacity by group of interchangeable resources
    groups: dict[frozenset, list[int]] = defaultdict(list)
    for op in cp.ops:
        if op.fixed is not None or not op.modes:
            continue
        key = frozenset(m.res for m in op.modes if not m.sub)
        if key:
            groups[key].append(op.idx)
    shortages: list[dict[str, Any]] = []
    for key, ops in groups.items():
        ops.sort(key=lambda i: cp.orders[cp.ops[i].order].due)
        cum = 0
        worst = None
        for i in ops:
            op = cp.ops[i]
            cum += min(m.nominal for m in op.modes if m.res in key)
            due = cp.orders[op.order].due
            cap = sum(cp.resources[r].cal.working_between(cp.work_lb, due) * (1 if cp.resources[r].unary else max(cp.resources[r].capacity, 1)) for r in key)
            if cum > cap:
                deficit = cum - cap
                if worst is None or deficit > worst[0]:
                    worst = (deficit, due, i)
        if worst is not None:
            deficit, due, i = worst
            codes = sorted(cp.resources[r].code for r in key)
            late_orders = {cp.orders[cp.ops[j].order].id for j in ops if cp.orders[cp.ops[j].order].due <= due}
            shortages.append(
                {
                    "resources": codes,
                    "checkpoint": cp.dt(due).isoformat(),
                    "required_h": None,
                    "deficit_h": round(deficit / 60.0, 1),
                    "orders_at_risk": len(late_orders),
                }
            )
    shortages.sort(key=lambda s: -s["deficit_h"])
    impossible_ids = {x["order_id"] for x in impossible}
    # attach capacity requirement to impossible orders when their resources are short
    for x in impossible:
        x["required_additional_capacity_h"] = None
    reasons = defaultdict(int)
    for x in impossible:
        reasons[x["reason"]] += 1
    if shortages:
        reasons["CAPACITY"] += sum(s["orders_at_risk"] for s in shortages)
    ranked = sorted(reasons.items(), key=lambda kv: -kv[1])
    n_orders = sum(1 for o in cp.orders if o.ops)
    if not impossible and not shortages:
        # not "yes, feasible": only that no lower bound rules the orders out
        status = "NO_BOUND_VIOLATED"
    elif len(impossible_ids) >= n_orders and n_orders:
        status = "NO"
    else:
        status = "PARTIALLY"
    crit_issues = [i for i in cp.issues if i.severity == "CRITICAL"]
    return {
        "check": "LOWER_BOUND",
        "status": status,
        # a lower-bound check never proves that a finite-capacity schedule meets every due date: only
        # a detailed schedule (a planning run) and its validation can say that
        "detailed_schedule_feasible": None,
        "orders": n_orders,
        "orders_impossible": len(impossible_ids),
        "primary_reason": ranked[0][0] if ranked else None,
        "secondary_reason": ranked[1][0] if len(ranked) > 1 else None,
        "critical_resource": shortages[0]["resources"][0] if shortages and len(shortages[0]["resources"]) == 1 else (" / ".join(shortages[0]["resources"]) if shortages else None),
        "capacity_shortages": shortages[:20],
        "impossible_orders": sorted(impossible, key=lambda x: -(x.get("delay_minutes") or 10**9))[:200],
        "critical_data_issues": len(crit_issues),
        "note": "Lower bounds only (infinite-capacity lead times and rough-cut capacity per group). NO_BOUND_VIOLATED is not a guarantee: the detailed finite-capacity schedule may still be late because of sequencing, setups, labour, tools or materials. NO / PARTIALLY are proofs: those orders cannot be on time.",
    }


def _no_route_reason(cp: CompiledProblem, o) -> str:
    for i in o.ops:
        op = cp.ops[i]
        if not op.modes:
            return f"{op.id}: no compatible resource"
        if all(m.cal.total_minutes == 0 for m in op.modes):
            return f"{op.id}: no resource with working time"
    return "no feasible route within the calendars"


def _critical_constraint(cp: CompiledProblem, tm: Timing, o) -> str | None:
    crit = max(o.ops, key=lambda i: tm.eft[i])
    for _ in range(100):
        preds = [p for p, *_ in cp.ops[crit].preds]
        if not preds:
            break
        nxt = max(preds, key=lambda p: tm.eft[p])
        if tm.eft[nxt] + 1 < tm.est[crit]:
            break
        crit = nxt
    op = cp.ops[crit]
    m = tm.est_mode[crit]
    res = cp.resources[op.modes[m].res].code if m >= 0 else None
    return f"{op.id}" + (f" on {res}" if res else "")
