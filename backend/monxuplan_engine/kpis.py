"""KPI engine.

KPIs are computed from the schedule and the problem only (never estimated elsewhere) and come with
drill-down details so that every number in the UI can be clicked through to the orders, operations
and causes behind it.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .builder import BuildResult
from .validator import ValidationResult

H = 60.0


def order_status(result: BuildResult) -> list[dict[str, Any]]:
    cp = result.cp
    rows = []
    for o in cp.orders:
        placed = [result.placements[i] for i in o.ops if result.placements[i] is not None]
        missing = [i for i in o.ops if result.placements[i] is None]
        c = result.order_completion(o.idx)
        start = min((p.setup_start for p in placed), default=None)
        if not o.ops:
            status = "COMPLETED"
        elif missing and not placed:
            status = "UNSCHEDULED"
        elif missing:
            status = "PARTIAL"
        elif c is not None and c > o.due:
            status = "LATE"
        else:
            status = "ON_TIME"
        mat_status = "NONE"
        if any(cp.ops[i].materials for i in o.ops):
            mat_status = "OK"
            if any(result.unscheduled.get(i) and result.unscheduled[i].reason == "MATERIAL_SHORTAGE" for i in missing) or any(p.shortage for p in placed):
                mat_status = "SHORTAGE"
            elif any(p.mat_wait > 0 for p in placed):
                mat_status = "LATE_SUPPLY" if status == "LATE" else "RISK"
        rows.append(
            {
                "order": o.idx,
                "status": status,
                "start": start,
                "end": c,
                "lateness": max(0, c - o.due) if c is not None else None,
                "material_status": mat_status,
            }
        )
    return rows


def compute(result: BuildResult, validation: ValidationResult, orders: list[dict[str, Any]], stability: dict | None = None) -> tuple[dict[str, float | None], dict[str, Any]]:
    cp = result.cp
    k: dict[str, float | None] = {}
    d: dict[str, Any] = {}
    open_orders = [r for r in orders if r["status"] != "COMPLETED"]
    n = len(open_orders)
    on_time = [r for r in open_orders if r["status"] == "ON_TIME"]
    late = [r for r in open_orders if r["status"] == "LATE"]
    uns = [r for r in open_orders if r["status"] in ("UNSCHEDULED", "PARTIAL")]
    in_full = [r for r in on_time if r["material_status"] != "SHORTAGE"]
    k["orders_total"] = n
    k["orders_scheduled"] = n - len(uns)
    k["orders_unscheduled"] = len(uns)
    k["on_time_delivery"] = round(100.0 * len(on_time) / n, 2) if n else None
    k["otif"] = round(100.0 * len(in_full) / n, 2) if n else None
    k["late_orders"] = len(late)
    delays = [r["lateness"] for r in late]
    k["average_delay_h"] = round(sum(delays) / len(delays) / H, 2) if delays else 0.0
    k["maximum_delay_h"] = round(max(delays) / H, 2) if delays else 0.0
    k["past_due_orders"] = sum(1 for r in open_orders if cp.orders[r["order"]].due < cp.as_of)
    k["critical_orders_late"] = sum(1 for r in late if cp.orders[r["order"]].critical)

    # production
    placed = [p for p in result.placements if p is not None]
    horizon_days = max((cp.h_end - cp.as_of) / 1440.0, 1e-9)
    completed_in_horizon = [r for r in open_orders if r["end"] is not None and r["end"] <= cp.h_end and r["status"] in ("ON_TIME", "LATE")]
    k["throughput_units"] = round(sum(cp.orders[r["order"]].qty for r in completed_in_horizon), 3)
    k["throughput_orders_per_day"] = round(len(completed_in_horizon) / horizon_days, 3)
    lead = [(r["end"] - r["start"]) for r in open_orders if r["end"] is not None and r["start"] is not None]
    k["lead_time_avg_h"] = round(sum(lead) / len(lead) / H, 2) if lead else None
    proc: dict[int, int] = defaultdict(int)
    for p in placed:
        proc[result.cp.ops[p.op].order] += p.end - p.setup_start
    cycle = [proc[r["order"]] for r in open_orders if r["end"] is not None]
    k["cycle_time_avg_h"] = round(sum(cycle) / len(cycle) / H, 2) if cycle else None
    k["wip_avg_orders"] = round(sum(lead) / max(cp.h_end - cp.as_of, 1), 2) if lead else 0.0
    k["setup_h"] = round(sum(p.setup for p in placed) / H, 2)
    queue = []
    for r in open_orders:
        if r["end"] is None or r["start"] is None:
            continue
        queue.append(max(0, (r["end"] - r["start"]) - proc[r["order"]]))
    k["queue_time_avg_h"] = round(sum(queue) / len(queue) / H, 2) if queue else None

    # resources
    cap_total = busy_total = 0
    per_res: dict[str, dict[str, float]] = {}
    busy_by_res: dict[int, int] = defaultdict(int)
    for p in placed:  # one pass over the schedule (not one per resource)
        m = cp.ops[p.op].modes[p.mode]
        busy_by_res[p.res] += m.cal.working_between(max(p.setup_start, cp.as_of), min(p.end, cp.h_end))
    for r in cp.resources:
        if not r.finite or r.kind in ("LABOR_POOL", "TOOL"):
            continue
        cap = r.cal.working_between(cp.as_of, cp.h_end) * (1 if r.unary else max(r.capacity, 1))
        busy = busy_by_res.get(r.idx, 0)
        cap_total += cap
        busy_total += busy
        per_res[r.id] = {"capacity_h": round(cap / H, 2), "busy_h": round(busy / H, 2), "utilization": round(100.0 * busy / cap, 2) if cap else None}
    k["capacity_h"] = round(cap_total / H, 2)
    k["utilization"] = round(100.0 * busy_total / cap_total, 2) if cap_total else None
    k["idle_h"] = round(max(cap_total - busy_total, 0) / H, 2)
    k["overtime_h"] = round(sum(p.overtime for p in placed) / H, 2)
    d["resources"] = per_res

    # materials
    shortage_ops = [i for i, u in result.unscheduled.items() if u.reason == "MATERIAL_SHORTAGE"]
    short_mats: set[str] = set()
    for i in shortage_ops:
        for m in result.unscheduled[i].details.get("materials", []):
            short_mats.add(m["material_id"])
    for v in validation.violations:
        if v.type == "MATERIAL_SHORTAGE" and v.mat is not None:
            short_mats.add(cp.materials[v.mat].id)
    k["material_shortages"] = len(short_mats)
    k["material_delayed_operations"] = sum(1 for p in placed if p.mat_wait > 0)
    risk = []
    for m in cp.materials:
        acc = result.ledger.accounts[m.idx]
        if not acc.events or m.safety_stock <= 0:
            continue
        lvl, _t = acc.min_level()
        if lvl < m.safety_stock:
            risk.append(m.id)
    k["inventory_risk_materials"] = len(risk)
    d["material_shortages"] = sorted(short_mats)
    d["inventory_risk"] = risk

    # plan quality
    hard = [v for v in validation.violations if v.hardness == "HARD" and not v.type.startswith("DATA_") and v.type != "UNSCHEDULED"]
    soft = [v for v in validation.violations if v.hardness == "SOFT" and not v.type.startswith("DATA_")]
    k["constraint_violations_hard"] = len(hard)
    k["constraint_violations_soft"] = len(soft)
    k["frozen_plan_changes"] = sum(1 for v in validation.violations if v.type == "FROZEN_CHANGED")
    if stability:
        k["schedule_stability_ops_moved"] = stability.get("operations_moved")
        k["schedule_stability_avg_shift_h"] = stability.get("average_shift_h")
        k["schedule_stability_resource_changes"] = stability.get("changed_resources")
        k["schedule_stability_sequence_changes"] = stability.get("changed_sequences")
    k["cost_total"] = round(sum(p.cost for p in placed), 2)
    energy = 0.0
    co2 = 0.0
    for p in placed:
        r = cp.resources[p.res]
        if r.energy_kw:
            kwh = r.energy_kw * (p.end - p.setup_start) / H
            energy += kwh
            co2 += kwh * r.co2
    k["energy_kwh"] = round(energy, 1)
    k["co2_kg"] = round(co2, 1)

    # drill-down: late orders with first cause
    late_detail = []
    cause_counter: Counter[str] = Counter()
    for r in late + uns:
        o = cp.orders[r["order"]]
        cause = _first_cause(result, r["order"])
        cause_counter[cause["category"]] += 1
        late_detail.append(
            {
                "order_id": o.id,
                "number": o.number,
                "status": r["status"],
                "lateness_minutes": r["lateness"],
                "due": cp.dt(o.due).isoformat(),
                "end": cp.dt(r["end"]).isoformat() if r["end"] is not None else None,
                "cause": cause,
            }
        )
    d["late_orders"] = sorted(late_detail, key=lambda x: -(x["lateness_minutes"] or 10**9))
    d["late_causes"] = dict(cause_counter)
    d["otif_breakdown"] = {
        "on_time_in_full": len(in_full),
        "on_time_material_shortage": len(on_time) - len(in_full),
        "late": len(late),
        "unscheduled": len(uns),
    }
    return k, d


def _first_cause(result: BuildResult, oi: int) -> dict[str, Any]:
    """Classify the primary reason for an order's lateness: Machine, Material, Labor, Tool,
    Calendar, Capacity(infinite lower bound), Data."""
    cp = result.cp
    o = cp.orders[oi]
    missing = [i for i in o.ops if result.placements[i] is None]
    if missing:
        u = result.unscheduled.get(missing[0])
        reason = u.reason if u else "UNSCHEDULED"
        cat = "Material" if reason == "MATERIAL_SHORTAGE" else "Data" if reason in ("NO_COMPATIBLE_RESOURCE", "PRECEDENCE_CYCLE") else "Capacity"
        return {"category": cat, "code": reason, "text": u.message if u else "not scheduled"}
    eft = result.timing.order_eft[oi]
    if eft is not None and eft > o.due:
        lim = result.timing.order_limit[oi] or "LEAD_TIME"
        return {
            "category": "Material" if lim == "MATERIAL" else "Lead time",
            "code": "DUE_DATE_IMPOSSIBLE",
            "text": f"Even at infinite capacity the order cannot finish before {cp.dt(eft).isoformat()} ({lim.lower()})",
        }
    # walk the critical chain: the largest real wait along it is the primary cause
    cur = max(o.last_ops, key=lambda i: result.placements[i].end)
    best = None
    seen = set()
    for _ in range(60):
        if cur in seen:
            break
        seen.add(cur)
        pl = result.placements[cur]
        b = pl.binding
        if b is not None and b.type in ("RESOURCE", "SETUP", "LABOR", "TOOL", "CALENDAR", "MATERIAL") and b.wait > 0 and (best is None or b.wait > best[0].wait):
            best = (b, cur)
        nb = pl.lb_src if (b is not None and b.type in ("RESOURCE", "SETUP", "LABOR", "TOOL", "CALENDAR")) else b
        if nb is not None and nb.type == "MATERIAL" and nb.wait > 0 and (best is None or nb.wait > best[0].wait):
            best = (nb, cur)
        if nb is not None and nb.type == "PREDECESSOR" and nb.ref in cp.op_index and result.placements[cp.op_index[nb.ref]] is not None:
            cur = cp.op_index[nb.ref]
            continue
        break
    if best is not None:
        b, i = best
        cat = {"RESOURCE": "Machine", "SETUP": "Machine", "MATERIAL": "Material", "LABOR": "Labor", "TOOL": "Tool", "CALENDAR": "Calendar"}[b.type]
        res = cp.resources[result.placements[i].res]
        return {"category": cat, "code": b.type, "text": f"{cp.ops[i].id} on {res.code}: {b.detail or b.type.lower()} ({b.wait // 60} h waiting)", "resource": res.code, "op_id": cp.ops[i].id}
    return {"category": "Priority", "code": "SEQUENCE", "text": "sequencing decision"}
