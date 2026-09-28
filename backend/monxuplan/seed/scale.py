"""High-volume plant for load and performance testing: N production orders per day (default 100 000).

    python -m monxuplan.seed.scale --orders 100000

Adds plant "SCL" (Scale Plant) to the demo tenant (run ``python -m monxuplan.seed.demo`` first, so
users and roles exist): ~1 machine per 100 daily orders in cells of 8, three shifts Monday–Saturday,
600 products in 6 families with routings of 1–3 operations and 2–3 alternative machines, a family
changeover matrix, raw materials with stock and BOM lines, and N open orders due over the next day.
Master data goes through the ORM; the high-volume tables use bulk inserts. Deterministic (seeded).
"""

from __future__ import annotations

import argparse
import json
import random
import time as _time
import uuid
from datetime import time, timedelta
from typing import Any

from sqlalchemy import delete, insert, select

from .. import models as M
from ..core.clock import now
from ..core.db import create_all, new_session
from ..services.planning import enqueue_run  # noqa: F401  (import check: planning service available)

CELL = 8
FAMS = "ABCDEF"


def _bulk(s, model, rows: list[dict[str, Any]], batch: int = 20_000) -> None:
    for i in range(0, len(rows), batch):
        s.execute(insert(model.__table__), rows[i : i + batch])


def seed_scale(orders: int = 100_000, machines: int | None = None, seed: int = 11, reset: bool = False) -> dict[str, Any]:
    rng = random.Random(seed)
    t_start = _time.monotonic()
    with new_session(None, "seed") as s0:
        tenant = s0.scalar(select(M.Tenant).where(M.Tenant.slug == "monxu"))
        if tenant is None:
            raise SystemExit("Run python -m monxuplan.seed.demo first (the scale plant is added to the demo tenant).")
        tid = tenant.id
    with new_session(tid, "seed") as s:
        plant = s.scalar(select(M.Plant).where(M.Plant.code == "SCL"))
        if plant is not None:
            if not reset:
                return {"status": "exists", "plant_id": str(plant.id)}
            _delete_plant(s, plant)
            s.commit()
        today = now().replace(second=0, microsecond=0)
        n_m = machines or max(8, orders // 100)
        cells = max(1, n_m // CELL)

        cal = M.Calendar(tenant_id=tid, code="SCL-3S", name="Scale plant — three shifts Mon–Sat", timezone="Europe/Madrid")
        s.add(cal)
        s.flush()
        for d in range(6):
            for code, a, b in (("M", time(6), time(14)), ("T", time(14), time(22)), ("N", time(22), time(6))):
                s.add(M.CalendarShift(tenant_id=tid, calendar_id=cal.id, weekday=d, shift_code=code, start_time=a, end_time=b))
        plant = M.Plant(tenant_id=tid, code="SCL", name="Scale Plant", timezone="Europe/Madrid", country="ES", default_calendar_id=cal.id)
        s.add(plant)
        s.flush()
        areas = [M.PlanningArea(tenant_id=tid, plant_id=plant.id, code=f"SCL-A{k:02d}", name=f"Hall {k + 1}", sort_order=k) for k in range(10)]
        s.add_all(areas)
        grp_all = M.ResourceGroup(tenant_id=tid, plant_id=plant.id, code="SCL-ALL", name="Scale plant machines")
        s.add(grp_all)
        s.flush()
        res_rows = []
        mids = []
        for k in range(n_m):
            rid = uuid.uuid4()
            mids.append(rid)
            res_rows.append(
                {"id": rid, "tenant_id": tid, "plant_id": plant.id, "area_id": areas[(k // CELL) % len(areas)].id, "code": f"SCL-M{k:05d}", "name": f"Machine {k + 1}", "kind": "MACHINE", "capacity": 1, "calendar_id": cal.id, "efficiency": rng.choice([0.9, 1.0, 1.1]), "is_finite": True, "is_active": True, "cost_per_hour": 40.0}
            )
        _bulk(s, M.Resource, res_rows)
        _bulk(s, M.ResourceGroupMember, [{"id": uuid.uuid4(), "tenant_id": tid, "group_id": grp_all.id, "resource_id": r} for r in mids])
        s.add(M.SetupMatrix(tenant_id=tid, code="SCL-FAM", name="Scale family changeover", attribute="family", same_minutes=1, default_minutes=6, group_id=grp_all.id))
        fams = {}
        for f in FAMS:
            fam = s.scalar(select(M.ProductFamily).where(M.ProductFamily.code == f"SCL-{f}"))
            if fam is None:
                fam = M.ProductFamily(tenant_id=tid, code=f"SCL-{f}", name=f"Scale family {f}")
                s.add(fam)
            fams[f] = fam
        s.flush()

        # raw materials with plenty of stock
        raws = []
        for k in range(50):
            it = s.scalar(select(M.Item).where(M.Item.code == f"SCL-RAW{k:02d}"))
            if it is None:
                it = M.Item(tenant_id=tid, code=f"SCL-RAW{k:02d}", name=f"Raw material {k}", item_type="RAW", make_or_buy="BUY", uom="pcs", quantity_type="INTEGER")
                s.add(it)
            raws.append(it)
        s.flush()
        _bulk(s, M.Inventory, [{"id": uuid.uuid4(), "tenant_id": tid, "item_id": r.id, "plant_id": plant.id, "location": "MAIN", "on_hand": orders * 20.0} for r in raws])

        # products, BOMs, routings
        prod_rows, bom_rows, line_rows, rt_rows, ro_rows, orr_rows = [], [], [], [], [], []
        products = []  # (item_id, routing_id, bom_id, [(ro_id, seq, code, name)])
        for k in range(600):
            f = FAMS[k % len(FAMS)]
            iid, rtid, bid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            code = f"SCL-P{f}{k:03d}"
            if s.scalar(select(M.Item.id).where(M.Item.code == code)) is not None:
                code = f"{code}-{uuid.uuid4().hex[:4]}"
            prod_rows.append({"id": iid, "tenant_id": tid, "code": code, "name": f"Product {f}{k:03d}", "item_type": "FINISHED", "make_or_buy": "MAKE", "uom": "pcs", "quantity_type": "INTEGER", "family_id": fams[f].id, "is_active": True})
            bom_rows.append({"id": bid, "tenant_id": tid, "item_id": iid, "version_code": "1", "base_quantity": 1.0, "is_active": True})
            line_rows.append({"id": uuid.uuid4(), "tenant_id": tid, "bom_id": bid, "component_id": raws[k % len(raws)].id, "quantity_per": 1.0, "position": 10})
            rt_rows.append({"id": rtid, "tenant_id": tid, "item_id": iid, "plant_id": plant.id, "version_code": "1", "is_active": True})
            c0 = rng.randrange(cells)
            ops = []
            for j in range(rng.choice((1, 2, 2, 3))):
                roid = uuid.uuid4()
                seq = (j + 1) * 10
                ro_rows.append(
                    {"id": roid, "tenant_id": tid, "routing_id": rtid, "seq": seq, "code": f"OP{seq}", "name": ("Cut", "Machine", "Finish")[j], "setup_minutes": 2.0, "run_minutes_per_unit": round(rng.uniform(0.05, 0.3), 3), "setup_attributes": {"family": f}, "interruptible": True}
                )
                cell = (c0 + j) % cells
                cands = mids[cell * CELL : cell * CELL + CELL] or mids
                for pref, rid in enumerate(rng.sample(cands, min(len(cands), rng.randint(2, 3)))):
                    orr_rows.append({"id": uuid.uuid4(), "tenant_id": tid, "routing_operation_id": roid, "resource_id": rid, "role": "PRIMARY" if pref == 0 else "ALTERNATIVE", "preference": pref})
                ops.append((roid, seq, f"OP{seq}", ("Cut", "Machine", "Finish")[j]))
            products.append((iid, rtid, bid, ops))
        _bulk(s, M.Item, prod_rows)
        _bulk(s, M.Bom, bom_rows)
        _bulk(s, M.BomLine, line_rows)
        _bulk(s, M.Routing, rt_rows)
        _bulk(s, M.RoutingOperation, ro_rows)
        _bulk(s, M.OperationResource, orr_rows)

        # orders and their operations
        order_rows, op_rows = [], []
        for k in range(orders):
            iid, rtid, bid, ops = products[rng.randrange(len(products))]
            oid = uuid.uuid4()
            order_rows.append(
                {
                    "id": oid,
                    "tenant_id": tid,
                    "number": f"SCL-{k:07d}",
                    "plant_id": plant.id,
                    "item_id": iid,
                    "quantity": float(rng.randint(1, 20)),
                    "status": "RELEASED",
                    "due_date": today + timedelta(hours=rng.uniform(8, 32)),
                    "priority": rng.randint(1, 10),
                    "routing_id": rtid,
                    "bom_id": bid,
                    "source": "SCALE",
                }
            )
            for roid, seq, code, name in ops:
                op_rows.append({"id": uuid.uuid4(), "tenant_id": tid, "order_id": oid, "routing_operation_id": roid, "seq": seq, "code": code, "name": name, "status": "PLANNED"})
        _bulk(s, M.ProductionOrder, order_rows)
        _bulk(s, M.ProductionOrderOperation, op_rows)

        live = M.Scenario(tenant_id=tid, plant_id=plant.id, name="Live plan", is_live=True, kind="LIVE", owner="planner", config={"horizon_days": 4, "frozen_hours": 0, "profile_code": "BALANCED"}, description="High-volume test plant")
        s.add(live)
        s.flush()
        plant.live_scenario_id = live.id
        s.commit()
        return {
            "status": "created",
            "plant_id": str(plant.id),
            "scenario_id": str(live.id),
            "machines": n_m,
            "orders": orders,
            "operations": len(op_rows),
            "seconds": round(_time.monotonic() - t_start, 1),
        }


def _delete_plant(s, plant: M.Plant) -> None:
    """Remove the scale plant and everything that belongs to it (plans, orders, resources…)."""
    pid = plant.id
    plan_ids = [p for p in s.scalars(select(M.Plan.id).where(M.Plan.plant_id == pid))]
    sc_ids = [x for x in s.scalars(select(M.Scenario.id).where(M.Scenario.plant_id == pid))]
    plant.live_scenario_id = None
    plant.published_plan_id = None
    s.flush()
    for sc in s.scalars(select(M.Scenario).where(M.Scenario.plant_id == pid)):
        sc.head_plan_id = None
        sc.base_plan_id = None
    s.flush()
    if plan_ids:
        for model in (M.ScheduledOperation, M.ConstraintViolation, M.KpiValue):
            s.execute(delete(model).where(model.plan_id.in_(plan_ids)))
        for extra in ("PlanOrder", "PlanPeg", "PlanUnscheduled", "PlanResource", "PlanMaterial"):
            model = getattr(M, extra, None)
            if model is not None:
                s.execute(delete(model).where(model.plan_id.in_(plan_ids)))
        s.execute(delete(M.Alert).where(M.Alert.plan_id.in_(plan_ids))) if hasattr(M.Alert, "plan_id") else None
    s.execute(delete(M.PlanningRun).where(M.PlanningRun.scenario_id.in_(sc_ids))) if sc_ids else None
    if plan_ids:
        s.execute(delete(M.Plan).where(M.Plan.id.in_(plan_ids)))
    s.execute(delete(M.ScenarioChange).where(M.ScenarioChange.scenario_id.in_(sc_ids))) if sc_ids else None
    s.execute(delete(M.Scenario).where(M.Scenario.plant_id == pid))
    order_ids = select(M.ProductionOrder.id).where(M.ProductionOrder.plant_id == pid)
    s.execute(delete(M.ProductionOrderOperation).where(M.ProductionOrderOperation.order_id.in_(order_ids)))
    s.execute(delete(M.ProductionOrder).where(M.ProductionOrder.plant_id == pid))
    rt_ids = select(M.Routing.id).where(M.Routing.plant_id == pid)
    ro_ids = select(M.RoutingOperation.id).where(M.RoutingOperation.routing_id.in_(rt_ids))
    s.execute(delete(M.OperationResource).where(M.OperationResource.routing_operation_id.in_(ro_ids)))
    s.execute(delete(M.RoutingOperation).where(M.RoutingOperation.routing_id.in_(rt_ids)))
    item_ids = select(M.Routing.item_id).where(M.Routing.plant_id == pid)
    bom_ids = select(M.Bom.id).where(M.Bom.item_id.in_(item_ids))
    s.execute(delete(M.BomLine).where(M.BomLine.bom_id.in_(bom_ids)))
    s.execute(delete(M.Bom).where(M.Bom.item_id.in_(item_ids)))
    prod_ids = [x for x in s.scalars(item_ids)]
    s.execute(delete(M.Routing).where(M.Routing.plant_id == pid))
    if prod_ids:
        s.execute(delete(M.Item).where(M.Item.id.in_(prod_ids)))
    s.execute(delete(M.Inventory).where(M.Inventory.plant_id == pid))
    grp = s.scalar(select(M.ResourceGroup).where(M.ResourceGroup.code == "SCL-ALL"))
    if grp is not None:
        s.execute(delete(M.SetupMatrix).where(M.SetupMatrix.group_id == grp.id))
        s.execute(delete(M.ResourceGroupMember).where(M.ResourceGroupMember.group_id == grp.id))
        s.delete(grp)
    s.execute(delete(M.Resource).where(M.Resource.plant_id == pid))
    s.execute(delete(M.PlanningArea).where(M.PlanningArea.plant_id == pid))
    s.delete(plant)
    s.flush()
    cal = s.scalar(select(M.Calendar).where(M.Calendar.code == "SCL-3S"))
    if cal is not None:
        s.execute(delete(M.CalendarShift).where(M.CalendarShift.calendar_id == cal.id))
        s.delete(cal)


def main() -> None:  # pragma: no cover
    ap = argparse.ArgumentParser(description="Create the high-volume Scale Plant")
    ap.add_argument("--orders", type=int, default=100_000)
    ap.add_argument("--machines", type=int, default=None)
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()
    create_all()
    print(json.dumps(seed_scale(args.orders, args.machines, reset=args.reset), indent=2))


if __name__ == "__main__":
    main()
