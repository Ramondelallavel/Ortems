"""Materials: availability per order, shortages, projected inventory, pegging in both directions."""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.errors import NotFound
from ..models import Customer, Item, Plan, ProductionOrder, PurchaseOrder, PurchaseOrderLine, SalesOrder, SalesOrderLine, ScheduledOperation, Supplier
from .context import Ctx
from .engine_view import replay
from .views import _get_plan


def availability(s: Session, ctx: Ctx, plan_id: uuid.UUID) -> dict[str, Any]:
    """Material status per order + shortages list (OK / RISK / SHORTAGE / LATE_SUPPLY)."""
    ctx.require("orders:read")
    plan = _get_plan(s, ctx, plan_id)
    orders = (plan.analysis or {}).get("orders", [])
    counts: dict[str, int] = defaultdict(int)
    for o in orders:
        counts[o.get("material_status") or "NONE"] += 1
    shortages: dict[str, dict[str, Any]] = {}
    for u in (plan.analysis or {}).get("unscheduled", []):
        if u["reason"] != "MATERIAL_SHORTAGE":
            continue
        for m in u["details"].get("materials", []):
            e = shortages.setdefault(m["material_id"], {"material_id": m["material_id"], "material": m["material"], "uom": m.get("uom"), "required": 0.0, "shortfall": 0.0, "operations": [], "orders": set(), "replenishment_lead_time_minutes": m.get("replenishment_lead_time_minutes")})
            e["required"] += m["required"]
            e["shortfall"] += m["shortfall"]
            e["operations"].append(u["op_id"])
            e["orders"].add(u["order_id"])
    for e in shortages.values():
        e["orders"] = sorted(e["orders"])
    # late supply: operations that waited for material, grouped by material
    late: dict[str, dict[str, Any]] = {}
    cp, _res = replay(s, plan)
    for so in s.scalars(select(ScheduledOperation).where(ScheduledOperation.plan_id == plan.id)):
        b = so.binding or {}
        if b.get("type") == "MATERIAL":
            mi = cp.mat_index.get(b.get("ref") or "")
            code = cp.materials[mi].code if mi is not None else b.get("ref")
            e = late.setdefault(b.get("ref"), {"material_id": b.get("ref"), "material": code, "operations": 0, "wait_minutes": 0, "orders": set()})
            e["operations"] += 1
            e["wait_minutes"] += b.get("wait_minutes", 0)
            e["orders"].add(so.order_key)
    for e in late.values():
        e["orders"] = sorted(e["orders"])
    return {
        "plan_id": str(plan.id),
        "status_counts": dict(counts),
        "orders": [{"order_id": o["order_id"], "number": o["number"], "status": o["status"], "material_status": o.get("material_status"), "due": o["due"], "end": o.get("end")} for o in orders if o.get("material_status") not in (None, "NONE")],
        "shortages": sorted(shortages.values(), key=lambda e: -e["shortfall"]),
        "late_supply": sorted(late.values(), key=lambda e: -e["wait_minutes"]),
    }


def projection(s: Session, ctx: Ctx, plan_id: uuid.UUID, material_id: str) -> dict[str, Any]:
    """Projected stock of one material under the plan (supplies, consumptions, safety stock)."""
    ctx.require("orders:read")
    plan = _get_plan(s, ctx, plan_id)
    cp, res = replay(s, plan)
    mi = cp.mat_index.get(material_id)
    if mi is None:
        raise NotFound("Material not used by this plan")
    m = cp.materials[mi]
    acc = res.ledger.accounts[mi]
    level = 0.0
    points = []
    for e in acc.events:
        level += e.delta
        points.append(
            {
                "time": cp.dt(max(e.time, cp.as_of)).isoformat(),
                "delta": round(e.delta, 4),
                "level": round(level, 4),
                "kind": e.kind,
                "ref": e.meta.get("ref") or e.ref,
                "supply_kind": e.meta.get("kind"),
                "supplier": e.meta.get("supplier"),
            }
        )
    alerts = []
    min_level, t = acc.min_level()
    if min_level < -1e-9:
        alerts.append({"type": "STOCKOUT", "at": cp.dt(t).isoformat() if t is not None else None, "level": min_level})
    elif m.safety_stock and min_level < m.safety_stock:
        alerts.append({"type": "SAFETY_STOCK_BREACH", "at": cp.dt(t).isoformat() if t is not None else None, "level": min_level, "safety_stock": m.safety_stock})
    total_in = sum(e.delta for e in acc.events if e.delta > 0)
    total_out = -sum(e.delta for e in acc.events if e.delta < 0)
    if total_out > 0 and level > 3 * total_out:
        alerts.append({"type": "EXCESS_INVENTORY", "level": level})
    return {"material_id": m.id, "code": m.code, "name": m.name, "uom": m.uom, "safety_stock": m.safety_stock, "points": points, "alerts": alerts, "supply_total": total_in, "demand_total": total_out}


def pegging_for_order(s: Session, ctx: Ctx, plan_id: uuid.UUID, order_id: str) -> dict[str, Any]:
    """Sales order → production order → sub-assembly orders → raw materials → purchase orders."""
    ctx.require("orders:read")
    plan = _get_plan(s, ctx, plan_id)
    pegs = (plan.analysis or {}).get("pegging", [])
    by_consumer: dict[str, list[dict]] = defaultdict(list)
    for p in pegs:
        by_consumer[p["consumer_order_id"]].append(p)
    orders = {str(o.id): o for o in s.scalars(select(ProductionOrder).where(ProductionOrder.plant_id == plan.plant_id))}
    items = {str(i.id): i for i in s.scalars(select(Item))}

    def node(oid: str, depth: int, seen: set[str]) -> dict[str, Any]:
        o = orders.get(oid)
        n = {"type": "PRODUCTION_ORDER", "order_id": oid, "number": o.number if o else oid, "item": items[str(o.item_id)].code if o and str(o.item_id) in items else None, "quantity": o.quantity if o else None, "children": []}
        if depth > 10 or oid in seen:
            return n
        agg: dict[tuple, dict[str, Any]] = {}
        for p in by_consumer.get(oid, []):
            mat = items.get(p["material_id"])
            key = (p["material_id"], p["supply_kind"], p.get("supply_ref"), p.get("supply_order_id"))
            e = agg.setdefault(key, {"type": "MATERIAL", "material_id": p["material_id"], "material": mat.code if mat else p["material_id"], "supply_kind": p["supply_kind"], "supply_ref": p.get("supply_ref"), "supply_time": p.get("supply_time"), "quantity": 0.0, "late": False, "children": []})
            e["quantity"] += p["quantity"]
            if p.get("supply_time") and p.get("need_time") and p["supply_time"] > p["need_time"]:
                e["late"] = True
            if p.get("supply_order_id") and not e["children"]:
                e["children"].append(node(p["supply_order_id"], depth + 1, seen | {oid}))
        n["children"] = sorted(agg.values(), key=lambda x: x["material"])
        return n

    root = node(order_id, 0, set())
    o = orders.get(order_id)
    if o is not None and o.sales_order_line_id:
        sl = s.get(SalesOrderLine, o.sales_order_line_id)
        so = s.get(SalesOrder, sl.sales_order_id) if sl else None
        cust = s.get(Customer, so.customer_id) if so else None
        root = {"type": "SALES_ORDER", "number": so.number if so else None, "customer": cust.name if cust else None, "line": sl.line_no if sl else None, "quantity": sl.quantity if sl else None, "due": sl.due_date.isoformat() if sl else None, "children": [root]}
    return root


def impact_of_material(s: Session, ctx: Ctx, plan_id: uuid.UUID, material_id: str, supply_ref: str | None = None) -> dict[str, Any]:
    """Reverse pegging: which orders and customers depend on a material (or one receipt of it)?"""
    ctx.require("orders:read")
    plan = _get_plan(s, ctx, plan_id)
    pegs = (plan.analysis or {}).get("pegging", [])
    by_supply_order: dict[str, list[dict]] = defaultdict(list)
    for p in pegs:
        if p.get("supply_order_id"):
            by_supply_order[p["supply_order_id"]].append(p)
    direct = [p for p in pegs if p["material_id"] == material_id and (supply_ref is None or p.get("supply_ref") == supply_ref or p.get("supply_id") == supply_ref)]
    affected: set[str] = set()
    frontier = [p["consumer_order_id"] for p in direct]
    while frontier:
        oid = frontier.pop()
        if oid in affected:
            continue
        affected.add(oid)
        for p in by_supply_order.get(oid, []):
            frontier.append(p["consumer_order_id"])
    orders = {str(o.id): o for o in s.scalars(select(ProductionOrder).where(ProductionOrder.id.in_([uuid.UUID(x) for x in affected if _is_uuid(x)])))} if affected else {}
    custs = {c.id: c for c in s.scalars(select(Customer))}
    res_orders = {o["order_id"]: o for o in (plan.analysis or {}).get("orders", [])}
    rows = []
    revenue = 0.0
    for oid in affected:
        o = orders.get(oid)
        if o is None:
            continue
        sl = s.get(SalesOrderLine, o.sales_order_line_id) if o.sales_order_line_id else None
        if sl is not None and sl.unit_price:
            revenue += float(sl.unit_price) * float(sl.quantity)
        rows.append(
            {
                "order_id": oid,
                "number": o.number,
                "customer": custs[o.customer_id].name if o.customer_id in custs else None,
                "due": o.due_date.isoformat(),
                "planned_end": res_orders.get(oid, {}).get("end"),
                "status": res_orders.get(oid, {}).get("status"),
                "direct": any(p["consumer_order_id"] == oid for p in direct),
                "revenue": round(float(sl.unit_price) * float(sl.quantity), 2) if sl is not None and sl.unit_price else None,
            }
        )
    rows.sort(key=lambda r: r["due"])
    item = s.get(Item, uuid.UUID(material_id)) if _is_uuid(material_id) else None
    return {
        "material_id": material_id,
        "material": item.code if item else material_id,
        "supply_ref": supply_ref,
        "orders_affected": len(rows),
        "customers_affected": len({r["customer"] for r in rows if r["customer"]}),
        "revenue_exposure": round(revenue, 2) if revenue else None,
        "orders": rows,
    }


def receipts(s: Session, ctx: Ctx, plant_id: uuid.UUID) -> list[dict[str, Any]]:
    ctx.require("orders:read")
    rows = s.execute(
        select(PurchaseOrderLine, PurchaseOrder, Item, Supplier)
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
        .join(Item, Item.id == PurchaseOrderLine.item_id)
        .join(Supplier, Supplier.id == PurchaseOrder.supplier_id)
        .where(PurchaseOrderLine.status == "OPEN")
        .order_by(PurchaseOrderLine.expected_date)
    ).all()
    return [
        {
            "line_id": str(ln.id),
            "purchase_order": po.number,
            "line": ln.line_no,
            "item_id": str(it.id),
            "item": it.code,
            "quantity": ln.quantity,
            "received": ln.received_quantity,
            "expected": ln.expected_date.isoformat(),
            "original": ln.original_date.isoformat() if ln.original_date else None,
            "confirmed": ln.confirmed,
            "supplier": sup.name,
            "supplier_code": sup.code,
        }
        for ln, po, it, sup in rows
        if po.plant_id in (None, plant_id)
    ]


def _is_uuid(v: str) -> bool:
    try:
        uuid.UUID(v)
        return True
    except (ValueError, TypeError):
        return False


_ = Plan
