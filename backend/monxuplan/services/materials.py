"""Materials: availability per order, shortages, projected inventory, pegging in both directions."""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from ..core.errors import NotFound
from ..models import Customer, Item, Plan, PlanOrder, ProductionOrder, PurchaseOrder, PurchaseOrderLine, SalesOrder, SalesOrderLine, ScheduledOperation, Supplier
from . import plan_store
from .context import Ctx
from .engine_view import replay
from .views import _get_plan

ORDERS_LIMIT = 2000  # order rows of the availability view (most critical first; totals are given)
IMPACT_LIMIT = 2000  # affected orders listed by a reverse pegging (totals are given)
_SEVERITY = {"SHORTAGE": 0, "LATE_SUPPLY": 1, "RISK": 2, "OK": 3}


def _item_codes(s: Session, ids: set[str]) -> dict[str, Item]:
    ok = [uuid.UUID(x) for x in ids if _is_uuid(x)]
    return {str(i.id): i for part in plan_store.chunks(ok) for i in s.execute(select(Item.id, Item.code, Item.uom).where(Item.id.in_(part)))}


def availability(s: Session, ctx: Ctx, plan_id: uuid.UUID) -> dict[str, Any]:
    """Material status per order + shortages list (OK / RISK / SHORTAGE / LATE_SUPPLY)."""
    ctx.require("orders:read")
    plan = _get_plan(s, ctx, plan_id)
    counts = plan_store.material_status_counts(s, plan)
    shortages: dict[str, dict[str, Any]] = {}
    for u in plan_store.unscheduled(s, plan, reason="MATERIAL_SHORTAGE"):
        for m in u["details"].get("materials", []):
            e = shortages.setdefault(m["material_id"], {"material_id": m["material_id"], "material": m["material"], "uom": m.get("uom"), "required": 0.0, "shortfall": 0.0, "operations": [], "orders": set(), "replenishment_lead_time_minutes": m.get("replenishment_lead_time_minutes")})
            e["required"] += m["required"]
            e["shortfall"] += m["shortfall"]
            e["operations"].append(u["op_id"])
            e["orders"].add(u["order_id"])
    for e in shortages.values():
        e["orders"] = sorted(e["orders"])
    # late supply: operations whose start was bound by a material, grouped by material
    late: dict[str, dict[str, Any]] = {}
    SO = ScheduledOperation
    for so in s.execute(select(SO.order_key, SO.binding).where(SO.plan_id == plan.id, SO.binding["type"].as_string() == "MATERIAL")):
        b = so.binding or {}
        e = late.setdefault(b.get("ref"), {"material_id": b.get("ref"), "material": b.get("ref"), "operations": 0, "wait_minutes": 0, "orders": set()})
        e["operations"] += 1
        e["wait_minutes"] += b.get("wait_minutes", 0)
        e["orders"].add(so.order_key)
    codes = _item_codes(s, {k for k in late if k})
    for k, e in late.items():
        if k in codes:
            e["material"] = codes[k].code
        e["orders"] = sorted(e["orders"])
    # orders with a material status, most critical first
    PO = PlanOrder
    rows = s.execute(
        select(PO.order_key, PO.number, PO.status, PO.material_status, PO.due, PO.end)
        .where(PO.plan_id == plan.id, PO.material_status.in_(list(_SEVERITY)))
        .order_by(case(_SEVERITY, value=PO.material_status, else_=9), PO.due, PO.order_key)
        .limit(ORDERS_LIMIT)
    ).all()
    orders = [{"order_id": r.order_key, "number": r.number, "status": r.status, "material_status": r.material_status, "due": plan_store.iso(r.due), "end": plan_store.iso(r.end)} for r in rows]
    if not orders:
        legacy = [o for o in plan_store.order_results(s, plan).values() if o.get("material_status") not in (None, "NONE")]
        legacy.sort(key=lambda o: (_SEVERITY.get(o["material_status"], 9), o.get("due") or ""))
        orders = [{"order_id": o["order_id"], "number": o["number"], "status": o["status"], "material_status": o.get("material_status"), "due": o["due"], "end": o.get("end")} for o in legacy[:ORDERS_LIMIT]]
    total = sum(v for k, v in counts.items() if k not in (None, "NONE"))
    return {
        "plan_id": str(plan.id),
        "status_counts": counts,
        "orders": orders,
        "orders_total": total,
        "orders_limit": ORDERS_LIMIT,
        "shortages": sorted(shortages.values(), key=lambda e: -e["shortfall"]),
        "late_supply": sorted(late.values(), key=lambda e: -e["wait_minutes"]),
    }


def projection(s: Session, ctx: Ctx, plan_id: uuid.UUID, material_id: str) -> dict[str, Any]:
    """Projected stock of one material under the plan (supplies, consumptions, safety stock)."""
    ctx.require("orders:read")
    plan = _get_plan(s, ctx, plan_id)
    doc = plan_store.document(s, plan, plan_store.DOC_MATERIAL, material_id)
    if doc is not None:
        return doc
    if plan_store.has_documents(s, plan, plan_store.DOC_MATERIAL):
        raise NotFound("Material not used by this plan")
    from monxuplan_engine.views import material_projection

    cp, res = replay(s, plan)  # plans stored before the read models
    mi = cp.mat_index.get(material_id)
    if mi is None:
        raise NotFound("Material not used by this plan")
    return material_projection(res, mi)


def pegging_for_order(s: Session, ctx: Ctx, plan_id: uuid.UUID, order_id: str) -> dict[str, Any]:
    """Sales order → production order → sub-assembly orders → raw materials → purchase orders."""
    ctx.require("orders:read")
    plan = _get_plan(s, ctx, plan_id)
    # the order's supply tree, level by level (component orders feed their parents through pegging)
    by_consumer: dict[str, list[dict]] = defaultdict(list)
    seen: set[str] = set()
    frontier = [order_id]
    depth = 0
    while frontier and depth <= 10:
        batch = [o for o in frontier if o not in seen]
        seen.update(batch)
        frontier = []
        for p in plan_store.pegs(s, plan, consumer_orders=batch):
            by_consumer[p["consumer_order_id"]].append(p)
            if p.get("supply_order_id") and p["supply_order_id"] not in seen:
                frontier.append(p["supply_order_id"])
        depth += 1
    order_uuids = [uuid.UUID(x) for x in seen if _is_uuid(x)]
    orders = {str(o.id): o for part in plan_store.chunks(order_uuids) for o in s.execute(select(ProductionOrder.id, ProductionOrder.number, ProductionOrder.item_id, ProductionOrder.quantity, ProductionOrder.sales_order_line_id).where(ProductionOrder.id.in_(part)))}
    items = _item_codes(s, {str(o.item_id) for o in orders.values()} | {p["material_id"] for lst in by_consumer.values() for p in lst})

    def node(oid: str, depth: int, seen: set[str]) -> dict[str, Any]:
        o = orders.get(oid)
        it = items.get(str(o.item_id)) if o else None
        n = {"type": "PRODUCTION_ORDER", "order_id": oid, "number": o.number if o else oid, "item": it.code if it else None, "quantity": o.quantity if o else None, "children": []}
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
    direct = [p for p in plan_store.pegs(s, plan, material_id=material_id) if supply_ref is None or p.get("supply_ref") == supply_ref or p.get("supply_id") == supply_ref]
    direct_orders = {p["consumer_order_id"] for p in direct}
    affected: set[str] = set()
    frontier = list(direct_orders)
    while frontier:
        batch = [o for o in set(frontier) if o not in affected]
        affected.update(batch)
        frontier = [p["consumer_order_id"] for p in plan_store.pegs(s, plan, supply_orders=batch)] if batch else []
    ids = [uuid.UUID(x) for x in affected if _is_uuid(x)]
    PO = ProductionOrder
    orders = {str(o.id): o for part in plan_store.chunks(ids) for o in s.execute(select(PO.id, PO.number, PO.customer_id, PO.due_date, PO.sales_order_line_id).where(PO.id.in_(part)))}
    cust_ids = {o.customer_id for o in orders.values() if o.customer_id}
    custs = {c.id: c for part in plan_store.chunks(cust_ids) for c in s.execute(select(Customer.id, Customer.name).where(Customer.id.in_(part)))}
    line_ids = {o.sales_order_line_id for o in orders.values() if o.sales_order_line_id}
    lines = {sl.id: sl for part in plan_store.chunks(line_ids) for sl in s.execute(select(SalesOrderLine.id, SalesOrderLine.unit_price, SalesOrderLine.quantity).where(SalesOrderLine.id.in_(part)))}
    res_orders = plan_store.order_results(s, plan, list(orders))
    rows = []
    revenue = 0.0
    for oid, o in orders.items():
        sl = lines.get(o.sales_order_line_id) if o.sales_order_line_id else None
        value = round(float(sl.unit_price) * float(sl.quantity), 2) if sl is not None and sl.unit_price else None
        if value:
            revenue += value
        rows.append(
            {
                "order_id": oid,
                "number": o.number,
                "customer": custs[o.customer_id].name if o.customer_id in custs else None,
                "due": o.due_date.isoformat(),
                "planned_end": res_orders.get(oid, {}).get("end"),
                "status": res_orders.get(oid, {}).get("status"),
                "direct": oid in direct_orders,
                "revenue": value,
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
        "orders": rows[:IMPACT_LIMIT],
        "orders_limit": IMPACT_LIMIT,
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
