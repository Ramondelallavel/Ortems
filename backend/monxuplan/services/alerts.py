"""Automatic alerts derived from a plan (every alert links to its context)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from monxuplan_engine.contract import Solution

from ..core.clock import now
from ..core.events import bus
from ..models import Alert, Downtime, Plan, Resource, Scenario
from .context import Ctx

# An alert text: an English template with {placeholders} and its values. The rendered English text is
# stored as title/message (API, webhooks, exports); template and values go to context["i18n"] so the
# interface shows the alert in the user's language. Values ending in "_at" are ISO date-times.
Text = tuple[str, dict[str, Any]]


def render(text: Text | str) -> str:
    if isinstance(text, str):
        return text
    tpl, values = text

    def show(k: str, v: Any) -> Any:
        if v is None:
            return "—"
        if k.endswith("_at") and isinstance(v, str):
            return datetime.fromisoformat(v).strftime("%a %d %b %H:%M")  # already in plant local time
        return v

    return tpl.format(**{k: show(k, v) for k, v in values.items()})


def i18n(title: Text | str, message: Text | str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for part, text in (("title", title), ("message", message)):
        if not isinstance(text, str):
            out[part] = {"key": text[0], "values": text[1]}
    return out


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
    from zoneinfo import ZoneInfo

    from ..models import Plant

    plant = s.get(Plant, sc.plant_id)
    tz = ZoneInfo(plant.timezone if plant else "UTC")

    def iso(d) -> str | None:
        return d.astimezone(tz).isoformat() if d is not None else None

    def add(type_: str, severity: str, title: Text, message: Text | str, context: dict[str, Any], count: float | None = None, ui_message: Text | None = None) -> None:
        context = {**context, "i18n": i18n(title, ui_message or message)}
        a = Alert(tenant_id=ctx.tenant_id, plant_id=sc.plant_id, plan_id=plan.id, type=type_, severity=severity, title=render(title), message=render(message), context=context, count=count)
        s.add(a)
        out.append(a)

    late = [o for o in sol.orders if o.status == "LATE"]
    critical_late = [o for o in late if o.weight >= 3.5]
    if late:
        add("ORDERS_AT_RISK", "CRITICAL" if critical_late else "WARNING", ("{n} orders at risk", {"n": len(late)}), ("{n} orders finish after their due date ({hp} high priority).", {"n": len(late), "hp": len(critical_late)}), {"page": "orders", "filter": {"status": "LATE"}}, len(late))
    for o in critical_late[:10]:
        add("ORDER_LATE", "CRITICAL", ("{order} late by {h} h", {"order": o.number, "h": o.lateness_minutes // 60}), ("Due {due_at}, planned end {end_at} ({tz})", {"due_at": iso(o.due), "end_at": iso(o.end), "tz": plant.timezone if plant else "UTC"}), {"page": "order", "order_id": o.order_id, "order_number": o.number})
    impossible = [o for o in sol.orders if o.earliest_possible_end is not None and o.earliest_possible_end > o.due]
    if impossible:
        add("DUE_DATE_IMPOSSIBLE", "CRITICAL", ("{n} due dates impossible", {"n": len(impossible)}), ("Even at infinite capacity these orders cannot meet their date (lead time or material).", {}), {"page": "orders", "filter": {"impossible": True}}, len(impossible))
    unsched = sol.unscheduled
    if unsched:
        by_reason: dict[str, int] = {}
        for u in unsched:
            by_reason[u.reason] = by_reason.get(u.reason, 0) + 1
        for reason, n in by_reason.items():
            sev = "CRITICAL"
            title: Text = (
                {
                    "MATERIAL_SHORTAGE": "{n} operations blocked by material shortage",
                    "NO_FEASIBLE_SLOT": "{n} operations without feasible slot",
                    "NO_COMPATIBLE_RESOURCE": "{n} operations without compatible resource",
                    "PREDECESSOR_UNSCHEDULED": "{n} operations waiting for unscheduled predecessors",
                }.get(reason, "{n} operations unscheduled ({reason})"),
                {"n": n, "reason": reason},
            )
            ops = [u.op_id for u in unsched if u.reason == reason]
            shown = ", ".join(ops[:20]) + ("…" if len(ops) > 20 else "")
            if reason == "MATERIAL_SHORTAGE":
                mats = sorted({m["material"] for u in unsched if u.reason == reason for m in (u.details or {}).get("materials", [])})
                summary: Text = ("Short materials: {materials}. Operations: {ops}", {"materials": ", ".join(mats), "ops": shown})
            elif reason == "PREDECESSOR_UNSCHEDULED":
                summary = ("Waiting operations: {ops}", {"ops": shown})
            else:
                summary = ("Operations: {ops}", {"ops": shown})
            # the stored message keeps the engine's full English explanation; the interface shows the summary
            add("UNSCHEDULED_" + reason, sev, title, "; ".join(u.message for u in unsched if u.reason == reason)[:1500], {"page": "planning", "panel": "exceptions", "reason": reason}, n, ui_message=summary)
    for b in sol.bottlenecks:
        if b.kind == "OVERLOADED":
            add("RESOURCE_OVERLOADED", "WARNING", ("{res} overloaded {h} h", {"res": b.ref, "h": b.overload_minutes // 60}), ("Requirement {req} h vs capacity {cap} h; {n} late orders waited on it.", {"req": b.requirement_minutes // 60, "cap": b.capacity_minutes // 60, "n": b.orders_affected}), {"page": "capacity", "resource_id": b.resource_id, "resource_code": b.ref}, b.overload_minutes / 60)
        elif b.kind == "MATERIAL" and b.orders_affected:
            add("MATERIAL_LATE", "WARNING", ("Material {m} delays {n} orders", {"m": b.ref, "n": b.orders_affected}), ("Late or insufficient supply.", {}), {"page": "materials", "material": b.ref}, b.orders_affected)
        elif b.kind in ("LABOR", "TOOL") and b.induced_wait_minutes > 480:
            add(f"{b.kind}_CONSTRAINT", "WARNING", ("{res}: {h} h of waiting", {"res": b.ref, "h": b.induced_wait_minutes // 60}), ("Operations waited for labour availability." if b.kind == "LABOR" else "Operations waited for tool availability.", {}), {"page": "capacity", "resource_id": b.resource_id, "resource_code": b.ref}, b.induced_wait_minutes / 60)
    shortages = [v for v in sol.violations if v.type == "MATERIAL_SHORTAGE"]
    if shortages:
        add("NEGATIVE_INVENTORY", "CRITICAL", ("{n} materials would go negative", {"n": len(shortages)}), ("Material shortage allowed by the scenario settings.", {}), {"page": "materials"}, len(shortages))
    for d in s.scalars(select(Downtime).where(Downtime.end.is_(None))):
        r = s.get(Resource, d.resource_id)
        if r is not None and r.plant_id == sc.plant_id:
            add("MACHINE_BREAKDOWN", "CRITICAL", ("{res} down", {"res": r.code}), d.reason or ("Unplanned downtime", {}), {"page": "resources", "resource_id": str(r.id)})
    past_due = sol.kpis.get("past_due_orders") or 0
    if past_due:
        add("PAST_DUE", "WARNING", ("{n} orders past due", {"n": int(past_due)}), ("Their due date is already in the past.", {}), {"page": "orders", "filter": {"past_due": True}}, past_due)
    s.flush()
    if out:
        bus().publish("alerts.updated", str(ctx.tenant_id), {"plan_id": str(plan.id), "count": len(out)}, str(sc.plant_id))
    return out


def acknowledge(s: Session, ctx: Ctx, alert_id, note: str | None = None, resolve: bool = False) -> Alert:
    ctx.require("alerts:manage")
    a = s.get(Alert, alert_id)
    if a is None or not ctx.can_access_plant(a.plant_id):
        # an alert of a plant outside the caller's scope does not exist for them
        from ..core.errors import NotFound

        raise NotFound("Alert not found")
    a.status = "RESOLVED" if resolve else "ACKNOWLEDGED"
    a.acknowledged_by = ctx.username
    a.acknowledged_at = now()
    a.note = note
    return a
