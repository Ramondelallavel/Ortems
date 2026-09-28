"""Build the engine :class:`Problem` for a scenario from the database.

This is the only place that translates the relational model into the engine contract. It loads the
plant's data with a bounded number of queries, applies the scenario's copy-on-write changes and
records every data problem it meets (they end up in the plan's violations and the Data Quality
Center) — nothing is silently skipped.
"""

from __future__ import annotations

import json
import math
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Text, cast, select
from sqlalchemy.orm import Session

from monxuplan_engine.calendars import TimeAxis, expand_calendar
from monxuplan_engine.changes import apply_changes
from monxuplan_engine.contract import (
    BaselineOpSpec,
    CalendarSpec,
    DurationSpec,
    FixedAssignmentSpec,
    LotRulesSpec,
    MaterialUseSpec,
    OperationSpec,
    OrderSpec,
    Problem,
)

from ..core.clock import now
from ..models import (
    Bom,
    BomLine,
    Calendar,
    CalendarException,
    CalendarShift,
    Customer,
    Downtime,
    Inventory,
    Item,
    LaborPool,
    Maintenance,
    OperationPrecedence,
    OperationResource,
    Operator,
    OperatorAbsence,
    OptimizationProfile,
    Plan,
    PlanningArea,
    PlanningRule,
    Plant,
    ProductFamily,
    ProductionOrder,
    ProductionOrderOperation,
    PurchaseOrder,
    PurchaseOrderLine,
    Resource,
    ResourceGroup,
    ResourceGroupMember,
    RoutingOperation,
    Scenario,
    ScenarioChange,
    ScheduledOperation,
    SequenceRule,
    SetupMatrix,
    SetupMatrixEntry,
    SetupRule,
    Supplier,
    ToolCompatibility,
    WorkCenter,
)

OPEN_STATUSES = ("PLANNED", "FIRMED", "RELEASED", "IN_PRODUCTION", "PARTIALLY_COMPLETED")
EXC_KIND = {"HOLIDAY": "CLOSED", "CLOSURE": "CLOSED", "CLOSED": "CLOSED", "EXTRA_WORK": "WORKING", "WORKING": "WORKING", "OVERTIME": "OVERTIME"}
MAINT_KIND = {"PREVENTIVE": "MAINTENANCE_PREVENTIVE", "CORRECTIVE": "MAINTENANCE_CORRECTIVE", "PLANNED": "MAINTENANCE_PLANNED", "UNPLANNED": "BREAKDOWN"}
DEFAULT_CONFIG: dict[str, Any] = {
    "horizon_days": 42,
    "frozen_hours": 24,
    "flexible_days": 7,
    "overflow_days": 60,
    "profile_code": None,
    "objectives": {},
    "constraints": {},
    "solver": {},
}


@dataclass
class BuildInfo:
    plant_id: uuid.UUID
    scenario_id: uuid.UUID
    op_rows: dict[str, tuple[uuid.UUID, uuid.UUID]] = field(default_factory=dict)  # op_key -> (order id, order op id)
    order_numbers: dict[str, str] = field(default_factory=dict)
    resource_codes: dict[str, str] = field(default_factory=dict)
    item_codes: dict[str, str] = field(default_factory=dict)
    issues: list[dict[str, Any]] = field(default_factory=list)
    change_log: list[str] = field(default_factory=list)
    excluded_orders: list[dict[str, Any]] = field(default_factory=list)
    horizon_start: datetime | None = None
    frozen_until: datetime | None = None


def _chunks(ids, size: int = 10_000):
    """Split an id collection for IN lists (databases cap the number of bound parameters)."""
    lst = list(ids)
    for i in range(0, len(lst), size):
        yield lst[i : i + size]


def op_key(order_number: str, seq: int) -> str:
    return f"{order_number}/{seq:03d}"


def scenario_config(s: Session, scenario: Scenario) -> dict[str, Any]:
    cfg = {**DEFAULT_CONFIG, **(scenario.config or {})}
    prof = None
    if cfg.get("profile_code"):
        prof = s.scalar(select(OptimizationProfile).where(OptimizationProfile.code == cfg["profile_code"]))
    if prof is None:
        prof = s.scalar(select(OptimizationProfile).where(OptimizationProfile.is_default.is_(True)))
    if prof is not None:
        cfg["objectives"] = {**(prof.objectives or {}), **(cfg.get("objectives") or {})}
        cfg["constraints"] = {**(prof.constraints or {}), **(cfg.get("constraints") or {})}
        cfg["solver"] = {**(prof.solver or {}), **(cfg.get("solver") or {})}
        cfg["profile_code"] = prof.code
    return cfg


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def build_problem(
    s: Session,
    scenario: Scenario,
    *,
    as_of: datetime | None = None,
    baseline_plan: Plan | None = None,
    frozen_plan: Plan | None = None,
    apply_scenario_changes: bool = True,
    overrides: dict[str, Any] | None = None,
) -> tuple[Problem, BuildInfo]:
    plant: Plant = s.get(Plant, scenario.plant_id)
    cfg = scenario_config(s, scenario)
    if overrides:
        for k in ("objectives", "constraints", "solver"):
            if k in overrides:
                cfg[k] = {**cfg.get(k, {}), **overrides[k]}
        for k in ("horizon_days", "frozen_hours", "flexible_days"):
            if k in overrides:
                cfg[k] = overrides[k]
    info = BuildInfo(plant_id=plant.id, scenario_id=scenario.id)
    t0 = (as_of or now()).astimezone(UTC).replace(second=0, microsecond=0)
    h_end = t0 + timedelta(days=float(cfg["horizon_days"]))
    frozen_until = t0 + timedelta(hours=float(cfg["frozen_hours"])) if cfg.get("frozen_hours") else None
    flexible_until = t0 + timedelta(days=float(cfg["flexible_days"])) if cfg.get("flexible_days") else None
    info.horizon_start = t0
    info.frozen_until = frozen_until
    tz = plant.timezone or "UTC"
    lo = t0 - timedelta(days=8)
    hi = h_end + timedelta(days=float(cfg.get("overflow_days", 60)))

    # ------------------------------------------------------------------ resources & groups
    resources = list(s.scalars(select(Resource).where(Resource.plant_id == plant.id, Resource.is_active.is_(True)).order_by(Resource.code)))
    res_by_id = {r.id: r for r in resources}
    info.resource_codes = {str(r.id): r.code for r in resources}
    areas = {a.id: a for a in s.scalars(select(PlanningArea).where(PlanningArea.plant_id == plant.id))}
    wcs = {w.id: w for w in s.scalars(select(WorkCenter).where(WorkCenter.plant_id == plant.id))}
    groups = {g.id: g for g in s.scalars(select(ResourceGroup))}
    members: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    res_groups: dict[uuid.UUID, list[str]] = defaultdict(list)
    for m in s.scalars(select(ResourceGroupMember)):
        if m.resource_id in res_by_id and m.group_id in groups:
            members[m.group_id].append(m.resource_id)
            res_groups[m.resource_id].append(groups[m.group_id].code)

    # ------------------------------------------------------------------ calendars
    cal_rows = {c.id: c for c in s.scalars(select(Calendar))}
    shifts: dict[uuid.UUID, list[CalendarShift]] = defaultdict(list)
    for sh in s.scalars(select(CalendarShift)):
        shifts[sh.calendar_id].append(sh)
    excs: dict[uuid.UUID, list[CalendarException]] = defaultdict(list)
    for ex in s.scalars(select(CalendarException)):
        excs[ex.calendar_id].append(ex)

    def cal_spec(cid: uuid.UUID) -> dict[str, Any]:
        c = cal_rows[cid]
        return {
            "id": str(c.id),
            "name": c.name,
            "timezone": c.timezone or tz,
            "always_available": c.always_available,
            "parent_id": str(c.parent_id) if c.parent_id else None,
            "shifts": [
                {
                    "weekday": sh.weekday,
                    "start": sh.start_time.strftime("%H:%M"),
                    "end": sh.end_time.strftime("%H:%M"),
                    "kind": sh.kind,
                    "label": sh.shift_code,
                    "breaks": sh.breaks or [],
                }
                for sh in sorted(shifts.get(c.id, []), key=lambda x: (x.weekday, x.start_time))
            ],
            "exceptions": [
                {"start": ex.start_local.isoformat(), "end": ex.end_local.isoformat(), "kind": EXC_KIND.get(ex.kind, "CLOSED"), "reason": ex.reason}
                for ex in excs.get(c.id, [])
                if ex.end_local >= lo.replace(tzinfo=None) - timedelta(days=1) and ex.start_local <= hi.replace(tzinfo=None) + timedelta(days=1)
            ],
        }

    used_cals: set[uuid.UUID] = set()

    def resolve_cal(r: Resource) -> uuid.UUID | None:
        cid = r.calendar_id
        if cid is None and r.work_center_id and r.work_center_id in wcs:
            cid = wcs[r.work_center_id].calendar_id
        if cid is None:
            cid = plant.default_calendar_id
        return cid

    # ------------------------------------------------------------------ unavailability
    unav: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
    for m in s.scalars(select(Maintenance).where(Maintenance.end >= lo, Maintenance.start <= hi, Maintenance.status.notin_(["CANCELLED", "DONE"]))):
        if m.resource_id in res_by_id:
            unav[m.resource_id].append({"id": str(m.id), "start": _iso(m.start), "end": _iso(m.end), "kind": MAINT_KIND.get(m.kind, "MAINTENANCE_PLANNED"), "reason": m.description})
    for d in s.scalars(select(Downtime).where(Downtime.start <= hi)):
        if d.resource_id not in res_by_id:
            continue
        end = d.end
        if end is None:
            end = max(t0, d.start.astimezone(UTC) if d.start.tzinfo else d.start.replace(tzinfo=UTC)) + timedelta(hours=4)
            info.issues.append({"severity": "WARNING", "type": "OPEN_DOWNTIME", "message": f"{res_by_id[d.resource_id].code} is down with no expected end; assumed until {end.isoformat()}", "resource_id": str(d.resource_id)})
        if (end.tzinfo and end < lo) or (not end.tzinfo and end < lo.replace(tzinfo=None)):
            continue
        unav[d.resource_id].append({"id": str(d.id), "start": _iso(d.start), "end": _iso(end), "kind": "BREAKDOWN", "reason": d.reason})

    # ------------------------------------------------------------------ setup matrices
    matrices = list(s.scalars(select(SetupMatrix)))
    entries: dict[uuid.UUID, list[SetupMatrixEntry]] = defaultdict(list)
    for e in s.scalars(select(SetupMatrixEntry)):
        entries[e.matrix_id].append(e)
    res_matrices: dict[uuid.UUID, list[str]] = defaultdict(list)
    for mx in matrices:
        if mx.resource_id is not None:
            if mx.resource_id in res_by_id:
                res_matrices[mx.resource_id].append(str(mx.id))
        elif mx.group_id is not None:
            for rid in members.get(mx.group_id, []):
                res_matrices[rid].append(str(mx.id))
        else:
            for r in resources:
                if r.kind == "MACHINE":
                    res_matrices[r.id].append(str(mx.id))

    # ------------------------------------------------------------------ labour pools (capacity profile from operators)
    pools = {p.resource_id: p for p in s.scalars(select(LaborPool).where(LaborPool.plant_id == plant.id))}
    pool_by_id = {p.id: p for p in pools.values()}
    operators = list(s.scalars(select(Operator).where(Operator.plant_id == plant.id, Operator.is_active.is_(True))))
    absences: dict[uuid.UUID, list[OperatorAbsence]] = defaultdict(list)
    for a in s.scalars(select(OperatorAbsence).where(OperatorAbsence.end >= lo, OperatorAbsence.start <= hi)):
        absences[a.operator_id].append(a)
    axis = TimeAxis(t0, tz)
    lo_m, hi_m = axis.to_min(lo), axis.to_min(hi)
    cal_specs_cache: dict[uuid.UUID, CalendarSpec] = {}

    def spec_obj(cid: uuid.UUID) -> CalendarSpec:
        if cid not in cal_specs_cache:
            cal_specs_cache[cid] = CalendarSpec.model_validate(cal_spec(cid))
        return cal_specs_cache[cid]

    pool_profiles: dict[uuid.UUID, list[dict[str, Any]]] = {}
    pool_headcount: dict[uuid.UUID, int] = {}
    registry = {str(cid): spec_obj(cid) for cid in cal_rows} if operators else {}
    for res_id, pool in pools.items():
        mem = [o for o in operators if o.labor_pool_id == pool.id]
        pool_headcount[res_id] = len(mem)
        deltas: dict[int, int] = defaultdict(int)
        for o in mem:
            cid = o.calendar_id or res_by_id[res_id].calendar_id or plant.default_calendar_id
            if cid is None or cid not in cal_rows:
                deltas[lo_m] += 1
                deltas[hi_m] -= 1
                continue
            wc = expand_calendar(spec_obj(cid), axis, lo_m, hi_m, registry)
            blocks = [(axis.to_min(a.start), axis.to_min(a.end)) for a in absences.get(o.id, [])]
            wc = wc.subtract(blocks)
            for a, b in zip(wc.starts, wc.ends, strict=True):
                deltas[a] += 1
                deltas[b] -= 1
        prof = []
        level = 0
        mult = max(pool.machines_per_operator, 1)
        pts = sorted(deltas)
        if not pts or pts[0] > lo_m:
            prof.append((lo_m, 0))
        for t in pts:
            level += deltas[t]
            prof.append((t, level * mult))
        spec_prof = []
        for k, (t, c) in enumerate(prof):
            e = prof[k + 1][0] if k + 1 < len(prof) else hi_m
            if e > t:
                spec_prof.append({"start": axis.to_dt(t).isoformat(), "end": axis.to_dt(e).isoformat(), "capacity": max(c, 0)})
        pool_profiles[res_id] = spec_prof

    # ------------------------------------------------------------------ resource specs
    res_specs = []
    for r in resources:
        cid = resolve_cal(r)
        if r.kind == "LABOR_POOL" and r.id in pools:
            cap = max(pool_headcount.get(r.id, 0) * max(pools[r.id].machines_per_operator, 1), 0)
            res_specs.append(
                {
                    "id": str(r.id),
                    "code": r.code,
                    "name": r.name,
                    "kind": "LABOR_POOL",
                    "capacity": cap,
                    "calendar_id": None,
                    "capacity_profile": pool_profiles.get(r.id, []),
                    "area": areas[r.area_id].code if r.area_id in areas else None,
                    "cost_per_hour": r.cost_per_hour,
                }
            )
            if cap == 0:
                info.issues.append({"severity": "WARNING", "type": "LABOR_POOL_EMPTY", "message": f"Labour pool {r.code} has no active operators", "resource_id": str(r.id)})
            continue
        if cid is not None and cid in cal_rows:
            used_cals.add(cid)
        elif r.kind in ("MACHINE", "WORK_CENTER") and r.is_finite:
            info.issues.append({"severity": "WARNING", "type": "RESOURCE_WITHOUT_CALENDAR", "message": f"{r.code} has no calendar: treated as available 24/7", "resource_id": str(r.id)})
        res_specs.append(
            {
                "id": str(r.id),
                "code": r.code,
                "name": r.name,
                "kind": r.kind,
                "capacity": r.capacity,
                "calendar_id": str(cid) if cid is not None and cid in cal_rows else None,
                "efficiency": max(r.efficiency * (r.speed_factor or 1.0), 0.01),
                "finite": r.is_finite,
                "groups": res_groups.get(r.id, []),
                "area": areas[r.area_id].code if r.area_id in areas else None,
                "plant": plant.code,
                "attributes": r.attributes or {},
                "unavailability": unav.get(r.id, []),
                "initial_state": r.initial_state,
                "setup_matrix_ids": res_matrices.get(r.id, []),
                "setup_combine": r.setup_combine or "MAX",
                "detached_setup": bool(r.detached_setup),
                "cost_per_hour": r.cost_per_hour,
                "overtime_cost_per_hour": r.overtime_cost_per_hour,
                "setup_cost_per_hour": r.setup_cost_per_hour,
                "energy_kw": r.energy_kw,
                "co2_kg_per_kwh": r.co2_kg_per_kwh,
                "available_from": _iso(r.available_from),
                "available_until": _iso(r.available_until),
            }
        )
    # parents of used calendars must be present too
    frontier = list(used_cals)
    while frontier:
        c = cal_rows.get(frontier.pop())
        if c is not None and c.parent_id and c.parent_id not in used_cals and c.parent_id in cal_rows:
            used_cals.add(c.parent_id)
            frontier.append(c.parent_id)

    # ------------------------------------------------------------------ orders
    # high-volume tables are read as plain rows (attribute access like ORM objects, no identity map),
    # restricted with joins/subqueries rather than id lists (100 000 orders exceed any IN list) and
    # fetched in one call per query
    PO, POO = ProductionOrder.__table__, ProductionOrderOperation.__table__
    open_orders = select(PO.c.id).where(PO.c.plant_id == plant.id, PO.c.status.in_(OPEN_STATUSES))
    po_cols = [
        PO.c[n]
        for n in ("id", "number", "item_id", "quantity", "completed_quantity", "status", "due_date", "release_date", "requested_date", "promised_date", "priority", "customer_id", "expedite", "planner_priority", "sales_order_line_id", "bom_id", "routing_id")
    ]
    orders = s.execute(select(*po_cols).where(PO.c.plant_id == plant.id, PO.c.status.in_(OPEN_STATUSES + ("BLOCKED",)))).all()
    for o in [o for o in orders if o.status == "BLOCKED"]:
        info.excluded_orders.append({"order_id": str(o.id), "number": o.number, "reason": "BLOCKED"})
        info.issues.append({"severity": "WARNING", "type": "ORDER_BLOCKED", "message": f"{o.number} is blocked and not scheduled", "order_id": str(o.id)})
    orders = [o for o in orders if o.status != "BLOCKED"]
    item_ids = {o.item_id for o in orders}
    poo_cols = [POO.c[n] for n in ("id", "order_id", "seq", "code", "name", "status", "routing_operation_id", "completed_quantity", "pinned_resource_id", "actual_start", "actual_resource_id")]
    # overrides are rare: read as text and parsed only when present
    poo_cols.append(cast(POO.c.overrides, Text).label("overrides_json"))
    ops_all = s.execute(select(*poo_cols).where(POO.c.order_id.in_(open_orders))).all() if orders else []
    ops_by_order: dict[uuid.UUID, list] = defaultdict(list)
    for op in ops_all:
        ops_by_order[op.order_id].append(op)
    used_rops = select(POO.c.routing_operation_id).where(POO.c.order_id.in_(open_orders), POO.c.routing_operation_id.is_not(None)).distinct()
    rops = {r.id: r for r in s.scalars(select(RoutingOperation).where(RoutingOperation.id.in_(used_rops)))} if ops_all else {}
    op_res: dict[uuid.UUID, list[OperationResource]] = defaultdict(list)
    if rops:
        for orr in s.scalars(select(OperationResource).where(OperationResource.routing_operation_id.in_(used_rops))):
            op_res[orr.routing_operation_id].append(orr)
    precs: dict[uuid.UUID, list[OperationPrecedence]] = defaultdict(list)
    routing_ids = {o.routing_id for o in orders if o.routing_id}
    if routing_ids:
        for pr in s.scalars(select(OperationPrecedence).where(OperationPrecedence.routing_id.in_(select(PO.c.routing_id).where(PO.c.id.in_(open_orders)).distinct()))):
            precs[pr.routing_id].append(pr)
    tool_compat: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)
    for tc in s.scalars(select(ToolCompatibility)):
        tool_compat[tc.tool_id].add(tc.machine_id)
    cust_ids = {o.customer_id for o in orders if o.customer_id}
    customers = {c.id: c for part in _chunks(cust_ids) for c in s.scalars(select(Customer).where(Customer.id.in_(part)))}
    suppliers = {x.id: x for x in s.scalars(select(Supplier))}

    # BOMs (explicit or the item's active BOM)
    bom_by_id: dict[uuid.UUID, Bom] = {}
    bom_by_item: dict[uuid.UUID, Bom] = {}
    for b in s.scalars(select(Bom).where(Bom.is_active.is_(True))):
        bom_by_id[b.id] = b
        bom_by_item.setdefault(b.item_id, b)
    lines: dict[uuid.UUID, list[BomLine]] = defaultdict(list)
    for ln in s.scalars(select(BomLine)):
        lines[ln.bom_id].append(ln)
    order_bom: dict[uuid.UUID, Bom | None] = {o.id: (bom_by_id.get(o.bom_id) if o.bom_id else bom_by_item.get(o.item_id)) for o in orders}
    comp_ids: set[uuid.UUID] = set()
    for b in order_bom.values():
        if b is not None:
            comp_ids.update(ln.component_id for ln in lines.get(b.id, []))
    items = {i.id: i for chunk in _chunks(item_ids | comp_ids) for i in s.scalars(select(Item).where(Item.id.in_(chunk)))}
    families = {f.id: f for f in s.scalars(select(ProductFamily))}
    info.item_codes = {str(i.id): i.code for i in items.values()}

    # ------------------------------------------------------------------ baseline / frozen / locked positions
    SO = ScheduledOperation.__table__
    pos_cols = (SO.c.op_key, SO.c.resource_id, SO.c.setup_start, SO.c.start, SO.c.end, SO.c.is_locked, SO.c.setup_minutes)
    base_positions: dict[str, Any] = {}
    if baseline_plan is not None:
        for so in s.execute(select(*pos_cols).where(SO.c.plan_id == baseline_plan.id)).all():
            base_positions[so.op_key] = so
    frozen_positions: dict[str, Any] = {}
    fp = frozen_plan
    if fp is not None and frozen_until is not None and (cfg.get("constraints") or {}).get("frozen", "HARD") == "HARD":
        for so in s.execute(select(*pos_cols).where(SO.c.plan_id == fp.id, SO.c.setup_start < frozen_until)).all():
            frozen_positions[so.op_key] = so

    # Orders and operations are built as engine objects directly: the parts shared by many orders
    # (an operation template per routing operation, lot rules and attributes per item) are
    # validated once, the per-order values are computed here with explicit guards. Validating
    # 200 000 operations one by one would take longer than scheduling them.
    order_specs: list[OrderSpec] = []
    op_specs: list[OperationSpec] = []
    prec_specs: list[dict[str, Any]] = []
    consumed_items: set[uuid.UUID] = set()
    for o in orders:
        b = order_bom.get(o.id)
        if b is not None:
            consumed_items.update(ln.component_id for ln in lines.get(b.id, []))
    res_str = {rid: str(rid) for rid in res_by_id}
    item_parts: dict[uuid.UUID, tuple] = {}

    def item_part(item: Item) -> tuple:
        part = item_parts.get(item.id)
        if part is None:
            fam = families.get(item.family_id) if item.family_id else None
            attrs = {k: v for k, v in (item.attributes or {}).items() if isinstance(v, str | int | float | bool)}
            lot = LotRulesSpec.model_validate({"min_lot": item.min_lot, "max_lot": item.max_lot, "multiple": item.lot_multiple, "integer": item.quantity_type == "INTEGER"})
            part = (str(item.id), fam.code if fam else None, attrs, lot, str(item.id) if item.id in consumed_items else None, float(item.safety_time_minutes or 0))
            item_parts[item.id] = part
        return part

    def bounded(v: Any, default: int, what: str, o) -> int:
        """Priorities are 0-10 in the engine contract: out-of-range values are clamped and reported."""
        n = int(default if v is None else v)
        if 0 <= n <= 10:
            return n
        info.issues.append({"severity": "WARNING", "type": "PRIORITY_OUT_OF_RANGE", "message": f"{o.number}: {what} {n} is outside 0-10 and was clamped", "order_id": str(o.id)})
        return min(max(n, 0), 10)

    templates: dict[uuid.UUID | None, tuple] = {}

    def template(ro, ov: dict[str, Any]) -> tuple:
        """(duration, modes, setup state, interruptible, splittable, transfer batch, overlap %) of a
        routing operation — validated once and shared by every order operation without overrides."""
        cacheable = not ov
        if cacheable and (ro.id if ro is not None else None) in templates:
            return templates[ro.id if ro is not None else None]

        def param(name: str, default: Any = 0) -> Any:
            if name in ov:
                return ov[name]
            return getattr(ro, name, default) if ro is not None else default

        modes = []
        labor_res = None
        if param("labor_pool_id", None):
            lp = pool_by_id.get(param("labor_pool_id", None))
            labor_res = lp.resource_id if lp else None
        tool = param("tool_id", None)
        for orr in sorted(op_res.get(ro.id, []) if ro else [], key=lambda x: x.preference):
            targets = [orr.resource_id] if orr.resource_id else list(members.get(orr.group_id, []))
            for rid in targets:
                if rid not in res_by_id:
                    continue
                if orr.role == "SUBCONTRACT":
                    sup = suppliers.get(orr.supplier_id) if orr.supplier_id else None
                    modes.append(
                        {
                            "resource_id": res_str[rid],
                            "preference": orr.preference,
                            "subcontract": {"supplier_id": sup.code if sup else None, "lead_time_minutes": int(orr.subcontract_lead_time_minutes or 0), "cost": float(orr.subcontract_cost or 0)},
                            "label": "subcontract",
                        }
                    )
                    continue
                if tool and tool_compat.get(tool) and rid not in tool_compat[tool]:
                    continue
                sec = []
                if labor_res is not None:
                    sec.append({"resource_id": str(labor_res), "units": int(param("labor_units", 1) or 1)})
                if tool and tool in res_by_id:
                    sec.append({"resource_id": str(tool), "units": int(param("tool_units", 1) or 1)})
                modes.append(
                    {
                        "resource_id": res_str[rid],
                        "preference": orr.preference,
                        "speed_factor": orr.speed_factor or 1.0,
                        "setup_minutes": orr.setup_minutes,
                        "run_minutes_per_unit": orr.run_minutes_per_unit,
                        "secondary": sec,
                    }
                )
        duration = DurationSpec.model_validate(
            {
                "setup_minutes": float(param("setup_minutes", 0) or 0),
                "run_minutes_per_unit": float(param("run_minutes_per_unit", 0) or 0),
                "run_tiers": param("run_tiers", []) or [],
                "fixed_minutes": float(param("fixed_minutes", 0) or 0),
                "batch_size": param("batch_size", None),
                "minutes_per_batch": float(param("minutes_per_batch", 0) or 0),
                "teardown_minutes": float(param("teardown_minutes", 0) or 0),
                "queue_minutes": float(param("queue_minutes", 0) or 0),
                "move_minutes": float(param("move_minutes", 0) or 0),
                "wait_minutes": float(param("wait_minutes", 0) or 0),
                "buffer_before_minutes": float(param("buffer_before_minutes", 0) or 0),
                "buffer_after_minutes": float(param("buffer_after_minutes", 0) or 0),
            }
        )
        head = OperationSpec.model_validate(
            {
                "id": "template",
                "order_id": "template",
                "seq": 0,
                "quantity": 1,
                "modes": modes,
                "setup_state": dict(param("setup_attributes", {}) or {}),
                "interruptible": bool(param("interruptible", True)),
                "splittable": bool(param("splittable", False)),
                "transfer_batch": param("transfer_batch", None),
                "overlap_percent": param("overlap_percent", None),
            }
        )
        t = (duration, head.modes, head.setup_state, head.interruptible, head.splittable, head.transfer_batch, head.overlap_percent)
        if cacheable:
            templates[ro.id if ro is not None else None] = t
        return t

    for o in sorted(orders, key=lambda x: x.number):
        item = items.get(o.item_id)
        oid = str(o.id)
        if item is None:
            info.issues.append({"severity": "CRITICAL", "type": "ORDER_UNKNOWN_ITEM", "message": f"{o.number} references an unknown item", "order_id": oid})
            continue
        remaining = max(float(o.quantity) - float(o.completed_quantity or 0), 0.0)
        o_ops = sorted(ops_by_order.get(o.id, []), key=lambda x: x.seq)
        open_ops = [op for op in o_ops if op.status != "COMPLETED"]
        if remaining <= 0 or not open_ops:
            continue
        if not o_ops:
            info.issues.append({"severity": "CRITICAL", "type": "ORDER_WITHOUT_OPERATIONS", "message": f"{o.number} has no operations (routing missing)", "order_id": oid})
            continue
        cust = customers.get(o.customer_id) if o.customer_id else None
        info.order_numbers[oid] = o.number
        item_s, fam_code, attrs, lot, produces, item_safety = item_part(item)
        order_specs.append(
            OrderSpec.fast(
                id=oid,
                number=o.number,
                item_id=item_s,
                item_code=item.code,
                quantity=remaining,
                due=_aware(o.due_date),
                release=_aware(o.release_date),
                requested=_aware(o.requested_date),
                promised=_aware(o.promised_date),
                priority=bounded(o.priority, 5, "priority", o),
                customer_id=str(cust.id) if cust else None,
                customer_priority=bounded(cust.priority, 5, "customer priority", o) if cust else 5,
                strategic=bool(cust.is_strategic) if cust else False,
                expedite=bool(o.expedite),
                planner_priority=bounded(o.planner_priority, 5, "planner priority", o) if o.planner_priority is not None else None,
                family=fam_code,
                attributes=attrs,
                produces_material_id=produces,
                safety_time_minutes=item_safety + float(cust.safety_time_minutes if cust else 0),
                lot=lot,
                status=o.status,
                sales_order_ref=str(o.sales_order_line_id) if o.sales_order_line_id else None,
            )
        )
        bom = order_bom.get(o.id)
        bom_lines = lines.get(bom.id, []) if bom else []
        first_open_seq = open_ops[0].seq
        for op in o_ops:
            if op.status == "COMPLETED":
                continue
            key = op_key(o.number, op.seq)
            ro = rops.get(op.routing_operation_id) if op.routing_operation_id else None
            ov = json.loads(op.overrides_json) if op.overrides_json not in (None, "", "{}", "null") else {}
            duration, modes, state, interruptible, splittable, transfer_batch, overlap = template(ro, ov or {})
            done = float(op.completed_quantity or 0)
            qty = max(remaining if op.status != "IN_PROGRESS" else float(o.quantity) - done, 0.001)
            mats = []
            for ln in bom_lines:
                at_seq = ln.operation_seq if ln.operation_seq is not None else first_open_seq
                if at_seq != op.seq and not (ln.operation_seq is None and op.seq == first_open_seq):
                    continue
                base_q = float(bom.base_quantity or 1)
                need = remaining * float(ln.quantity_per) / base_q / max(1 - (ln.scrap_pct or 0) / 100.0, 0.01)
                comp = items.get(ln.component_id)
                if comp is not None and comp.quantity_type == "INTEGER":
                    need = math.ceil(need - 1e-9)
                if need <= 0:
                    info.issues.append({"severity": "WARNING", "type": "BOM_ZERO_QUANTITY", "message": f"{key}: component {comp.code if comp else ln.component_id} has no quantity to consume", "op_id": key, "order_id": oid})
                    continue
                mats.append(MaterialUseSpec.fast(material_id=str(ln.component_id), quantity=float(need)))
            fixed = None
            remaining_qty = None
            if op.status == "IN_PROGRESS":
                remaining_qty = max(float(o.quantity) - done, 0.001)
                if op.actual_start and op.actual_resource_id:
                    fixed = FixedAssignmentSpec.fast(resource_id=str(op.actual_resource_id), start=_aware(op.actual_start), reason="IN_PROGRESS")
            elif key in frozen_positions:
                so = frozen_positions[key]
                if so.resource_id is not None:
                    fixed = FixedAssignmentSpec.fast(resource_id=str(so.resource_id), start=_aware(so.setup_start), end=_aware(so.end), reason="FROZEN", setup_minutes=float(so.setup_minutes))
            elif key in base_positions and base_positions[key].is_locked and base_positions[key].resource_id is not None:
                so = base_positions[key]
                fixed = FixedAssignmentSpec.fast(resource_id=str(so.resource_id), start=_aware(so.setup_start), end=_aware(so.end), reason="LOCKED", setup_minutes=float(so.setup_minutes))
            if not modes:
                info.issues.append({"severity": "CRITICAL", "type": "OPERATION_WITHOUT_RESOURCE", "message": f"{key} ({op.name}) has no active compatible resource in {plant.code}", "op_id": key, "order_id": oid})
            op_specs.append(
                OperationSpec.fast(
                    id=key,
                    order_id=oid,
                    seq=op.seq,
                    code=op.code,
                    name=op.name,
                    quantity=qty,
                    duration=duration,
                    modes=list(modes),
                    materials=mats,
                    setup_state=state,
                    interruptible=interruptible,
                    splittable=splittable,
                    transfer_batch=transfer_batch,
                    overlap_percent=overlap,
                    status="IN_PROGRESS" if op.status == "IN_PROGRESS" else ("RELEASED" if op.status == "RELEASED" else "PLANNED"),
                    remaining_quantity=remaining_qty,
                    fixed=fixed,
                    pinned_resource_id=str(op.pinned_resource_id) if op.pinned_resource_id else None,
                )
            )
            info.op_rows[key] = (o.id, op.id)
        # precedences from the routing (else the engine chains operations by sequence)
        if o.routing_id and precs.get(o.routing_id):
            seqs = {op.seq for op in o_ops if op.status != "COMPLETED"}
            for pr in precs[o.routing_id]:
                if pr.pred_seq in seqs and pr.succ_seq in seqs:
                    prec_specs.append({"pred": op_key(o.number, pr.pred_seq), "succ": op_key(o.number, pr.succ_seq), "type": pr.type, "lag_minutes": pr.lag_minutes})

    # ------------------------------------------------------------------ materials
    mat_ids = set()
    for sp in op_specs:
        for m in sp.materials:
            mat_ids.add(uuid.UUID(m.material_id))
    for os_ in order_specs:
        if os_.produces_material_id:
            mat_ids.add(uuid.UUID(os_.produces_material_id))
    inv = defaultdict(float)
    for chunk in _chunks(mat_ids):
        for row in s.scalars(select(Inventory).where(Inventory.plant_id == plant.id, Inventory.item_id.in_(chunk))):
            inv[row.item_id] += row.available
    po_lines = []
    for chunk in _chunks(mat_ids):
        po_lines += s.execute(
            select(PurchaseOrderLine, PurchaseOrder)
            .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
            .where(PurchaseOrderLine.item_id.in_(chunk), PurchaseOrderLine.status == "OPEN", PurchaseOrder.status == "OPEN")
        ).all()
    supplies: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
    for ln, po in po_lines:
        if po.plant_id is not None and po.plant_id != plant.id:
            continue
        open_q = float(ln.quantity) - float(ln.received_quantity or 0)
        if open_q <= 0:
            continue
        sup = suppliers.get(po.supplier_id)
        supplies[ln.item_id].append(
            {
                "id": str(ln.id),
                "time": _iso(ln.expected_date),
                "quantity": open_q,
                "kind": "PURCHASE" if ln.confirmed else "PROJECTED",
                "firm": bool(ln.confirmed),
                "ref": f"{po.number}/{ln.line_no}",
                "supplier_id": sup.code if sup else None,
            }
        )
    mat_specs = []
    for mid in sorted(mat_ids, key=str):
        it = items.get(mid) or s.get(Item, mid)
        if it is None:
            continue
        sup_list = list(supplies.get(mid, []))
        if inv.get(mid, 0) > 0:
            sup_list.insert(0, {"id": f"OH:{mid}", "quantity": inv[mid], "kind": "ON_HAND", "ref": "stock"})
        mat_specs.append(
            {
                "id": str(mid),
                "code": it.code,
                "name": it.name,
                "uom": it.uom,
                "quantity_type": it.quantity_type if it.quantity_type in ("INTEGER", "DECIMAL") else "DECIMAL",
                "make_or_buy": it.make_or_buy if it.make_or_buy in ("MAKE", "BUY") else "BUY",
                "supplies": sup_list,
                "safety_stock": float(it.safety_stock or 0),
                "replenishment_lead_time_minutes": int((it.purchase_lead_time_days or 0) * 1440) or None,
            }
        )

    # ------------------------------------------------------------------ rules
    rules = [
        {"id": r.code, "name": r.name, "condition": r.condition or {}, "actions": r.actions or [], "priority": r.priority, "active": r.is_active}
        for r in s.scalars(select(PlanningRule).where(PlanningRule.is_active.is_(True)))
    ]
    setup_rules = []
    for r in s.scalars(select(SetupRule).where(SetupRule.is_active.is_(True))):
        rids = None
        if r.resource_ids:
            rids = [str(x) for x in r.resource_ids]
        setup_rules.append({"id": r.code, "description": r.description, "resource_ids": rids, "when_prev": r.when_prev or {}, "when_next": r.when_next or {}, "add_minutes": r.add_minutes})
    seq_constraints = [
        {"id": r.code, "type": r.type, "resource_ids": [str(x) for x in r.resource_ids] if r.resource_ids else None, "prev_match": r.prev_match or {}, "next_match": r.next_match or {}, "description": r.description}
        for r in s.scalars(select(SequenceRule).where(SequenceRule.is_active.is_(True)))
        if r.type == "NOT_IMMEDIATELY_AFTER"
    ]

    baseline = [
        BaselineOpSpec.fast(op_id=key, resource_id=str(so.resource_id), start=_aware(so.start), end=_aware(so.end), setup_start=_aware(so.setup_start))
        for key, so in base_positions.items()
        if key in info.op_rows and so.resource_id is not None
    ]

    used_matrix_ids = {m for lst in res_matrices.values() for m in lst}
    data: dict[str, Any] = {
        "scenario_id": str(scenario.id),
        "label": scenario.name,
        "as_of": t0.isoformat(),
        "horizon": {
            "start": t0.isoformat(),
            "end": h_end.isoformat(),
            "frozen_until": frozen_until.isoformat() if frozen_until and fp is not None else None,
            "flexible_until": flexible_until.isoformat() if flexible_until else None,
            "timezone": tz,
            "overflow_days": int(cfg.get("overflow_days", 60)),
        },
        "calendars": [cal_spec(cid) for cid in sorted(used_cals, key=str)],
        "resources": res_specs,
        "setup_matrices": [
            {
                "id": str(mx.id),
                "name": mx.name,
                "attribute": mx.attribute,
                "same_minutes": mx.same_minutes,
                "default_minutes": mx.default_minutes,
                "entries": [{"from": e.from_value, "to": e.to_value, "minutes": e.minutes} for e in entries.get(mx.id, [])],
            }
            for mx in matrices
            if str(mx.id) in used_matrix_ids
        ],
        "setup_rules": setup_rules,
        "materials": mat_specs,
        "orders": [],
        "operations": [],
        "precedences": prec_specs,
        "sequence_constraints": seq_constraints,
        "rules": rules,
        "constraints": cfg.get("constraints") or {},
        "objectives": cfg.get("objectives") or {},
        "solver": cfg.get("solver") or {},
        "baseline": [],
    }
    if data["constraints"].get("frozen_zone_blocks_new_work") is None:
        data["constraints"]["frozen_zone_blocks_new_work"] = fp is not None
    problem = Problem.model_validate(data)
    # the high-volume lists were built as engine objects above
    problem.orders, problem.operations, problem.baseline = order_specs, op_specs, baseline
    if apply_scenario_changes:
        changes = list(s.scalars(select(ScenarioChange).where(ScenarioChange.scenario_id == scenario.id, ScenarioChange.is_active.is_(True)).order_by(ScenarioChange.seq)))
        chain: list[ScenarioChange] = []
        all_changes = [{"type": c.type, "payload": c.payload} for c in chain + changes]
        if all_changes:
            problem, info.change_log = apply_changes(problem, all_changes)
    return problem, info
