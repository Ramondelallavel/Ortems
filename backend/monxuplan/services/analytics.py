"""Analytics: capacity load & heatmap, KPI drill-down, bottlenecks, plan/scenario comparison, trends."""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from monxuplan_engine.capacity import load_profile
from monxuplan_engine.diff import compare_solutions

from ..core.errors import NotFound, ValidationFailed
from ..models import KpiValue, Plan, Scenario
from .context import Ctx
from .engine_view import replay
from .planning import solution_from_plan
from .views import _get_plan, plan_summary

KPI_CATALOGUE: list[dict[str, Any]] = [
    {"code": "otif", "label": "OTIF", "unit": "%", "group": "Customer service", "better": "up"},
    {"code": "on_time_delivery", "label": "On-time delivery", "unit": "%", "group": "Customer service", "better": "up"},
    {"code": "late_orders", "label": "Late orders", "unit": "", "group": "Customer service", "better": "down"},
    {"code": "average_delay_h", "label": "Average delay", "unit": "h", "group": "Customer service", "better": "down"},
    {"code": "maximum_delay_h", "label": "Maximum delay", "unit": "h", "group": "Customer service", "better": "down"},
    {"code": "past_due_orders", "label": "Past-due orders", "unit": "", "group": "Customer service", "better": "down"},
    {"code": "orders_unscheduled", "label": "Unscheduled orders", "unit": "", "group": "Customer service", "better": "down"},
    {"code": "throughput_units", "label": "Throughput (horizon)", "unit": "units", "group": "Production", "better": "up"},
    {"code": "lead_time_avg_h", "label": "Lead time", "unit": "h", "group": "Production", "better": "down"},
    {"code": "cycle_time_avg_h", "label": "Cycle time", "unit": "h", "group": "Production", "better": "down"},
    {"code": "wip_avg_orders", "label": "WIP (avg orders)", "unit": "", "group": "Production", "better": "down"},
    {"code": "setup_h", "label": "Setup time", "unit": "h", "group": "Production", "better": "down"},
    {"code": "queue_time_avg_h", "label": "Queue time", "unit": "h", "group": "Production", "better": "down"},
    {"code": "utilization", "label": "Utilisation", "unit": "%", "group": "Resources", "better": "up"},
    {"code": "capacity_h", "label": "Capacity", "unit": "h", "group": "Resources", "better": None},
    {"code": "idle_h", "label": "Idle time", "unit": "h", "group": "Resources", "better": "down"},
    {"code": "overtime_h", "label": "Overtime", "unit": "h", "group": "Resources", "better": "down"},
    {"code": "material_shortages", "label": "Material shortages", "unit": "", "group": "Materials", "better": "down"},
    {"code": "material_delayed_operations", "label": "Operations delayed by material", "unit": "", "group": "Materials", "better": "down"},
    {"code": "inventory_risk_materials", "label": "Materials below safety stock", "unit": "", "group": "Materials", "better": "down"},
    {"code": "constraint_violations_hard", "label": "Hard violations", "unit": "", "group": "Plan quality", "better": "down"},
    {"code": "constraint_violations_soft", "label": "Soft deviations", "unit": "", "group": "Plan quality", "better": "down"},
    {"code": "frozen_plan_changes", "label": "Frozen zone changes", "unit": "", "group": "Plan quality", "better": "down"},
    {"code": "schedule_stability_ops_moved", "label": "Operations moved vs baseline", "unit": "", "group": "Plan quality", "better": "down"},
    {"code": "schedule_stability_avg_shift_h", "label": "Average shift vs baseline", "unit": "h", "group": "Plan quality", "better": "down"},
    {"code": "cost_total", "label": "Planned cost", "unit": "€", "group": "Cost", "better": "down"},
    {"code": "energy_kwh", "label": "Energy", "unit": "kWh", "group": "Sustainability", "better": "down"},
    {"code": "co2_kg", "label": "CO₂ estimate", "unit": "kg", "group": "Sustainability", "better": "down"},
]


def capacity(s: Session, ctx: Ctx, plan_id: uuid.UUID, bucket: str = "day", group_by: str = "resource", start=None, end=None) -> dict[str, Any]:
    if bucket not in ("hour", "shift", "day", "week", "month"):
        raise ValidationFailed("bucket must be hour, shift, day, week or month")
    plan = _get_plan(s, ctx, plan_id)
    cp, res = replay(s, plan)
    a = cp.axis.to_min(start) if start else None
    b = cp.axis.to_min(end) if end else None
    bks, loads = load_profile(cp, res.placements, res.timing, bucket, a, b)
    rows = []
    for rl in loads:
        r = cp.resources[rl.resource]
        if r.kind in ("SUBCONTRACTOR", "STORAGE", "TRANSPORT"):
            continue
        rows.append({"id": r.id, "code": r.code, "name": r.name, "kind": r.kind, "area": r.area, "groups": r.groups, "buckets": rl.buckets, "capacity": rl.capacity, "scheduled": rl.scheduled, "requirement": rl.requirement})
    if group_by in ("group", "area", "plant"):
        agg: dict[str, dict[str, Any]] = {}
        for r in rows:
            if r["kind"] in ("LABOR_POOL", "TOOL"):
                continue
            keys = r["groups"] if group_by == "group" else [r["area"] or "—"] if group_by == "area" else ["PLANT"]
            for k in keys or ["(no group)"]:
                g = agg.setdefault(k, {"id": k, "code": k, "name": k, "kind": group_by.upper(), "buckets": [dict(bk, capacity=0, scheduled=0, requirement=0) for bk in r["buckets"]], "capacity": 0, "scheduled": 0, "requirement": 0})
                for gb, rb in zip(g["buckets"], r["buckets"], strict=True):
                    for f in ("capacity", "scheduled", "requirement"):
                        gb[f] += rb[f]
                for f in ("capacity", "scheduled", "requirement"):
                    g[f] += r[f]
        from monxuplan_engine.capacity import heat_state

        for g in agg.values():
            for gb in g["buckets"]:
                c = gb["capacity"]
                gb["overload"] = max(0, gb["requirement"] - c)
                gb["available"] = max(0, c - gb["scheduled"])
                gb["utilization"] = gb["scheduled"] / c if c else None
                gb["requirement_utilization"] = gb["requirement"] / c if c else None
                gb["state"] = heat_state(c, gb["scheduled"], gb["requirement"])
        rows = sorted(agg.values(), key=lambda g: g["code"])
    return {
        "plan_id": str(plan.id),
        "bucket": bucket,
        "group_by": group_by,
        "buckets": [{"label": bk.label, "start": cp.dt(bk.start).isoformat(), "end": cp.dt(bk.end).isoformat()} for bk in bks],
        "rows": rows,
        "states": ["UNDERLOADED", "BALANCED", "HIGH_LOAD", "OVERLOADED", "UNAVAILABLE"],
    }


def kpis(s: Session, ctx: Ctx, plan_id: uuid.UUID) -> dict[str, Any]:
    ctx.require("analytics:read")
    plan = _get_plan(s, ctx, plan_id)
    values = plan.kpis or {}
    return {"plan": plan_summary(plan), "catalogue": KPI_CATALOGUE, "values": values}


def drilldown(s: Session, ctx: Ctx, plan_id: uuid.UUID, code: str) -> dict[str, Any]:
    ctx.require("analytics:read")
    plan = _get_plan(s, ctx, plan_id)
    d = plan.kpi_details or {}
    orders = (plan.analysis or {}).get("orders", [])
    if code in ("otif", "on_time_delivery", "late_orders", "average_delay_h", "maximum_delay_h", "orders_unscheduled"):
        late = d.get("late_orders", [])
        by_cat = Counter((x.get("cause") or {}).get("category", "Unknown") for x in late)
        by_res = Counter((x.get("cause") or {}).get("resource") for x in late if (x.get("cause") or {}).get("resource"))
        total = sum(1 for o in orders if o["status"] != "COMPLETED")
        return {
            "code": code,
            "value": (plan.kpis or {}).get(code),
            "levels": [
                {"label": "Orders", "value": total},
                {"label": "Late or unscheduled", "value": len(late), "share": round(len(late) / total, 4) if total else None},
            ],
            "breakdown": d.get("otif_breakdown", {}),
            "causes": [{"category": k, "orders": v, "share": round(v / len(late), 4) if late else 0} for k, v in by_cat.most_common()],
            "resources": [{"resource": k, "orders": v} for k, v in by_res.most_common(10)],
            "orders": late[:500],
        }
    if code in ("utilization", "capacity_h", "idle_h"):
        return {"code": code, "value": (plan.kpis or {}).get(code), "resources": [{"resource_id": k, **v} for k, v in sorted(d.get("resources", {}).items(), key=lambda kv: -(kv[1].get("utilization") or 0))]}
    if code in ("material_shortages", "material_delayed_operations", "inventory_risk_materials"):
        unsched = [u for u in (plan.analysis or {}).get("unscheduled", []) if u["reason"] == "MATERIAL_SHORTAGE"]
        return {"code": code, "value": (plan.kpis or {}).get(code), "materials": d.get("material_shortages", []), "inventory_risk": d.get("inventory_risk", []), "operations": unsched[:300]}
    if code in ("setup_h",):
        sched = solution_from_plan(s, plan).schedule
        per_res: dict[str, int] = defaultdict(int)
        for x in sched:
            per_res[x.resource_id] += x.setup_minutes
        return {"code": code, "value": (plan.kpis or {}).get(code), "resources": [{"resource_id": k, "setup_h": round(v / 60, 2)} for k, v in sorted(per_res.items(), key=lambda kv: -kv[1])]}
    if code.startswith("schedule_stability"):
        return {"code": code, "value": (plan.kpis or {}).get(code), "stability": d.get("stability", {})}
    return {"code": code, "value": (plan.kpis or {}).get(code), "details": None}


def bottlenecks(s: Session, ctx: Ctx, plan_id: uuid.UUID) -> dict[str, Any]:
    ctx.require("analytics:read")
    plan = _get_plan(s, ctx, plan_id)
    return {
        "plan_id": str(plan.id),
        "ranking_basis": "overload minutes, then induced waiting minutes, then orders affected, then utilisation — measured values, no synthetic score",
        "bottlenecks": (plan.analysis or {}).get("bottlenecks", []),
    }


def compare_plans(s: Session, ctx: Ctx, plan_ids: list[uuid.UUID]) -> dict[str, Any]:
    ctx.require("analytics:read")
    if not 2 <= len(plan_ids) <= 6:
        raise ValidationFailed("Select between 2 and 6 plans to compare")
    plans = []
    for pid in plan_ids:
        plans.append(_get_plan(s, ctx, pid))
    base = plans[0]
    base_sol = solution_from_plan(s, base)
    table = []
    for k in KPI_CATALOGUE:
        row = {"code": k["code"], "label": k["label"], "unit": k["unit"], "better": k["better"], "values": [p.kpis.get(k["code"]) if p.kpis else None for p in plans]}
        table.append(row)
    diffs = []
    for p in plans[1:]:
        cmpd = compare_solutions(base_sol, solution_from_plan(s, p))
        cmpd.pop("kpis", None)
        diffs.append({"plan_id": str(p.id), "number": p.number, **cmpd})
    scen = {sc.id: sc for sc in s.scalars(select(Scenario).where(Scenario.id.in_({p.scenario_id for p in plans})))}
    return {
        "plans": [{**plan_summary(p), "scenario_name": scen[p.scenario_id].name if p.scenario_id in scen else None, "bottlenecks": (p.analysis or {}).get("bottlenecks", [])[:5]} for p in plans],
        "kpis": table,
        "diffs_vs_first": diffs,
    }


def trends(s: Session, ctx: Ctx, plant_id: uuid.UUID, codes: list[str], limit: int = 30) -> dict[str, Any]:
    ctx.require("analytics:read")
    plans = list(s.scalars(select(Plan).where(Plan.plant_id == plant_id).order_by(Plan.created_at.desc()).limit(limit)))
    plans.reverse()
    vals = defaultdict(dict)
    if plans:
        for kv in s.scalars(select(KpiValue).where(KpiValue.plan_id.in_([p.id for p in plans]), KpiValue.code.in_(codes))):
            vals[kv.plan_id][kv.code] = kv.value
    return {"points": [{"plan_id": str(p.id), "number": p.number, "status": p.status, "created_at": p.created_at.isoformat(), **vals.get(p.id, {})} for p in plans]}


def get_plan_or_404(s: Session, plan_id: uuid.UUID) -> Plan:
    p = s.get(Plan, plan_id)
    if p is None:
        raise NotFound("Plan not found")
    return p
