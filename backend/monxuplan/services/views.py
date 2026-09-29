"""Read models for the UI: plan headers, Gantt windows, operation/order detail, constraint explorer,
dispatch lists, supervisor and operator views, plan vs actual."""

from __future__ import annotations

import uuid
from bisect import bisect_right
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
from . import plan_store
from .context import Ctx
from .engine_view import replay
from .masterdata import row_dict, to_json
from .planning import get_scenario

GANTT_FULL_PLAN_MAX_OPS = 30_000  # above this a plan is browsed by windows (default: its first day)
GANTT_MAX_OPS = 60_000  # operations per Gantt response
DISPATCH_MAX_ROWS = 5_000  # dispatch rows per response without a resource filter
SUPERVISOR_LIST_CAP = 20  # items per list and resource in the supervisor view (totals are given)
PVA_MAX_ACTUALS = 50_000  # plan-vs-actual compares the most recent executions (the response says so)


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
        "publish_reason": plan.publish_reason,
        "publish_overrides": plan.publish_overrides or [],
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
    an = plan.analysis or {}
    counts = an.get("counts") or {}
    out["operations"] = counts["operations"] if "operations" in counts else s.scalar(select(func.count()).select_from(ScheduledOperation).where(ScheduledOperation.plan_id == plan.id))
    out["unscheduled_count"] = plan_store.unscheduled_count(s, plan)
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
    include_operations: bool = True,
    include_resources: bool = True,
) -> dict[str, Any]:
    plan = _get_plan(s, ctx, plan_id)
    counts = (plan.analysis or {}).get("counts") or {}
    large = (counts.get("operations") or 0) > GANTT_FULL_PLAN_MAX_OPS
    if start is None:
        start = _aware(plan.horizon_start) - timedelta(days=0 if large else 1)
    if end is None:
        if large:
            # a 200 000-operation plan is browsed window by window: the default is its first day
            end = start + timedelta(days=1)
        else:
            # the default window shows every operation, including those that overflow the horizon
            last = s.scalar(select(func.max(ScheduledOperation.end)).where(ScheduledOperation.plan_id == plan.id))
            end = max(_aware(plan.horizon_end), _aware(last) if last else _aware(plan.horizon_end))
    if end <= start:
        raise ValidationFailed("end must be after start")
    if (end - start).days > 400:
        raise ValidationFailed("Window too large (max 400 days)")
    SO = ScheduledOperation
    q = select(
        SO.op_key, SO.order_key, SO.order_id, SO.resource_key, SO.secondary, SO.setup_start, SO.start, SO.end, SO.setup_minutes, SO.run_minutes, SO.working_minutes, SO.quantity,
        SO.is_fixed, SO.fixed_reason, SO.is_locked, SO.is_late, SO.zone, SO.subcontracted, SO.binding,
    ).where(SO.plan_id == plan.id, SO.end >= start, SO.setup_start <= end)
    rows = []
    if not include_operations:
        pass
    elif resource_ids:
        for part in plan_store.chunks(resource_ids):
            rows.extend(s.execute(q.where(SO.resource_key.in_(part))).all())
    else:
        rows = s.execute(q.limit(GANTT_MAX_OPS + 1)).all()
    truncated = len(rows) > GANTT_MAX_OPS
    rows = rows[:GANTT_MAX_OPS]
    order_ids = {r.order_id for r in rows if r.order_id}
    PO = ProductionOrder
    orders = {o.id: o for part in plan_store.chunks(order_ids) for o in s.execute(select(PO.id, PO.number, PO.item_id, PO.customer_id, PO.priority, PO.expedite, PO.due_date).where(PO.id.in_(part)))}
    item_ids = {o.item_id for o in orders.values()}
    items = {i.id: i for part in plan_store.chunks(item_ids) for i in s.execute(select(Item.id, Item.code, Item.name, Item.family_id, Item.attributes).where(Item.id.in_(part)))}
    fams = {f.id: f for f in s.scalars(select(ProductFamily))}
    cust_ids = {o.customer_id for o in orders.values() if o.customer_id}
    custs = {c.id: c for part in plan_store.chunks(cust_ids) for c in s.execute(select(Customer.id, Customer.name).where(Customer.id.in_(part)))}
    plan_orders = plan_store.order_results(s, plan, {r.order_key for r in rows})
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
    plant = s.get(Plant, plan.plant_id)
    busy: dict[str, int] | None = None
    if include_operations:
        busy = defaultdict(int)
        for r in rows:
            busy[r.resource_key] += r.working_minutes or 0
    resources = resource_rows(s, plan, start, end, resource_ids, include_secondary, busy) if include_resources else []
    return {
        "plan": {"id": str(plan.id), "number": plan.number, "status": plan.status, "version": plan.version},
        "timezone": plant.timezone,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "now": now().isoformat(),
        "frozen_until": to_json(_aware(plan.frozen_until)),
        "resources": resources,
        "operations": ops,
        "truncated": truncated,
        "operation_limit": GANTT_MAX_OPS,
        "operation_count": counts.get("operations"),
        "large": large,
    }


def find_operations(s: Session, ctx: Ctx, plan_id: uuid.UUID, q: str, limit: int = 20) -> list[dict[str, Any]]:
    """Operations of a plan whose key (order number / sequence) contains ``q`` — to jump to them in a
    Gantt that only holds the visible window."""
    plan = _get_plan(s, ctx, plan_id)
    q = (q or "").strip().lower()
    if not q:
        return []
    SO = ScheduledOperation
    rows = s.execute(
        select(SO.op_key, SO.order_key, SO.resource_key, SO.setup_start, SO.end).where(SO.plan_id == plan.id, func.lower(SO.op_key).like(f"%{q}%")).order_by(SO.op_key).limit(max(1, min(limit, 200)))
    ).all()
    return [{"op_id": r.op_key, "order_id": r.order_key, "resource_id": r.resource_key, "setup_start": to_json(_aware(r.setup_start)), "end": to_json(_aware(r.end))} for r in rows]


def gantt_blocks(s: Session, ctx: Ctx, plan_id: uuid.UUID, start: datetime, end: datetime, resource_ids: list[str] | None = None, resolution_minutes: int = 30) -> dict[str, Any]:
    """Operations merged into busy blocks per resource — the zoomed-out Gantt of a dense plan (a
    machine running 200 jobs a day is drawn as its busy stretches, not as 200 one-pixel bars). A block
    joins consecutive operations separated by less than ``resolution_minutes``."""
    plan = _get_plan(s, ctx, plan_id)
    if end <= start:
        raise ValidationFailed("end must be after start")
    if (end - start).days > 400:
        raise ValidationFailed("Window too large (max 400 days)")
    SO = ScheduledOperation
    q = select(SO.resource_key, SO.setup_start, SO.end, SO.is_late, SO.is_locked).where(SO.plan_id == plan.id, SO.end >= start, SO.setup_start <= end).order_by(SO.resource_key, SO.setup_start)
    rows = []
    if resource_ids:
        for part in plan_store.chunks(resource_ids):
            rows.extend(s.execute(q.where(SO.resource_key.in_(part))).all())
    else:
        rows = s.execute(q).all()
    gap = timedelta(minutes=max(int(resolution_minutes), 1))
    lanes: dict[str, list[list[Any]]] = {}
    cur_key, cur = None, None
    for r in rows:
        ss, ee = _aware(r.setup_start), _aware(r.end)
        if r.resource_key != cur_key or cur is None or ss - cur[1] > gap:
            cur = [ss, ee, 0, 0, 0]  # start, end, operations, late, locked
            lanes.setdefault(r.resource_key, []).append(cur)
            cur_key = r.resource_key
        cur[1] = max(cur[1], ee)
        cur[2] += 1
        cur[3] += 1 if r.is_late else 0
        cur[4] += 1 if r.is_locked else 0
    return {
        "plan": {"id": str(plan.id), "number": plan.number},
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "resolution_minutes": int(gap.total_seconds() // 60),
        "operations": len(rows),
        "lanes": {k: [[b[0].isoformat(), b[1].isoformat(), b[2], b[3], b[4]] for b in v] for k, v in lanes.items()},
    }


def _calendar_rows(s: Session, plan: Plan, resource_ids: list[str] | None) -> list[dict[str, Any]]:
    """Calendar of every resource of the plan (stored read model; replayed for older plans)."""
    docs = plan_store.documents(s, plan, plan_store.DOC_CALENDAR, resource_ids)
    if docs or plan_store.has_documents(s, plan, plan_store.DOC_CALENDAR):
        return sorted(docs.values(), key=lambda d: d.get("order", 0))
    from monxuplan_engine.views import resource_calendar

    cp, res = replay(s, plan)
    wanted = set(resource_ids) if resource_ids else None
    return [resource_calendar(res, r.idx) for r in cp.resources if wanted is None or r.id in wanted]


def resource_rows(s: Session, plan: Plan, start: datetime, end: datetime, resource_ids: list[str] | None = None, include_secondary: bool = False, busy: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """Gantt rows: resources with non-working time, unavailability and capacity inside a window.
    ``busy`` holds the working minutes of the window's operations; without it (rows requested
    without their operations) the utilisation shown is the resource's over the whole plan."""
    plan_util = None
    if busy is None:
        # kpi_details keeps percentages; Gantt rows use fractions
        plan_util = {k: round(v["utilization"] / 100.0, 4) for k, v in ((plan.kpi_details or {}).get("resources") or {}).items() if isinstance(v, dict) and v.get("utilization") is not None}
        busy = {}
    db_res = {str(r.id): r for r in s.execute(select(Resource.id, Resource.area_id, Resource.status).where(Resource.plant_id == plan.plant_id))}
    areas = {a.id: a for a in s.scalars(select(PlanningArea).where(PlanningArea.plant_id == plan.plant_id))}
    groups: dict[str, list[str]] = defaultdict(list)
    for rid, code in s.execute(select(ResourceGroupMember.resource_id, ResourceGroup.code).join(ResourceGroup, ResourceGroup.id == ResourceGroupMember.group_id)):
        groups[str(rid)].append(code)
    out = []
    for cal in _calendar_rows(s, plan, resource_ids):
        if cal["kind"] in ("LABOR_POOL", "TOOL") and not include_secondary:
            continue
        origin = datetime.fromisoformat(cal["origin"])
        a_min = int((start - origin).total_seconds() // 60)
        b_min = int((end - origin).total_seconds() // 60)

        def dt(m: int, _o=origin) -> str:
            return (_o + timedelta(minutes=m)).isoformat()

        working = cal["working"]
        k = max(bisect_right([w[1] for w in working], a_min) - 1, 0) if working else 0
        nonwork, prev, cap = [], a_min, 0
        for ws, we in working[k:]:
            if we <= a_min:
                continue
            if ws >= b_min:
                break
            if ws > prev:
                nonwork.append([dt(prev), dt(ws)])
            cap += min(we, b_min) - max(ws, a_min)
            prev = max(prev, we)
        if prev < b_min:
            nonwork.append([dt(prev), dt(b_min)])
        if not cal["finite"]:
            cap = 0
        dbr = db_res.get(cal["id"])
        area = areas.get(dbr.area_id) if dbr is not None else None
        rid = cal["id"]
        out.append(
            {
                "id": rid,
                "code": cal["code"],
                "name": cal["name"],
                "kind": cal["kind"],
                "area": cal["area"],
                "area_name": area.name if area else None,
                "area_order": area.sort_order if area else 99,
                "groups": groups.get(rid, []),
                "status": dbr.status if dbr is not None else None,
                "finite": cal["finite"],
                "non_working": nonwork,
                "unavailability": [{"id": u["id"], "start": dt(u["start"]), "end": dt(u["end"]), "kind": u["kind"], "reason": u["reason"]} for u in cal["unavailability"] if u["end"] > a_min and u["start"] < b_min],
                "capacity_minutes": cap,
                "busy_minutes": busy.get(rid, 0) if plan_util is None else None,
                "utilization": (round(busy.get(rid, 0) / cap, 4) if cap else None) if plan_util is None else plan_util.get(rid),
            }
        )
    out.sort(key=lambda r: (r["area_order"], r["kind"] != "MACHINE", r["code"]))
    return out


def order_chain(s: Session, ctx: Ctx, plan_id: uuid.UUID, order_key: str) -> dict[str, Any]:
    """Operations of one order (and its component orders) with dependencies, for Gantt highlighting."""
    from monxuplan_engine.views import chain_shard
    from monxuplan_engine.views import order_chain as engine_chain

    plan = _get_plan(s, ctx, plan_id)
    shard = plan_store.document(s, plan, plan_store.DOC_CHAINS, chain_shard(order_key))
    if shard is not None or plan_store.has_documents(s, plan, plan_store.DOC_CHAINS):
        chain = (shard or {}).get(order_key)
        if chain is None:
            raise NotFound("Order not in this plan")
        return chain
    cp, res = replay(s, plan)  # plans stored before the read models
    oi = cp.order_index.get(order_key)
    if oi is None:
        raise NotFound("Order not in this plan")
    return engine_chain(res, oi)


# =============================================================================================
# Operation / order detail
# =============================================================================================


def operation_detail(s: Session, ctx: Ctx, plan_id: uuid.UUID, op_key: str) -> dict[str, Any]:
    plan = _get_plan(s, ctx, plan_id)
    r = s.scalar(select(ScheduledOperation).where(ScheduledOperation.plan_id == plan.id, ScheduledOperation.op_key == op_key))
    unsched = None if r is not None else next(iter(plan_store.unscheduled(s, plan, op_key=op_key, limit=1)), None)
    if r is None and unsched is None:
        raise NotFound("Operation not in this plan")
    out: dict[str, Any] = {"op_id": op_key, "scheduled": row_dict(r) if r else None, "unscheduled": unsched}
    if r is not None:
        ex = plan_store.explanation(s, plan, r.resource_key, op_key)
        if ex:
            out["explanation"] = ex
    order_key = r.order_key if r else unsched["order_id"]
    o = s.get(ProductionOrder, uuid.UUID(order_key)) if _is_uuid(order_key) else None
    if o is not None:
        it = s.get(Item, o.item_id)
        out["order"] = {"id": str(o.id), "number": o.number, "item": it.code if it else None, "item_name": it.name if it else None, "quantity": o.quantity, "due": to_json(_aware(o.due_date)), "priority": o.priority, "status": o.status, "expedite": o.expedite}
        poo = s.scalar(select(ProductionOrderOperation).where(ProductionOrderOperation.order_id == o.id, ProductionOrderOperation.seq == int(op_key.split("/")[-1])))
        if poo is not None:
            ro = s.get(RoutingOperation, poo.routing_operation_id) if poo.routing_operation_id else None
            out["operation"] = {"code": poo.code, "name": poo.name, "status": poo.status, "completed_quantity": poo.completed_quantity, "instructions": ro.instructions if ro else None, "setup_attributes": ro.setup_attributes if ro else {}}
    pegs = plan_store.pegs(s, plan, consumer_orders=[order_key], consumer_op=op_key)
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
    res_order = plan_store.order_result(s, plan, order_key)
    rows = list(s.scalars(select(ScheduledOperation).where(ScheduledOperation.plan_id == plan.id, ScheduledOperation.order_key == order_key).order_by(ScheduledOperation.start)))
    details = plan.kpi_details or {}
    chain = details.get("root_causes", {}).get(order_key, [])
    late = next((x for x in details.get("late_orders", []) if x["order_id"] == order_key), None)
    if late is None and res_order is not None and res_order["status"] in ("LATE", "UNSCHEDULED", "PARTIAL"):
        # beyond the most delayed orders kept in the plan's KPI details: the stored order result
        late = {k: res_order.get(k) for k in ("order_id", "number", "status", "lateness_minutes", "due", "end", "cause")}
        late["earliest_possible_infinite_capacity"] = res_order.get("earliest_possible_end")
    return {
        "result": res_order,
        "operations": [row_dict(r) for r in rows],
        "root_cause": chain,
        "deadline": late,
        "pegging": plan_store.pegs(s, plan, consumer_orders=[order_key], supply_orders=[order_key]),
        "unscheduled": plan_store.unscheduled(s, plan, order_key=order_key),
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
    SO, POO = ScheduledOperation, ProductionOrderOperation
    q = (
        select(SO.op_key, SO.order_key, SO.order_id, SO.resource_key, SO.setup_start, SO.start, SO.end, SO.setup_minutes, SO.quantity, SO.is_late, SO.is_fixed, POO.name.label("op_name"), POO.status.label("op_status"))
        .outerjoin(POO, POO.id == SO.order_operation_id)
        .where(SO.plan_id == plan.id, SO.end >= t0, SO.setup_start <= t1)
        .order_by(SO.resource_key, SO.setup_start)
    )
    if resource_id:
        q = q.where(SO.resource_key == resource_id)
    rows = s.execute(q.limit(DISPATCH_MAX_ROWS + 1)).all()
    truncated = len(rows) > DISPATCH_MAX_ROWS
    rows = rows[:DISPATCH_MAX_ROWS]
    PO = ProductionOrder
    orders = {o.id: o for part in plan_store.chunks({r.order_id for r in rows if r.order_id}) for o in s.execute(select(PO.id, PO.number, PO.item_id).where(PO.id.in_(part)))}
    items = {i.id: i for part in plan_store.chunks({o.item_id for o in orders.values()}) for i in s.execute(select(Item.id, Item.code, Item.name).where(Item.id.in_(part)))}
    res = {str(r.id): r for r in s.execute(select(Resource.id, Resource.code).where(Resource.plant_id == plant_id))}
    plan_orders = plan_store.order_results(s, plan, {r.order_key for r in rows})
    out = []
    for r in rows:
        o = orders.get(r.order_id)
        it = items.get(o.item_id) if o else None
        out.append(
            {
                "resource_id": r.resource_key,
                "resource": res[r.resource_key].code if r.resource_key in res else r.resource_key,
                "op_id": r.op_key,
                "operation": r.op_name or r.op_key,
                "order": o.number if o else None,
                "product": it.code if it else None,
                "product_name": it.name if it else None,
                "quantity": r.quantity,
                "setup_start": to_json(_aware(r.setup_start)),
                "start": to_json(_aware(r.start)),
                "end": to_json(_aware(r.end)),
                "setup_minutes": r.setup_minutes,
                "material": plan_orders.get(r.order_key, {}).get("material_status"),
                "status": r.op_status or "PLANNED",
                "late": r.is_late,
                "fixed": r.is_fixed,
            }
        )
    return {"plan": plan_summary(plan), "from": t0.isoformat(), "to": t1.isoformat(), "rows": out, "truncated": truncated, "row_limit": DISPATCH_MAX_ROWS}


def supervisor_view(s: Session, ctx: Ctx, plant_id: uuid.UUID, area_id: uuid.UUID | None = None) -> dict[str, Any]:
    ctx.require("plan:read")
    plan = _published_or_head(s, ctx, plant_id)
    t = now()
    rq = select(Resource.id, Resource.code, Resource.status).where(Resource.plant_id == plant_id, Resource.kind.in_(["MACHINE", "WORK_CENTER"]))
    if area_id is not None:
        rq = rq.where(Resource.area_id == area_id)
    res = {str(r.id): r for r in s.execute(rq)}
    SO, POO, PO = ScheduledOperation, ProductionOrderOperation, ProductionOrder
    q = (
        select(SO.op_key, SO.order_key, SO.order_id, SO.resource_key, SO.setup_start, SO.start, SO.end, SO.quantity, POO.name.label("op_name"), POO.status.label("op_status"))
        .outerjoin(POO, POO.id == SO.order_operation_id)
        .where(SO.plan_id == plan.id, SO.end >= t - timedelta(hours=12), SO.setup_start <= t + timedelta(hours=16))
        .order_by(SO.setup_start)
    )
    rows = []
    if area_id is not None:
        for part in plan_store.chunks(res):
            rows.extend(s.execute(q.where(SO.resource_key.in_(part))).all())
        rows.sort(key=lambda r: _aware(r.setup_start))
    else:
        rows = s.execute(q).all()
    blocked_orders = set(s.scalars(select(PO.id).where(PO.plant_id == plant_id, PO.status == "BLOCKED")))
    short = {"SHORTAGE", "LATE_SUPPLY"}
    material_orders = {k for k, v in plan_store.material_status_keys(s, plan, short).items()}
    by_res: dict[str, dict[str, Any]] = {}
    for rid, rr in res.items():
        by_res[rid] = {"resource_id": rid, "resource": rr.code, "status": rr.status, "now": None, "next": [], "delayed": [], "blocked": [], "material_issues": [], "delayed_total": 0, "blocked_total": 0, "material_issues_total": 0}
    picked: list[dict[str, Any]] = []
    cap = SUPERVISOR_LIST_CAP

    def add(slot: dict[str, Any], key: str, item: dict[str, Any]) -> None:
        slot[key + "_total"] += 1
        if len(slot[key]) < cap:
            slot[key].append(item)

    horizon = t + timedelta(hours=16)
    for r in rows:
        slot = by_res.get(r.resource_key)
        if slot is None:
            continue
        item = {"op_id": r.op_key, "order_id": r.order_id, "order": None, "operation": r.op_name, "start": to_json(_aware(r.start)), "end": to_json(_aware(r.end)), "quantity": r.quantity, "status": r.op_status}
        st = r.op_status or "PLANNED"
        ss, ee = _aware(r.setup_start), _aware(r.end)
        if ss <= t < ee:
            slot["now"] = item
            if st not in ("IN_PROGRESS", "COMPLETED") and t - ss > timedelta(minutes=30):
                add(slot, "delayed", {**item, "reason": "planned start passed, not started"})
        elif ss > t:
            if len(slot["next"]) < 3:
                slot["next"].append(item)
        elif ee <= t and st not in ("COMPLETED",):
            add(slot, "delayed", {**item, "reason": "should be finished"})
        if r.order_id in blocked_orders:
            add(slot, "blocked", item)
        if r.order_key in material_orders and ss < horizon:
            add(slot, "material_issues", item)
    # order numbers only for the items shown
    for slot in by_res.values():
        picked.extend(x for x in [slot["now"], *slot["next"], *slot["delayed"], *slot["blocked"], *slot["material_issues"]] if x is not None)
    numbers = {i: n for part in plan_store.chunks({x["order_id"] for x in picked if x["order_id"]}) for i, n in s.execute(select(PO.id, PO.number).where(PO.id.in_(part)))}
    for x in picked:
        oid = x.pop("order_id", None)
        x["order"] = numbers.get(oid)
    return {"plan": {"id": str(plan.id), "number": plan.number, "status": plan.status}, "at": t.isoformat(), "list_limit": cap, "resources": sorted(by_res.values(), key=lambda x: x["resource"])}


def operator_view(s: Session, ctx: Ctx, plant_id: uuid.UUID, resource_id: str) -> dict[str, Any]:
    ctx.require("plan:read")
    plan = _published_or_head(s, ctx, plant_id)
    t = now()
    res = s.get(Resource, uuid.UUID(resource_id))
    if res is None:
        raise NotFound("Resource not found")
    rows = list(s.scalars(select(ScheduledOperation).where(ScheduledOperation.plan_id == plan.id, ScheduledOperation.resource_key == resource_id, ScheduledOperation.end >= t).order_by(ScheduledOperation.setup_start).limit(6)))
    explanations = plan_store.explanations_for(s, plan, resource_id) if rows else {}
    jobs = []
    for r in rows:
        reasons = (explanations.get(r.op_key) or {}).get("reasons", [])
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
                "setup_attributes": reasons and [x for x in reasons if x.get("code") == "SETUP"],
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
    AP, POO, PO = ActualProduction, ProductionOrderOperation, ProductionOrder
    acts = s.execute(
        select(AP.resource_id, AP.start, AP.end, AP.good_quantity, AP.scrap_quantity, POO.seq, POO.name, PO.number)
        .join(POO, POO.id == AP.order_operation_id)
        .join(PO, PO.id == POO.order_id)
        .where(PO.plant_id == plant_id, AP.start >= t0)
        .order_by(AP.start.desc())
        .limit(PVA_MAX_ACTUALS + 1)
    ).all()
    capped = len(acts) > PVA_MAX_ACTUALS
    keys = {f"{x.number}/{x.seq:03d}": x for x in acts[:PVA_MAX_ACTUALS]}
    plans = {p.id: p for p in s.scalars(select(Plan).where(Plan.plant_id == plant_id, Plan.status.in_(["PUBLISHED", "SUPERSEDED"])))}
    planned: dict[str, Any] = {}
    if plans and keys:
        SO = ScheduledOperation
        for part in plan_store.chunks(keys):
            for so in s.execute(select(SO.plan_id, SO.op_key, SO.start, SO.end).where(SO.plan_id.in_(list(plans)), SO.op_key.in_(part))):
                prev = planned.get(so.op_key)
                if prev is None or plans[so.plan_id].created_at > plans[prev.plan_id].created_at:
                    planned[so.op_key] = so
    rows = []
    on_time = 0
    dev_start, dev_dur = [], []
    res_codes = {r.id: r.code for r in s.execute(select(Resource.id, Resource.code).where(Resource.plant_id == plant_id))}
    for key, a in keys.items():
        so = planned.get(key)
        row = {
            "op_id": key,
            "order": a.number,
            "operation": a.name,
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
        "sample_limited_to": PVA_MAX_ACTUALS if capped else None,  # measured on the most recent executions
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
