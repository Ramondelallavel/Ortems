"""PlanValidator — independent feasibility check of any schedule.

It re-derives every HARD constraint from the compiled problem and the placements alone (it does not
trust the builder or the solver): capacity, calendars, setups, cumulative resources, precedences,
materials, releases, frozen operations, sequence rules, compatibility and lot rules; and reports SOFT
deviations (lateness, overtime, beyond horizon). Used after every solve, after manual edits and on
imported plans. The frontend never decides feasibility — this module does.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from .compile import CompiledProblem, Issue
from .setups import matches
from .timelines import CumulativeTimeline

EPS = 1e-9  # lot-size checks on order quantities (floats from the input); material balances are exact

HARD = "HARD"
SOFT = "SOFT"


@dataclass(slots=True)
class V:
    severity: str
    hardness: str
    type: str
    message: str
    order: int | None = None
    op: int | None = None
    res: int | None = None
    mat: int | None = None
    start: int | None = None
    end: int | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationResult:
    violations: list[V]

    @property
    def feasible(self) -> bool:
        return not any(v.hardness == HARD and v.severity == "CRITICAL" for v in self.violations)

    def count(self, hardness: str | None = None) -> int:
        return sum(1 for v in self.violations if hardness is None or v.hardness == hardness)


def issues_to_violations(issues: list[Issue], cp: CompiledProblem) -> list[V]:
    out = []
    for iss in issues:
        refs = iss.refs
        out.append(
            V(
                iss.severity,
                HARD if iss.severity == "CRITICAL" else SOFT,
                f"DATA_{iss.type}",
                iss.message,
                op=cp.op_index.get(refs.get("op_id", "")),
                order=cp.order_index.get(refs.get("order_id", "")),
                res=cp.res_index.get(refs.get("resource_id", "")),
                mat=cp.mat_index.get(refs.get("material_id", "")),
                details=dict(refs),
            )
        )
    return out


def validate(cp: CompiledProblem, placements, unscheduled: dict, include_data_issues: bool = True) -> ValidationResult:
    vs: list[V] = []
    if include_data_issues:
        vs.extend(issues_to_violations(cp.issues, cp))
    ops = cp.ops

    # ------------------------------------------------------------------ unscheduled
    for i, u in unscheduled.items():
        op = ops[i]
        vs.append(V("CRITICAL", HARD, "UNSCHEDULED", u.message, order=op.order, op=i, details={"reason": u.reason, **u.details}))

    by_res: dict[int, list] = defaultdict(list)
    for p in placements:
        if p is None:
            continue
        op = ops[p.op]
        m = op.modes[p.mode]
        res = cp.resources[p.res]
        by_res[p.res].append(p)
        # ---- compatibility
        if m.adhoc:
            vs.append(
                V("CRITICAL", HARD, "INCOMPATIBLE_RESOURCE", f"{op.id} is placed on {res.code}, which is not a resource of its routing", order=op.order, op=p.op, res=p.res, start=p.setup_start, end=p.end)
            )
        # ---- calendar
        if not m.sub:
            need = p.setup + m.run + m.teardown
            have = m.cal.working_between(p.setup_start, p.end)
            if have + 0 < need:
                vs.append(
                    V(
                        "CRITICAL",
                        HARD,
                        "CALENDAR",
                        f"{op.id} on {res.code} needs {need} working min but its interval contains only {have} (non-working time, maintenance or unavailable labour/tool)",
                        order=op.order,
                        op=p.op,
                        res=p.res,
                        start=p.setup_start,
                        end=p.end,
                        details={"required_minutes": need, "available_minutes": have},
                    )
                )
            elif not op.interruptible and not m.cal.fits_uninterrupted(p.setup_start, max(p.end - p.setup_start, 0)):
                vs.append(
                    V("CRITICAL", HARD, "CALENDAR_INTERRUPTED", f"{op.id} must run without interruption but spans non-working time", order=op.order, op=p.op, res=p.res, start=p.setup_start, end=p.end)
                )
        # ---- release / start in the past
        order = cp.orders[op.order]
        ready = p.start if res.detached else p.setup_start
        if not p.fixed:
            if order.release is not None and ready < order.release:
                vs.append(V("CRITICAL", HARD, "RELEASE", f"{op.id} starts before the release date of {order.number}", order=op.order, op=p.op, start=p.setup_start))
            if p.setup_start < cp.as_of:
                vs.append(V("CRITICAL", HARD, "START_IN_PAST", f"{op.id} starts in the past", order=op.order, op=p.op, start=p.setup_start))
        # ---- overtime (soft)
        if p.overtime > 0:
            vs.append(V("INFO", SOFT, "OVERTIME_USED", f"{op.id} uses {p.overtime} min of overtime on {res.code}", order=op.order, op=p.op, res=p.res, start=p.setup_start, end=p.end, details={"minutes": p.overtime}))
        if p.end > cp.h_end:
            vs.append(V("WARNING", SOFT, "BEYOND_HORIZON", f"{op.id} ends after the planning horizon", order=op.order, op=p.op, res=p.res, end=p.end))

    # ------------------------------------------------------------------ unary capacity + setups + sequence rules
    for ri, pls in by_res.items():
        res = cp.resources[ri]
        if not res.unary:
            continue
        pls.sort(key=lambda p: (p.setup_start, p.end))
        prev = None
        for p in pls:
            op = ops[p.op]
            m = op.modes[p.mode]
            sk = _sk(op)
            if prev is not None and p.setup_start < prev.end:
                vs.append(
                    V(
                        "CRITICAL",
                        HARD,
                        "CAPACITY_OVERLAP",
                        f"{res.code}: {op.id} overlaps {ops[prev.op].id}",
                        order=op.order,
                        op=p.op,
                        res=ri,
                        start=p.setup_start,
                        end=min(p.end, prev.end),
                        details={"other_op": ops[prev.op].id},
                    )
                )
            prev_state = _sk(ops[prev.op]) if prev is not None else res.initial_state
            if op.status != "IN_PROGRESS":
                required = cp.setup.setup(ri, prev_state, sk, m.setup_base)
                avail = m.cal.working_between(prev.end if prev is not None else p.setup_start, p.start)
                if avail < required:
                    vs.append(
                        V(
                            "CRITICAL",
                            HARD,
                            "SETUP_INSUFFICIENT",
                            f"{res.code}: changeover to {op.id} needs {required} min, only {avail} min available",
                            order=op.order,
                            op=p.op,
                            res=ri,
                            start=p.setup_start,
                            details={"required_minutes": required, "available_minutes": avail},
                        )
                    )
            if prev is not None:
                for rs, pm, nm, rid in cp.forbidden:
                    if rs is not None and ri not in rs:
                        continue
                    if matches(_sk(ops[prev.op]), pm) and matches(sk, nm):
                        vs.append(
                            V("CRITICAL", HARD, "SEQUENCE_RULE", f"{res.code}: {op.id} cannot immediately follow {ops[prev.op].id} (rule {rid})", order=op.order, op=p.op, res=ri, start=p.setup_start, details={"rule": rid})
                        )
            prev = p

    # ------------------------------------------------------------------ cumulative capacity
    cum: dict[int, CumulativeTimeline] = {}
    for p in placements:
        if p is None:
            continue
        op = ops[p.op]
        m = op.modes[p.mode]
        reqs = list(m.sec)
        res = cp.resources[p.res]
        if not res.unary and res.finite:
            reqs.append((p.res, 1))
        pieces = [(p.setup_start, p.end)] if (m.sub or not op.interruptible) else (m.cal.pieces(p.setup_start, p.end) or [(p.setup_start, p.end)])
        for ri, units in reqs:
            tl = cum.get(ri)
            if tl is None:
                tl = cum[ri] = CumulativeTimeline(ri, cp.resources[ri].profile)
            for a, b in pieces:
                tl.reserve(a, b, units)
    for ri, tl in cum.items():
        for a, b, missing in tl.shortfalls():
            users = [
                ops[p.op].id
                for p in placements
                if p is not None and p.setup_start < b and p.end > a and (p.res == ri or any(r == ri for r, _ in ops[p.op].modes[p.mode].sec))
            ]
            res = cp.resources[ri]
            vs.append(
                V(
                    "CRITICAL",
                    HARD,
                    "CAPACITY_EXCEEDED",
                    f"{res.code}: {missing} unit(s) over capacity ({len(users)} operations overlap)",
                    res=ri,
                    start=a,
                    end=b,
                    details={"missing_units": missing, "operations": users[:20]},
                )
            )

    # ------------------------------------------------------------------ precedences
    for p in placements:
        if p is None:
            continue
        op = ops[p.op]
        for pi, kind, lag, frac in op.preds:
            pp = placements[pi]
            po = ops[pi]
            if pp is None:
                if pi not in unscheduled:
                    continue
                vs.append(V("CRITICAL", HARD, "PRECEDENCE", f"{op.id} is scheduled but its predecessor {po.id} is not", order=op.order, op=p.op))
                continue
            ok = True
            ready = p.start if (cp.resources[p.res].detached and op.interruptible) else p.setup_start
            if kind == "FS":
                ok = ready >= pp.end + po.move + po.wait + op.queue + po.buf_after + op.buf_before + lag
            elif kind == "SS":
                ok = ready >= pp.start + lag
            elif kind == "OVL":
                pm = po.modes[pp.mode]
                first = pm.cal.add_work(pp.start, int(frac * pm.run)) or pp.end
                ok = ready >= first + po.move + lag and p.end >= pp.end + po.move
            elif kind == "FF":
                ok = p.end >= pp.end + lag
            elif kind == "SF":
                ok = p.end >= pp.start + lag
            if not ok:
                vs.append(
                    V(
                        "CRITICAL",
                        HARD,
                        "PRECEDENCE",
                        f"{op.id} starts before {po.id} allows ({kind}{'+' + str(lag) if lag else ''})",
                        order=op.order,
                        op=p.op,
                        res=p.res,
                        start=p.setup_start,
                        details={"predecessor": po.id, "type": kind},
                    )
                )

    # ------------------------------------------------------------------ materials
    if cp.constraints.materials != "IGNORE":
        vs.extend(_validate_materials(cp, placements))

    # ------------------------------------------------------------------ frozen operations
    if cp.frozen_until is not None and cp.baseline:
        for i, (b_res, b_ss, b_start, _b_end) in cp.baseline.items():
            if b_ss >= cp.frozen_until:
                continue
            p = placements[i]
            if p is None:
                continue
            if p.res != b_res or abs(p.start - b_start) > 1:
                hard = cp.constraints.frozen == "HARD"
                vs.append(
                    V(
                        "CRITICAL" if hard else "WARNING",
                        HARD if hard else SOFT,
                        "FROZEN_CHANGED",
                        f"{ops[i].id} is in the frozen zone and was changed",
                        order=ops[i].order,
                        op=i,
                        res=p.res,
                        start=p.setup_start,
                        details={"baseline_resource": cp.resources[b_res].code},
                    )
                )

    # ------------------------------------------------------------------ orders: due dates and lots
    for o in cp.orders:
        spec = o.spec
        if spec.lot is not None:
            lot = spec.lot
            if lot.max_lot is not None and o.qty > lot.max_lot + EPS:
                vs.append(V("CRITICAL", HARD, "LOT_MAX", f"{o.number}: quantity {o.qty:g} exceeds maximum lot {lot.max_lot:g}", order=o.idx))
            if lot.min_lot is not None and o.qty < lot.min_lot - EPS:
                vs.append(V("WARNING", SOFT, "LOT_MIN", f"{o.number}: quantity {o.qty:g} below minimum lot {lot.min_lot:g}", order=o.idx))
            if lot.multiple and abs(o.qty / lot.multiple - round(o.qty / lot.multiple)) > 1e-6:
                vs.append(V("WARNING", SOFT, "LOT_MULTIPLE", f"{o.number}: quantity {o.qty:g} is not a multiple of {lot.multiple:g}", order=o.idx))
            if lot.integer and abs(o.qty - round(o.qty)) > 1e-9:
                vs.append(V("CRITICAL", HARD, "LOT_INTEGER", f"{o.number}: quantity {o.qty:g} must be an integer", order=o.idx))
        if not o.last_ops or any(placements[i] is None for i in o.last_ops):
            continue
        c = max(placements[i].end for i in o.last_ops)
        if c > o.due:
            late = c - o.due
            vs.append(
                V(
                    "CRITICAL" if o.critical else "WARNING",
                    SOFT,
                    "DUE_DATE",
                    f"{o.number} finishes {late // 60}h{late % 60:02d} after its due date",
                    order=o.idx,
                    end=c,
                    details={"lateness_minutes": late},
                )
            )
    return ValidationResult(vs)


def _sk(op):
    from .setups import state_key

    return state_key(op.state) or ()


def _validate_materials(cp: CompiledProblem, placements) -> list[V]:
    """Material balance of a schedule, computed here independently of the builder's ledger: every
    supply (stock, receipts, production of the placed operations) and every consumption as an exact
    integer movement (micro-units, ``quantities.py``), sorted by time with supplies first at equal
    time. A negative running level is a shortage; the consumptions that cannot be served from the
    supplies available at their own time (FIFO) are the operations reported."""
    from .quantities import from_units, to_units

    moves: dict[int, list[tuple[int, int, int, str]]] = defaultdict(list)  # material -> (time, 0 supply | 1 consumption, units, ref)
    for m in cp.materials:
        for t, q, sid, *_ in m.supplies:
            moves[m.idx].append((t, 0, to_units(q), sid))
    touched: set[int] = set()
    for p in placements:
        if p is None:
            continue
        op = cp.ops[p.op]
        for mi, q in op.materials:
            moves[mi].append((p.start, 1, -to_units(q), op.id))
            touched.add(mi)
        if op.produces is not None:
            mat, qty = op.produces
            moves[mat].append((p.end + op.move + op.wait, 0, to_units(qty), f"PROD:{cp.orders[op.order].id}"))
    vs: list[V] = []
    for mi in sorted(touched):
        evs = sorted(moves[mi], key=lambda e: (e[0], e[1]))
        level, low, low_t = 0, 0, None
        for t, _k, u, _ref in evs:
            level += u
            if level < low:
                low, low_t = level, t
        if low >= 0:
            continue
        pool: deque[list[int]] = deque()  # FIFO of [units left] of supplies already available
        short_ops = []
        for _t, kind, u, ref in evs:
            if kind == 0:
                pool.append([u])
                continue
            need = -u
            while need > 0 and pool:
                take = min(pool[0][0], need)
                need -= take
                pool[0][0] -= take
                if pool[0][0] == 0:
                    pool.popleft()
            if need > 0:
                short_ops.append(ref)
        mat = cp.materials[mi]
        shortfall = from_units(-low)
        vs.append(
            V(
                "CRITICAL",
                HARD,
                "MATERIAL_SHORTAGE",
                f"{mat.code}: projected stock goes negative ({-shortfall:g} {mat.uom}); {len(short_ops)} operation(s) not covered",
                mat=mi,
                start=low_t,
                details={"shortfall": shortfall, "uom": mat.uom, "operations": short_ops[:50]},
            )
        )
    return vs
