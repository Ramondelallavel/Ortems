"""Command Center: one call that tells the planner what needs attention now.

Everything is read from stored data (live plan head, published plan, alerts, data-quality checks,
runs, execution events). Deltas are computed between two real plan versions — never invented.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.errors import NotFound
from ..models import Alert, Event, Maintenance, Plan, PlanningRun, Plant, ProductionOrder, Resource, Scenario, ScheduledOperation, User
from .context import Ctx
from .views import _aware, plan_health, plan_summary

TILES = [
    ("otif", "OTIF", "%", "up"),
    ("late_orders", "Late orders", "", "down"),
    ("orders_unscheduled", "Unscheduled", "", "down"),
    ("utilization", "Utilisation", "%", None),
    ("setup_h", "Setup", "h", "down"),
    ("material_shortages", "Material shortages", "", "down"),
    ("constraint_violations_hard", "Hard violations", "", "down"),
    ("overtime_h", "Overtime", "h", "down"),
]


def _greeting(hour: int, lang: str) -> str:
    if lang == "es":
        return "Buenos días" if hour < 13 else "Buenas tardes" if hour < 21 else "Buenas noches"
    return "Good morning" if hour < 12 else "Good afternoon" if hour < 18 else "Good evening"


def command_center(s: Session, ctx: Ctx, plant_id: uuid.UUID) -> dict[str, Any]:
    ctx.require("plan:read")
    ctx.require_plant(plant_id)
    plant = s.get(Plant, plant_id)
    if plant is None:
        raise NotFound("Plant not found")
    tz = ZoneInfo(plant.timezone)
    t = now()
    user = s.get(User, ctx.user_id) if ctx.user_id else None
    lang = (user.locale if user else ctx.locale) or "en"
    local = t.astimezone(tz)

    live = s.get(Scenario, plant.live_scenario_id) if plant.live_scenario_id else None
    head = s.get(Plan, live.head_plan_id) if live and live.head_plan_id else None
    published = s.get(Plan, plant.published_plan_id) if plant.published_plan_id else None
    last_run = s.scalar(select(PlanningRun).where(PlanningRun.scenario_id == live.id).order_by(PlanningRun.created_at.desc()).limit(1)) if live else None

    tiles = []
    if head is not None:
        k = head.kpis or {}
        pk = (published.kpis or {}) if published is not None and published.id != head.id else {}
        for code, label, unit, better in TILES:
            v = k.get(code)
            ref = pk.get(code)
            delta = round(v - ref, 2) if isinstance(v, int | float) and isinstance(ref, int | float) else None
            trend = None
            if delta is not None and delta != 0 and better:
                trend = "better" if (delta > 0) == (better == "up") else "worse"
            tiles.append({"code": code, "label": label, "unit": unit, "value": v, "published_value": ref, "delta": delta, "trend": trend})

    # attention required: open alerts, blocking data issues, failed runs, stale plan, machines down
    attention: list[dict[str, Any]] = []
    sev_rank = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
    for a in s.scalars(select(Alert).where(Alert.plant_id == plant_id, Alert.status == "OPEN").order_by(Alert.created_at.desc()).limit(40)):
        attention.append({"source": "ALERT", "id": str(a.id), "type": a.type, "severity": a.severity, "title": a.title, "message": a.message, "count": a.count, "context": a.context, "at": _aware(a.created_at).isoformat()})
    if last_run is not None and last_run.status == "FAILED":
        attention.append({"source": "RUN", "id": str(last_run.id), "type": "PLANNING_RUN_FAILED", "severity": "CRITICAL", "title": "Last planning run failed", "message": last_run.error_message or last_run.error_code or "", "at": _aware(last_run.created_at).isoformat()})
    down = list(s.scalars(select(Resource).where(Resource.plant_id == plant_id, Resource.status.in_(["DOWN", "MAINTENANCE"]))))
    for r in down:
        if not any(x.get("context", {}) and x["context"].get("resource_id") == str(r.id) for x in attention):
            attention.append({"source": "RESOURCE", "id": str(r.id), "type": "RESOURCE_" + r.status, "severity": "CRITICAL" if r.status == "DOWN" else "WARNING", "title": f"{r.code} is {r.status.lower()}", "message": "Reported by the shop floor. Check the plan impact and reschedule if needed.", "at": None})
    if head is not None and published is not None and published.id != head.id:
        attention.append({"source": "PLAN", "id": str(head.id), "type": "UNPUBLISHED_CHANGES", "severity": "INFO", "title": f"{head.number} is not published", "message": f"The shop floor still works with {published.number}.", "at": _aware(head.created_at).isoformat()})
    if head is None:
        attention.append({"source": "PLAN", "id": None, "type": "NO_PLAN", "severity": "WARNING", "title": "No plan yet", "message": "Run the planner to create the first plan of this plant.", "at": None})
    attention.sort(key=lambda a: (sev_rank.get(a["severity"], 3), a.get("at") or ""), reverse=False)

    # today on the floor (published plan preferred — that is what is being executed)
    floor_plan = published or head
    today: dict[str, Any] = {}
    if floor_plan is not None:
        day_end = t + timedelta(hours=24)
        n_ops = s.scalar(select(func.count()).select_from(ScheduledOperation).where(ScheduledOperation.plan_id == floor_plan.id, ScheduledOperation.setup_start >= t, ScheduledOperation.setup_start < day_end)) or 0
        busy_res = s.scalar(select(func.count(func.distinct(ScheduledOperation.resource_key))).where(ScheduledOperation.plan_id == floor_plan.id, ScheduledOperation.end > t, ScheduledOperation.setup_start < day_end)) or 0
        maint = [
            {"resource": code, "start": _aware(m.start).isoformat(), "end": _aware(m.end).isoformat(), "kind": m.kind, "description": m.description}
            for m, code in s.execute(select(Maintenance, Resource.code).join(Resource, Resource.id == Maintenance.resource_id).where(Resource.plant_id == plant_id, Maintenance.end > t, Maintenance.start < day_end)).all()
        ]
        today = {"plan": floor_plan.number, "operations_starting_24h": n_ops, "resources_busy_24h": busy_res, "maintenance_24h": maint, "resources_down": [r.code for r in down]}

    # what changed since the user's previous visit (real rows)
    since = _aware(user.last_login_at) if user and user.last_login_at else t - timedelta(hours=24)
    changes = {
        "since": since.isoformat(),
        "new_orders": s.scalar(select(func.count()).select_from(ProductionOrder).where(ProductionOrder.plant_id == plant_id, ProductionOrder.created_at >= since)) or 0,
        "new_alerts": s.scalar(select(func.count()).select_from(Alert).where(Alert.plant_id == plant_id, Alert.created_at >= since)) or 0,
        "shop_floor_events": s.scalar(select(func.count()).select_from(Event).where(Event.plant_id == plant_id, Event.received_at >= since)) or 0,
        "plan_versions": s.scalar(select(func.count()).select_from(Plan).where(Plan.plant_id == plant_id, Plan.created_at >= since)) or 0,
    }

    an = (head.analysis or {}) if head else {}
    late = ((head.kpi_details or {}).get("late_orders", []) if head else [])[:8]
    res_codes = {str(r.id): r.code for r in s.scalars(select(Resource).where(Resource.plant_id == plant_id))}
    bottlenecks = [
        {"rank": b.get("rank"), "resource": res_codes.get(b.get("resource_id") or "", b.get("ref")), "resource_id": b.get("resource_id"), "kind": b.get("kind"), "utilization": b.get("utilization"), "overload_minutes": b.get("overload_minutes"), "induced_wait_minutes": b.get("induced_wait_minutes"), "orders_affected": b.get("orders_affected")}
        for b in an.get("bottlenecks", [])[:5]
    ]
    return {
        "plant": {"id": str(plant.id), "code": plant.code, "name": plant.name, "timezone": plant.timezone},
        "greeting": {"text": _greeting(local.hour, lang), "name": (user.full_name.split(" ")[0] if user and user.full_name else ctx.username), "local_time": local.isoformat()},
        "live_scenario_id": str(live.id) if live else None,
        "head_plan": plan_summary(head) if head else None,
        "published_plan": {"id": str(published.id), "number": published.number, "published_at": _aware(published.published_at).isoformat() if published.published_at else None, "published_by": published.published_by} if published else None,
        "health": plan_health(head, {"CRITICAL": (head.kpis or {}).get("constraint_violations_hard") or 0, "WARNING": (head.kpis or {}).get("constraint_violations_soft") or 0}) if head else None,
        "last_run": {"id": str(last_run.id), "status": last_run.status, "kind": last_run.kind, "created_at": _aware(last_run.created_at).isoformat(), "duration_s": last_run.duration_s, "solver_status": last_run.solver_status, "error": last_run.error_message} if last_run else None,
        "tiles": tiles,
        "attention": attention[:30],
        "today": today,
        "changes": changes,
        "late_orders": [{"order_id": x["order_id"], "number": x["number"], "status": x["status"], "lateness_minutes": x.get("lateness_minutes"), "due": x.get("due"), "cause": (x.get("cause") or {}).get("category"), "cause_text": (x.get("cause") or {}).get("text")} for x in late],
        "bottlenecks": bottlenecks,
        "material_issues": _codes(s, ((head.kpi_details or {}).get("material_shortages", []) if head else [])[:8]),
    }


def _codes(s: Session, ids: list[str]) -> list[str]:
    from ..models import Item

    ok = []
    for x in ids:
        try:
            ok.append(uuid.UUID(x))
        except ValueError:
            continue
    codes = {str(i): c for i, c in s.execute(select(Item.id, Item.code).where(Item.id.in_(ok)))} if ok else {}
    return [codes.get(x, x) for x in ids]


def plants_overview(s: Session, ctx: Ctx) -> list[dict[str, Any]]:
    """Multi-plant summary for the plant selector and the executive view."""
    ctx.require("plan:read")
    out = []
    for p in s.scalars(select(Plant).order_by(Plant.code)):
        if not ctx.can_access_plant(p.id):
            continue
        live = s.get(Scenario, p.live_scenario_id) if p.live_scenario_id else None
        head = s.get(Plan, live.head_plan_id) if live and live.head_plan_id else None
        k = (head.kpis or {}) if head else {}
        out.append(
            {
                "id": str(p.id),
                "code": p.code,
                "name": p.name,
                "timezone": p.timezone,
                "live_scenario_id": str(live.id) if live else None,
                "head_plan": head.number if head else None,
                "head_plan_id": str(head.id) if head else None,
                "published_plan_id": str(p.published_plan_id) if p.published_plan_id else None,
                "otif": k.get("otif"),
                "late_orders": k.get("late_orders"),
                "utilization": k.get("utilization"),
                "open_alerts": s.scalar(select(func.count()).select_from(Alert).where(Alert.plant_id == p.id, Alert.status == "OPEN")) or 0,
            }
        )
    return out


_ = datetime
