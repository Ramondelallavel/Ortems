"""Order-centric read models: order book with plan status, BOM trees."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..core.errors import NotFound
from ..models import Bom, BomLine, Customer, Item, Plan, PlanOrder, Plant, ProductFamily, ProductionOrder, Scenario
from . import plan_store
from .context import Ctx
from .masterdata import to_json


def _plan_for(s: Session, plant_id: uuid.UUID | None, plan_id: uuid.UUID | None) -> Plan | None:
    if plan_id:
        return s.get(Plan, plan_id)
    if plant_id:
        plant = s.get(Plant, plant_id)
        if plant and plant.live_scenario_id:
            sc = s.get(Scenario, plant.live_scenario_id)
            if sc and sc.head_plan_id:
                return s.get(Plan, sc.head_plan_id)
    return None


PLAN_STATUSES = ("ON_TIME", "LATE", "UNSCHEDULED", "PARTIAL", "COMPLETED")
MATERIAL_STATUSES = ("OK", "RISK", "SHORTAGE", "LATE_SUPPLY")
ORDER_SORTS: dict[str, Any] = {
    "number": ProductionOrder.number,
    "due_date": ProductionOrder.due_date,
    "release_date": ProductionOrder.release_date,
    "priority": ProductionOrder.priority,
    "quantity": ProductionOrder.quantity,
    "status": ProductionOrder.status,
    "planned_start": PlanOrder.start,
    "planned_end": PlanOrder.end,
    "lateness_minutes": PlanOrder.lateness_minutes,
    "plan_status": PlanOrder.status,
    "material_status": PlanOrder.material_status,
}


def list_orders(s: Session, ctx: Ctx, plant_id: uuid.UUID | None, q: str | None, filters: dict[str, Any], offset: int, limit: int, plan_id: uuid.UUID | None = None) -> dict[str, Any]:
    """The order book, paged, sorted and filtered in the database — also by the result of each order
    in the plan (a large plant has 100 000+ open orders; only the requested page leaves the server)."""
    ctx.require("orders:read")
    if plant_id:
        ctx.require_plant(plant_id)
    plan = _plan_for(s, plant_id, plan_id)
    if plan is not None:
        ctx.require_plant(plan.plant_id)
    stmt = select(ProductionOrder)
    if plant_id:
        stmt = stmt.where(ProductionOrder.plant_id == plant_id)
    elif ctx.plant_ids is not None:
        stmt = stmt.where(ProductionOrder.plant_id.in_(list(ctx.plant_ids)))  # a plant-scoped user sees their plants only
    status = filters.get("status")
    if status:
        stmt = stmt.where(ProductionOrder.status.in_(status.split(",")))
    elif not filters.get("all"):
        stmt = stmt.where(ProductionOrder.status.notin_(["COMPLETED", "CANCELLED"]))
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.join(Item, Item.id == ProductionOrder.item_id).where(or_(func.lower(ProductionOrder.number).like(like), func.lower(Item.code).like(like), func.lower(Item.name).like(like)))
    sort = filters.get("sort") or "due_date"
    sort_col = ORDER_SORTS.get(sort, ProductionOrder.due_date)
    wanted = [x for x in (filters.get("plan_status") or "").split(",") if x]
    if plan is not None and (wanted or sort_col.class_ is PlanOrder):
        on = and_(PlanOrder.plan_id == plan.id, PlanOrder.order_id == ProductionOrder.id)
        if wanted:
            stmt = stmt.join(PlanOrder, on).where(or_(PlanOrder.status.in_([x for x in wanted if x in PLAN_STATUSES]), PlanOrder.material_status.in_([x for x in wanted if x in MATERIAL_STATUSES])))
        else:
            stmt = stmt.outerjoin(PlanOrder, on)
    elif sort_col.class_ is PlanOrder:
        sort_col = ProductionOrder.due_date
    total = s.scalar(select(func.count()).select_from(stmt.subquery()))
    order = sort_col.desc() if filters.get("dir") == "desc" else sort_col.asc()
    rows = list(s.scalars(stmt.order_by(order.nulls_last(), ProductionOrder.number).offset(offset).limit(min(limit, 5000))))
    items = {i.id: i for i in s.scalars(select(Item).where(Item.id.in_({r.item_id for r in rows})))} if rows else {}
    fams = {f.id: f for f in s.scalars(select(ProductFamily))}
    cust_ids = {r.customer_id for r in rows if r.customer_id}
    custs = {c.id: c for c in s.scalars(select(Customer).where(Customer.id.in_(cust_ids)))} if cust_ids else {}
    # plan results of this page only (a plan holds one result per order: 100 000+ at scale)
    res_by_order = plan_store.order_results(s, plan, [str(r.id) for r in rows]) if plan is not None and rows else {}
    legacy_causes: dict[str, Any] | None = None
    out = []
    for r in rows:
        it = items.get(r.item_id)
        fam = fams.get(it.family_id) if it and it.family_id else None
        pr = res_by_order.get(str(r.id), {})
        cause = pr.get("cause")
        if cause is None and pr and "cause" not in pr and plan is not None:  # plans stored before plan_order
            if legacy_causes is None:
                legacy_causes = {x["order_id"]: x.get("cause") for x in (plan.kpi_details or {}).get("late_orders", [])}
            cause = legacy_causes.get(str(r.id))
        out.append(
            {
                "id": str(r.id),
                "number": r.number,
                "item_id": str(r.item_id),
                "item": it.code if it else None,
                "item_name": it.name if it else None,
                "family": fam.code if fam else None,
                "quantity": r.quantity,
                "completed_quantity": r.completed_quantity,
                "customer": custs[r.customer_id].name if r.customer_id in custs else None,
                "customer_priority": custs[r.customer_id].priority if r.customer_id in custs else None,
                "strategic": custs[r.customer_id].is_strategic if r.customer_id in custs else False,
                "priority": r.priority,
                "expedite": r.expedite,
                "status": r.status,
                "source": r.source,
                "release_date": to_json(r.release_date),
                "due_date": to_json(r.due_date),
                "requested_date": to_json(r.requested_date),
                "promised_date": to_json(r.promised_date),
                "sales_order_line": str(r.sales_order_line_id) if r.sales_order_line_id else None,
                "sales_order": None,
                "plan_status": pr.get("status"),
                "planned_start": pr.get("start"),
                "planned_end": pr.get("end"),
                "lateness_minutes": pr.get("lateness_minutes"),
                "material_status": pr.get("material_status"),
                "earliest_possible_end": pr.get("earliest_possible_end"),
                "cause": cause,
                "version": r.version,
            }
        )
    return {"items": out, "total": total, "plan_id": str(plan.id) if plan else None, "plan_number": plan.number if plan else None}


def bom_tree(s: Session, item_id: uuid.UUID, depth: int = 0, qty: float = 1.0, seen: frozenset = frozenset()) -> dict[str, Any]:
    it = s.get(Item, item_id)
    if it is None:
        raise NotFound("Item not found")
    node: dict[str, Any] = {"item_id": str(it.id), "code": it.code, "name": it.name, "type": it.item_type, "make_or_buy": it.make_or_buy, "uom": it.uom, "quantity": round(qty, 6), "children": []}
    if item_id in seen or depth > 15:
        node["cycle"] = True
        return node
    bom = s.scalar(select(Bom).where(Bom.item_id == item_id, Bom.is_active.is_(True)))
    if bom is None:
        return node
    for ln in s.scalars(select(BomLine).where(BomLine.bom_id == bom.id).order_by(BomLine.position)):
        q = qty * float(ln.quantity_per) / float(bom.base_quantity or 1)
        child = bom_tree(s, ln.component_id, depth + 1, q, seen | {item_id})
        child["operation_seq"] = ln.operation_seq
        child["scrap_pct"] = ln.scrap_pct
        node["children"].append(child)
    return node
