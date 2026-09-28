"""Master Production Schedule, MRP and aggregate planning from the database."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from monxuplan_engine.mrp import MrpProblem, run_mrp
from monxuplan_engine.providers.mip import AggregateProblem, MipProvider

from ..core.clock import now
from ..core.errors import NotFound, ValidationFailed
from ..models import (
    Bom,
    BomLine,
    Calendar,
    Demand,
    Inventory,
    Item,
    OperationResource,
    Plant,
    ProductFamily,
    ProductionOrder,
    PurchaseOrder,
    PurchaseOrderLine,
    Resource,
    ResourceGroup,
    ResourceGroupMember,
    Routing,
    RoutingOperation,
    SalesOrderLine,
)
from . import audit
from .context import Ctx
from .masterdata import generate_order_operations

OPEN = ("PLANNED", "FIRMED", "RELEASED", "IN_PRODUCTION", "PARTIALLY_COMPLETED")


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def build_mrp_problem(s: Session, plant: Plant, weeks: int, start: date | None) -> tuple[MrpProblem, dict[str, Item]]:
    tz = ZoneInfo(plant.timezone or "UTC")
    start = start or _week_start(now().astimezone(tz).date())
    items = {str(i.id): i for i in s.scalars(select(Item).where(Item.is_active.is_(True)))}
    inv = defaultdict(float)
    for r in s.scalars(select(Inventory).where(Inventory.plant_id == plant.id)):
        inv[str(r.item_id)] += r.available
    boms = {b.item_id: b for b in s.scalars(select(Bom).where(Bom.is_active.is_(True)))}
    bom_lines = []
    for ln in s.scalars(select(BomLine)):
        b = next((b for b in boms.values() if b.id == ln.bom_id), None)
        if b is not None:
            bom_lines.append({"parent_id": str(b.item_id), "component_id": str(ln.component_id), "qty_per": float(ln.quantity_per) / float(b.base_quantity or 1), "scrap_pct": ln.scrap_pct or 0})
    demands = []
    for d in s.scalars(select(Demand).where(Demand.plant_id == plant.id, Demand.period_start >= start - timedelta(days=7))):
        demands.append({"item_id": str(d.item_id), "date": d.period_start, "quantity": float(d.quantity), "type": d.demand_type if d.demand_type in ("FORECAST", "CUSTOMER_ORDER", "FIRM", "EXPECTED", "PROMOTIONAL", "SAFETY_STOCK") else "FORECAST", "ref": d.ref})
    for sl in s.scalars(select(SalesOrderLine).where(SalesOrderLine.status == "OPEN", (SalesOrderLine.plant_id == plant.id) | (SalesOrderLine.plant_id.is_(None)))):
        open_q = float(sl.quantity) - float(sl.delivered_quantity or 0)
        if open_q > 0:
            demands.append({"item_id": str(sl.item_id), "date": sl.due_date.astimezone(tz).date(), "quantity": open_q, "type": "CUSTOMER_ORDER", "ref": str(sl.id)})
    receipts = []
    for o in s.scalars(select(ProductionOrder).where(ProductionOrder.plant_id == plant.id, ProductionOrder.status.in_(OPEN))):
        receipts.append({"item_id": str(o.item_id), "date": o.due_date.astimezone(tz).date(), "quantity": float(o.quantity) - float(o.completed_quantity or 0), "type": "PRODUCTION_ORDER", "ref": o.number})
    for ln, po in s.execute(select(PurchaseOrderLine, PurchaseOrder).join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id).where(PurchaseOrderLine.status == "OPEN")).all():
        if po.plant_id not in (None, plant.id):
            continue
        receipts.append({"item_id": str(ln.item_id), "date": ln.expected_date.astimezone(tz).date(), "quantity": float(ln.quantity) - float(ln.received_quantity or 0), "type": "PURCHASE_ORDER", "ref": po.number})
    fams = {f.id: f.code for f in s.scalars(select(ProductFamily))}
    mitems = []
    for iid, it in items.items():
        policy = it.lot_policy if it.lot_policy in ("LFL", "FIXED", "MIN", "MULTIPLE", "MAX", "EOQ") else "LFL"
        lot_qty = it.fixed_lot if policy == "FIXED" else it.economic_lot if policy == "EOQ" else it.lot_multiple if policy == "MULTIPLE" else None
        mitems.append(
            {
                "id": iid,
                "code": it.code,
                "make_or_buy": it.make_or_buy if it.make_or_buy in ("MAKE", "BUY") else "MAKE",
                "lead_time_days": float(it.production_lead_time_days if it.make_or_buy == "MAKE" else it.purchase_lead_time_days) or 0,
                "lot_policy": policy,
                "lot_qty": lot_qty,
                "min_lot": it.min_lot,
                "max_lot": it.max_lot,
                "multiple": it.lot_multiple,
                "safety_stock": float(it.safety_stock or 0),
                "on_hand": inv.get(iid, 0.0),
                "integer": it.quantity_type == "INTEGER",
                "family": fams.get(it.family_id),
            }
        )
    pb = MrpProblem.model_validate({"start": start, "bucket_days": 7, "periods": weeks, "items": mitems, "bom": bom_lines, "demands": demands, "receipts": receipts})
    return pb, items


def run_mps(s: Session, ctx: Ctx, plant_id: uuid.UUID, weeks: int = 12, start: date | None = None) -> dict[str, Any]:
    ctx.require("orders:read")
    ctx.require_plant(plant_id)
    plant = s.get(Plant, plant_id)
    if plant is None:
        raise NotFound("Plant not found")
    pb, items = build_mrp_problem(s, plant, weeks, start)
    out = run_mrp(pb)
    # rough-cut capacity of planned production per resource group and week
    out["capacity"] = rough_cut(s, plant, out, pb)
    for row in out["items"]:
        it = items.get(row["item_id"])
        row["item_type"] = it.item_type if it else None
        row["name"] = it.name if it else None
    return out


def _hours_per_unit(s: Session) -> dict[str, dict[str, float]]:
    """item → {resource group/resource code: minutes per unit (primary resource)}."""
    out: dict[str, dict[str, float]] = defaultdict(dict)
    groups = {g.id: g.code for g in s.scalars(select(ResourceGroup))}
    res = {r.id: r.code for r in s.scalars(select(Resource))}
    routings = {r.id: r for r in s.scalars(select(Routing).where(Routing.is_active.is_(True)))}
    for ro in s.scalars(select(RoutingOperation)):
        r = routings.get(ro.routing_id)
        if r is None:
            continue
        prim = s.scalar(select(OperationResource).where(OperationResource.routing_operation_id == ro.id).order_by(OperationResource.preference).limit(1))
        if prim is None or prim.role == "SUBCONTRACT":
            continue
        key = groups.get(prim.group_id) if prim.group_id else res.get(prim.resource_id)
        if key is None:
            continue
        per_unit = (ro.run_minutes_per_unit or 0) + ((ro.minutes_per_batch or 0) / ro.batch_size if ro.batch_size else 0)
        out[str(r.item_id)][key] = out[str(r.item_id)].get(key, 0.0) + per_unit
    return out


def rough_cut(s: Session, plant: Plant, mrp: dict[str, Any], pb: MrpProblem) -> list[dict[str, Any]]:
    hpu = _hours_per_unit(s)
    periods = len(mrp["periods"])
    load: dict[str, list[float]] = defaultdict(lambda: [0.0] * periods)
    for row in mrp["items"]:
        mins = hpu.get(row["item_id"], {})
        for key, m in mins.items():
            for k in range(periods):
                q = row["planned_receipts"][k] + row["scheduled_receipts"][k]
                load[key][k] += q * m / 60.0
    # capacity per group/resource: weekly working hours of members' calendars (approximation by
    # the calendar's weekly regular hours)
    cal_hours = {}
    from ..models import CalendarShift

    for c in s.scalars(select(Calendar)):
        mins = 0.0
        for sh in s.scalars(select(CalendarShift).where(CalendarShift.calendar_id == c.id, CalendarShift.kind == "REGULAR")):
            a = sh.start_time.hour * 60 + sh.start_time.minute
            b = sh.end_time.hour * 60 + sh.end_time.minute
            dur = (b - a) % 1440 or 1440
            brk = sum(((int(x["end"][:2]) * 60 + int(x["end"][3:5])) - (int(x["start"][:2]) * 60 + int(x["start"][3:5]))) % 1440 for x in (sh.breaks or []))
            mins += dur - brk
        cal_hours[c.id] = mins / 60.0
    res = {r.code: r for r in s.scalars(select(Resource).where(Resource.plant_id == plant.id))}
    members: dict[str, list[Resource]] = defaultdict(list)
    for m, g in s.execute(select(ResourceGroupMember, ResourceGroup).join(ResourceGroup, ResourceGroup.id == ResourceGroupMember.group_id)).all():
        r = next((x for x in res.values() if x.id == m.resource_id), None)
        if r is not None:
            members[g.code].append(r)
    out = []
    for key, vals in sorted(load.items()):
        rs = members.get(key) or ([res[key]] if key in res else [])
        cap = sum(cal_hours.get(r.calendar_id or plant.default_calendar_id, 0.0) * (r.efficiency or 1.0) for r in rs)
        out.append({"key": key, "resources": [r.code for r in rs], "load_h": [round(v, 1) for v in vals], "capacity_h": [round(cap, 1)] * periods, "utilization": [round(v / cap, 3) if cap else None for v in vals]})
    return out


def firm_planned_orders(s: Session, ctx: Ctx, plant_id: uuid.UUID, planned: list[dict[str, Any]]) -> dict[str, Any]:
    """Planner decision: turn MRP proposals into firm production orders (make items only)."""
    ctx.require("orders:write")
    plant = s.get(Plant, plant_id)
    created = []
    tz = ZoneInfo(plant.timezone or "UTC")
    for p in planned:
        it = s.get(Item, uuid.UUID(p["item_id"]))
        if it is None or it.make_or_buy != "MAKE":
            continue
        n = s.scalar(select(ProductionOrder).where(ProductionOrder.number.like("WO-M%")).order_by(ProductionOrder.number.desc()).limit(1))
        seq = int(n.number[4:]) + 1 if n is not None and n.number[4:].isdigit() else 1
        due_d = date.fromisoformat(p["due_date"])
        due = datetime.combine(due_d, datetime.min.time()).replace(hour=22, tzinfo=tz).astimezone(UTC)
        rel = date.fromisoformat(p.get("release_date") or p["due_date"])
        o = ProductionOrder(tenant_id=ctx.tenant_id, number=f"WO-M{seq:05d}", plant_id=plant.id, item_id=it.id, quantity=float(p["quantity"]), status="FIRMED", due_date=due, release_date=datetime.combine(rel, datetime.min.time()).replace(hour=6, tzinfo=tz).astimezone(UTC), source="MRP", priority=5)
        s.add(o)
        s.flush()
        generate_order_operations(s, ctx, o)
        created.append(o.number)
        audit.record(s, ctx, "FIRM_PLANNED_ORDER", "production_order", o.id, o.number, after={"item": it.code, "quantity": o.quantity, "due": due.isoformat()})
    return {"created": created}


def aggregate_plan(s: Session, ctx: Ctx, plant_id: uuid.UUID, weeks: int = 12, integer: bool = False) -> dict[str, Any]:
    """Family × week production plan under resource-group capacity (MIP provider)."""
    ctx.require("plan:read")
    mps = run_mps(s, ctx, plant_id, weeks)
    items = {r["item_id"]: r for r in mps["items"]}
    fam_demand: dict[str, list[float]] = defaultdict(lambda: [0.0] * weeks)
    fam_inv: dict[str, float] = defaultdict(float)
    hpu = _hours_per_unit(s)
    fam_hours: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for iid, row in items.items():
        if row.get("item_type") != "FINISHED" or not row.get("family"):
            continue
        f = row["family"]
        for k in range(weeks):
            fam_demand[f][k] += row["gross_requirements"][k]
        fam_inv[f] += row["on_hand"]
        for key, m in hpu.get(iid, {}).items():
            fam_hours[f][key].append(m / 60.0)
    groups_cap = {c["key"]: c["capacity_h"] for c in mps["capacity"]}
    families = []
    for f, dem in fam_demand.items():
        families.append({"id": f, "demand": dem, "initial_inventory": fam_inv[f], "hours_per_unit": {k: sum(v) / len(v) for k, v in fam_hours[f].items()}, "holding_cost": 1.0, "backlog_cost": 15.0})
    used = {k for f in families for k in f["hours_per_unit"]}
    groups = [{"id": k, "capacity_hours": groups_cap.get(k, [0.0] * weeks), "overtime_max_hours": [c * 0.15 for c in groups_cap.get(k, [0.0] * weeks)], "overtime_cost_per_hour": 40.0} for k in sorted(used)]
    if not families:
        raise ValidationFailed("No finished-goods demand to plan", code="NO_DEMAND")
    pb = AggregateProblem(periods=mps["periods"], families=families, groups=groups, integer=integer)
    out = MipProvider().solve_aggregate(pb)
    out["provider"] = "mip"
    return out
