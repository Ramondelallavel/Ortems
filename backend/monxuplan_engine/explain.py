"""Explainability: why is an operation here, what blocks it, why is an order late.

All explanations are derived from facts computed by the engine (binding constraints, evaluated
alternatives, ledger events) — never from free text. Each reason has a stable ``code`` (localised by
the UI), an English ``text`` and structured ``data``.
"""

from __future__ import annotations

from typing import Any

from .builder import BuildConfig, BuildResult, ScheduleBuilder
from .compile import CompiledProblem
from .contract import Alternative, Binding, Explanation, Reason
from .materials import fifo_pegging


def fmt_minutes(m: int) -> str:
    sign = "-" if m < 0 else "+"
    m = abs(int(m))
    if m >= 60:
        return f"{sign}{m // 60}h {m % 60:02d}m"
    return f"{sign}{m} min"


def binding_out(cp: CompiledProblem, b) -> Binding | None:
    if b is None:
        return None
    return Binding(type=b.type, ref=b.ref, detail=b.detail, at=cp.dt(b.at) if b.at is not None else None, wait_minutes=int(b.wait))


def _block_reasons(cp: CompiledProblem, slot) -> list[Reason]:
    """Summarise what delayed or blocked an alternative."""
    out: list[Reason] = []
    if not slot.feasible:
        out.append(Reason(code=slot.reason or "INFEASIBLE", text=slot.detail or "not feasible"))
        return out
    seen: set[tuple] = set()
    busy_ops: list[str] = []
    for p in slot.pushes:
        if p.type == "RESOURCE" and p.ref in cp.op_index:
            busy_ops.append(p.ref)
            continue
        if p.type == "CALENDAR" and p.data.get("kind"):
            key = ("MAINT", p.ref)
            if key in seen:
                continue
            seen.add(key)
            kind = p.data["kind"]
            code = "MAINTENANCE" if kind.startswith("MAINTENANCE") else "DOWNTIME"
            out.append(
                Reason(
                    code=code,
                    text=p.detail or kind,
                    data={"from": cp.dt(p.data.get("from")).isoformat(), "to": cp.dt(p.data.get("to")).isoformat(), "kind": kind},
                )
            )
            continue
        key = (p.type, p.ref)
        if key in seen:
            continue
        seen.add(key)
        code = {"LABOR": "LABOR_UNAVAILABLE", "TOOL": "TOOL_UNAVAILABLE", "SETUP": "SETUP_CONFLICT", "PREDECESSOR": "PREDECESSOR", "CALENDAR": "NON_WORKING_TIME"}.get(p.type, p.type)
        out.append(Reason(code=code, text=p.detail or p.type, data={"ref": p.ref, "until": cp.dt(p.at).isoformat()}))
    # maintenance / downtime inside the candidate interval (the job would pause over it)
    for ri in (slot.mode.res, *(r for r, _ in slot.mode.sec)):
        res = cp.resources[ri]
        for a, b, kind, reason, uid, _loss in res.unavail:
            if a < slot.end and b > slot.setup_start and ("MAINT", uid or res.id) not in seen:
                seen.add(("MAINT", uid or res.id))
                code = "MAINTENANCE" if kind.startswith("MAINTENANCE") else "DOWNTIME"
                out.append(
                    Reason(
                        code=code,
                        text=f"{res.code} {kind.replace('_', ' ').lower()} {cp.dt(a).isoformat()} – {cp.dt(b).isoformat()}" + (f" ({reason})" if reason else ""),
                        data={"from": cp.dt(a).isoformat(), "to": cp.dt(b).isoformat(), "kind": kind},
                    )
                )
    if busy_ops:
        uniq = list(dict.fromkeys(busy_ops))
        res = cp.resources[slot.mode.res]
        out.insert(0, Reason(code="RESOURCE_BUSY", text=f"{res.code} busy with {len(uniq)} job(s) ({', '.join(uniq[:3])}{'…' if len(uniq) > 3 else ''})", data={"operations": uniq[:20]}))
    return out


def explain_operation(result: BuildResult, i: int) -> Explanation:
    cp = result.cp
    op = cp.ops[i]
    order = cp.orders[op.order]
    pl = result.placements[i]
    if pl is None:
        u = result.unscheduled.get(i)
        return Explanation(
            op_id=op.id,
            order_id=order.id,
            resource_id=None,
            reasons=[Reason(code=u.reason if u else "UNSCHEDULED", text=u.message if u else "not scheduled", data=u.details if u else {})],
            rules_applied=op.rules_applied + order.rules_applied,
        )
    m = op.modes[pl.mode]
    res = cp.resources[pl.res]
    reasons: list[Reason] = []
    if pl.fixed:
        reasons.append(Reason(code="FIXED", text=f"Kept fixed ({pl.fixed_reason})", data={"reason": pl.fixed_reason}))
    if m.adhoc:
        reasons.append(Reason(code="NOT_ROUTING_RESOURCE", text=f"{res.code} is not a routing resource of this operation"))
    elif m.sub:
        reasons.append(Reason(code="SUBCONTRACTED", text=f"Subcontracted to {res.code} (lead time {m.run // 60}h)", data={"cost": m.sub_cost}))
    else:
        reasons.append(Reason(code="COMPATIBLE", text=f"{res.code} is {'the primary' if m.pref == 0 else 'an alternative'} resource for {op.name}", data={"preference": m.pref}))
    for ri, units in m.sec:
        sr = cp.resources[ri]
        code = "LABOR_AVAILABLE" if sr.kind in ("LABOR_POOL", "HUMAN") else "TOOL_AVAILABLE" if sr.kind == "TOOL" else "RESOURCE_AVAILABLE"
        what = "qualified operator" if code == "LABOR_AVAILABLE" else "tool" if code == "TOOL_AVAILABLE" else "resource"
        reasons.append(Reason(code=code, text=f"{sr.code}: {units} {what}(s) available for the whole operation", data={"resource": sr.code, "units": units}))
    if op.materials:
        for mi, q in op.materials:
            mat = cp.materials[mi]
            ready = pl.mat_ready
            reasons.append(
                Reason(
                    code="MATERIAL_READY",
                    text=f"{mat.code}: {q:g} {mat.uom} available" + (f" at {cp.dt(ready).isoformat()}" if ready is not None and pl.mat_wait > 0 else ""),
                    data={"material": mat.code, "quantity": q, "ready": cp.dt(ready).isoformat() if ready is not None else None},
                )
            )
    if pl.shortage:
        for mi, q, a in pl.shortage:
            reasons.append(Reason(code="MATERIAL_SHORTAGE_ALLOWED", text=f"{cp.materials[mi].code}: short {q - a:g} (shortage allowed by scenario)"))
    if pl.prev_op is not None:
        prev = cp.ops[pl.prev_op]
        if prev.family and prev.family == op.family:
            reasons.append(Reason(code="KEEPS_FAMILY", text=f"Keeps product family {op.family} after {prev.id}", data={"family": op.family}))
    feasible_alts = [s for s in pl.alternatives if s.feasible and s.mode.idx != pl.mode]
    if pl.alternatives:
        if feasible_alts:
            best_alt_end = min(s.end for s in feasible_alts)
            if pl.end <= best_alt_end:
                reasons.append(Reason(code="EARLIEST_FINISH", text=f"Earliest finish: {fmt_minutes(best_alt_end - pl.end)[1:]} before the best alternative", data={"minutes": best_alt_end - pl.end}))
            min_alt_setup = min(s.setup for s in feasible_alts)
            if pl.setup < min_alt_setup:
                reasons.append(Reason(code="SETUP_SAVING", text=f"Reduces {min_alt_setup - pl.setup} min of setup versus the best alternative", data={"minutes": min_alt_setup - pl.setup}))
        elif len(pl.alternatives) > 1:
            reasons.append(Reason(code="ONLY_FEASIBLE", text="Only feasible resource at this time"))
    if pl.setup:
        brk = cp.setup.breakdown(res.idx, _prev_state(result, pl), _state(result, i), m.setup_base)
        reasons.append(Reason(code="SETUP", text=f"Setup {pl.setup} min", data={"minutes": pl.setup, "breakdown": brk}))
    # due-date status
    c = result.order_completion(op.order)
    if c is not None:
        if c <= order.due:
            reasons.append(Reason(code="MEETS_DUE_DATE", text=f"Allows order {order.number} to meet its due date ({fmt_minutes(order.due - c)[1:]} slack)", data={"slack_minutes": order.due - c}))
        else:
            reasons.append(Reason(code="ORDER_LATE", text=f"Order {order.number} is late by {fmt_minutes(c - order.due)[1:]}", data={"lateness_minutes": c - order.due}))
    b = pl.binding
    if b is not None and b.type not in ("NONE", "HORIZON_START", "FIXED"):
        reasons.append(Reason(code=f"BOUND_BY_{b.type}", text=f"Start determined by {b.type.lower()}: {b.detail or b.ref or ''}".strip(), data={"ref": b.ref, "wait_minutes": b.wait}))
    for rid in op.rules_applied + order.rules_applied:
        reasons.append(Reason(code="RULE", text=f"Planning rule {rid} applied", data={"rule": rid}))
    alts: list[Alternative] = []
    for s in pl.alternatives:
        r = cp.resources[s.mode.res]
        alts.append(
            Alternative(
                mode_index=s.mode.idx,
                resource_id=r.id,
                feasible=s.feasible,
                chosen=s.mode.idx == pl.mode,
                start=cp.dt(s.setup_start) if s.feasible else None,
                end=cp.dt(s.end) if s.feasible else None,
                delta_finish_minutes=(s.end - pl.end) if s.feasible else None,
                setup_minutes=s.setup if s.feasible else None,
                blocking=[] if s.mode.idx == pl.mode else _block_reasons(cp, s),
            )
        )
    return Explanation(
        op_id=op.id,
        order_id=order.id,
        resource_id=res.id,
        start=cp.dt(pl.setup_start),
        end=cp.dt(pl.end),
        reasons=reasons,
        alternatives=alts,
        binding=binding_out(cp, b),
        rules_applied=op.rules_applied + order.rules_applied,
    )


def _state(result: BuildResult, i: int):
    from .setups import state_key

    return state_key(result.cp.ops[i].state) or ()


def _prev_state(result: BuildResult, pl):
    if pl.prev_op is not None:
        return _state(result, pl.prev_op)
    return result.cp.resources[pl.res].initial_state


# =============================================================================================
# Constraint explorer (counterfactual)
# =============================================================================================


def explore_operation(result: BuildResult, i: int) -> dict[str, Any]:
    """Re-evaluate every alternative of an operation against the schedule of all *other* operations.

    Answers "why can't I schedule this operation (elsewhere / earlier)?".
    """
    cp = result.cp
    overrides = {}
    for p in result.placements:
        if p is None or p.op == i:
            continue
        overrides[p.op] = (p.mode, p.setup_start, p.end, "KEPT")
    cfg = BuildConfig(rules=result.config.rules, overrides=overrides, explain=True, mode_selection=result.config.mode_selection)
    b = ScheduleBuilder(cp, cfg, result.timing)
    b._place_fixed()
    op = cp.ops[i]
    lb, src, end_lb = b._lower_bound(op)
    rows = []
    for m in op.modes:
        s = b._slot(op, m, lb, end_lb)
        res = cp.resources[m.res]
        rows.append(
            {
                "resource_id": res.id,
                "resource": res.code,
                "preference": m.pref,
                "feasible": s.feasible,
                "start": cp.dt(s.setup_start).isoformat() if s.feasible else None,
                "end": cp.dt(s.end).isoformat() if s.feasible else None,
                "setup_minutes": s.setup if s.feasible else None,
                "reasons": [r.model_dump() for r in _block_reasons(cp, s)],
            }
        )
    mode_res = {m.res for m in op.modes}
    group_peers = set()
    for m in op.modes:
        for g in cp.resources[m.res].groups:
            group_peers.update(cp.group_members.get(g, []))
    for ri in sorted(group_peers - mode_res):
        res = cp.resources[ri]
        rows.append(
            {
                "resource_id": res.id,
                "resource": res.code,
                "preference": None,
                "feasible": False,
                "start": None,
                "end": None,
                "setup_minutes": None,
                "reasons": [{"code": "INCOMPATIBLE", "text": f"{res.code} is not qualified for {op.name} (not in the routing)", "data": {}}],
            }
        )
    feasible = [r for r in rows if r["feasible"]]
    recommended = min(feasible, key=lambda r: (r["end"], r["preference"] or 0)) if feasible else None
    current = result.placements[i]
    return {
        "op_id": op.id,
        "order_id": cp.orders[op.order].id,
        "earliest_start_from_predecessors": cp.dt(lb).isoformat(),
        "lower_bound": {"type": src.type, "ref": src.ref, "detail": src.detail},
        "current_resource": cp.resources[current.res].id if current else None,
        "alternatives": rows,
        "recommended": recommended["resource_id"] if recommended else None,
    }


def check_position(result: BuildResult, i: int, res_id: str, start: int) -> dict[str, Any]:
    """Checklist of every constraint for placing operation i on a resource at a given start."""
    cp = result.cp
    op = cp.ops[i]
    ri = cp.res_index.get(res_id)
    checks: list[dict[str, Any]] = []

    def add(code: str, ok: bool, text: str, **data):
        checks.append({"code": code, "ok": ok, "text": text, "data": data})

    mode = next((m for m in op.modes if m.res == ri), None) if ri is not None else None
    if mode is None:
        add("COMPATIBILITY", False, f"{res_id} is not a resource of this operation's routing")
        return {"feasible": False, "checks": checks}
    res = cp.resources[ri]
    add("COMPATIBILITY", True, f"{res.code} is compatible")
    overrides = {p.op: (p.mode, p.setup_start, p.end, "KEPT") for p in result.placements if p is not None and p.op != i}
    b = ScheduleBuilder(cp, BuildConfig(overrides=overrides, explain=False), result.timing)
    b._place_fixed()
    lb, _src, _end_lb = b._lower_bound(op)
    add("PRECEDENCE", start >= lb, "Predecessors allow this start" if start >= lb else f"Predecessors finish at {cp.dt(lb).isoformat()}", earliest=cp.dt(lb).isoformat())
    if cp.orders[op.order].release is not None:
        rel = cp.orders[op.order].release
        add("RELEASE", start >= rel, "After order release" if start >= rel else "Before order release")
    add("CALENDAR", mode.cal.is_working(start), "Working time" if mode.cal.is_working(start) else "Non-working time (shift, holiday, maintenance or labour/tool unavailability)")
    setup = mode.setup_base
    if res.unary:
        tl = b.unary[ri]
        k = tl.prev_index(start)
        prev_state = tl.blocks[k].state_key if k >= 0 else res.initial_state
        setup = cp.setup.setup(ri, prev_state, _state(result, i), mode.setup_base)
    work = setup + mode.run + mode.teardown
    end = mode.cal.add_work(start, work) or cp.hi
    if res.unary:
        tl = b.unary[ri]
        busy = [cp.ops[bl.op].id for bl in tl.blocks if bl.setup_start < end and bl.end > start]
        add("CAPACITY", not busy, f"{res.code} free" if not busy else f"{res.code} busy with {', '.join(busy[:3])}", operations=busy)
    elif res.finite:
        nf = b.cumulative[ri].next_fit(start, end, 1)
        add("CAPACITY", nf is None, f"{res.code} has free capacity" if nf is None else f"{res.code} full")
    for r2, units in mode.sec:
        nf = b.cumulative[r2].next_fit(start, end, units)
        sr = cp.resources[r2]
        code = "LABOR" if sr.kind in ("LABOR_POOL", "HUMAN") else "TOOL"
        add(code, nf is None, f"{sr.code} available" if nf is None else f"{sr.code} not available ({units} needed)")
    for mi, q in op.materials:
        t = b.ledger.earliest(mi, q, start)
        ok = t is not None and t <= start
        mat = cp.materials[mi]
        add("MATERIAL", ok, f"{mat.code} available" if ok else (f"{mat.code} available only at {cp.dt(t).isoformat()}" if t is not None else f"{mat.code} not available in the horizon"))
    if cp.frozen_until is not None and cp.constraints.frozen_zone_blocks_new_work and start < cp.frozen_until and op.fixed is None:
        add("FROZEN_ZONE", False, "Inside the frozen zone")
    return {
        "feasible": all(c["ok"] for c in checks),
        "checks": checks,
        "setup_minutes": setup,
        "end": cp.dt(end).isoformat(),
    }


# =============================================================================================
# Root cause of lateness
# =============================================================================================


def root_cause(result: BuildResult, order_idx: int, max_steps: int = 25) -> list[dict[str, Any]]:
    cp = result.cp
    order = cp.orders[order_idx]
    steps: list[dict[str, Any]] = []
    c = result.order_completion(order_idx)
    if c is None:
        missing = [i for i in order.ops if result.placements[i] is None]
        for i in missing[:1]:
            u = result.unscheduled.get(i)
            steps.append({"level": "ORDER", "code": "UNSCHEDULED", "text": f"{order.number} cannot be completed", "ref": order.number})
            if u:
                steps.append({"level": "OPERATION", "code": u.reason, "text": u.message, "ref": cp.ops[i].id, "data": u.details})
        return steps
    late = c - order.due
    steps.append(
        {
            "level": "ORDER",
            "code": "LATE" if late > 0 else "ON_TIME",
            "text": f"{order.number} {'late by ' + fmt_minutes(late)[1:] if late > 0 else 'on time'}",
            "ref": order.number,
            "data": {"due": cp.dt(order.due).isoformat(), "end": cp.dt(c).isoformat(), "lateness_minutes": late},
        }
    )
    cur = max(order.last_ops, key=lambda i: result.placements[i].end)
    visited: set[int] = set()
    waits = {"RESOURCE", "SETUP", "LABOR", "TOOL", "CALENDAR"}
    while cur is not None and len(steps) < max_steps and cur not in visited:
        visited.add(cur)
        pl = result.placements[cur]
        op = cp.ops[cur]
        res = cp.resources[pl.res]
        b = pl.binding
        base = {
            "level": "OPERATION",
            "ref": op.id,
            "order": cp.orders[op.order].number,
            "resource": res.code,
            "start": cp.dt(pl.setup_start).isoformat(),
            "end": cp.dt(pl.end).isoformat(),
        }
        if b is not None and b.type in waits:
            if b.type == "RESOURCE" and b.ref in cp.op_index:
                bo = cp.ops[cp.op_index[b.ref]]
                steps.append({**base, "code": "RESOURCE_BUSY", "text": f"{op.id}: {res.code} busy with {bo.id} ({cp.orders[bo.order].number}) — waited {fmt_minutes(b.wait)[1:]} of working time", "data": {"wait_minutes": b.wait, "blocking_op": bo.id}})
            elif b.type == "RESOURCE":
                steps.append({**base, "code": "RESOURCE_FULL", "text": f"{op.id}: {res.code} at full capacity — waited {fmt_minutes(b.wait)[1:]}", "data": {"wait_minutes": b.wait}})
            elif b.type == "CALENDAR" and b.wait == 0:
                pass  # only the next shift start: not worth a step
            else:
                label = {"LABOR": "qualified operators unavailable", "TOOL": "tool unavailable", "CALENDAR": "maintenance / non-working time", "SETUP": "changeover conflict"}.get(b.type, b.type)
                steps.append({**base, "code": f"WAITS_{b.type}", "text": f"{op.id}: {label} ({b.detail or b.ref}) — {fmt_minutes(b.wait)[1:]}", "data": {"wait_minutes": b.wait}})
            b = pl.lb_src  # …and why was it not ready earlier?
        if b is None or b.type in ("NONE", "HORIZON_START", "FIXED", "FROZEN_ZONE", "RELEASE"):
            what = {"RELEASE": "release date", "FROZEN_ZONE": "frozen zone", "FIXED": "fixed by planner / in progress", "HORIZON_START": "start of horizon"}.get(b.type if b else "NONE", "no waiting")
            steps.append({**base, "code": f"STARTS_AT_{b.type if b else 'NONE'}", "text": f"{op.id} on {res.code} could not start earlier: {what}"})
            break
        if b.type == "PREDECESSOR":
            nxt = cp.op_index.get(b.ref) if b.ref else None
            steps.append({**base, "code": "WAITS_PREDECESSOR", "text": f"{op.id} ready only when predecessor {b.ref} finished"})
            if nxt is None or result.placements[nxt] is None:
                break
            cur = nxt
            continue
        if b.type == "MATERIAL":
            mi = cp.mat_index.get(b.ref or "")
            steps.append({**base, "code": "WAITS_MATERIAL", "text": f"{op.id} waits for material {b.detail or b.ref} — {fmt_minutes(b.wait)[1:]}", "data": {"wait_minutes": b.wait}})
            if mi is not None:
                supply = _supply_for(result, mi, op.id)
                if supply is not None:
                    kind = supply.meta.get("kind")
                    if kind == "PRODUCTION":
                        oi = supply.meta.get("order")
                        steps.append({"level": "MATERIAL", "code": "COMPONENT_ORDER", "text": f"{cp.materials[mi].code} comes from production order {supply.meta.get('ref')} finishing {cp.dt(supply.time).isoformat()}", "ref": supply.meta.get("ref")})
                        if oi is not None and cp.orders[oi].last_ops and all(result.placements[i] is not None for i in cp.orders[oi].last_ops):
                            cur = max(cp.orders[oi].last_ops, key=lambda i: result.placements[i].end)
                            continue
                    else:
                        steps.append(
                            {
                                "level": "SUPPLY",
                                "code": "SUPPLY_ARRIVAL",
                                "text": f"{cp.materials[mi].code} receipt {supply.meta.get('ref') or supply.ref} expected {cp.dt(supply.time).isoformat()}"
                                + (f" from supplier {supply.meta.get('supplier')}" if supply.meta.get("supplier") else ""),
                                "ref": supply.meta.get("ref") or supply.ref,
                                "data": {"supplier": supply.meta.get("supplier")},
                            }
                        )
            break
        break
    return steps


def _supply_for(result: BuildResult, mi: int, op_id: str):
    acc = result.ledger.accounts[mi]
    last = None
    for s, c, _q in fifo_pegging(acc):
        if c.ref == op_id:
            if last is None or s.time > last.time:
                last = s
    return last
