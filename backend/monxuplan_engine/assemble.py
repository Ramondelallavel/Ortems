"""Turn a :class:`BuildResult` into the contract :class:`Solution`."""

from __future__ import annotations

from .builder import BuildResult
from .capacity import bottlenecks as compute_bottlenecks
from .contract import (
    Bottleneck,
    BottleneckCause,
    OrderResult,
    PegLink,
    ScheduledOperation,
    SecondaryAllocation,
    Solution,
    SolverMetadata,
    UnscheduledOperation,
    Violation,
)
from .explain import binding_out, explain_operation
from .kpis import compute as compute_kpis
from .kpis import order_status
from .materials import fifo_pegging
from .stability import stability
from .validator import ValidationResult, validate


def zone_of(cp, t: int) -> str:
    if cp.frozen_until is not None and t < cp.frozen_until:
        return "FROZEN"
    if cp.flexible_until is not None and t < cp.flexible_until:
        return "FLEXIBLE"
    return "PLANNING"


def assemble(result: BuildResult, meta: SolverMetadata, validation: ValidationResult | None = None, explain: bool = True) -> Solution:
    cp = result.cp
    validation = validation or validate(cp, result.placements, result.unscheduled)
    orders = order_status(result)
    stab = stability(cp, result.placements)
    kpis, details = compute_kpis(result, validation, orders, stab if cp.baseline else None)
    details["stability"] = stab
    # full causal chains for late / unscheduled orders (Root Cause Analysis) and "deadline impossible"
    from .explain import root_cause

    chains: dict[str, list] = {}
    for row in orders:
        if row["status"] in ("LATE", "UNSCHEDULED", "PARTIAL") and len(chains) < 400:
            o = cp.orders[row["order"]]
            chains[o.id] = root_cause(result, o.idx)
    details["root_causes"] = chains
    for lo in details.get("late_orders", []):
        oi = cp.order_index.get(lo["order_id"])
        if oi is None:
            continue
        eft = result.timing.order_eft[oi]
        lo["earliest_possible_infinite_capacity"] = cp.dt(eft).isoformat() if eft is not None else None
        lateness = lo.get("lateness_minutes") or 0
        waits = sum(st.get("data", {}).get("wait_minutes", 0) for st in chains.get(lo["order_id"], []) if st.get("code") in ("RESOURCE_BUSY", "RESOURCE_FULL", "WAITS_LABOR", "WAITS_TOOL", "WAITS_SETUP"))
        lo["required_additional_capacity_h"] = round(min(lateness, waits) / 60.0, 1) if lateness and waits else (0.0 if lateness else None)

    schedule: list[ScheduledOperation] = []
    for p in result.placements:
        if p is None:
            continue
        op = cp.ops[p.op]
        m = op.modes[p.mode]
        o = cp.orders[op.order]
        c = result.order_completion(op.order)
        schedule.append(
            ScheduledOperation.fast(
                op_id=op.id,
                order_id=o.id,
                resource_id=cp.resources[p.res].id,
                mode_index=p.mode,
                secondary=[SecondaryAllocation.fast(resource_id=cp.resources[r].id, units=u) for r, u in m.sec],
                setup_start=cp.dt(p.setup_start),
                start=cp.dt(p.start),
                end=cp.dt(p.end),
                setup_minutes=p.setup,
                run_minutes=m.run,
                teardown_minutes=m.teardown,
                working_minutes=(p.end - p.setup_start) if m.sub else m.cal.working_between(p.setup_start, p.end),
                overtime_minutes=p.overtime,
                quantity=op.qty,
                fixed=p.fixed,
                fixed_reason=p.fixed_reason,
                late=c is not None and c > o.due,
                zone=zone_of(cp, p.setup_start),
                prev_op_id=cp.ops[p.prev_op].id if p.prev_op is not None else None,
                material_ready=cp.dt(p.mat_ready) if p.mat_ready is not None else None,
                binding=binding_out(cp, p.binding),
                subcontracted=m.sub,
                cost=round(p.cost, 2),
            )
        )
    schedule.sort(key=lambda s: (s.resource_id, s.setup_start))

    unscheduled = [
        UnscheduledOperation.fast(op_id=cp.ops[i].id, order_id=cp.orders[cp.ops[i].order].id, reason=u.reason, message=u.message, details=u.details)
        for i, u in sorted(result.unscheduled.items())
    ]

    violations = [
        Violation.fast(
            severity=v.severity,
            hardness=v.hardness,
            type=v.type,
            message=v.message,
            order_id=cp.orders[v.order].id if v.order is not None else None,
            op_id=cp.ops[v.op].id if v.op is not None else None,
            resource_id=cp.resources[v.res].id if v.res is not None else None,
            material_id=cp.materials[v.mat].id if v.mat is not None else None,
            start=cp.dt(v.start) if v.start is not None else None,
            end=cp.dt(v.end) if v.end is not None else None,
            details=v.details,
        )
        for v in validation.violations
    ]

    order_late: dict[int, int] = {}
    order_results: list[OrderResult] = []
    for row in orders:
        o = cp.orders[row["order"]]
        last_binding = None
        if row["end"] is not None and o.last_ops:
            crit = max(o.last_ops, key=lambda i: result.placements[i].end)
            last_binding = binding_out(cp, result.placements[crit].binding)
        eft = result.timing.order_eft[o.idx]
        if row["lateness"]:
            order_late[o.idx] = row["lateness"]
        order_results.append(
            OrderResult.fast(
                order_id=o.id,
                number=o.number,
                status=row["status"],
                start=cp.dt(row["start"]) if row["start"] is not None else None,
                end=cp.dt(row["end"]) if row["end"] is not None else None,
                due=cp.dt(o.due),
                lateness_minutes=row["lateness"] or 0,
                weight=round(o.weight, 3),
                earliest_possible_end=cp.dt(eft) if eft is not None else None,
                limiting=last_binding,
                material_status=row["material_status"],
                rules_applied=o.rules_applied,
            )
        )

    bns = [
        Bottleneck(
            resource_id=b.get("resource_id"),
            kind=b["kind"],
            rank=b["rank"],
            capacity_minutes=b.get("capacity_minutes", 0),
            scheduled_minutes=b.get("scheduled_minutes", 0),
            requirement_minutes=b.get("requirement_minutes", 0),
            overload_minutes=b.get("overload_minutes", 0),
            utilization=b.get("utilization", 0.0),
            induced_wait_minutes=b.get("induced_wait_minutes", 0),
            orders_affected=b.get("orders_affected", 0),
            causes=[BottleneckCause(**c) for c in b.get("causes", [])],
            ref=b.get("ref"),
        )
        for b in compute_bottlenecks(cp, result.placements, result.timing, result.unscheduled, order_late)
    ]

    explanations = {}
    if explain:
        for p in result.placements:
            if p is not None:
                explanations[cp.ops[p.op].id] = explain_operation(result, p.op)
        for i in result.unscheduled:
            explanations[cp.ops[i].id] = explain_operation(result, i)

    pegging: list[PegLink] = []
    for acc in result.ledger.accounts:
        if not acc.events:
            continue
        mat = cp.materials[acc.material]
        for s, c, q in fifo_pegging(acc):
            consumer = cp.op_index.get(c.ref)
            if consumer is None:
                continue
            kind = s.meta.get("kind", "SUPPLY")
            supply_order = s.meta.get("order")
            pegging.append(
                PegLink.fast(
                    material_id=mat.id,
                    supply_id=s.ref,
                    supply_kind=kind,
                    supply_ref=s.meta.get("ref"),
                    supply_order_id=cp.orders[supply_order].id if supply_order is not None else None,
                    supply_time=cp.dt(max(s.time, cp.lo)),
                    consumer_op_id=c.ref,
                    consumer_order_id=cp.orders[cp.ops[consumer].order].id,
                    need_time=cp.dt(c.time),
                    quantity=round(q, 6),
                )
            )

    meta.input_hash = cp.input_hash
    sol = Solution.fast(
        scenario_id=cp.problem.scenario_id,
        schedule=schedule,
        unscheduled=unscheduled,
        violations=violations,
        feasible=validation.feasible,
        orders=order_results,
        kpis=kpis,
        kpi_details=details,
        bottlenecks=bns,
        explanations=explanations,
        pegging=pegging,
        solver_metadata=meta,
    )
    sol._state = result  # in-process only (read models for the platform), never serialised
    return sol
