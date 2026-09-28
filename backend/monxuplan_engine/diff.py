"""Comparison of two solutions (impact preview, rescheduling, scenario comparison)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .contract import Solution

KPI_DIRECTIONS = {
    # +1 = higher is better, -1 = lower is better
    "otif": 1,
    "on_time_delivery": 1,
    "late_orders": -1,
    "average_delay_h": -1,
    "maximum_delay_h": -1,
    "orders_unscheduled": -1,
    "setup_h": -1,
    "overtime_h": -1,
    "utilization": 1,
    "lead_time_avg_h": -1,
    "wip_avg_orders": -1,
    "throughput_units": 1,
    "material_shortages": -1,
    "constraint_violations_hard": -1,
    "cost_total": -1,
}


def compare_solutions(a: Solution, b: Solution) -> dict[str, Any]:
    """Differences from plan ``a`` (before) to plan ``b`` (after)."""
    orders = []
    a_orders = {o.order_id: o for o in a.orders}
    for ob in b.orders:
        oa = a_orders.get(ob.order_id)
        if oa is None:
            orders.append({"order_id": ob.order_id, "number": ob.number, "before": None, "after": _iso(ob.end), "delta_minutes": None, "status_before": None, "status_after": ob.status})
            continue
        delta = None
        if oa.end is not None and ob.end is not None:
            delta = int((ob.end - oa.end).total_seconds() // 60)
        if delta or oa.status != ob.status:
            orders.append(
                {
                    "order_id": ob.order_id,
                    "number": ob.number,
                    "before": _iso(oa.end),
                    "after": _iso(ob.end),
                    "delta_minutes": delta,
                    "status_before": oa.status,
                    "status_after": ob.status,
                    "due": _iso(ob.due),
                }
            )
    orders.sort(key=lambda r: -(abs(r["delta_minutes"]) if r["delta_minutes"] is not None else 10**9))

    kpis = {}
    for k in sorted(set(a.kpis) | set(b.kpis)):
        va, vb = a.kpis.get(k), b.kpis.get(k)
        if isinstance(va, int | float) and isinstance(vb, int | float):
            d = round(vb - va, 4)
            direction = KPI_DIRECTIONS.get(k, 0)
            kpis[k] = {"before": va, "after": vb, "delta": d, "better": (d * direction > 0) if direction and d else None}
        else:
            kpis[k] = {"before": va, "after": vb, "delta": None, "better": None}

    sa = {s.op_id: s for s in a.schedule}
    moved, res_changed = [], []
    for s in b.schedule:
        o = sa.get(s.op_id)
        if o is None:
            continue
        shift = int((s.start - o.start).total_seconds() // 60)
        if o.resource_id != s.resource_id:
            res_changed.append({"op_id": s.op_id, "from": o.resource_id, "to": s.resource_id, "shift_minutes": shift})
        elif shift:
            moved.append({"op_id": s.op_id, "resource_id": s.resource_id, "shift_minutes": shift})

    def prev_map(sol: Solution) -> dict[str, str | None]:
        by_res = defaultdict(list)
        for s in sol.schedule:
            by_res[s.resource_id].append((s.start, s.op_id))
        out = {}
        for lst in by_res.values():
            lst.sort()
            for k, (_t, op) in enumerate(lst):
                out[op] = lst[k - 1][1] if k else None
        return out

    pa, pb = prev_map(a), prev_map(b)
    seq_changes = sum(1 for op, prev in pb.items() if op in pa and pa[op] != prev)

    def hard_keys(sol: Solution):
        return {(v.type, v.op_id, v.resource_id, v.material_id) for v in sol.violations if v.hardness == "HARD" and not v.type.startswith("DATA_")}

    new_v = hard_keys(b) - hard_keys(a)
    resolved_v = hard_keys(a) - hard_keys(b)
    new_violations = [v.model_dump(mode="json") for v in b.violations if (v.type, v.op_id, v.resource_id, v.material_id) in new_v]
    setup_a = sum(s.setup_minutes for s in a.schedule)
    setup_b = sum(s.setup_minutes for s in b.schedule)
    ot_a = sum(s.overtime_minutes for s in a.schedule)
    ot_b = sum(s.overtime_minutes for s in b.schedule)
    shifts = [abs(m["shift_minutes"]) for m in moved + res_changed]
    return {
        "orders": orders,
        "orders_delayed": sum(1 for r in orders if (r["delta_minutes"] or 0) > 0),
        "orders_advanced": sum(1 for r in orders if (r["delta_minutes"] or 0) < 0),
        "kpis": kpis,
        "operations_moved": len(moved) + len(res_changed),
        "resource_changes": res_changed[:200],
        "moved": sorted(moved, key=lambda m: -abs(m["shift_minutes"]))[:200],
        "average_shift_minutes": round(sum(shifts) / len(shifts), 1) if shifts else 0.0,
        "sequence_changes": seq_changes,
        "setup_delta_minutes": setup_b - setup_a,
        "overtime_delta_minutes": ot_b - ot_a,
        "new_hard_violations": new_violations[:100],
        "new_hard_violation_count": len(new_v),
        "resolved_hard_violation_count": len(resolved_v),
        "feasible_before": a.feasible,
        "feasible_after": b.feasible,
    }


def _iso(dt):
    return dt.isoformat() if dt is not None else None
