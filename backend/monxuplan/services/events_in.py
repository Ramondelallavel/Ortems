"""Inbound industrial events (MES / ERP / IoT) and execution feedback.

Every event is stored first (event store), then applied to the operational data — never to a plan
directly. Plans react through rescheduling, automatically when the plant is configured for it
(``plant.settings.auto_reschedule``: list of event types + scope), otherwise by alerting the planner.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.errors import NotFound, ValidationFailed
from ..core.events import bus
from ..models import (
    ActualProduction,
    Alert,
    Downtime,
    Event,
    Inventory,
    Item,
    Maintenance,
    Plant,
    ProductionOrder,
    ProductionOrderOperation,
    PurchaseOrder,
    PurchaseOrderLine,
    Resource,
)
from . import audit
from .alerts import i18n
from .context import Ctx

log = logging.getLogger("monxuplan.events")

EVENT_TYPES = (
    "OrderCreated",
    "OrderUpdated",
    "MachineDown",
    "MachineAvailable",
    "MaterialReceived",
    "MaterialDelayed",
    "OperationStarted",
    "OperationFinished",
    "QuantityProduced",
    "Scrap",
    "InventoryChanged",
    "MaintenanceCreated",
)


def _dt(v: Any) -> datetime:
    if v is None:
        return now()
    dt = v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _in_scope(ctx: Ctx, stmt, plant_col):
    """Restrict a lookup by business code to the caller's plants (codes may repeat across plants)."""
    return stmt if ctx.plant_ids is None else stmt.where(plant_col.in_(ctx.plant_ids))


def _one(s: Session, stmt, what: str, ref: Any):
    rows = list(s.scalars(stmt.limit(2)))
    if len(rows) > 1:
        raise ValidationFailed(f"{what} {ref} is ambiguous (it exists in several plants): use its id.", code="AMBIGUOUS_REFERENCE")
    return rows[0] if rows else None


def _resource(s: Session, ctx: Ctx, ref: str) -> Resource:
    r = None
    try:
        r = s.get(Resource, uuid.UUID(str(ref)))
    except ValueError:
        r = _one(s, _in_scope(ctx, select(Resource).where(Resource.code == ref), Resource.plant_id), "Resource", ref)
    if r is None:
        raise NotFound(f"Unknown resource {ref}", code="UNKNOWN_RESOURCE")
    ctx.require_plant(r.plant_id)
    return r


def _order(s: Session, ctx: Ctx, number: Any) -> ProductionOrder | None:
    return _one(s, _in_scope(ctx, select(ProductionOrder).where(ProductionOrder.number == number), ProductionOrder.plant_id), "Order", number)


def _order_op(s: Session, ctx: Ctx, payload: dict) -> ProductionOrderOperation:
    if payload.get("order_operation_id"):
        op = s.get(ProductionOrderOperation, uuid.UUID(str(payload["order_operation_id"])))
    else:
        o = _order(s, ctx, payload.get("order"))
        if o is None:
            raise NotFound(f"Unknown order {payload.get('order')}", code="UNKNOWN_ORDER")
        op = s.scalar(select(ProductionOrderOperation).where(ProductionOrderOperation.order_id == o.id, ProductionOrderOperation.seq == int(payload.get("seq", 0))))
    if op is None:
        raise NotFound("Unknown order operation", code="UNKNOWN_OPERATION")
    ctx.require_plant(s.get(ProductionOrder, op.order_id).plant_id)
    return op


def ingest(s: Session, ctx: Ctx, type_: str, payload: dict[str, Any], source: str = "API", correlation_id: str | None = None, occurred_at: Any = None) -> dict[str, Any]:
    ctx.require("execution:report")
    if type_ not in EVENT_TYPES:
        raise ValidationFailed(f"Unknown event type {type_}", context={"allowed": EVENT_TYPES})
    if correlation_id:
        dup = s.scalar(select(Event).where(Event.correlation_id == correlation_id, Event.type == type_))
        if dup is not None:
            ctx.require_plant(dup.plant_id)
            return {"event_id": str(dup.id), "duplicate": True, "result": dup.processing_result}
    ev = Event(tenant_id=ctx.tenant_id, type=type_, payload=payload, source=source, correlation_id=correlation_id, occurred_at=_dt(occurred_at))
    s.add(ev)
    s.flush()
    result = _apply(s, ctx, ev)
    ev.processed = True
    ev.processing_result = result
    if ev.plant_id is None and result.get("plant_id"):
        ev.plant_id = uuid.UUID(result["plant_id"])
    audit.record(s, ctx, "EVENT", "event", ev.id, type_, after={"payload": payload, "result": result})
    bus().publish(f"event.{type_}", str(ctx.tenant_id), {"event_id": str(ev.id), "payload": payload, "result": result}, result.get("plant_id"))
    _maybe_auto_reschedule(s, ctx, ev, result)
    return {"event_id": str(ev.id), "duplicate": False, "result": result}


def _apply(s: Session, ctx: Ctx, ev: Event) -> dict[str, Any]:
    p = ev.payload
    t = ev.type
    if t == "MachineDown":
        r = _resource(s, ctx, p["resource"])
        r.status = "DOWN"
        d = Downtime(tenant_id=ctx.tenant_id, resource_id=r.id, start=_dt(p.get("start") or ev.occurred_at), end=_dt(p["expected_end"]) if p.get("expected_end") else None, reason=p.get("reason"), source=ev.source)
        s.add(d)
        s.add(Alert(tenant_id=ctx.tenant_id, plant_id=r.plant_id, type="MACHINE_BREAKDOWN", severity="CRITICAL", title=f"{r.code} down", message=p.get("reason") or "", context={"page": "resources", "resource_id": str(r.id), "i18n": i18n(("{res} down", {"res": r.code}), "")}))
        return {"plant_id": str(r.plant_id), "resource": r.code, "downtime_id": str(d.id) if d.id else None, "action": "downtime recorded"}
    if t == "MachineAvailable":
        r = _resource(s, ctx, p["resource"])
        r.status = "AVAILABLE"
        closed = 0
        for d in s.scalars(select(Downtime).where(Downtime.resource_id == r.id, Downtime.end.is_(None))):
            d.end = _dt(p.get("at") or ev.occurred_at)
            closed += 1
        return {"plant_id": str(r.plant_id), "resource": r.code, "closed_downtimes": closed}
    if t in ("OperationStarted", "OperationFinished", "QuantityProduced", "Scrap"):
        action = {"OperationStarted": "START", "OperationFinished": "FINISH", "QuantityProduced": "QUANTITY", "Scrap": "QUANTITY"}[t]
        op = _order_op(s, ctx, p)
        return report_execution(s, ctx, {"order_operation_id": op.id, "action": action, "resource_id": _resource(s, ctx, p["resource"]).id if p.get("resource") else None, "good_quantity": float(p.get("good_quantity", p.get("quantity", 0)) if t != "Scrap" else 0), "scrap_quantity": float(p.get("scrap_quantity", p.get("quantity", 0)) if t == "Scrap" else p.get("scrap_quantity", 0)), "at": p.get("at") or ev.occurred_at}, log_event=False)
    if t == "MaterialReceived":
        ln = _po_line(s, ctx, p)
        q = float(p["quantity"])
        ln.received_quantity = (ln.received_quantity or 0) + q
        if ln.received_quantity >= ln.quantity - 1e-9:
            ln.status = "RECEIVED"
        po = s.get(PurchaseOrder, ln.purchase_order_id)
        inv = s.scalar(select(Inventory).where(Inventory.item_id == ln.item_id, Inventory.plant_id == po.plant_id)) if po.plant_id else None
        if inv is not None:
            inv.on_hand = (inv.on_hand or 0) + q
        return {"plant_id": str(po.plant_id) if po.plant_id else None, "purchase_order": po.number, "received": q}
    if t == "MaterialDelayed":
        ln = _po_line(s, ctx, p)
        before = ln.expected_date
        ln.original_date = ln.original_date or ln.expected_date
        ln.expected_date = _dt(p["new_date"])
        po = s.get(PurchaseOrder, ln.purchase_order_id)
        it = s.get(Item, ln.item_id)
        s.add(Alert(tenant_id=ctx.tenant_id, plant_id=po.plant_id, type="MATERIAL_LATE", severity="WARNING", title=f"{it.code}: receipt {po.number} delayed", message=f"{before.isoformat()} → {ln.expected_date.isoformat()}", context={"page": "materials", "material_id": str(it.id), "i18n": i18n(("{m}: receipt {po} delayed", {"m": it.code, "po": po.number}), "")}))
        return {"plant_id": str(po.plant_id) if po.plant_id else None, "purchase_order": po.number, "from": before.isoformat(), "to": ln.expected_date.isoformat()}
    if t == "InventoryChanged":
        it = s.scalar(select(Item).where(Item.code == p["item"]))
        plant = s.scalar(select(Plant).where(Plant.code == p["plant"]))
        if it is None or plant is None:
            raise NotFound("Unknown item or plant")
        ctx.require_plant(plant.id)
        inv = s.scalar(select(Inventory).where(Inventory.item_id == it.id, Inventory.plant_id == plant.id, Inventory.location == p.get("location", "MAIN")))
        if inv is None:
            inv = Inventory(tenant_id=ctx.tenant_id, item_id=it.id, plant_id=plant.id, location=p.get("location", "MAIN"))
            s.add(inv)
        for k in ("on_hand", "reserved", "blocked", "quality_hold"):
            if k in p:
                setattr(inv, k, float(p[k]))
        return {"plant_id": str(plant.id), "item": it.code}
    if t == "MaintenanceCreated":
        r = _resource(s, ctx, p["resource"])
        m = Maintenance(tenant_id=ctx.tenant_id, resource_id=r.id, start=_dt(p["start"]), end=_dt(p["end"]), kind=p.get("kind", "PLANNED"), description=p.get("description"))
        s.add(m)
        return {"plant_id": str(r.plant_id), "resource": r.code}
    if t in ("OrderCreated", "OrderUpdated"):
        from .masterdata import create_row, update_row

        o = _order(s, ctx, p["number"])
        if o is not None:
            ctx.require_plant(o.plant_id)
        data = dict(p)
        if "item" in data:
            it = s.scalar(select(Item).where(Item.code == data.pop("item")))
            if it is None:
                raise NotFound("Unknown item")
            data["item_id"] = str(it.id)
        if "plant" in data:
            code = data.pop("plant")
            pl = s.scalar(select(Plant).where(Plant.code == code))
            if pl is None:
                raise NotFound(f"Unknown plant {code}", code="UNKNOWN_PLANT")
            ctx.require_plant(pl.id)
            data["plant_id"] = str(pl.id)
        if o is None:
            row = create_row(s, ctx, "production-orders", data)
        else:
            data.pop("version", None)
            row = update_row(s, ctx, "production-orders", o.id, data)
        return {"plant_id": row.get("plant_id"), "order": row["number"]}
    return {}


def _po_line(s: Session, ctx: Ctx, p: dict) -> PurchaseOrderLine:
    po = _one(s, _in_scope(ctx, select(PurchaseOrder).where(PurchaseOrder.number == p["purchase_order"]), PurchaseOrder.plant_id), "Purchase order", p.get("purchase_order"))
    if po is None:
        raise NotFound(f"Unknown purchase order {p.get('purchase_order')}")
    ctx.require_plant(po.plant_id)
    ln = s.scalar(select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == po.id, PurchaseOrderLine.line_no == int(p.get("line", 10))))
    if ln is None:
        raise NotFound("Unknown purchase order line")
    return ln


def report_execution(s: Session, ctx: Ctx, body: dict[str, Any], log_event: bool = True) -> dict[str, Any]:
    """Operator / MES feedback. Actuals are stored separately from the schedule (never mixed)."""
    ctx.require("execution:report")
    op = s.get(ProductionOrderOperation, uuid.UUID(str(body["order_operation_id"])))
    if op is None:
        raise NotFound("Operation not found")
    order = s.get(ProductionOrder, op.order_id)
    ctx.require_plant(order.plant_id)
    at = _dt(body.get("at"))
    action = body["action"]
    res_id = uuid.UUID(str(body["resource_id"])) if body.get("resource_id") else (op.actual_resource_id or op.pinned_resource_id)
    if body.get("resource_id"):
        res = s.get(Resource, res_id)
        if res is None or res.plant_id != order.plant_id:
            raise ValidationFailed("The resource does not belong to the order's plant.", code="RESOURCE_PLANT_MISMATCH")
    open_act = s.scalar(select(ActualProduction).where(ActualProduction.order_operation_id == op.id, ActualProduction.end.is_(None)).order_by(ActualProduction.start.desc()))
    if action in ("START", "RESUME"):
        if op.status == "COMPLETED":
            raise ValidationFailed("Operation already completed")
        op.status = "IN_PROGRESS"
        op.actual_start = op.actual_start or at
        op.actual_resource_id = res_id
        if open_act is None:
            s.add(ActualProduction(tenant_id=ctx.tenant_id, order_operation_id=op.id, resource_id=res_id, operator_code=ctx.username, start=at, source="MES" if ctx.via == "api_key" else "OPERATOR"))
        if order.status in ("PLANNED", "FIRMED", "RELEASED"):
            order.status = "IN_PRODUCTION"
    elif action == "PAUSE":
        if open_act is not None:
            open_act.end = at
    elif action in ("QUANTITY", "FINISH"):
        good = float(body.get("good_quantity") or 0)
        scrap = float(body.get("scrap_quantity") or 0)
        op.completed_quantity = (op.completed_quantity or 0) + good
        op.scrap_quantity = (op.scrap_quantity or 0) + scrap
        if open_act is not None:
            open_act.good_quantity += good
            open_act.scrap_quantity += scrap
        else:
            s.add(ActualProduction(tenant_id=ctx.tenant_id, order_operation_id=op.id, resource_id=res_id, operator_code=ctx.username, start=at, end=at if action == "FINISH" else None, good_quantity=good, scrap_quantity=scrap))
        if action == "FINISH":
            op.status = "COMPLETED"
            op.actual_end = at
            if open_act is not None:
                open_act.end = at
            ops = list(s.scalars(select(ProductionOrderOperation).where(ProductionOrderOperation.order_id == order.id)))
            last_seq = max(x.seq for x in ops)
            if op.seq == last_seq:
                order.completed_quantity = (order.completed_quantity or 0) + (op.completed_quantity or 0)
                order.status = "COMPLETED" if all(x.status == "COMPLETED" for x in ops) else "PARTIALLY_COMPLETED"
    s.flush()
    if log_event:
        audit.record(s, ctx, f"EXECUTION_{action}", "production_order_operation", op.id, f"{order.number}/{op.seq}", after={"good": body.get("good_quantity"), "scrap": body.get("scrap_quantity")})
        bus().publish("execution.reported", str(ctx.tenant_id), {"order": order.number, "seq": op.seq, "action": action}, str(order.plant_id))
    return {"plant_id": str(order.plant_id), "order": order.number, "operation": op.seq, "status": op.status, "completed_quantity": op.completed_quantity}


def _maybe_auto_reschedule(s: Session, ctx: Ctx, ev: Event, result: dict[str, Any]) -> None:
    pid = result.get("plant_id")
    if not pid:
        return
    plant = s.get(Plant, uuid.UUID(pid))
    cfg = (plant.settings or {}).get("auto_reschedule") if plant else None
    if not cfg or ev.type not in cfg.get("events", []) or not plant.live_scenario_id:
        return
    from .planning import reschedule

    sp = s.begin_nested()  # a failed reschedule must not leave half-written plan rows in the event's transaction
    try:
        _plan, res = reschedule(s, ctx, plant.live_scenario_id, cfg.get("scope", "LOCAL"), note=f"auto-reschedule on {ev.type}")
        sp.commit()
        result["auto_reschedule"] = {"plan": res["plan_number"], "affected_operations": len(res["affected_operations"])}
    except Exception as exc:  # noqa: BLE001 - the event is stored even if the reschedule fails
        sp.rollback()
        log.exception("auto-reschedule failed", extra={"extra_data": {"event": str(ev.id)}})
        result["auto_reschedule_error"] = str(exc)[:300]
