"""Data Quality Center: can I trust my data?

Every check returns a status (OK / WARNING / ERROR), a count and examples with references so the UI
can take the user to the record. ERROR checks that affect open orders block planning (unless forced
by the planner, which is audited) — data problems are never hidden.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.clock import now
from ..models import (
    Bom,
    BomLine,
    Calendar,
    Item,
    LaborPool,
    OperationPrecedence,
    OperationResource,
    Operator,
    OperatorSkill,
    Plant,
    ProductionOrder,
    ProductionOrderOperation,
    PurchaseOrder,
    PurchaseOrderLine,
    Resource,
    ResourceGroupMember,
    Routing,
    RoutingOperation,
    SetupMatrix,
    WorkCenter,
)

OPEN = ("PLANNED", "FIRMED", "RELEASED", "IN_PRODUCTION", "PARTIALLY_COMPLETED")


def _check(code: str, title: str, level: str, items: list[dict[str, Any]], hint: str) -> dict[str, Any]:
    return {
        "code": code,
        "title": title,
        "status": "OK" if not items else level,
        "count": len(items),
        "examples": items[:25],
        "hint": hint,
    }


def run_checks(s: Session, plant_id: uuid.UUID) -> dict[str, Any]:
    plant = s.get(Plant, plant_id)
    items = {i.id: i for i in s.scalars(select(Item))}
    boms = list(s.scalars(select(Bom).where(Bom.is_active.is_(True))))
    bom_items = {b.item_id for b in boms}
    lines = list(s.scalars(select(BomLine)))
    routings = list(s.scalars(select(Routing).where(Routing.is_active.is_(True))))
    routing_items = {r.item_id for r in routings}
    rops = list(s.scalars(select(RoutingOperation)))
    op_res = defaultdict(list)
    for orr in s.scalars(select(OperationResource)):
        op_res[orr.routing_operation_id].append(orr)
    resources = list(s.scalars(select(Resource).where(Resource.plant_id == plant_id, Resource.is_active.is_(True))))
    res_ids = {r.id for r in resources}
    wcs = {w.id: w for w in s.scalars(select(WorkCenter))}
    cals = {c.id for c in s.scalars(select(Calendar))}
    orders = list(s.scalars(select(ProductionOrder).where(ProductionOrder.plant_id == plant_id, ProductionOrder.status.in_(OPEN))))
    open_items = {o.item_id for o in orders}
    order_ops = defaultdict(list)
    if orders:
        for op in s.scalars(select(ProductionOrderOperation).where(ProductionOrderOperation.order_id.in_([o.id for o in orders]))):
            order_ops[op.order_id].append(op)
    used_rops = {op.routing_operation_id for ops in order_ops.values() for op in ops if op.routing_operation_id}
    checks: list[dict[str, Any]] = []

    # ---- products without BOM / routing
    make_items = [i for i in items.values() if i.make_or_buy == "MAKE" and i.is_active and i.item_type in ("FINISHED", "SEMI_FINISHED")]
    no_bom = [{"item_id": str(i.id), "code": i.code, "open_orders": i.id in open_items} for i in make_items if i.id not in bom_items]
    checks.append(_check("PRODUCT_WITHOUT_BOM", "Products without BOM", "WARNING", no_bom, "Make items need a bill of materials for material planning."))
    no_routing = [{"item_id": str(i.id), "code": i.code} for i in make_items if i.id not in routing_items and i.id in open_items]
    checks.append(_check("PRODUCT_WITHOUT_ROUTING", "Products with open orders but no routing", "ERROR", no_routing, "Orders of these products cannot be scheduled."))
    orders_no_ops = [{"order_id": str(o.id), "number": o.number} for o in orders if not order_ops.get(o.id)]
    checks.append(_check("ORDER_WITHOUT_OPERATIONS", "Open orders without operations", "ERROR", orders_no_ops, "Generate operations from the routing or import them."))

    # ---- operations without resource
    no_res = []
    for ro in rops:
        cands = [x for x in op_res.get(ro.id, []) if (x.resource_id in res_ids) or x.group_id is not None]
        if not cands and ro.id in used_rops:
            no_res.append({"routing_operation_id": str(ro.id), "code": ro.code, "name": ro.name})
    checks.append(_check("OPERATION_WITHOUT_RESOURCE", "Operations without a feasible resource", "ERROR", no_res, "Add a primary or alternative resource in the routing."))
    empty_groups = []
    grp_members = defaultdict(set)
    for m in s.scalars(select(ResourceGroupMember)):
        grp_members[m.group_id].add(m.resource_id)
    for ro in rops:
        for x in op_res.get(ro.id, []):
            if x.group_id is not None and not (grp_members.get(x.group_id, set()) & res_ids) and ro.id in used_rops:
                empty_groups.append({"routing_operation_id": str(ro.id), "code": ro.code})
    checks.append(_check("RESOURCE_GROUP_EMPTY", "Operations assigned to an empty resource group", "ERROR", empty_groups, "The group has no active resource in this plant."))

    # ---- resources without calendar
    no_cal = []
    for r in resources:
        if r.kind not in ("MACHINE", "WORK_CENTER") or not r.is_finite:
            continue
        cid = r.calendar_id or (wcs[r.work_center_id].calendar_id if r.work_center_id in wcs else None) or plant.default_calendar_id
        if cid is None or cid not in cals:
            no_cal.append({"resource_id": str(r.id), "code": r.code})
    checks.append(_check("RESOURCE_WITHOUT_CALENDAR", "Resources without calendar", "WARNING", no_cal, "They would be treated as available 24/7."))

    # ---- materials without unit
    no_uom = [{"item_id": str(i.id), "code": i.code} for i in items.values() if not (i.uom or "").strip()]
    checks.append(_check("MATERIAL_WITHOUT_UNIT", "Materials without unit of measure", "WARNING", no_uom, "Quantities cannot be interpreted without a unit."))

    # ---- orders
    past_due = [{"order_id": str(o.id), "number": o.number, "due": o.due_date.isoformat()} for o in orders if _aw(o.due_date) < now()]
    checks.append(_check("ORDER_PAST_DUE", "Open orders already past their due date", "WARNING", past_due, "Review promised dates with the customer."))
    bad_qty = [{"order_id": str(o.id), "number": o.number} for o in orders if (o.quantity or 0) <= 0]
    checks.append(_check("ORDER_INVALID_QUANTITY", "Orders with zero or negative quantity", "ERROR", bad_qty, "Correct the quantity."))

    # ---- invalid routing
    invalid = []
    by_routing = defaultdict(list)
    for ro in rops:
        by_routing[ro.routing_id].append(ro)
    precs = defaultdict(list)
    for pr in s.scalars(select(OperationPrecedence)):
        precs[pr.routing_id].append(pr)
    for rid, lst in by_routing.items():
        seqs = [x.seq for x in lst]
        if len(seqs) != len(set(seqs)):
            invalid.append({"routing_id": str(rid), "problem": "duplicate sequence numbers"})
        for pr in precs.get(rid, []):
            if pr.pred_seq not in seqs or pr.succ_seq not in seqs:
                invalid.append({"routing_id": str(rid), "problem": f"precedence {pr.pred_seq}->{pr.succ_seq} references a missing operation"})
        if _has_cycle([(p.pred_seq, p.succ_seq) for p in precs.get(rid, [])]):
            invalid.append({"routing_id": str(rid), "problem": "precedence cycle"})
        for x in lst:
            if (x.run_minutes_per_unit or 0) <= 0 and (x.fixed_minutes or 0) <= 0 and (x.minutes_per_batch or 0) <= 0:
                invalid.append({"routing_id": str(rid), "problem": f"operation {x.seq} has no run time"})
    checks.append(_check("INVALID_ROUTING", "Invalid routings", "ERROR", invalid, "Fix sequence numbers, precedences and times."))

    # ---- circular BOM
    edges = []
    bom_item = {b.id: b.item_id for b in boms}
    for ln in lines:
        if ln.bom_id in bom_item:
            edges.append((bom_item[ln.bom_id], ln.component_id))
    cyc = _cycle_nodes(edges)
    checks.append(_check("CIRCULAR_BOM", "Circular BOMs", "ERROR", [{"item_id": str(i), "code": items[i].code if i in items else str(i)} for i in cyc], "A component cannot contain its own parent."))

    # ---- missing setup attributes
    missing_setup = []
    for mx in s.scalars(select(SetupMatrix)):
        for i in items.values():
            if i.id in open_items and mx.attribute not in ("item", "family") and mx.attribute not in (i.attributes or {}):
                missing_setup.append({"item_id": str(i.id), "code": i.code, "attribute": mx.attribute, "matrix": mx.code})
    checks.append(_check("MISSING_SETUP", "Items without the attribute used by a setup matrix", "WARNING", missing_setup, "The matrix default changeover time will be used."))

    # ---- skills / labour
    pools = list(s.scalars(select(LaborPool).where(LaborPool.plant_id == plant_id)))
    ops_by_pool = defaultdict(list)
    for o in s.scalars(select(Operator).where(Operator.plant_id == plant_id, Operator.is_active.is_(True))):
        if o.labor_pool_id:
            ops_by_pool[o.labor_pool_id].append(o)
    skills_of = defaultdict(set)
    for osk in s.scalars(select(OperatorSkill)):
        skills_of[osk.operator_id].add(osk.skill_id)
    unknown = []
    for p in pools:
        if p.skill_id is None:
            unknown.append({"labor_pool": p.code, "problem": "pool without skill"})
        for o in ops_by_pool.get(p.id, []):
            if p.skill_id is not None and p.skill_id not in skills_of.get(o.id, set()):
                unknown.append({"labor_pool": p.code, "operator": o.code, "problem": "member lacks the pool skill"})
        if not ops_by_pool.get(p.id):
            unknown.append({"labor_pool": p.code, "problem": "pool without operators"})
    checks.append(_check("UNKNOWN_SKILLS", "Skill / labour pool inconsistencies", "WARNING", unknown, "Operations requiring these pools may be infeasible."))

    # ---- supply
    overdue = []
    for ln, po in s.execute(select(PurchaseOrderLine, PurchaseOrder).join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id).where(PurchaseOrderLine.status == "OPEN")).all():
        if _aw(ln.expected_date) < now() and (ln.quantity or 0) > (ln.received_quantity or 0):
            overdue.append({"purchase_order": po.number, "line": ln.line_no, "item": items[ln.item_id].code if ln.item_id in items else None, "expected": ln.expected_date.isoformat()})
    checks.append(_check("OVERDUE_RECEIPTS", "Overdue purchase receipts", "WARNING", overdue, "Confirm new dates with the supplier: planning assumes they arrive now."))

    summary = {"ok": sum(1 for c in checks if c["status"] == "OK"), "warnings": sum(1 for c in checks if c["status"] == "WARNING"), "errors": sum(1 for c in checks if c["status"] == "ERROR")}
    return {"plant_id": str(plant_id), "checked_at": now().isoformat(), "summary": summary, "checks": checks, "planning_blocked": summary["errors"] > 0}


def blocking_issues(s: Session, plant_id: uuid.UUID) -> list[dict[str, Any]]:
    res = run_checks(s, plant_id)
    return [{"code": c["code"], "title": c["title"], "count": c["count"]} for c in res["checks"] if c["status"] == "ERROR"]


def _aw(dt):
    from datetime import UTC

    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _has_cycle(edges: list[tuple[int, int]]) -> bool:
    return bool(_cycle_nodes(edges))


def _cycle_nodes(edges: list[tuple]) -> set:
    adj = defaultdict(list)
    nodes = set()
    for a, b in edges:
        adj[a].append(b)
        nodes.update((a, b))
    indeg = dict.fromkeys(nodes, 0)
    for a, b in edges:
        indeg[b] += 1
    q = [n for n, d in indeg.items() if d == 0]
    seen = set()
    while q:
        n = q.pop()
        seen.add(n)
        for m in adj.get(n, []):
            indeg[m] -= 1
            if indeg[m] == 0:
                q.append(m)
    return nodes - seen
