"""Scenarios: copy-on-write what-ifs, collaboration (locking), what-if wizards."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from monxuplan_engine.changes import CHANGE_TYPES

from ..core.clock import now
from ..core.errors import Conflict, NotFound, ValidationFailed
from ..models import Item, Plan, PlanningRun, Plant, ProductionOrder, Resource, Scenario, ScenarioChange
from . import audit
from .context import Ctx
from .masterdata import row_dict, to_json
from .planning import check_edit, get_scenario
from .problem_builder import DEFAULT_CONFIG, op_key

LOCK_MINUTES = 30


def scenario_dict(s: Session, sc: Scenario, with_changes: bool = True) -> dict[str, Any]:
    out = row_dict(sc)
    head = s.get(Plan, sc.head_plan_id) if sc.head_plan_id else None
    out["head_plan"] = {"id": str(head.id), "number": head.number, "status": head.status, "kpis": head.kpis, "feasible": head.feasible, "created_at": to_json(head.created_at)} if head else None
    base = s.get(Plan, sc.base_plan_id) if sc.base_plan_id else None
    out["base_plan"] = {"id": str(base.id), "number": base.number} if base else None
    parent = s.get(Scenario, sc.parent_id) if sc.parent_id else None
    out["parent"] = {"id": str(parent.id), "name": parent.name} if parent else None
    run = s.scalar(select(PlanningRun).where(PlanningRun.scenario_id == sc.id).order_by(PlanningRun.created_at.desc()).limit(1))
    out["last_run"] = {"id": str(run.id), "status": run.status, "kind": run.kind, "created_at": to_json(run.created_at), "finished_at": to_json(run.finished_at), "error": run.error_message} if run else None
    out["plans"] = s.scalar(select(func.count()).select_from(Plan).where(Plan.scenario_id == sc.id))
    if with_changes:
        out["changes"] = [row_dict(c) for c in s.scalars(select(ScenarioChange).where(ScenarioChange.scenario_id == sc.id).order_by(ScenarioChange.seq))]
    return out


def list_scenarios(s: Session, ctx: Ctx, plant_id: uuid.UUID, include_archived: bool = False) -> list[dict[str, Any]]:
    ctx.require("scenario:read")
    ctx.require_plant(plant_id)
    q = select(Scenario).where(Scenario.plant_id == plant_id).order_by(Scenario.is_live.desc(), Scenario.created_at)
    if not include_archived:
        q = q.where(Scenario.status != "ARCHIVED")
    out = []
    for sc in s.scalars(q):
        if sc.visibility == "PRIVATE" and sc.owner != ctx.username and not ({"COMPANY_ADMIN", "SUPER_ADMIN"} & ctx.roles):
            continue
        out.append(scenario_dict(s, sc))
    return out


def create_scenario(s: Session, ctx: Ctx, plant_id: uuid.UUID, name: str, description: str | None = None, clone_from: uuid.UUID | None = None, config: dict | None = None, visibility: str = "SHARED") -> Scenario:
    ctx.require("scenario:write")
    ctx.require_plant(plant_id)
    if not name.strip():
        raise ValidationFailed("Scenario name is required")
    if s.scalar(select(Scenario).where(Scenario.plant_id == plant_id, Scenario.name == name)):
        raise Conflict(f"A scenario named '{name}' already exists", code="DUPLICATE_SCENARIO")
    src = get_scenario(s, ctx, clone_from) if clone_from else None
    cfg = dict(src.config if src else DEFAULT_CONFIG)
    if config:
        cfg.update(config)
    sc = Scenario(
        tenant_id=ctx.tenant_id,
        plant_id=plant_id,
        name=name,
        description=description,
        parent_id=src.id if src else None,
        base_plan_id=src.head_plan_id if src else None,
        head_plan_id=src.head_plan_id if src else None,  # starts from the same plan: instant clone
        is_live=False,
        kind="WHAT_IF",
        owner=ctx.username,
        visibility=visibility,
        config=cfg,
    )
    s.add(sc)
    s.flush()
    if src is not None:
        # copy-on-write: only the change list is copied; factory data is shared, never duplicated
        for c in s.scalars(select(ScenarioChange).where(ScenarioChange.scenario_id == src.id).order_by(ScenarioChange.seq)):
            s.add(ScenarioChange(tenant_id=ctx.tenant_id, scenario_id=sc.id, seq=c.seq, type=c.type, payload=c.payload, description=c.description, is_active=c.is_active))
    audit.record(s, ctx, "CREATE", "scenario", sc.id, sc.name, after={"clone_of": src.name if src else None, "config": cfg})
    return sc


def update_scenario(s: Session, ctx: Ctx, scenario_id: uuid.UUID, data: dict[str, Any]) -> Scenario:
    ctx.require("scenario:write")
    sc = get_scenario(s, ctx, scenario_id)
    check_edit(ctx, sc)
    if "version" in data and data["version"] is not None and int(data["version"]) != sc.version:
        raise Conflict("The scenario was changed by someone else. Reload it.", code="VERSION_CONFLICT")
    before = audit.snapshot(sc, ["name", "description", "config", "visibility", "editors", "status"])
    for k in ("name", "description", "visibility", "editors", "status"):
        if k in data and data[k] is not None:
            setattr(sc, k, data[k])
    if "config" in data and data["config"] is not None:
        cfg = dict(sc.config or {})
        cfg.update(data["config"])
        _validate_config(cfg)
        sc.config = cfg
    audit.record(s, ctx, "UPDATE", "scenario", sc.id, sc.name, before=before, after=audit.snapshot(sc, ["name", "description", "config", "visibility", "editors", "status"]))
    return sc


def _validate_config(cfg: dict) -> None:
    from monxuplan_engine.contract import ConstraintSettings, ObjectiveSpec, SolverSettings

    try:
        if cfg.get("objectives"):
            ObjectiveSpec.model_validate(cfg["objectives"])
        if cfg.get("constraints"):
            ConstraintSettings.model_validate(cfg["constraints"])
        if cfg.get("solver"):
            SolverSettings.model_validate(cfg["solver"])
    except Exception as exc:  # noqa: BLE001
        raise ValidationFailed(f"Invalid optimisation configuration: {exc}", code="INVALID_CONFIG") from exc
    for k, lo, hi in (("horizon_days", 1, 400), ("frozen_hours", 0, 24 * 30), ("flexible_days", 0, 120)):
        if k in cfg and cfg[k] is not None and not (lo <= float(cfg[k]) <= hi):
            raise ValidationFailed(f"{k} must be between {lo} and {hi}")


def add_change(s: Session, ctx: Ctx, scenario_id: uuid.UUID, type_: str, payload: dict[str, Any], description: str | None = None) -> ScenarioChange:
    ctx.require("scenario:write")
    sc = get_scenario(s, ctx, scenario_id)
    check_edit(ctx, sc)
    if sc.is_live and type_ not in ("ADD_DOWNTIME", "LOCK_SEQUENCE", "PIN_RESOURCE", "SET_CONSTRAINTS", "SET_OBJECTIVES", "CHANGE_PRIORITY"):
        raise ValidationFailed("The live scenario follows real data; create a what-if scenario for hypothetical changes.", code="LIVE_SCENARIO")
    if type_ not in CHANGE_TYPES:
        raise ValidationFailed(f"Unknown change type {type_}", context={"allowed": sorted(CHANGE_TYPES)})
    seq = (s.scalar(select(func.max(ScenarioChange.seq)).where(ScenarioChange.scenario_id == sc.id)) or 0) + 1
    ch = ScenarioChange(tenant_id=ctx.tenant_id, scenario_id=sc.id, seq=seq, type=type_, payload=payload, description=description or type_.replace("_", " ").lower(), created_by=ctx.username)
    s.add(ch)
    s.flush()
    # validate by applying to an empty-ish copy lazily at run time; here: structural check only
    audit.record(s, ctx, "SCENARIO_CHANGE_ADDED", "scenario", sc.id, sc.name, after={"type": type_, "payload": payload, "description": ch.description})
    return ch


def remove_change(s: Session, ctx: Ctx, scenario_id: uuid.UUID, change_id: uuid.UUID) -> None:
    ctx.require("scenario:write")
    sc = get_scenario(s, ctx, scenario_id)
    check_edit(ctx, sc)
    ch = s.get(ScenarioChange, change_id)
    if ch is None or ch.scenario_id != sc.id:
        raise NotFound("Change not found")
    audit.record(s, ctx, "SCENARIO_CHANGE_REMOVED", "scenario", sc.id, sc.name, before={"type": ch.type, "payload": ch.payload})
    s.delete(ch)


def lock(s: Session, ctx: Ctx, scenario_id: uuid.UUID, release: bool = False) -> Scenario:
    ctx.require("scenario:write")
    sc = get_scenario(s, ctx, scenario_id)
    if release:
        if sc.locked_by and sc.locked_by != ctx.username and not ({"COMPANY_ADMIN", "SUPER_ADMIN"} & ctx.roles):
            raise Conflict(f"Locked by {sc.locked_by}", code="SCENARIO_LOCKED")
        sc.locked_by = None
        sc.lock_expires_at = None
    else:
        check_edit(ctx, sc)
        sc.locked_by = ctx.username
        sc.lock_expires_at = now() + timedelta(minutes=LOCK_MINUTES)
    audit.record(s, ctx, "UNLOCK" if release else "LOCK", "scenario", sc.id, sc.name)
    return sc


def archive(s: Session, ctx: Ctx, scenario_id: uuid.UUID) -> Scenario:
    ctx.require("scenario:write")
    sc = get_scenario(s, ctx, scenario_id)
    if sc.is_live:
        raise ValidationFailed("The live scenario cannot be archived")
    sc.status = "ARCHIVED"
    audit.record(s, ctx, "ARCHIVE", "scenario", sc.id, sc.name)
    return sc


# ---------------------------------------------------------------------------------------------
# What-if wizards → scenario + changes (the planning run is started by the caller)
# ---------------------------------------------------------------------------------------------


def what_if(s: Session, ctx: Ctx, plant_id: uuid.UUID, kind: str, params: dict[str, Any], base_scenario_id: uuid.UUID | None = None, name: str | None = None) -> tuple[Scenario, list[ScenarioChange]]:
    plant = s.get(Plant, plant_id)
    if plant is None:
        raise NotFound("Plant not found")
    base = base_scenario_id or plant.live_scenario_id
    stamp = now().strftime("%d %b %H:%M")
    title = name or {
        "NIGHT_SHIFT": "What-if: night shift",
        "ADD_MACHINE": "What-if: additional machine",
        "RUSH_ORDER": "What-if: rush order",
        "MATERIAL_DELAY": "What-if: supplier delay",
        "BREAKDOWN": "What-if: breakdown",
        "ADD_OPERATOR": "What-if: extra operator",
        "OVERTIME": "What-if: allow overtime",
    }.get(kind, f"What-if {kind}")
    sc = create_scenario(s, ctx, plant_id, f"{title} ({stamp})", clone_from=base)
    changes: list[ScenarioChange] = []

    def add(t: str, p: dict, d: str) -> None:
        changes.append(add_change(s, ctx, sc.id, t, p, d))

    if kind == "NIGHT_SHIFT":
        res = _resources(s, params.get("resource_ids"))
        days = params.get("weekdays", [0, 1, 2, 3, 4])
        add("ADD_SHIFT", {"resource_ids": [str(r.id) for r in res], "label": "night", "shifts": [{"weekday": d, "start": params.get("start", "22:00"), "end": params.get("end", "06:00"), "kind": "REGULAR"} for d in days]}, f"+ Night shift on {', '.join(r.code for r in res)}")
    elif kind == "ADD_MACHINE":
        src = _resources(s, [params["clone_of"]])[0]
        new_id = str(uuid.uuid4())
        add("ADD_RESOURCE", {"clone_of": str(src.id), "id": new_id, "code": params.get("code", f"{src.code}-NEW"), "name": params.get("name"), "available_from": params.get("available_from"), "efficiency": params.get("efficiency"), "cost_per_hour": params.get("cost_per_hour")}, f"+ {params.get('code', src.code + '-NEW')} (like {src.code})")
    elif kind == "RUSH_ORDER":
        add("ADD_ORDER", rush_order_payload(s, ctx, plant, params), f"+ Rush order {params.get('quantity')} × {params.get('item_code')}")
    elif kind == "MATERIAL_DELAY":
        p = {k: params[k] for k in ("material_id", "supply_id", "supplier_id", "delay_minutes", "new_time") if params.get(k) is not None}
        add("MATERIAL_DELAY", p, f"Supplier delay {params.get('delay_minutes', 0) // 1440 if params.get('delay_minutes') else params.get('new_time')} ")
    elif kind == "BREAKDOWN":
        r = _resources(s, [params["resource_id"]])[0]
        add("ADD_DOWNTIME", {"resource_id": str(r.id), "start": params["start"], "end": params["end"], "kind": "BREAKDOWN", "reason": params.get("reason", "what-if breakdown")}, f"{r.code} breakdown {params['start']} → {params['end']}")
    elif kind == "ADD_OPERATOR":
        r = _resources(s, [params["resource_id"]])[0]
        add("CHANGE_CAPACITY", {"resource_id": str(r.id), "capacity": int(params.get("capacity", r.capacity + 1))}, f"{r.code} capacity {params.get('capacity')}")
    elif kind == "OVERTIME":
        add("SET_CONSTRAINTS", {"constraints": {"allow_overtime": True}}, "Overtime windows allowed")
    else:
        raise ValidationFailed(f"Unknown what-if {kind}")
    sc.description = "; ".join(c.description for c in changes)
    return sc, changes


def _resources(s: Session, ids_or_codes: list[str] | None) -> list[Resource]:
    if not ids_or_codes:
        raise ValidationFailed("Select at least one resource")
    out = []
    for v in ids_or_codes:
        r = None
        try:
            r = s.get(Resource, uuid.UUID(str(v)))
        except ValueError:
            r = s.scalar(select(Resource).where(Resource.code == v))
        if r is None:
            raise NotFound(f"Resource {v} not found")
        out.append(r)
    return out


def rush_order_payload(s: Session, ctx: Ctx, plant: Plant, params: dict[str, Any]) -> dict[str, Any]:
    """Engine payload of a hypothetical order built from the item's routing and BOM."""
    from ..models import Bom, BomLine, OperationResource, ResourceGroupMember, Routing, RoutingOperation

    item = s.scalar(select(Item).where(Item.code == params["item_code"]))
    if item is None:
        raise NotFound(f"Item {params['item_code']} not found")
    qty = float(params["quantity"])
    due = datetime.fromisoformat(params["due"])
    number = params.get("number") or f"RUSH-{now().strftime('%m%d%H%M')}"
    oid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"rush-{number}"))
    routing = s.scalar(select(Routing).where(Routing.item_id == item.id, Routing.is_active.is_(True)))
    if routing is None:
        raise ValidationFailed(f"{item.code} has no routing")
    bom = s.scalar(select(Bom).where(Bom.item_id == item.id, Bom.is_active.is_(True)))
    lines = list(s.scalars(select(BomLine).where(BomLine.bom_id == bom.id))) if bom else []
    rops = list(s.scalars(select(RoutingOperation).where(RoutingOperation.routing_id == routing.id).order_by(RoutingOperation.seq)))
    from ..models import LaborPool

    ops = []
    for k, ro in enumerate(rops):
        modes = []
        for orr in s.scalars(select(OperationResource).where(OperationResource.routing_operation_id == ro.id).order_by(OperationResource.preference)):
            targets = [orr.resource_id] if orr.resource_id else [m.resource_id for m in s.scalars(select(ResourceGroupMember).where(ResourceGroupMember.group_id == orr.group_id))]
            for rid in targets:
                if orr.role == "SUBCONTRACT":
                    modes.append({"resource_id": str(rid), "preference": orr.preference, "subcontract": {"lead_time_minutes": int(orr.subcontract_lead_time_minutes or 0), "cost": float(orr.subcontract_cost or 0)}})
                    continue
                sec = []
                if ro.labor_pool_id:
                    lp = s.get(LaborPool, ro.labor_pool_id)
                    if lp:
                        sec.append({"resource_id": str(lp.resource_id), "units": ro.labor_units or 1})
                if ro.tool_id:
                    sec.append({"resource_id": str(ro.tool_id), "units": ro.tool_units or 1})
                modes.append({"resource_id": str(rid), "preference": orr.preference, "speed_factor": orr.speed_factor or 1.0, "secondary": sec})
        mats = [{"material_id": str(ln.component_id), "quantity": qty * float(ln.quantity_per) / float(bom.base_quantity or 1)} for ln in lines if (ln.operation_seq or rops[0].seq) == ro.seq] if bom else []
        ops.append(
            {
                "id": op_key(number, ro.seq),
                "order_id": oid,
                "seq": ro.seq,
                "code": ro.code,
                "name": ro.name,
                "quantity": qty,
                "duration": {"setup_minutes": ro.setup_minutes or 0, "run_minutes_per_unit": ro.run_minutes_per_unit or 0, "fixed_minutes": ro.fixed_minutes or 0, "batch_size": ro.batch_size, "minutes_per_batch": ro.minutes_per_batch or 0, "move_minutes": ro.move_minutes or 0, "wait_minutes": ro.wait_minutes or 0, "teardown_minutes": ro.teardown_minutes or 0},
                "modes": modes,
                "materials": mats,
                "setup_state": ro.setup_attributes or {},
                "interruptible": ro.interruptible,
            }
        )
    return {
        "order": {"id": oid, "number": number, "item_id": str(item.id), "item_code": item.code, "quantity": qty, "due": due.isoformat(), "priority": int(params.get("priority", 10)), "expedite": bool(params.get("expedite", True)), "family": _family_code(s, item), "attributes": {k: v for k, v in (item.attributes or {}).items() if isinstance(v, str | int | float)}},
        "operations": ops,
    }


def _unused() -> None:  # keep imports referenced for type checkers
    _ = (ProductionOrder,)


def _family_code(s: Session, item: Any) -> str | None:
    from ..models import ProductFamily

    fam = s.get(ProductFamily, item.family_id) if getattr(item, "family_id", None) else None
    return fam.code if fam else None
