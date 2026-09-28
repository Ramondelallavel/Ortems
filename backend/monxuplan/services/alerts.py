"""Automatic alerts derived from a plan (every alert links to its context)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from monxuplan_engine.contract import Solution

from ..core.clock import now
from ..core.events import bus
from ..models import Alert, Downtime, Plan, Resource, Scenario
from .context import Ctx


def generate_alerts(s: Session, ctx: Ctx, plan: Plan, sol: Solution, sc: Scenario) -> list[Alert]:
    """Alerts are produced for the plant's live scenario; previous automatic alerts are resolved."""
    if not sc.is_live:
        return []
    s.execute(
        update(Alert)
        .where(Alert.plant_id == sc.plant_id, Alert.status != "RESOLVED", Alert.plan_id.is_not(None))
        .values(status="RESOLVED", note="superseded by " + plan.number)
    )
    out: list[Alert] = []

    def add(type_: str, severity: str, title: str, message: str, context: dict[str, Any], count: float | None = None) -> None:
        a = Alert(tenant_id=ctx.tenant_id, plant_id=sc.plant_id, plan_id=plan.id, type=type_, severity=severity, title=title, message=message, context=context, count=count)
        s.add(a)
        out.append(a)

    late = [o for o in sol.orders if o.status == "LATE"]
    critical_late = [o for o in late if o.weight >= 3.5]
    if late:
        add("ORDERS_AT_RISK", "CRITICAL" if critical_late else "WARNING", f"{len(late)} orders at risk", f"{len(late)} orders finish after their due date ({len(critical_late)} high priority).", {"page": "orders", "filter": {"status": "LATE"}}, len(late))
    for o in critical_late[:10]:
        add("ORDER_LATE", "CRITICAL", f"{o.number} late by {o.lateness_minutes // 60} h", f"Due {o.due.isoformat()}, planned end {o.end.isoformat() if o.end else '—'}", {"page": "order", "order_id": o.order_id})
    impossible = [o for o in sol.orders if o.earliest_possible_end is not None and o.earliest_possible_end > o.due]
    if impossible:
        add("DUE_DATE_IMPOSSIBLE", "CRITICAL", f"{len(impossible)} due dates impossible", "Even at infinite capacity these orders cannot meet their date (lead time or material).", {"page": "orders", "filter": {"impossible": True}}, len(impossible))
    unsched = sol.unscheduled
    if unsched:
        by_reason: dict[str, int] = {}
        for u in unsched:
            by_reason[u.reason] = by_reason.get(u.reason, 0) + 1
        for reason, n in by_reason.items():
            sev = "CRITICAL"
            title = {
                "MATERIAL_SHORTAGE": f"{n} operations blocked by material shortage",
                "NO_FEASIBLE_SLOT": f"{n} operations without feasible slot",
                "NO_COMPATIBLE_RESOURCE": f"{n} operations without compatible resource",
                "PREDECESSOR_UNSCHEDULED": f"{n} operations waiting for unscheduled predecessors",
            }.get(reason, f"{n} operations unscheduled ({reason})")
            add("UNSCHEDULED_" + reason, sev, title, "; ".join(u.message for u in unsched if u.reason == reason)[:1500], {"page": "planning", "panel": "exceptions", "reason": reason}, n)
    for b in sol.bottlenecks:
        if b.kind == "OVERLOADED":
            add("RESOURCE_OVERLOADED", "WARNING", f"{b.ref} overloaded {b.overload_minutes // 60} h", f"Requirement {b.requirement_minutes // 60} h vs capacity {b.capacity_minutes // 60} h; {b.orders_affected} late orders waited on it.", {"page": "capacity", "resource_id": b.resource_id}, b.overload_minutes / 60)
        elif b.kind == "MATERIAL" and b.orders_affected:
            add("MATERIAL_LATE", "WARNING", f"Material {b.ref} delays {b.orders_affected} orders", "Late or insufficient supply.", {"page": "materials", "material": b.ref}, b.orders_affected)
        elif b.kind in ("LABOR", "TOOL") and b.induced_wait_minutes > 480:
            add(f"{b.kind}_CONSTRAINT", "WARNING", f"{b.ref}: {b.induced_wait_minutes // 60} h of waiting", f"Operations waited for {b.kind.lower()} availability.", {"page": "capacity", "resource_id": b.resource_id}, b.induced_wait_minutes / 60)
    shortages = [v for v in sol.violations if v.type == "MATERIAL_SHORTAGE"]
    if shortages:
        add("NEGATIVE_INVENTORY", "CRITICAL", f"{len(shortages)} materials would go negative", "Material shortage allowed by the scenario settings.", {"page": "materials"}, len(shortages))
    for d in s.scalars(select(Downtime).where(Downtime.end.is_(None))):
        r = s.get(Resource, d.resource_id)
        if r is not None and r.plant_id == sc.plant_id:
            add("MACHINE_BREAKDOWN", "CRITICAL", f"{r.code} down", d.reason or "Unplanned downtime", {"page": "resources", "resource_id": str(r.id)})
    past_due = sol.kpis.get("past_due_orders") or 0
    if past_due:
        add("PAST_DUE", "WARNING", f"{int(past_due)} orders past due", "Their due date is already in the past.", {"page": "orders", "filter": {"past_due": True}}, past_due)
    s.flush()
    if out:
        bus().publish("alerts.updated", str(ctx.tenant_id), {"plan_id": str(plan.id), "count": len(out)}, str(sc.plant_id))
    return out


def acknowledge(s: Session, ctx: Ctx, alert_id, note: str | None = None, resolve: bool = False) -> Alert:
    ctx.require("alerts:manage")
    a = s.get(Alert, alert_id)
    if a is None:
        from ..core.errors import NotFound

        raise NotFound("Alert not found")
    a.status = "RESOLVED" if resolve else "ACKNOWLEDGED"
    a.acknowledged_by = ctx.username
    a.acknowledged_at = now()
    a.note = note
    return a
