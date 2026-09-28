"""Read models for the UI: plan headers, Gantt windows, operation/order detail, constraint explorer,
dispatch lists, supervisor and operator views, plan vs actual."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from monxuplan_engine.explain import check_position, explore_operation

from ..core.clock import now
from ..core.errors import NotFound, ValidationFailed
from ..models import (
    ActualProduction,
    Alert,
    ConstraintViolation,
    Customer,
    Item,
    Plan,
    PlanningArea,
    PlanningRun,
    Plant,
    ProductFamily,
    ProductionOrder,
    ProductionOrderOperation,
    Resource,
    ResourceGroup,
    ResourceGroupMember,
    RoutingOperation,
    Scenario,
    ScheduledOperation,
)
from .context import Ctx
from .engine_view import replay
from .masterdata import row_dict, to_json
from .planning import get_scenario


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _get_plan(s: Session, ctx: Ctx, plan_id: uuid.UUID) -> Plan:
    ctx.require("plan:read")
    plan = s.get(Plan, plan_id)
    if plan is None:
        raise NotFound("Plan not found", code="PLAN_NOT_FOUND")
    ctx.require_plant(plan.plant_id)
    return plan


def plan_summary(plan: Plan) -> dict[str, Any]:
    md = plan.solver_metadata or {}
    return {
        "id": str(plan.id),
        "number": plan.number,
        "version_no": plan.version_no,
        "version": plan.version,
        "kind": plan.kind,
        "status": plan.status,
        "feasible": plan.feasible,
        "scenario_id": str(plan.scenario_id),
        "plant_id": str(plan.plant_id),
        "parent_id": str(plan.parent_id) if plan.parent_id else None,
        "horizon_start": to_json(_aware(plan.horizon_start)),
        "horizon_end": to_json(_aware(plan.horizon_end)),
        "frozen_until": to_json(_aware(plan.frozen_until)),
        "created_at": to_json(_aware(plan.created_at)),
        "created_by": plan.created_by,
        "published_at": to_json(_aware(plan.published_at)),
        "published_by": plan.published_by,
        "note": plan.note,
        "kpis": plan.kpis,
        "change_summary": plan.change_summary,
        "solver": {
            "provider": md.get("provider"),
            "status": md.get("status"),
            "objective": md.get("objective"),
            "gap": md.get("gap"),
            "best_bound": md.get("best_bound"),
            "proven_optimal": md.get("proven_optimal"),
            "runtime_s": md.get("runtime_s"),
            "time_limit_s": md.get("time_limit_s"),
            "iterations": md.get("iterations"),
            "input_hash": md.get("input_hash"),
            "engine_version": md.get("engine_version"),
            "messages": md.get("messages", []),
            "phases": md.get("phases", []),
            "objective_breakdown": md.get("objective_breakdown", {}),
            "details": {k: v for k, v in (md.get("details") or {}).items() if k not in ("scales",)},
        },
        "params": plan.params,
    }


def plan_header(s: Session, ctx: Ctx, plan_id: uuid.UUID) -> dict[str, Any]:
    plan = _get_plan(s, ctx, plan_id)
    out = plan_summary(plan)
    counts = dict(
        s.execute(select(ConstraintViolation.severity, func.count()).where(ConstraintViolation.plan_id == plan.id).group_by(ConstraintViolation.severity)).all()
    )
    out["violation_counts"] = counts
    out["hard_violations_placed"] = s.scalar(
        select(func.count()).select_from(ConstraintViolation).where(ConstraintViolation.plan_id == plan.id, ConstraintViolation.hardness == "HARD", ConstraintViolation.type != "UNSCHEDULED")
    ) or 0
    out["operations"] = s.scalar(select(func.count()).select_from(ScheduledOperation).where(ScheduledOperation.plan_id == plan.id))
    an = plan.analysis or {}
    out["unscheduled_count"] = len(an.get("unscheduled", []))
    out["bottlenecks"] = an.get("bottlenecks", [])[:10]
    out["data_issues"] = an.get("data_issues", [])
    out["change_log"] = an.get("change_log", [])
    sc = s.get(Scenario, plan.scenario_id)
    out["scenario"] = {"id": str(sc.id), "name": sc.name, "is_live": sc.is_live, "head_plan_id": str(sc.head_plan_id) if sc.head_plan_id else None, "redo_available": bool(sc.redo_stack)}
    out["is_head"] = sc.head_plan_id == plan.id
    out["health"] = plan_health(plan, counts)
    out["health"]["hard_violations_placed"] = out["hard_violations_placed"]
    out["health"]["unscheduled_operations"] = out["unscheduled_count"]
    return out


def plan_health(plan: Plan, counts: dict[str, int]) -> dict[str, Any]:
    """Descriptive plan health — counts, never an opaque score."""
    k = plan.kpis or {}
    bn = (plan.analysis or {}).get("bottlenecks", [])
    return {
        "critical_issues": counts.get("CRITICAL", 0),
        "warnings": counts.get("WARNING", 0),
        "orders_at_risk": int((k.get("late_orders") or 0) + (k.get("orders_unscheduled") or 0)),
        "overloaded_resources": sum(1 for b in bn if b.get("kind") == "OVERLOADED"),
        "material_issues": int(k.get("material_shortages") or 0) + int(k.get("material_delayed_operations") or 0 > 0),
    }


def list_plans(s: Session, ctx: Ctx, scenario_id: uuid.UUID, limit: int = 50) -> list[dict[str, Any]]:
    get_scenario(s, ctx, scenario_id)
    rows = s.scalars(select(Plan).where(Plan.scenario_id == scenario_id).order_by(Plan.version_no.desc()).limit(limit))
    return [plan_summary(p) for p in rows]


# =============================================================================================
# Gantt
# =============================================================================================


def gantt(
    s: Session,
    ctx: Ctx,
    plan_id: uuid.UUID,
    start: datetime | None = None,
    end: datetime | None = None,
    resource_ids: list[str] | None = None,
    include_secondary: bool = False,
) -> dict[str, Any]:
    plan = _get_plan(s, ctx, plan_id)
    start = start or _aware(plan.horizon_start) - timedelta(days=1)
    if end is None:
        # the default window shows every operation, including those that overflow the horizon
        last = s.scalar(select(func.max(ScheduledOperation.end)).where(ScheduledOperation.plan_id == plan.id))
        end = max(_aware(plan.horizon_end), _aware(last) if last else _aware(plan.horizon_end))
    if end <= start:
        raise ValidationFailed("end must be after start")
    if (end - start).days > 400:
        raise ValidationFailed("Window too large (max 400 days)")
    q = select(ScheduledOperation).where(ScheduledOperation.plan_id == plan.id, ScheduledOperation.end >= start, ScheduledOperation.setup_start <= end)
    if resource_ids:
        q = q.where(ScheduledOperation.resource_key.in_(resource_ids))
    rows = list(s.scalars(q))
    order_ids = {r.order_id for r in rows if r.order_id}
    orders = {o.id: o for o in s.scalars(select(ProductionOrder).where(ProductionOrder.id.in_(order_ids)))} if order_ids else {}
    items = {i.id: i for i in s.scalars(select(Item).where(Item.id.in_({o.item_id for o in orders.values()})))} if orders else {}
    fams = {f.id: f for f in s.scalars(select(ProductFamily))}
    custs = {c.id: c for c in s.scalars(select(Customer))}
    plan_orders = {o["order_id"]: o for o in (plan.analysis or {}).get("orders", [])}
    ops = []
    for r in rows:
        o = orders.get(r.order_id)
        it = items.get(o.item_id) if o else None
        fam = fams.get(it.family_id) if it and it.family_id else None
        po = plan_orders.get(r.order_key, {})
        ops.append(
            {
                "id": r.op_key,
                "order_id": r.order_key,
                "order": o.number if o else r.order_key,
                "item": it.code if it else None,
                "item_name": it.name if it else None,
                "family": fam.code if fam else None,
                "family_color": fam.color_hint if fam else None,
                "color": (it.attributes or {}).get("color") if it else None,
                "customer": custs[o.customer_id].name if o and o.customer_id in custs else None,
                "priority": o.priority if o else None,
                "expedite": o.expedite if o else False,
                "due": to_json(_aware(o.due_date)) if o else None,
                "resource_id": r.resource_key,
                "secondary": r.secondary,
                "setup_start": to_json(_aware(r.setup_start)),
                "start": to_json(_aware(r.start)),
                "end": to_json(_aware(r.end)),
                "setup_minutes": r.setup_minutes,
                "run_minutes": r.run_minutes,
                "quantity": r.quantity,
                "fixed": r.is_fixed,
                "fixed_reason": r.fixed_reason,
                "locked": r.is_locked,
                "late": r.is_late,
                "zone": r.zone,
                "subcontracted": r.subcontracted,
                "binding": r.binding,
                "order_status": po.get("status"),
                "material_status": po.get("material_status"),
                "op_code": r.op_key.split("/")[-1],
            }
        )
    # resources (rows) with calendars and unavailability inside the window
    cp, _res = replay(s, plan)
    plant = s.get(Plant, plan.plant_id)
    db_res = {str(r.id): r for r in s.scalars(select(Resource).where(Resource.plant_id == plan.plant_id))}
    areas = {a.id: a for a in s.scalars(select(PlanningArea).where(PlanningArea.plant_id == plan.plant_id))}
    grp_rows = s.execute(select(ResourceGroupMember.resource_id, ResourceGroup.code).join(ResourceGroup, ResourceGroup.id == ResourceGroupMember.group_id)).all()
    groups: dict[str, list[str]] = defaultdict(list)
    for rid, code in grp_rows:
        groups[str(rid)].append(code)
    a_min, b_min = cp.axis.to_min(start), cp.axis.to_min(end)
    resources = []
    busy: dict[str, int] = defaultdict(int)
    for r in rows:
        busy[r.resource_key] += r.working_minutes or 0
    for cr in cp.resources:
        if resource_ids and cr.id not in resource_ids:
            continue
        dbr = db_res.get(cr.id)
        if cr.kind in ("LABOR_POOL",) and not include_secondary:
            continue
        if cr.kind == "TOOL" and not include_secondary:
            continue
        nonwork = []
        prev = a_min
        for ws, we in cr.cal.segments():
            if we <= a_min or ws >= b_min:
                continue
            if ws > prev:
                nonwork.append([cp.dt(prev).isoformat(), cp.dt(ws).isoformat()])
            prev = max(prev, we)
        if prev < b_min:
            nonwork.append([cp.dt(prev).isoformat(), cp.dt(b_min).isoformat()])
        cap = cr.cal.working_between(a_min, b_min) if cr.finite else 0
        resources.append(
            {
                "id": cr.id,
                "code": cr.code,
                "name": cr.name,
                "kind": cr.kind,
                "area": cr.area,
                "area_name": areas[dbr.area_id].name if dbr and dbr.area_id in areas else None,
                "area_order": areas[dbr.area_id].sort_order if dbr and dbr.area_id in areas else 99,
                "groups": groups.get(cr.id, []),
                "status": dbr.status if dbr else None,
                "finite": cr.finite,
                "non_working": nonwork,
                "unavailability": [
                    {"id": uid, "start": cp.dt(a).isoformat(), "end": cp.dt(b).isoformat(), "kind": kind, "reason": reason}
                    for a, b, kind, reason, uid, _loss in cr.unavail
                    if b > a_min and a < b_min
                ],
                "capacity_minutes": cap,
                "busy_minutes": busy.get(cr.id, 0),
                "utilization": round(busy.get(cr.id, 0) / cap, 4) if cap else None,
            }
        )
    resources.sort(key=lambda r: (r["area_order"], r["kind"] != "MACHINE", r["code"]))
    return {
        "plan": {"id": str(plan.id), "number": plan.number, "status": plan.status, "version": plan.version},
        "timezone": plant.timezone,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "now": now().isoformat(),
        "frozen_until": to_json(_aware(plan.frozen_until)),
        "resources": resources,
        "operations": ops,
    }


def order_chain(s: Session, ctx: Ctx, plan_id: uuid.UUID, order_key: str) -> dict[str, Any]:
    """Operations of one order (and its component orders) with dependencies, for Gantt highlighting."""
    plan = _get_plan(s, ctx, plan_id)
    cp, res = replay(s, plan)
    oi = cp.order_index.get(order_key)
    if oi is None:
        raise NotFound("Order not in this plan")
    ops = set(cp.orders[oi].ops)
    # component orders feeding this order (make-item pegging)
    for peg in cp.static_pegs:
        if peg.consumer_op in ops and peg.producer_order is not None:
            ops.update(cp.orders[peg.producer_order].ops)
    deps = []
    for i in ops:
        for p, kind, lag, _f in cp.ops[i].preds:
            if p in ops:
                deps.append({"from": cp.ops[p].id, "to": cp.ops[i].id, "type": "FS" if kind == "OVL" else kind, "lag_minutes": lag, "overlap": kind == "OVL"})
    return {"order_id": order_key, "operations": sorted(cp.ops[i].id for i in ops), "dependencies": deps}


# =============================================================================================
# Operation / order detail
# =============================================================================================


def operation_detail(s: Session, ctx: Ctx, plan_id: uuid.UUID, op_key: str) -> dict[str, Any]:
    plan = _get_plan(s, ctx, plan_id)
    r = s.scalar(select(ScheduledOperation).where(ScheduledOperation.plan_id == plan.id, ScheduledOperation.op_key == op_key))
    unsched = next((u for u in (plan.analysis or {}).get("unscheduled", []) if u["op_id"] == op_key), None)
    if r is None and unsched is None:
        raise NotFound("Operation not in this plan")
    out: dict[str, Any] = {"op_id": op_key, "scheduled": row_dict(r) if r else None, "unscheduled": unsched}
    if r is not None and r.explanation:
        out["explanation"] = r.explanation
    order_key = r.order_key if r else unsched["order_id"]
    o = s.get(ProductionOrder, uuid.UUID(order_key)) if _is_uuid(order_key) else None
    if o is not None:
        it = s.get(Item, o.item_id)
        out["order"] = {"id": str(o.id), "number": o.number, "item": it.code if it else None, "item_name": it.name if it else None, "quantity": o.quantity, "due": to_json(_aware(o.due_date)), "priority": o.priority, "status": o.status, "expedite": o.expedite}
        poo = s.scalar(select(ProductionOrderOperation).where(ProductionOrderOperation.order_id == o.id, ProductionOrderOperation.seq == int(op_key.split("/")[-1])))
        if poo is not None:
            ro = s.get(RoutingOperation, poo.routing_operation_id) if poo.routing_operation_id else None
            out["operation"] = {"code": poo.code, "name": poo.name, "status": poo.status, "completed_quantity": poo.completed_quantity, "instructions": ro.instructions if ro else None, "setup_attributes": ro.setup_attributes if ro else {}}
    pegs = [dict(p) for p in (plan.analysis or {}).get("pegging", []) if p["consumer_op_id"] == op_key]
    mids = {uuid.UUID(p["material_id"]) for p in pegs if _is_uuid(p["material_id"])}
    codes = {str(i): c for i, c in s.execute(select(Item.id, Item.code).where(Item.id.in_(mids)))} if mids else {}
    for p in pegs:
        p["material"] = codes.get(p["material_id"], p["material_id"])
    out["pegging"] = pegs
    out["violations"] = [row_dict(v) for v in s.scalars(select(ConstraintViolation).where(ConstraintViolation.plan_id == plan.id, ConstraintViolation.op_key == op_key))]
    return out


def _is_uuid(v: str | None) -> bool:
    try:
        uuid.UUID(str(v))
        return True
    except ValueError:
        return False


def explore(s: Session, ctx: Ctx, plan_id: uuid.UUID, op_key: str) -> dict[str, Any]:
    plan = _get_plan(s, ctx, plan_id)
    cp, res = replay(s, plan)
    i = cp.op_index.get(op_key)
    if i is None:
        raise NotFound("Operation not in this plan")
    return explore_operation(res, i)


def position_check(s: Session, ctx: Ctx, plan_id: uuid.UUID, op_key: str, resource_id: str, start: datetime) -> dict[str, Any]:
    plan = _get_plan(s, ctx, plan_id)
    cp, res = replay(s, plan)
    i = cp.op_index.get(op_key)
    if i is None:
        raise NotFound("Operation not in this plan")
    return check_position(res, i, resource_id, cp.axis.to_min(start))


def order_detail(s: Session, ctx: Ctx, plan_id: uuid.UUID, order_key: str) -> dict[str, Any]:
    plan = _get_plan(s, ctx, plan_id)
    res_order = next((o for o in (plan.analysis or {}).get("orders", []) if o["order_id"] == order_key), None)
    rows = list(s.scalars(select(ScheduledOperation).where(ScheduledOperation.plan_id == plan.id, ScheduledOperation.order_key == order_key).order_by(ScheduledOperation.start)))
    chain = (plan.kpi_details or {}).get("root_causes", {}).get(order_key, [])
    late = next((x for x in (plan.kpi_details or {}).get("late_orders", []) if x["order_id"] == order_key), None)
    pegs = [p for p in (plan.analysis or {}).get("pegging", []) if p["consumer_order_id"] == order_key or p.get("supply_order_id") == order_key]
    return {
        "result": res_order,
        "operations": [row_dict(r, skip={"explanation"}) for r in rows],
        "root_cause": chain,
        "deadline": late,
        "pegging": pegs,
        "unscheduled": [u for u in (plan.analysis or {}).get("unscheduled", []) if u["order_id"] == order_key],
    }


# =============================================================================================
# Shop-floor views (published plan)
# =============================================================================================


def _published_or_head(s: Session, ctx: Ctx, plant_id: uuid.UUID) -> Plan:
    plant = s.get(Plant, plant_id)
    if plant is None:
        raise NotFound("Plant not found")
    ctx.require_plant(plant.id)
    plan = s.get(Plan, plant.published_plan_id) if plant.published_plan_id else None
    if plan is None and plant.live_scenario_id:
        sc = s.get(Scenario, plant.live_scenario_id)
        plan = s.get(Plan, sc.head_plan_id) if sc and sc.head_plan_id else None
    if plan is None:
        raise NotFound("No plan available for this plant yet", code="NO_PLAN")
    return plan


def dispatch_list(s: Session, ctx: Ctx, plant_id: uuid.UUID, resource_id: str | None = None, date_from: datetime | None = None, hours: int = 24) -> dict[str, Any]:
    ctx.require("plan:read")
    plan = _published_or_head(s, ctx, plant_id)
    t0 = date_from or now()
    t1 = t0 + timedelta(hours=hours)
    q = select(ScheduledOperation).where(ScheduledOperation.plan_id == plan.id, ScheduledOperation.end >= t0, ScheduledOperation.setup_start <= t1).order_by(ScheduledOperation.resource_key, ScheduledOperation.setup_start)
    if resource_id:
        q = q.where(ScheduledOperation.resource_key == resource_id)
    rows = list(s.scalars(q))
    orders = {o.id: o for o in s.scalars(select(ProductionOrder).where(ProductionOrder.id.in_({r.order_id for r in rows if r.order_id})))} if rows else {}
    items = {i.id: i for i in s.scalars(select(Item).where(Item.id.in_({o.item_id for o in orders.values()})))} if orders else {}
    poos = {}
    if rows:
        for poo in s.scalars(select(ProductionOrderOperation).where(ProductionOrderOperation.id.in_({r.order_operation_id for r in rows if r.order_operation_id}))):
            poos[poo.id] = poo
    res = {str(r.id): r for r in s.scalars(select(Resource).where(Resource.plant_id == plant_id))}
    plan_orders = {o["order_id"]: o for o in (plan.analysis or {}).get("orders", [])}
    out = []
    for r in rows:
        o = orders.get(r.order_id)
        it = items.get(o.item_id) if o else None
        poo = poos.get(r.order_operation_id)
        out.append(
            {
                "resource_id": r.resource_key,
                "resource": res[r.resource_key].code if r.resource_key in res else r.resource_key,
                "op_id": r.op_key,
                "operation": poo.name if poo else r.op_key,
                "order": o.number if o else None,
                "product": it.code if it else None,
                "product_name": it.name if it else None,
                "quantity": r.quantity,
                "setup_start": to_json(_aware(r.setup_start)),
                "start": to_json(_aware(r.start)),
                "end": to_json(_aware(r.end)),
                "setup_minutes": r.setup_minutes,
                "material": plan_orders.get(r.order_key, {}).get("material_status"),
                "status": poo.status if poo else "PLANNED",
                "late": r.is_late,
                "fixed": r.is_fixed,
            }
        )
    return {"plan": plan_summary(plan), "from": t0.isoformat(), "to": t1.isoformat(), "rows": out}


def supervisor_view(s: Session, ctx: Ctx, plant_id: uuid.UUID) -> dict[str, Any]:
    ctx.require("plan:read")
    plan = _published_or_head(s, ctx, plant_id)
    t = now()
    rows = list(s.scalars(select(ScheduledOperation).where(ScheduledOperation.plan_id == plan.id, ScheduledOperation.end >= t - timedelta(hours=12), ScheduledOperation.setup_start <= t + timedelta(hours=16)).order_by(ScheduledOperation.setup_start)))
    poos = {p.id: p for p in s.scalars(select(ProductionOrderOperation).where(ProductionOrderOperation.id.in_({r.order_operation_id for r in rows if r.order_operation_id})))} if rows else {}
    res = {str(r.id): r for r in s.scalars(select(Resource).where(Resource.plant_id == plant_id, Resource.kind.in_(["MACHINE", "WORK_CENTER"])))}
    orders = {o.id: o for o in s.scalars(select(ProductionOrder).where(ProductionOrder.id.in_({r.order_id for r in rows if r.order_id})))} if rows else {}
    plan_orders = {o["order_id"]: o for o in (plan.analysis or {}).get("orders", [])}
    by_res: dict[str, dict[str, Any]] = {}
    for rid, rr in res.items():
        by_res[rid] = {"resource_id": rid, "resource": rr.code, "status": rr.status, "now": None, "next": [], "delayed": [], "blocked": [], "material_issues": []}
    for r in rows:
        slot = by_res.get(r.resource_key)
        if slot is None:
            continue
        poo = poos.get(r.order_operation_id)
        o = orders.get(r.order_id)
        item = {"op_id": r.op_key, "order": o.number if o else None, "operation": poo.name if poo else None, "start": to_json(_aware(r.start)), "end": to_json(_aware(r.end)), "quantity": r.quantity, "status": poo.status if poo else None}
        st = poo.status if poo else "PLANNED"
        if _aware(r.setup_start) <= t < _aware(r.end):
            slot["now"] = item
            if st not in ("IN_PROGRESS", "COMPLETED") and t - _aware(r.setup_start) > timedelta(minutes=30):
                slot["delayed"].append({**item, "reason": "planned start passed, not started"})
        elif _aware(r.setup_start) > t:
            if len(slot["next"]) < 3:
                slot["next"].append(item)
        elif _aware(r.end) <= t and st not in ("COMPLETED",):
            slot["delayed"].append({**item, "reason": "should be finished"})
        if o and o.status == "BLOCKED":
            slot["blocked"].append(item)
        if plan_orders.get(r.order_key, {}).get("material_status") in ("SHORTAGE", "LATE_SUPPLY") and _aware(r.setup_start) < t + timedelta(hours=16):
            slot["material_issues"].append(item)
    return {"plan": {"id": str(plan.id), "number": plan.number, "status": plan.status}, "at": t.isoformat(), "resources": sorted(by_res.values(), key=lambda x: x["resource"])}


def operator_view(s: Session, ctx: Ctx, plant_id: uuid.UUID, resource_id: str) -> dict[str, Any]:
    ctx.require("plan:read")
    plan = _published_or_head(s, ctx, plant_id)
    t = now()
    res = s.get(Resource, uuid.UUID(resource_id))
    if res is None:
        raise NotFound("Resource not found")
    rows = list(s.scalars(select(ScheduledOperation).where(ScheduledOperation.plan_id == plan.id, ScheduledOperation.resource_key == resource_id, ScheduledOperation.end >= t).order_by(ScheduledOperation.setup_start).limit(6)))
    jobs = []
    for r in rows:
        poo = s.get(ProductionOrderOperation, r.order_operation_id) if r.order_operation_id else None
        o = s.get(ProductionOrder, r.order_id) if r.order_id else None
        it = s.get(Item, o.item_id) if o else None
        ro = s.get(RoutingOperation, poo.routing_operation_id) if poo and poo.routing_operation_id else None
        jobs.append(
            {
                "op_id": r.op_key,
                "order_operation_id": str(r.order_operation_id) if r.order_operation_id else None,
                "order": o.number if o else None,
                "product": it.code if it else None,
                "product_name": it.name if it else None,
                "operation": poo.name if poo else None,
                "quantity": r.quantity,
                "completed_quantity": poo.completed_quantity if poo else 0,
                "setup_start": to_json(_aware(r.setup_start)),
                "start": to_json(_aware(r.start)),
                "end": to_json(_aware(r.end)),
                "setup_minutes": r.setup_minutes,
                "setup_instructions": (ro.instructions if ro else None),
                "setup_attributes": (r.explanation or {}).get("reasons", []) and [x for x in (r.explanation or {}).get("reasons", []) if x.get("code") == "SETUP"],
                "quality": "First-article inspection required" if (ro and ro.instructions and "inspection" in ro.instructions.lower()) else None,
                "status": poo.status if poo else "PLANNED",
            }
        )
    current = next((j for j in jobs if j["status"] == "IN_PROGRESS"), jobs[0] if jobs else None)
    nxt = [j for j in jobs if j is not current][:3]
    return {"resource": {"id": str(res.id), "code": res.code, "name": res.name, "status": res.status}, "plan_number": plan.number, "current": current, "next": nxt, "at": t.isoformat()}


# =============================================================================================
# Plan vs actual
# =============================================================================================


def plan_vs_actual(s: Session, ctx: Ctx, plant_id: uuid.UUID, days: int = 14) -> dict[str, Any]:
    """Compares executed operations (MES actuals) with the latest plan version that contained them.
    Schedule adherence = share of operations started within ±60 min of the planned start."""
    ctx.require("analytics:read")
    t0 = now() - timedelta(days=days)
    acts = s.execute(
        select(ActualProduction, ProductionOrderOperation, ProductionOrder)
        .join(ProductionOrderOperation, ProductionOrderOperation.id == ActualProduction.order_operation_id)
        .join(ProductionOrder, ProductionOrder.id == ProductionOrderOperation.order_id)
        .where(ProductionOrder.plant_id == plant_id, ActualProduction.start >= t0)
    ).all()
    keys = {f"{o.number}/{poo.seq:03d}": (a, poo, o) for a, poo, o in acts}
    plans = {p.id: p for p in s.scalars(select(Plan).where(Plan.plant_id == plant_id, Plan.status.in_(["PUBLISHED", "SUPERSEDED"])))}
    planned: dict[str, ScheduledOperation] = {}
    if plans and keys:
        for so in s.scalars(select(ScheduledOperation).where(ScheduledOperation.plan_id.in_(list(plans)), ScheduledOperation.op_key.in_(list(keys)))):
            prev = planned.get(so.op_key)
            if prev is None or plans[so.plan_id].created_at > plans[prev.plan_id].created_at:
                planned[so.op_key] = so
    rows = []
    on_time = 0
    dev_start, dev_dur = [], []
    res_codes = {r.id: r.code for r in s.scalars(select(Resource).where(Resource.plant_id == plant_id))}
    for key, (a, poo, o) in keys.items():
        so = planned.get(key)
        row = {
            "op_id": key,
            "order": o.number,
            "operation": poo.name,
            "resource": res_codes.get(a.resource_id),
            "actual_start": to_json(_aware(a.start)),
            "actual_end": to_json(_aware(a.end)),
            "good": a.good_quantity,
            "scrap": a.scrap_quantity,
            "planned_start": None,
            "planned_end": None,
            "start_deviation_min": None,
            "duration_deviation_pct": None,
        }
        if so is not None:
            ds = (_aware(a.start) - _aware(so.start)).total_seconds() / 60
            row.update(planned_start=to_json(_aware(so.start)), planned_end=to_json(_aware(so.end)), start_deviation_min=round(ds))
            dev_start.append(abs(ds))
            if abs(ds) <= 60:
                on_time += 1
            if a.end is not None and so.end > so.start:
                pd = (_aware(so.end) - _aware(so.start)).total_seconds()
                ad = (_aware(a.end) - _aware(a.start)).total_seconds()
                row["duration_deviation_pct"] = round(100 * (ad - pd) / pd, 1)
                dev_dur.append(row["duration_deviation_pct"])
        rows.append(row)
    compared = sum(1 for r in rows if r["planned_start"])
    return {
        "days": days,
        "operations_executed": len(rows),
        "operations_compared": compared,
        "schedule_adherence_pct": round(100 * on_time / compared, 1) if compared else None,
        "mean_abs_start_deviation_min": round(sum(dev_start) / len(dev_start), 1) if dev_start else None,
        "mean_duration_deviation_pct": round(sum(dev_dur) / len(dev_dur), 1) if dev_dur else None,
        "note": None if compared else "No published plan covers the executed operations of this period yet.",
        "rows": sorted(rows, key=lambda r: r["actual_start"] or "", reverse=True)[:500],
    }


def attention(s: Session, ctx: Ctx, plant_id: uuid.UUID) -> list[dict[str, Any]]:
    ctx.require("plan:read")
    rows = s.scalars(select(Alert).where(Alert.plant_id == plant_id, Alert.status == "OPEN").order_by(Alert.severity, Alert.created_at.desc()).limit(50))
    sev = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
    out = [row_dict(a) for a in rows]
    out.sort(key=lambda a: (sev.get(a["severity"], 3), -(a.get("count") or 0)))
    return out


def latest_run(s: Session, scenario_id: uuid.UUID) -> PlanningRun | None:
    return s.scalar(select(PlanningRun).where(PlanningRun.scenario_id == scenario_id).order_by(PlanningRun.created_at.desc()).limit(1))


_ = (and_, or_, ZoneInfo)
