"""Compile a contract :class:`Problem` into index-based structures for the algorithms.

The compiler resolves references, expands calendars, builds effective calendars per mode
(primary ∩ secondary resources, minus unavailability), computes durations, applies planning rules
(with trace), derives precedence edges (routing, overlap, sequence rules, make-item pegging) and
collects data issues. It never drops a problem silently: anything it cannot interpret becomes an
:class:`Issue` reported in the solution.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from . import rules as rules_mod
from .calendars import TimeAxis, WorkCalendar, expand_calendar
from .contract import (
    CalendarSpec,
    ConstraintSettings,
    ModeSpec,
    ObjectiveSpec,
    OperationSpec,
    OrderSpec,
    Problem,
    ResourceSpec,
    SolverSettings,
)
from .durations import run_minutes, whole
from .setups import SetupModel, StateKey, state_key

ENGINE_VERSION = "1.0.0"
DAY = 1440
UNARY_KINDS = {"MACHINE", "WORK_CENTER", "ROOM", "TRANSPORT", "HUMAN", "STORAGE"}


@dataclass(slots=True)
class Issue:
    severity: str  # CRITICAL | WARNING | INFO
    type: str
    message: str
    refs: dict[str, Any] = field(default_factory=dict)


class CRes:
    __slots__ = (
        "idx", "id", "code", "name", "kind", "capacity", "finite", "unary", "cal", "profile", "efficiency",
        "groups", "area", "plant", "attrs", "initial_state", "unavail", "cost_per_min", "ot_cost_per_min",
        "setup_cost_per_min", "energy_kw", "co2", "spec", "detached",
    )

    def __init__(self, idx: int, spec: ResourceSpec) -> None:
        self.idx = idx
        self.id = spec.id
        self.code = spec.code
        self.name = spec.name or spec.code
        self.kind = spec.kind
        self.capacity = spec.capacity
        self.finite = spec.finite
        self.unary = spec.finite and spec.capacity == 1 and spec.kind in UNARY_KINDS
        self.detached = bool(spec.detached_setup) and self.unary
        self.cal: WorkCalendar = WorkCalendar()
        self.profile: list[tuple[int, int]] = []
        self.efficiency = spec.efficiency
        self.groups = list(spec.groups)
        self.area = spec.area
        self.plant = spec.plant
        self.attrs = dict(spec.attributes)
        self.initial_state: StateKey | None = state_key(spec.initial_state)
        self.unavail: list[tuple[int, int, str, str | None, str | None, int | None]] = []
        self.cost_per_min = spec.cost_per_hour / 60.0
        self.ot_cost_per_min = spec.overtime_cost_per_hour / 60.0
        self.setup_cost_per_min = spec.setup_cost_per_hour / 60.0
        self.energy_kw = spec.energy_kw
        self.co2 = spec.co2_kg_per_kwh
        self.spec = spec


class CMode:
    __slots__ = (
        "idx", "res", "sec", "cal", "setup_base", "run", "teardown", "pref", "cost_per_min", "sub",
        "sub_cost", "label", "adhoc",
    )

    def __init__(self) -> None:
        self.idx = 0
        self.res = 0
        self.sec: tuple[tuple[int, int], ...] = ()
        self.cal: WorkCalendar = WorkCalendar()
        self.setup_base = 0
        self.run = 0
        self.teardown = 0
        self.pref = 0
        self.cost_per_min = 0.0
        self.sub = False
        self.sub_cost = 0.0
        self.label: str | None = None
        self.adhoc = False

    @property
    def nominal(self) -> int:
        return self.setup_base + self.run + self.teardown


class COp:
    __slots__ = (
        "idx", "id", "order", "seq", "code", "name", "qty", "modes", "materials", "state", "state_key",
        "interruptible", "queue", "move", "wait", "buf_before", "buf_after", "preds", "succs", "fixed",
        "pinned", "earliest", "status", "rules_applied", "is_last", "produces", "cycle", "extra_setup",
        "splittable", "spec", "family",
    )

    def __init__(self) -> None:
        self.preds: list[tuple[int, str, int, float]] = []  # (pred, kind, lag, frac)
        self.succs: list[tuple[int, str, int, float]] = []
        self.materials: list[tuple[int, float]] = []
        self.modes: list[CMode] = []
        self.fixed: tuple[int, int, int | None, str, int | None] | None = None  # mode, setup_start, end, reason, setup
        self.pinned: int | None = None
        self.earliest: int | None = None
        self.rules_applied: list[str] = []
        self.is_last = False
        self.produces: tuple[int, float] | None = None
        self.cycle = False
        self.extra_setup = 0


class COrd:
    __slots__ = (
        "idx", "id", "number", "item_id", "item_code", "qty", "due", "release", "weight", "priority",
        "customer_id", "customer_priority", "expedite", "strategic", "family", "ops", "last_ops", "produces",
        "safety", "rules_applied", "critical", "revenue", "spec", "requested", "promised",
    )


class CMat:
    __slots__ = (
        "idx", "id", "code", "name", "uom", "integer", "make", "supplies", "safety_stock", "lead", "producers",
        "consumers",
    )


@dataclass(slots=True)
class StaticPeg:
    material: int
    consumer_op: int
    qty: float
    supply_kind: str  # ON_HAND/PURCHASE/.../PRODUCTION/UNCOVERED
    supply_ref: str | None
    producer_order: int | None


@dataclass
class CompiledProblem:
    problem: Problem
    axis: TimeAxis
    lo: int
    hi: int
    h_end: int
    as_of: int
    frozen_until: int | None
    flexible_until: int | None
    work_lb: int  # earliest start for non-fixed work
    resources: list[CRes]
    ops: list[COp]
    orders: list[COrd]
    materials: list[CMat]
    setup: SetupModel
    constraints: ConstraintSettings
    objectives: ObjectiveSpec
    solver: SolverSettings
    res_index: dict[str, int]
    op_index: dict[str, int]
    order_index: dict[str, int]
    mat_index: dict[str, int]
    forbidden: list[tuple[set[int] | None, dict, dict, str]]
    topo: list[int]
    baseline: dict[int, tuple[int, int, int, int]]
    static_pegs: list[StaticPeg]
    issues: list[Issue]
    input_hash: str
    group_members: dict[str, list[int]]
    calendars: dict[str, WorkCalendar]

    def dt(self, minutes: int | None):
        return None if minutes is None else self.axis.to_dt(minutes)


def problem_hash(problem: Problem) -> str:
    payload = problem.model_dump(mode="json")
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(blob).hexdigest()


# =============================================================================================
# Compiler
# =============================================================================================


def compile_problem(problem: Problem) -> CompiledProblem:
    issues: list[Issue] = []
    hz = problem.horizon
    axis = TimeAxis(hz.start, hz.timezone)
    h_end = axis.to_min(hz.end)
    fixed_starts = [axis.to_min(op.fixed.start) for op in problem.operations if op.fixed is not None]
    lo = min([-7 * DAY] + [s - DAY for s in fixed_starts])
    hi = h_end + hz.overflow_days * DAY
    as_of = axis.to_min(problem.as_of) if problem.as_of else 0
    frozen_until = axis.to_min(hz.frozen_until) if hz.frozen_until else None
    flexible_until = axis.to_min(hz.flexible_until) if hz.flexible_until else None
    cons = problem.constraints
    work_lb = as_of
    if frozen_until is not None and cons.frozen_zone_blocks_new_work:
        work_lb = max(work_lb, frozen_until)

    # ------------------------------------------------------------------ calendars
    registry: dict[str, CalendarSpec] = {c.id: c for c in problem.calendars}
    expanded: dict[str, WorkCalendar] = {}

    def cal_for(cal_id: str | None) -> WorkCalendar | None:
        if cal_id is None:
            return WorkCalendar.always(lo, hi)
        if cal_id in expanded:
            return expanded[cal_id]
        spec = registry.get(cal_id)
        if spec is None:
            return None
        c = expand_calendar(spec, axis, lo, hi, registry)
        if not cons.allow_overtime:
            c = c.without_overtime()
        expanded[cal_id] = c
        return c

    for c in problem.calendars:
        cal_for(c.id)

    # ------------------------------------------------------------------ resources
    setup = SetupModel(problem.setup_matrices, problem.setup_rules)
    resources: list[CRes] = []
    res_index: dict[str, int] = {}
    group_members: dict[str, list[int]] = defaultdict(list)
    for spec in problem.resources:
        if spec.id in res_index:
            issues.append(Issue("CRITICAL", "DUPLICATE_RESOURCE", f"Resource id {spec.id} defined twice", {"resource_id": spec.id}))
            continue
        r = CRes(len(resources), spec)
        base = cal_for(spec.calendar_id)
        if base is None:
            issues.append(
                Issue(
                    "CRITICAL",
                    "RESOURCE_WITHOUT_CALENDAR",
                    f"Resource {spec.code} references unknown calendar {spec.calendar_id}; it has no capacity",
                    {"resource_id": spec.id, "calendar_id": spec.calendar_id},
                )
            )
            base = WorkCalendar()
        if spec.available_from or spec.available_until:
            a = axis.to_min(spec.available_from) if spec.available_from else lo
            b = axis.to_min(spec.available_until) if spec.available_until else hi
            base = base.clip(a, b)
        blocks_all: list[tuple[int, int]] = []
        partial: list[tuple[int, int, int]] = []
        for u in spec.unavailability:
            a, b = axis.to_min(u.start), axis.to_min_ceil(u.end)
            if b <= a:
                continue
            r.unavail.append((a, b, u.kind, u.reason, u.id, u.capacity_loss))
            if u.capacity_loss is None or r.unary:
                blocks_all.append((a, b))
            else:
                partial.append((a, b, u.capacity_loss))
        r.cal = base.subtract(blocks_all, key=f"R:{spec.id}")
        if not r.unary and r.finite:
            r.profile = _capacity_profile(r.cal, spec, axis, partial)
            # calendar of a cumulative resource = periods with capacity > 0
            wins = []
            for k, (t, c) in enumerate(r.profile):
                if c > 0:
                    e = r.profile[k + 1][0] if k + 1 < len(r.profile) else hi
                    wins.append((t, e, r.cal.overtime[r.cal.window_index(t)] if r.cal.window_index(t) >= 0 else False))
            r.cal = WorkCalendar(wins, key=f"R:{spec.id}")
        if r.cal.total_minutes == 0 and r.finite:
            issues.append(
                Issue("WARNING", "RESOURCE_NO_CAPACITY", f"Resource {spec.code} has no working time in the horizon", {"resource_id": spec.id})
            )
        unknown = setup.register_resource(r.idx, r.id, spec.setup_matrix_ids, spec.setup_combine)
        for m in unknown:
            issues.append(Issue("WARNING", "UNKNOWN_SETUP_MATRIX", f"Resource {spec.code} references unknown setup matrix {m}", {"resource_id": spec.id}))
        resources.append(r)
        res_index[spec.id] = r.idx
        for g in spec.groups:
            group_members[g].append(r.idx)

    # ------------------------------------------------------------------ materials
    materials: list[CMat] = []
    mat_index: dict[str, int] = {}
    for ms in problem.materials:
        m = CMat()
        m.idx = len(materials)
        m.id, m.code, m.name, m.uom = ms.id, ms.code, ms.name or ms.code, ms.uom
        m.integer = ms.quantity_type == "INTEGER"
        m.make = ms.make_or_buy == "MAKE"
        m.safety_stock = ms.safety_stock
        m.lead = ms.replenishment_lead_time_minutes
        m.producers = []
        m.consumers = []
        m.supplies = []
        for s in ms.supplies:
            if not s.firm or s.kind == "PROJECTED":
                if not cons.projected_receipts:
                    issues.append(
                        Issue("INFO", "PROJECTED_RECEIPT_IGNORED", f"Projected receipt {s.ref or s.id} of {ms.code} not counted (scenario setting)", {"material_id": ms.id, "supply_id": s.id})
                    )
                    continue
            if s.time is None or s.kind == "ON_HAND":
                t = lo
            else:
                t = axis.to_min(s.time)
                if t < as_of:
                    issues.append(
                        Issue(
                            "WARNING",
                            "OVERDUE_RECEIPT",
                            f"Receipt {s.ref or s.id} of {ms.code} was expected {s.time.isoformat()} and is not received; assumed available now",
                            {"material_id": ms.id, "supply_id": s.id},
                        )
                    )
                    t = as_of
            m.supplies.append((t, s.quantity, s.id, s.kind, s.ref, s.firm, s.supplier_id))
        materials.append(m)
        mat_index[ms.id] = m.idx

    # ------------------------------------------------------------------ orders
    active_rules = rules_mod.active_rules(problem.rules)
    order_rules = [r for r in active_rules if any(a.get("type") in rules_mod.ORDER_ACTIONS for a in r.actions)]
    op_rules = [r for r in active_rules if any(a.get("type") in rules_mod.OP_ACTIONS for a in r.actions)]
    pw = problem.objectives.priority_weighting
    orders: list[COrd] = []
    order_index: dict[str, int] = {}
    order_ctx: dict[int, dict[str, Any]] = {}
    for os_ in problem.orders:
        if os_.id in order_index:
            issues.append(Issue("CRITICAL", "DUPLICATE_ORDER", f"Order {os_.number} defined twice", {"order_id": os_.id}))
            continue
        o = COrd()
        o.idx = len(orders)
        o.id, o.number, o.item_id = os_.id, os_.number, os_.item_id
        o.item_code = os_.item_code or os_.item_id
        o.qty = os_.quantity
        o.due = axis.to_min(os_.due)
        o.release = axis.to_min(os_.release) if os_.release else None
        o.requested = axis.to_min(os_.requested) if os_.requested else None
        o.promised = axis.to_min(os_.promised) if os_.promised else None
        o.priority = os_.priority
        o.customer_id = os_.customer_id
        o.customer_priority = os_.customer_priority
        o.expedite = os_.expedite
        o.strategic = os_.strategic
        o.family = os_.family
        o.revenue = os_.revenue
        o.safety = whole(os_.safety_time_minutes)
        o.ops = []
        o.last_ops = []
        o.rules_applied = []
        o.spec = os_
        o.produces = mat_index.get(os_.produces_material_id) if os_.produces_material_id else None
        if os_.produces_material_id and o.produces is None:
            issues.append(Issue("WARNING", "UNKNOWN_MATERIAL", f"Order {os_.number} produces unknown material {os_.produces_material_id}", {"order_id": os_.id}))
        if os_.weight is not None:
            w = os_.weight
        else:
            w = (
                pw.base
                + pw.priority * os_.priority
                + pw.customer_priority * os_.customer_priority
                + pw.planner_priority * (os_.planner_priority or 0)
                + pw.expedite * (1 if os_.expedite else 0)
                + pw.strategic * (1 if os_.strategic else 0)
            )
        ctx = {"order": _order_ctx(os_)}
        for rule in order_rules:
            if rules_mod.evaluate(rule.condition, ctx):
                fired = False
                for a in rule.actions:
                    t = a.get("type")
                    if t == "ADD_WEIGHT":
                        w += float(a.get("value", 0))
                        fired = True
                    elif t == "MULTIPLY_WEIGHT":
                        w *= float(a.get("value", 1))
                        fired = True
                    elif t == "SET_WEIGHT":
                        w = float(a.get("value", w))
                        fired = True
                    elif t == "ADD_SAFETY_TIME":
                        o.safety += whole(float(a.get("minutes", 0)))
                        fired = True
                if fired:
                    o.rules_applied.append(rule.id)
        o.weight = max(0.1, w)
        o.critical = os_.expedite or os_.strategic or os_.priority >= 8
        orders.append(o)
        order_index[os_.id] = o.idx
        order_ctx[o.idx] = ctx
        if o.produces is not None:
            materials[o.produces].producers.append(o.idx)

    # ------------------------------------------------------------------ operations
    ops: list[COp] = []
    op_index: dict[str, int] = {}
    eff_cache: dict[tuple, WorkCalendar] = {}
    always = WorkCalendar.always(lo, hi, key="24/7")
    for spec in problem.operations:
        if spec.status == "COMPLETED":
            continue
        if spec.id in op_index:
            issues.append(Issue("CRITICAL", "DUPLICATE_OPERATION", f"Operation {spec.id} defined twice", {"op_id": spec.id}))
            continue
        oi = order_index.get(spec.order_id)
        if oi is None:
            issues.append(Issue("CRITICAL", "OPERATION_WITHOUT_ORDER", f"Operation {spec.id} references unknown order {spec.order_id}", {"op_id": spec.id}))
            continue
        order = orders[oi]
        op = COp()
        op.idx = len(ops)
        op.id = spec.id
        op.order = oi
        op.seq = spec.seq
        op.code = spec.code or str(spec.seq)
        op.name = spec.name or op.code
        op.spec = spec
        op.status = spec.status
        in_progress = spec.status == "IN_PROGRESS"
        op.qty = spec.remaining_quantity if (in_progress and spec.remaining_quantity is not None) else spec.quantity
        op.interruptible = spec.interruptible
        op.splittable = spec.splittable
        d = spec.duration
        op.queue, op.move, op.wait = whole(d.queue_minutes), whole(d.move_minutes), whole(d.wait_minutes)
        op.buf_before, op.buf_after = whole(d.buffer_before_minutes), whole(d.buffer_after_minutes)
        op.earliest = axis.to_min(spec.earliest_start) if spec.earliest_start else None
        op.family = order.family
        state: dict[str, Any] = {"item": order.item_code}
        if order.family:
            state["family"] = order.family
        state.update({k: v for k, v in order.spec.attributes.items() if isinstance(v, str | int | float | bool)})
        state.update(spec.setup_state)
        op.state = state
        ops_ctx = dict(order_ctx[oi])
        mode_groups = sorted({g for m in spec.modes if m.resource_id in res_index for g in resources[res_index[m.resource_id]].groups})
        ops_ctx["op"] = {
            "code": op.code,
            "name": op.name,
            "seq": op.seq,
            "quantity": op.qty,
            "resource_groups": mode_groups,
            "resources": [resources[res_index[m.resource_id]].code for m in spec.modes if m.resource_id in res_index],
        }
        # ---- modes
        prefs: dict[int, int] = {}
        forbidden_modes: set[int] = set()
        for rule in op_rules:
            if rules_mod.uses_resource_fields(rule.condition):
                for mi, m in enumerate(spec.modes):
                    ri_ = res_index.get(m.resource_id)
                    if ri_ is None:
                        continue
                    rr = resources[ri_]
                    rctx = dict(ops_ctx)
                    rctx["resource"] = {"code": rr.code, "kind": rr.kind, "groups": rr.groups, "area": rr.area, "attributes": rr.attrs}
                    if rules_mod.evaluate(rule.condition, rctx):
                        if _apply_op_actions(rule, spec, mi, resources, res_index, prefs, forbidden_modes, op) and rule.id not in op.rules_applied:
                            op.rules_applied.append(rule.id)
            elif rules_mod.evaluate(rule.condition, ops_ctx):
                if _apply_op_actions(rule, spec, None, resources, res_index, prefs, forbidden_modes, op) and rule.id not in op.rules_applied:
                    op.rules_applied.append(rule.id)
        for mi, ms in enumerate(spec.modes):
            ri = res_index.get(ms.resource_id)
            if ri is None:
                issues.append(Issue("CRITICAL", "UNKNOWN_RESOURCE", f"Operation {spec.id} references unknown resource {ms.resource_id}", {"op_id": spec.id, "resource_id": ms.resource_id}))
                continue
            if mi in forbidden_modes:
                continue
            if spec.pinned_resource_id and ms.resource_id != spec.pinned_resource_id:
                continue
            mode = _build_mode(ms, mi, ri, spec, op, resources, res_index, cons, eff_cache, always, issues)
            if mode is None:
                continue
            if mi in prefs:
                mode.pref = prefs[mi]
            mode.idx = len(op.modes)
            op.modes.append(mode)
        if spec.pinned_resource_id:
            op.pinned = res_index.get(spec.pinned_resource_id)
        # ---- fixed assignment
        fx = spec.fixed
        if fx is not None and fx.reason == "FROZEN" and cons.frozen == "ALLOW_CHANGES":
            fx = None
        if fx is not None:
            ri = res_index.get(fx.resource_id)
            if ri is None:
                issues.append(Issue("CRITICAL", "UNKNOWN_RESOURCE", f"Fixed operation {spec.id} on unknown resource {fx.resource_id}", {"op_id": spec.id}))
            else:
                mi = next((m.idx for m in op.modes if m.res == ri), None)
                if mi is None:
                    ms_like = next((m for m in spec.modes if m.resource_id == fx.resource_id), None)
                    mode = _build_mode(ms_like or ModeSpec(resource_id=fx.resource_id), len(spec.modes), ri, spec, op, resources, res_index, cons, eff_cache, always, issues)
                    if mode is not None:
                        mode.adhoc = ms_like is None
                        mode.idx = len(op.modes)
                        op.modes.append(mode)
                        mi = mode.idx
                        if ms_like is None:
                            issues.append(
                                Issue("WARNING", "FIXED_ON_UNLISTED_RESOURCE", f"Operation {spec.id} is fixed on {resources[ri].code}, which is not one of its routing resources", {"op_id": spec.id, "resource_id": fx.resource_id})
                            )
                if mi is not None:
                    s = axis.to_min(fx.start)
                    e = axis.to_min_ceil(fx.end) if fx.end else None
                    su = whole(fx.setup_minutes) if fx.setup_minutes is not None else (0 if in_progress else None)
                    op.fixed = (mi, s, e, fx.reason, su)
        if in_progress and op.fixed is None:
            issues.append(Issue("WARNING", "IN_PROGRESS_WITHOUT_ASSIGNMENT", f"Operation {spec.id} is in progress but has no actual resource/start; it will be rescheduled", {"op_id": spec.id}))
        if not op.modes:
            issues.append(Issue("CRITICAL", "OPERATION_WITHOUT_RESOURCE", f"Operation {spec.id} ({op.name}) of order {order.number} has no compatible resource", {"op_id": spec.id, "order_id": order.id}))
        # ---- materials (in-progress operations already consumed their material)
        if not in_progress and cons.materials != "IGNORE":
            for mu in spec.materials:
                mi_ = mat_index.get(mu.material_id)
                if mi_ is None:
                    issues.append(Issue("CRITICAL", "UNKNOWN_MATERIAL", f"Operation {spec.id} consumes unknown material {mu.material_id}", {"op_id": spec.id, "material_id": mu.material_id}))
                    continue
                op.materials.append((mi_, mu.quantity))
                materials[mi_].consumers.append(op.idx)
        ops.append(op)
        op_index[spec.id] = op.idx
        order.ops.append(op.idx)

    # ------------------------------------------------------------------ precedences
    edges: set[tuple[int, int]] = set()

    def add_edge(a: int, b: int, kind: str, lag: int, frac: float = 0.0) -> None:
        if a == b or (a, b) in edges:
            return
        edges.add((a, b))
        ops[a].succs.append((b, kind, lag, frac))
        ops[b].preds.append((a, kind, lag, frac))

    has_explicit: set[int] = set()
    for p in problem.precedences:
        a, b = op_index.get(p.pred), op_index.get(p.succ)
        if a is None or b is None:
            # a completed predecessor is not an error: nothing to wait for
            missing = p.pred if a is None else p.succ
            known = any(o.id == missing for o in problem.operations)
            if not known:
                issues.append(Issue("WARNING", "UNKNOWN_PRECEDENCE_REF", f"Precedence references unknown operation {missing}", {"pred": p.pred, "succ": p.succ}))
            continue
        kind, lag, frac = p.type, int(round(p.lag_minutes)), 0.0
        if kind == "FS" and p.kind == "ROUTING":
            frac = _overlap_fraction(ops[a])
            if frac > 0:
                kind = "OVL"
        add_edge(a, b, kind, lag, frac)
        if p.kind == "ROUTING" and ops[a].order == ops[b].order:
            has_explicit.add(ops[a].order)
    for o in orders:
        o.ops.sort(key=lambda i: (ops[i].seq, ops[i].id))
        if o.idx not in has_explicit and len(o.ops) > 1:
            for a, b in zip(o.ops, o.ops[1:], strict=False):
                frac = _overlap_fraction(ops[a])
                add_edge(a, b, "OVL" if frac > 0 else "FS", 0, frac)

    # ------------------------------------------------------------------ sequence constraints
    forbidden: list[tuple[set[int] | None, dict, dict, str]] = []
    if cons.sequence_rules:
        for sc in problem.sequence_constraints:
            if sc.type == "BEFORE":
                a, b = op_index.get(sc.op_a or ""), op_index.get(sc.op_b or "")
                if a is None or b is None:
                    issues.append(Issue("WARNING", "UNKNOWN_SEQUENCE_REF", f"Sequence rule {sc.id} references unknown operations", {"rule": sc.id}))
                    continue
                add_edge(a, b, "FS", 0)
            elif sc.type == "LOCKED_SEQUENCE":
                idxs = [op_index[x] for x in sc.op_ids if x in op_index]
                ri = res_index.get((sc.resource_ids or [None])[0] or "")
                for a, b in zip(idxs, idxs[1:], strict=False):
                    add_edge(a, b, "FS", 0)
                if ri is not None:
                    for i in idxs:
                        if any(m.res == ri for m in ops[i].modes):
                            ops[i].modes = [m for m in ops[i].modes if m.res == ri]
                            for k, m in enumerate(ops[i].modes):
                                m.idx = k
                            if ops[i].fixed is not None:
                                # keep fixed assignment consistent with new mode indexing
                                ops[i].fixed = (0,) + ops[i].fixed[1:]
                            ops[i].pinned = ri
            elif sc.type == "NOT_IMMEDIATELY_AFTER":
                rs = {res_index[r] for r in sc.resource_ids if r in res_index} if sc.resource_ids else None
                forbidden.append((rs, sc.prev_match, sc.next_match, sc.id))

    for o in orders:
        idxs = set(o.ops)
        o.last_ops = [i for i in o.ops if not any(s in idxs for s, *_ in ops[i].succs)]
        for i in o.last_ops:
            ops[i].is_last = True
        if o.produces is not None and o.ops:
            last = max(o.ops, key=lambda i: (ops[i].seq, i))
            ops[last].produces = (o.produces, o.qty)

    # ------------------------------------------------------------------ make-item pegging
    from .pegging import static_pegging

    static_pegs = static_pegging(ops, orders, materials, issues)
    for peg in static_pegs:
        if peg.producer_order is not None:
            prod = orders[peg.producer_order]
            if prod.ops:
                src = max(prod.ops, key=lambda i: (ops[i].seq, i))
                add_edge(src, peg.consumer_op, "FS", 0)

    # ------------------------------------------------------------------ topological order / cycles
    topo = _topological(ops)
    if len(topo) < len(ops):
        in_topo = set(topo)
        cyc = [o for o in ops if o.idx not in in_topo]
        for o in cyc:
            o.cycle = True
        issues.append(
            Issue(
                "CRITICAL",
                "PRECEDENCE_CYCLE",
                f"{len(cyc)} operations are part of a precedence cycle (routing, sequence rules or circular BOM)",
                {"op_ids": [o.id for o in cyc[:50]]},
            )
        )
        topo = topo + [o.idx for o in cyc]

    # ------------------------------------------------------------------ baseline
    baseline: dict[int, tuple[int, int, int, int]] = {}
    for b in problem.baseline:
        oi_ = op_index.get(b.op_id)
        ri_ = res_index.get(b.resource_id)
        if oi_ is None or ri_ is None:
            continue
        s = axis.to_min(b.start)
        ss = axis.to_min(b.setup_start) if b.setup_start else s
        baseline[oi_] = (ri_, ss, s, axis.to_min_ceil(b.end))

    return CompiledProblem(
        problem=problem,
        axis=axis,
        lo=lo,
        hi=hi,
        h_end=h_end,
        as_of=as_of,
        frozen_until=frozen_until,
        flexible_until=flexible_until,
        work_lb=work_lb,
        resources=resources,
        ops=ops,
        orders=orders,
        materials=materials,
        setup=setup,
        constraints=cons,
        objectives=problem.objectives,
        solver=problem.solver,
        res_index=res_index,
        op_index=op_index,
        order_index=order_index,
        mat_index=mat_index,
        forbidden=forbidden,
        topo=topo,
        baseline=baseline,
        static_pegs=static_pegs,
        issues=issues,
        input_hash=problem_hash(problem),
        group_members=dict(group_members),
        calendars=expanded,
    )


# =============================================================================================
# helpers
# =============================================================================================


def _order_ctx(o: OrderSpec) -> dict[str, Any]:
    return {
        "id": o.id,
        "number": o.number,
        "item_id": o.item_id,
        "item_code": o.item_code or o.item_id,
        "family": o.family,
        "priority": o.priority,
        "customer_id": o.customer_id,
        "customer_priority": o.customer_priority,
        "strategic": o.strategic,
        "expedite": o.expedite,
        "quantity": o.quantity,
        "attributes": o.attributes,
    }


def _apply_op_actions(rule, spec: OperationSpec, mode_i: int | None, resources, res_index, prefs, forbidden_modes, op: COp) -> bool:
    fired = False
    for a in rule.actions:
        t = a.get("type")
        if t in {"PREFER_RESOURCE", "AVOID_RESOURCE", "FORBID_RESOURCE"}:
            target = a.get("resource")
            for mi, m in enumerate(spec.modes):
                if mode_i is not None and mi != mode_i:
                    continue
                ri = res_index.get(m.resource_id)
                if ri is None:
                    continue
                is_target = target in (resources[ri].code, resources[ri].id)
                if t == "PREFER_RESOURCE":
                    prefs[mi] = 0 if is_target else max(prefs.get(mi, m.preference), 1)
                    fired = True
                elif t == "AVOID_RESOURCE" and is_target:
                    prefs[mi] = prefs.get(mi, m.preference) + int(a.get("penalty", 1))
                    fired = True
                elif t == "FORBID_RESOURCE" and is_target:
                    forbidden_modes.add(mi)
                    fired = True
            if t == "FORBID_RESOURCE" and len(forbidden_modes) >= len(spec.modes):
                # never let a rule silently remove every alternative
                forbidden_modes.clear()
        elif t == "ADD_SETUP_MINUTES":
            op.extra_setup += whole(float(a.get("value", 0)))
            fired = True
        elif t == "SET_SETUP_ATTRIBUTE" and a.get("key"):
            op.state[str(a["key"])] = a.get("value")
            fired = True
    return fired


def _build_mode(ms, mi, ri, spec: OperationSpec, op: COp, resources, res_index, cons, eff_cache, always, issues) -> CMode | None:
    r = resources[ri]
    mode = CMode()
    mode.res = ri
    mode.pref = ms.preference
    mode.label = ms.label
    d = spec.duration
    if ms.subcontract is not None:
        mode.sub = True
        mode.cal = always
        mode.run = int(ms.subcontract.lead_time_minutes)
        mode.setup_base = 0
        mode.teardown = 0
        mode.sub_cost = ms.subcontract.cost
        return mode
    in_progress = spec.status == "IN_PROGRESS"
    mode.run = run_minutes(d, op.qty, r.efficiency, ms.speed_factor, ms.run_minutes_per_unit)
    base_setup = ms.setup_minutes if ms.setup_minutes is not None else d.setup_minutes
    mode.setup_base = 0 if in_progress else whole(base_setup) + op.extra_setup
    mode.teardown = whole(d.teardown_minutes)
    cpm = ms.cost_per_hour / 60.0 if ms.cost_per_hour is not None else r.cost_per_min
    mode.cost_per_min = cpm
    secs: list[tuple[int, int]] = []
    for sreq in ms.secondary:
        si = res_index.get(sreq.resource_id)
        if si is None:
            issues.append(Issue("CRITICAL", "UNKNOWN_RESOURCE", f"Operation {spec.id} requires unknown resource {sreq.resource_id}", {"op_id": spec.id, "resource_id": sreq.resource_id}))
            return None
        sr = resources[si]
        if sr.kind == "LABOR_POOL" and cons.labor == "IGNORE":
            continue
        if sr.kind == "TOOL" and cons.tools == "IGNORE":
            continue
        if not sr.finite:
            continue
        if sr.capacity < sreq.units and not sr.spec.capacity_profile:
            issues.append(
                Issue(
                    "CRITICAL",
                    "SECONDARY_CAPACITY_TOO_SMALL",
                    f"Operation {spec.id} needs {sreq.units} × {sr.code} but only {sr.capacity} exist",
                    {"op_id": spec.id, "resource_id": sr.id},
                )
            )
        secs.append((si, sreq.units))
    mode.sec = tuple(secs)
    key = (ri, tuple(s for s, _ in secs))
    cal = eff_cache.get(key)
    if cal is None:
        cal = r.cal
        for si, _u in secs:
            cal = cal.intersect(resources[si].cal)
        eff_cache[key] = cal
    mode.cal = cal
    return mode


def _capacity_profile(cal: WorkCalendar, spec: ResourceSpec, axis: TimeAxis, partial: list[tuple[int, int, int]]) -> list[tuple[int, int]]:
    overrides = [(axis.to_min(c.start), axis.to_min(c.end), c.capacity) for c in spec.capacity_profile]
    points: set[int] = set()
    for s, e in zip(cal.starts, cal.ends, strict=True):
        points.add(s)
        points.add(e)
    for a, b, _ in overrides:
        points.add(a)
        points.add(b)
    for a, b, _ in partial:
        points.add(a)
        points.add(b)
    out: list[tuple[int, int]] = []
    for t in sorted(points):
        if not cal.is_working(t):
            c = 0
        else:
            c = spec.capacity
            for a, b, v in overrides:
                if a <= t < b:
                    c = v
            for a, b, loss in partial:
                if a <= t < b:
                    c -= loss
            c = max(c, 0)
        if not out or out[-1][1] != c:
            out.append((t, c))
    return out


def _overlap_fraction(op: COp) -> float:
    spec = op.spec
    if spec.transfer_batch and op.qty > 0:
        f = min(1.0, spec.transfer_batch / op.qty)
        return f if f < 1.0 else 0.0
    if spec.overlap_percent:
        return max(0.0, min(1.0, 1.0 - spec.overlap_percent / 100.0)) or 1e-6
    return 0.0


def _topological(ops: list[COp]) -> list[int]:
    indeg = [0] * len(ops)
    for o in ops:
        for s, *_ in o.succs:
            indeg[s] += 1
    q = deque(sorted((o.idx for o in ops if indeg[o.idx] == 0), key=lambda i: (ops[i].order, ops[i].seq)))
    out = []
    while q:
        i = q.popleft()
        out.append(i)
        for s, *_ in ops[i].succs:
            indeg[s] -= 1
            if indeg[s] == 0:
                q.append(s)
    return out
