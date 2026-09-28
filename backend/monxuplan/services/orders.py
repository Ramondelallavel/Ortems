"""Order-centric read models: order book with plan status, BOM trees."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..core.errors import NotFound
from ..models import Bom, BomLine, Customer, Item, Plan, Plant, ProductFamily, ProductionOrder, Scenario
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


def list_orders(s: Session, ctx: Ctx, plant_id: uuid.UUID | None, q: str | None, filters: dict[str, Any], offset: int, limit: int, plan_id: uuid.UUID | None = None) -> dict[str, Any]:
    ctx.require("orders:read")
    if plant_id:
        ctx.require_plant(plant_id)
    stmt = select(ProductionOrder)
    if plant_id:
        stmt = stmt.where(ProductionOrder.plant_id == plant_id)
    status = filters.get("status")
    if status:
        stmt = stmt.where(ProductionOrder.status.in_(status.split(",")))
    elif not filters.get("all"):
        stmt = stmt.where(ProductionOrder.status.notin_(["COMPLETED", "CANCELLED"]))
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.join(Item, Item.id == ProductionOrder.item_id).where(or_(func.lower(ProductionOrder.number).like(like), func.lower(Item.code).like(like), func.lower(Item.name).like(like)))
    total = s.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = list(s.scalars(stmt.order_by(ProductionOrder.due_date).offset(offset).limit(min(limit, 5000))))
    items = {i.id: i for i in s.scalars(select(Item).where(Item.id.in_({r.item_id for r in rows})))} if rows else {}
    fams = {f.id: f for f in s.scalars(select(ProductFamily))}
    custs = {c.id: c for c in s.scalars(select(Customer))}
    plan = _plan_for(s, plant_id, plan_id)
    res_by_order = {o["order_id"]: o for o in ((plan.analysis or {}).get("orders", []) if plan else [])}
    late_info = {x["order_id"]: x for x in ((plan.kpi_details or {}).get("late_orders", []) if plan else [])}
    out = []
    for r in rows:
        it = items.get(r.item_id)
        fam = fams.get(it.family_id) if it and it.family_id else None
        pr = res_by_order.get(str(r.id), {})
        li = late_info.get(str(r.id), {})
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
                "cause": li.get("cause"),
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
