"""Known industrial cases with hand-checked answers (A: breakdown with frozen and locked work,
B: material shortage quantities, exact quantities at large magnitudes)."""

from datetime import datetime, timedelta

from factory import T0, at, machine, operation, order, problem, two_shift_calendar

from monxuplan_engine import solve
from monxuplan_engine.changes import apply_changes
from monxuplan_engine.contract import Problem
from monxuplan_engine.repair import move, repair


def _base():
    cal = two_shift_calendar()
    res = [machine(r, "CAL-2S") for r in ("A", "B")]
    orders, ops = [], []
    for k in range(16):
        oid = f"O{k}"
        orders.append(order(oid, 24 * (1 + k // 4)))
        ops.append(operation(f"{oid}/10", oid, 10, ["A", "B"], 120, setup=10))
    return problem(calendars=[cal], resources=res, orders=orders, operations=ops, horizon={"start": T0.isoformat(), "end": (T0 + timedelta(days=10)).isoformat(), "frozen_until": at(3)})


def _with_baseline(p: Problem, sol, lock: set[str] = frozenset()) -> Problem:
    data = p.model_dump(mode="json", by_alias=True)
    data["baseline"] = [{"op_id": s.op_id, "resource_id": s.resource_id, "start": s.start.isoformat(), "end": s.end.isoformat(), "setup_start": s.setup_start.isoformat()} for s in sol.schedule]
    by = {s.op_id: s for s in sol.schedule}
    for op in data["operations"]:
        if op["id"] in lock:
            s = by[op["id"]]
            op["fixed"] = {"resource_id": s.resource_id, "start": s.setup_start.isoformat(), "end": s.end.isoformat(), "reason": "LOCKED", "setup_minutes": s.setup_minutes}
    return Problem.model_validate(data)


def test_case_a_breakdown_keeps_frozen_and_locked_work():
    p0 = _base()
    base = solve(p0)
    on_a = sorted((s for s in base.schedule if s.resource_id == "A"), key=lambda s: s.setup_start)
    frozen = [s for s in base.schedule if s.setup_start < datetime.fromisoformat(at(3))]
    locked = on_a[-1].op_id  # a job after the breakdown, locked by the planner
    p = _with_baseline(p0, base, {locked})
    p, _ = apply_changes(p, [{"type": "ADD_DOWNTIME", "payload": {"resource_id": "A", "start": at(4), "end": at(12), "kind": "BREAKDOWN"}}])
    out = repair(p, scope="REGIONAL", baseline_solution=base)
    sol = out.solution
    hard = [v for v in sol.violations if v.hardness == "HARD" and v.type not in ("UNSCHEDULED",) and not v.type.startswith("DATA_")]
    assert not hard, [(v.type, v.message) for v in hard]
    new = {s.op_id: s for s in sol.schedule}
    for s in frozen:  # the frozen zone does not move
        assert new[s.op_id].setup_start == s.setup_start and new[s.op_id].resource_id == s.resource_id
    # the locked job keeps its position and stays locked (not re-labelled as "kept")
    b = next(s for s in base.schedule if s.op_id == locked)
    assert new[locked].setup_start == b.setup_start and new[locked].fixed_reason == "LOCKED"
    # only affected operations and their followers were freed, all explained by the comparison
    assert set(out.freed_ops) <= {s.op_id for s in base.schedule}
    assert out.comparison and "otif" in out.comparison["kpis"]
    # left-in-place operations are not reported as planner-fixed
    assert not any(s.fixed_reason == "KEPT" for s in sol.schedule)


def test_locked_operation_survives_a_manual_move_of_another():
    p0 = _base()
    base = solve(p0)
    later = sorted((s for s in base.schedule if s.setup_start > datetime.fromisoformat(at(5))), key=lambda s: s.setup_start)
    locked, moved = later[-1], later[0]
    p = _with_baseline(p0, base, {locked.op_id})
    out = move(p, moved.op_id, moved.resource_id, moved.start + timedelta(hours=2), replan="DOWNSTREAM", baseline_solution=base)
    new = {s.op_id: s for s in out.solution.schedule}
    assert new[locked.op_id].setup_start == locked.setup_start and new[locked.op_id].fixed_reason == "LOCKED"


def test_case_b_shortage_is_exactly_the_missing_quantity():
    """Available 100, required 300 (3 × 100): one operation runs, the shortage is 200, nothing invented."""
    mats = [{"id": "MAT", "code": "MAT", "supplies": [{"id": "OH", "quantity": 100, "kind": "ON_HAND"}]}]
    ops = [operation(f"O{k}/10", f"O{k}", 10, ["M1"], 60, materials=[{"material_id": "MAT", "quantity": 100}]) for k in range(3)]
    sol = solve(problem(resources=[machine("M1")], materials=mats, orders=[order(f"O{k}", 48 + k) for k in range(3)], operations=ops))
    short = sum(d["required"] - d["available"] for u in sol.unscheduled for d in u.details["materials"])
    assert len(sol.schedule) == 1 and short == 200
    assert sum(pg.quantity for pg in sol.pegging) == 100  # the only real stock, pegged once
    assert {pg.consumer_op_id for pg in sol.pegging} == {sol.schedule[0].op_id}


def test_exact_quantities_at_large_magnitudes():
    """0.1 on hand + 100 000 000 received, then 100 000 000 and 0.1 consumed: the stock is exactly
    enough. In floating point 0.1 + 1e8 − 1e8 − 0.1 = −6·10⁻⁹, which the former absolute tolerance
    (10⁻⁹) turned into a false shortage of the second job. 0.100001 is really short."""
    for last, ok in ((0.1, True), (0.100001, False)):
        mats = [{"id": "MAT", "code": "MAT", "supplies": [{"id": "OH", "quantity": 0.1, "kind": "ON_HAND"}, {"id": "PO", "quantity": 1e8, "kind": "PURCHASE", "time": at(1)}]}]
        ops = [
            operation("O1/10", "O1", 10, ["M1"], 60, materials=[{"material_id": "MAT", "quantity": 1e8}]),
            operation("O2/10", "O2", 10, ["M1"], 60, materials=[{"material_id": "MAT", "quantity": last}]),
        ]
        sol = solve(problem(resources=[machine("M1")], materials=mats, orders=[order("O1", 24), order("O2", 48)], operations=ops))
        assert (len(sol.schedule) == 2) is ok, last
        assert not [v for v in sol.violations if v.type == "MATERIAL_SHORTAGE"]
