"""Critical rescheduling tests (spec §196) and manual moves with impact preview."""

from datetime import datetime, timedelta

from factory import T0, at, machine, operation, order, problem, two_shift_calendar

from monxuplan_engine import solve
from monxuplan_engine.changes import apply_changes
from monxuplan_engine.contract import Problem
from monxuplan_engine.repair import move, repair


def _cnc_problem(n_orders: int = 100, **solver):
    cal = two_shift_calendar()
    resources = [machine(r, "CAL-2S", groups=["CNC"]) for r in ("CNC-01", "CNC-02", "CNC-03")]
    orders, ops = [], []
    for k in range(n_orders):
        oid = f"WO-{1000 + k}"
        orders.append(order(oid, 24 * (1 + k // 8), item=f"P{k % 5}", family="AB"[k % 2]))
        # CNC-03 preferred, CNC-01/02 alternatives (slower)
        op = operation(f"{oid}/10", oid, 10, ["CNC-03", "CNC-01", "CNC-02"], 90, setup=15)
        op["modes"][1]["speed_factor"] = 0.85
        op["modes"][2]["speed_factor"] = 0.85
        ops.append(op)
    return problem(
        calendars=[cal],
        resources=resources,
        orders=orders,
        operations=ops,
        solver={"provider": "heuristic", "time_limit_s": 20, "explain": False, **solver},
        horizon={"start": T0.isoformat(), "end": (T0 + timedelta(days=21)).isoformat()},
    )


def _with_baseline(p: Problem, sol) -> Problem:
    data = p.model_dump(mode="json", by_alias=True)
    data["baseline"] = [
        {"op_id": s.op_id, "resource_id": s.resource_id, "start": s.start.isoformat(), "end": s.end.isoformat(), "setup_start": s.setup_start.isoformat()}
        for s in sol.schedule
    ]
    return Problem.model_validate(data)


def test_cnc03_breakdown_is_repaired_and_compared():
    base_p = _cnc_problem()
    base = solve(base_p)
    assert base.feasible
    on_cnc03_early = [s for s in base.schedule if s.resource_id == "CNC-03" and s.setup_start < datetime.fromisoformat(at(10)) and s.end > datetime.fromisoformat(at(2))]
    assert on_cnc03_early, "CNC-03 should be loaded at the breakdown time"
    p = _with_baseline(base_p, base)
    p, log = apply_changes(p, [{"type": "ADD_DOWNTIME", "payload": {"resource_id": "CNC-03", "start": at(2), "end": at(10), "kind": "BREAKDOWN", "reason": "Spindle failure"}}])
    assert "CNC-03" in log[0]
    out = repair(p, scope="REGIONAL", baseline_solution=base)
    # 1. affected operations detected
    assert set(s.op_id for s in on_cnc03_early) <= set(out.affected_ops)
    assert out.affected_orders
    sol = out.solution
    # 2. new plan is feasible: nothing runs on CNC-03 during the breakdown
    assert sol.feasible, [v.message for v in sol.violations if v.hardness == "HARD"][:5]
    b0, b1 = datetime.fromisoformat(at(2)), datetime.fromisoformat(at(10))
    for s in sol.schedule:
        if s.resource_id == "CNC-03":
            # the interval may only span the breakdown by pausing (never working inside it)
            assert not (s.setup_start >= b0 and s.end <= b1)
    # 3. alternatives used and comparison against baseline available
    cmp = out.comparison
    assert cmp is not None
    assert cmp["operations_moved"] > 0
    assert any(rc["from"] == "CNC-03" for rc in cmp["resource_changes"]) or cmp["orders_delayed"] > 0
    assert "otif" in cmp["kpis"]
    # 4. operations far away from the breakdown keep their position (local repair, stability)
    kept = [s for s in sol.schedule if s.op_id not in out.freed_ops]
    base_by = {s.op_id: s for s in base.schedule}
    assert all(base_by[s.op_id].start == s.start and base_by[s.op_id].resource_id == s.resource_id for s in kept)


def test_global_reschedule_after_breakdown():
    base_p = _cnc_problem(n_orders=40)
    base = solve(base_p)
    p = _with_baseline(base_p, base)
    p, _ = apply_changes(p, [{"type": "ADD_DOWNTIME", "payload": {"resource_id": "CNC-03", "start": at(2), "end": at(10)}}])
    out = repair(p, scope="GLOBAL", baseline_solution=base)
    assert out.solution.feasible
    assert out.comparison["kpis"]["late_orders"]["after"] is not None


def test_manual_move_preview_downstream_and_no_replan():
    p0 = _cnc_problem(n_orders=12)
    base = solve(p0)
    p = _with_baseline(p0, base)
    first = min((s for s in base.schedule if s.resource_id == "CNC-03"), key=lambda s: s.setup_start)
    target = datetime.fromisoformat(at(4))
    # NO_REPLAN: overlapping is reported, not fixed
    busy = [s for s in base.schedule if s.resource_id == "CNC-03" and s.setup_start <= target < s.end and s.op_id != first.op_id]
    out = move(p, first.op_id, "CNC-03", target, replan="NO_REPLAN", baseline_solution=base)
    moved = next(s for s in out.solution.schedule if s.op_id == first.op_id)
    assert moved.setup_start == target
    if busy:
        assert not out.solution.feasible
        assert any(v.type == "CAPACITY_OVERLAP" for v in out.solution.violations)
    # DOWNSTREAM: the rest is pushed right, plan stays feasible
    out2 = move(p, first.op_id, "CNC-03", target, replan="DOWNSTREAM", baseline_solution=base)
    assert out2.solution.feasible
    assert out2.comparison["operations_moved"] >= 1
    assert "otif" in out2.comparison["kpis"]


def test_rush_order_what_if():
    p0 = _cnc_problem(n_orders=30)
    base = solve(p0)
    p = _with_baseline(p0, base)
    rush = {
        "order": {"id": "RUSH-1", "number": "RUSH-1", "item_id": "P9", "quantity": 1500, "due": at(30), "priority": 10, "expedite": True},
        "operations": [operation("RUSH-1/10", "RUSH-1", 10, ["CNC-03", "CNC-01"], 600, setup=20, qty=1500)],
    }
    p, _ = apply_changes(p, [{"type": "ADD_ORDER", "payload": rush}])
    out = repair(p, scope="REGIONAL", baseline_solution=base)
    assert "RUSH-1/10" in out.affected_ops
    sched = {s.op_id: s for s in out.solution.schedule}
    assert "RUSH-1/10" in sched
    assert out.comparison is not None


def test_rush_order_competes_by_urgency_and_reports_impact():
    p0 = _cnc_problem(n_orders=30)
    base = solve(p0)
    p = _with_baseline(p0, base)
    rush = {
        "order": {"id": "RUSH-2", "number": "RUSH-2", "item_id": "P9", "quantity": 100, "due": at(12), "priority": 10, "expedite": True},
        "operations": [operation("RUSH-2/10", "RUSH-2", 10, ["CNC-03", "CNC-01", "CNC-02"], 240, setup=20, qty=100)],
    }
    p, _ = apply_changes(p, [{"type": "ADD_ORDER", "payload": rush}])
    out = repair(p, scope="REGIONAL", baseline_solution=base)
    r = next(o for o in out.solution.orders if o.order_id == "RUSH-2")
    assert r.status == "ON_TIME"
    # the insertion has a visible cost on other orders (shifted work), reported in the comparison
    assert out.comparison["operations_moved"] > 0
