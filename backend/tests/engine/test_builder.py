"""Schedule builder, validator and explanation tests (heuristic provider, no optimisation)."""

from datetime import datetime

from factory import T0, at, machine, operation, order, problem, two_shift_calendar

from monxuplan_engine import solve


def by_op(sol):
    return {s.op_id: s for s in sol.schedule}


def minutes(dt: datetime) -> int:
    return int((dt - T0).total_seconds() // 60)


def test_precedence_and_alternative_resources():
    p = problem(
        resources=[machine("M1"), machine("M2")],
        orders=[order("O1", 10), order("O2", 5)],
        operations=[
            operation("O1/10", "O1", 10, ["M1", "M2"], 120, setup=10),
            operation("O1/20", "O1", 20, ["M2"], 60),
            operation("O2/10", "O2", 10, ["M1"], 180),
        ],
    )
    sol = solve(p)
    s = by_op(sol)
    assert sol.feasible
    assert s["O1/20"].setup_start >= s["O1/10"].end
    # O2 is more urgent and only fits M1, so O1/10 goes to the alternative M2
    assert s["O1/10"].resource_id == "M2"
    assert s["O1/20"].binding.type == "PREDECESSOR"


def test_maintenance_window_is_avoided_and_explained():
    maint = {"id": "MT1", "start": at(8), "end": at(12), "kind": "MAINTENANCE_PREVENTIVE", "reason": "Spindle check"}
    p = problem(
        resources=[machine("CNC-01", unavailability=[maint]), machine("CNC-02")],
        orders=[order("O1", 48), order("O2", 48)],
        operations=[
            operation("O1/10", "O1", 10, ["CNC-01", "CNC-02"], 600),
            operation("O2/10", "O2", 10, ["CNC-01", "CNC-02"], 600),
        ],
    )
    sol = solve(p)
    for s in sol.schedule:
        if s.resource_id == "CNC-01":
            # interval may span the maintenance only by pausing over it
            work = (s.end - s.setup_start).total_seconds() / 60
            if s.setup_start < datetime.fromisoformat(at(12)) and s.end > datetime.fromisoformat(at(8)):
                assert work >= 600 + 240
    assert sol.feasible
    # the second job, if evaluated on CNC-01, must mention the maintenance or the busy machine
    ex = [e for e in sol.explanations.values() if any(a.resource_id == "CNC-01" and not a.chosen for a in e.alternatives)]
    if ex:
        codes = {r.code for a in ex[0].alternatives for r in a.blocking}
        assert codes & {"MAINTENANCE", "RESOURCE_BUSY"}


def test_calendar_pauses_over_weekend_and_non_interruptible_waits():
    cal = two_shift_calendar()
    p = problem(
        calendars=[cal],
        resources=[machine("M1", "CAL-2S"), machine("OVEN", "CAL-2S")],
        orders=[order("O1", 24 * 10), order("O2", 24 * 10)],
        operations=[
            # Friday 20:00 start: 4 h of work -> 2 h Friday, 2 h Monday
            operation("O1/10", "O1", 10, ["M1"], 240, earliest_start=at(24 * 4 + 14)),
            # non-interruptible 10 h cycle can never fit a 16 h day after 14:00 -> waits for next day
            operation("O2/10", "O2", 10, ["OVEN"], 600, interruptible=False, earliest_start=at(9)),
        ],
    )
    sol = solve(p)
    s = by_op(sol)
    assert s["O1/10"].setup_start.isoformat() == at(24 * 4 + 14)
    assert s["O1/10"].end.isoformat() == at(24 * 7 + 2)  # Monday 08:00
    assert s["O2/10"].setup_start.isoformat() == at(24)  # next day 06:00
    assert sol.feasible


def test_machine_operator_tool_material_must_coincide():
    """Test 198: machine + operator + tool + material all required at the same time."""
    base = dict(
        resources=[
            machine("M1"),
            {"id": "POOL-CNC", "code": "SKILL-CNC", "kind": "LABOR_POOL", "capacity": 1},
            {"id": "T18", "code": "TOOL-18", "kind": "TOOL", "capacity": 0},
        ],
        materials=[{"id": "MAT-4", "code": "MAT-004", "supplies": [{"id": "OH", "quantity": 10, "kind": "ON_HAND"}]}],
        orders=[order("O1", 48)],
    )
    op = operation("O1/10", "O1", 10, ["M1"], 60, materials=[{"material_id": "MAT-4", "quantity": 5}])
    op["modes"][0]["secondary"] = [{"resource_id": "POOL-CNC", "units": 1}, {"resource_id": "T18", "units": 1}]
    sol = solve(problem(operations=[op], **base))
    assert not sol.schedule
    assert sol.unscheduled[0].reason == "NO_FEASIBLE_SLOT"
    assert "TOOL-18" in sol.unscheduled[0].message
    assert not sol.feasible
    # with the tool available everything coincides
    base["resources"][2]["capacity"] = 1
    sol = solve(problem(operations=[op], **base))
    assert sol.feasible and len(sol.schedule) == 1
    codes = {r.code for r in sol.explanations["O1/10"].reasons}
    assert {"LABOR_AVAILABLE", "TOOL_AVAILABLE", "MATERIAL_READY"} <= codes


def test_shared_tool_prevents_parallel_use():
    tool = {"id": "T1", "code": "T1", "kind": "TOOL", "capacity": 1}
    ops = []
    for k, m in enumerate(["M1", "M2"]):
        op = operation(f"O{k}/10", f"O{k}", 10, [m], 120)
        op["modes"][0]["secondary"] = [{"resource_id": "T1"}]
        ops.append(op)
    sol = solve(problem(resources=[machine("M1"), machine("M2"), tool], orders=[order("O0", 48), order("O1", 48)], operations=ops))
    a, b = sorted(sol.schedule, key=lambda s: s.setup_start)
    assert b.setup_start >= a.end
    assert b.binding.type == "TOOL"
    assert sol.feasible


def test_material_shortage_is_detected_not_invented():
    """Test 197: 100 units available, 300 required → only one order can run; nothing is invented."""
    mats = [{"id": "MAT-A", "code": "MAT-A", "supplies": [{"id": "OH", "quantity": 100, "kind": "ON_HAND"}]}]
    orders = [order(f"O{k}", 48 + k) for k in range(3)]
    ops = [operation(f"O{k}/10", f"O{k}", 10, ["M1"], 60, materials=[{"material_id": "MAT-A", "quantity": 100}]) for k in range(3)]
    sol = solve(problem(resources=[machine("M1")], materials=mats, orders=orders, operations=ops))
    assert len(sol.schedule) == 1
    assert len(sol.unscheduled) == 2
    assert all(u.reason == "MATERIAL_SHORTAGE" for u in sol.unscheduled)
    d = sol.unscheduled[0].details["materials"][0]
    assert d["required"] == 100 and d["available"] == 0
    assert sol.kpis["material_shortages"] == 1
    assert not any(v.type == "MATERIAL_SHORTAGE" for v in sol.violations)  # ledger never negative
    assert sum(pg.quantity for pg in sol.pegging) == 100


def test_material_shortage_relaxed_mode_reports_violation():
    mats = [{"id": "MAT-A", "code": "MAT-A", "supplies": [{"id": "OH", "quantity": 100, "kind": "ON_HAND"}]}]
    sol = solve(
        problem(
            resources=[machine("M1")],
            materials=mats,
            orders=[order("O1", 48)],
            operations=[operation("O1/10", "O1", 10, ["M1"], 60, materials=[{"material_id": "MAT-A", "quantity": 300}])],
            constraints={"materials": "ALLOW_SHORTAGE"},
        )
    )
    assert len(sol.schedule) == 1
    v = [v for v in sol.violations if v.type == "MATERIAL_SHORTAGE"]
    assert v and v[0].details["shortfall"] == 200
    assert not sol.feasible


def test_purchase_receipt_delays_operation():
    mats = [{"id": "RAW", "code": "RAW-1", "supplies": [{"id": "PO-1", "quantity": 50, "kind": "PURCHASE", "time": at(30), "ref": "PO-1", "supplier_id": "SUP-9"}]}]
    sol = solve(
        problem(
            resources=[machine("M1")],
            materials=mats,
            orders=[order("O1", 20)],
            operations=[operation("O1/10", "O1", 10, ["M1"], 60, materials=[{"material_id": "RAW", "quantity": 50}])],
        )
    )
    s = sol.schedule[0]
    assert s.setup_start.isoformat() == at(30)
    assert s.binding.type == "MATERIAL"
    assert sol.orders[0].status == "LATE"
    assert sol.orders[0].material_status == "LATE_SUPPLY"
    assert sol.kpi_details["late_orders"][0]["cause"]["category"] == "Material"


def test_make_item_synchronisation_parent_after_child():
    mats = [{"id": "SUB", "code": "SUB-1", "make_or_buy": "MAKE"}]
    orders = [
        order("PARENT", 72, item="FG"),
        order("CHILD", 60, item="SUB", produces_material_id="SUB", qty=10),
    ]
    ops = [
        operation("PARENT/10", "PARENT", 10, ["ASM"], 60, materials=[{"material_id": "SUB", "quantity": 10}]),
        operation("CHILD/10", "CHILD", 10, ["M1"], 300, qty=10),
    ]
    sol = solve(problem(resources=[machine("M1"), machine("ASM")], materials=mats, orders=orders, operations=ops))
    s = by_op(sol)
    assert s["PARENT/10"].setup_start >= s["CHILD/10"].end
    assert sol.feasible
    peg = [pg for pg in sol.pegging if pg.consumer_op_id == "PARENT/10"]
    assert peg and peg[0].supply_order_id == "CHILD"


def test_overlap_transfer_batch():
    sol = solve(
        problem(
            resources=[machine("M1"), machine("M2")],
            orders=[order("O1", 48, qty=100)],
            operations=[
                operation("O1/10", "O1", 10, ["M1"], 600, qty=100, transfer_batch=20),
                operation("O1/20", "O1", 20, ["M2"], 300, qty=100),
            ],
        )
    )
    s = by_op(sol)
    assert s["O1/20"].setup_start < s["O1/10"].end  # overlapping
    assert s["O1/20"].setup_start >= s["O1/10"].start  # but not before the first batch
    assert s["O1/20"].end >= s["O1/10"].end
    assert sol.feasible


def test_sequence_dependent_setup_groups_families():
    matrix = {"id": "AB", "attribute": "item", "same_minutes": 5, "default_minutes": 60}
    orders, ops = [], []
    items = ["A", "B", "A", "B", "A", "B"]
    for k, it in enumerate(items):
        orders.append(order(f"O{k}", 200, item=it, family=it))
        ops.append(operation(f"O{k}/10", f"O{k}", 10, ["M1"], 60))
    p = problem(
        resources=[machine("M1", setup_matrix_ids=["AB"], initial_state={"item": "A"})],
        setup_matrices=[matrix],
        orders=orders,
        operations=ops,
        solver={"dispatch_rules": ["HYBRID_APS"]},
    )
    sol = solve(p)
    total_setup = sum(s.setup_minutes for s in sol.schedule)
    assert total_setup <= 5 * 5 + 60  # at most one changeover A -> B (six jobs)
    assert sol.feasible


def test_frozen_operations_are_kept_and_block_new_work():
    p = problem(
        resources=[machine("M1")],
        horizon={"frozen_until": at(24), "start": T0.isoformat(), "end": at(24 * 14)},
        orders=[order("O1", 100), order("O2", 5, priority=10)],
        operations=[
            operation("O1/10", "O1", 10, ["M1"], 120, fixed={"resource_id": "M1", "start": at(2), "reason": "FROZEN"}),
            operation("O2/10", "O2", 10, ["M1"], 60),
        ],
    )
    sol = solve(p)
    s = by_op(sol)
    assert s["O1/10"].setup_start.isoformat() == at(2) and s["O1/10"].fixed
    assert s["O2/10"].setup_start.isoformat() >= at(24)
    assert s["O2/10"].binding.type == "FROZEN_ZONE"


def test_planning_rule_prefers_resource_and_is_traced():
    rule = {
        "id": "R001",
        "name": "Family A on CNC-03",
        "condition": {"all": [{"field": "order.family", "op": "eq", "value": "A"}, {"field": "op.resource_groups", "op": "contains", "value": "CNC"}]},
        "actions": [{"type": "PREFER_RESOURCE", "resource": "CNC-03"}],
    }
    res = [machine("CNC-01", groups=["CNC"]), machine("CNC-03", groups=["CNC"])]
    p = problem(
        resources=res,
        rules=[rule],
        orders=[order("O1", 48, family="A")],
        operations=[operation("O1/10", "O1", 10, ["CNC-01", "CNC-03"], 60)],
        solver={"mode_selection": {"strategy": "PREFERRED"}},
    )
    sol = solve(p)
    assert sol.schedule[0].resource_id == "CNC-03"
    assert "R001" in sol.explanations["O1/10"].rules_applied


def test_forbidden_transition_rule():
    sc = {"id": "S1", "type": "NOT_IMMEDIATELY_AFTER", "prev_match": {"family": "D"}, "next_match": {"family": "C"}}
    orders = [order("OD", 10, family="D", item="D"), order("OC", 11, family="C", item="C"), order("OE", 50, family="E", item="E")]
    ops = [operation(f"{o['id']}/10", o["id"], 10, ["M1"], 60) for o in orders]
    sol = solve(problem(resources=[machine("M1")], orders=orders, operations=ops, sequence_constraints=[sc]))
    seq = [s.op_id for s in sorted(sol.schedule, key=lambda s: s.setup_start)]
    i = seq.index("OD/10")
    assert i == len(seq) - 1 or seq[i + 1] != "OC/10"
    assert sol.feasible


def test_deadline_impossible_reports_earliest_achievable():
    sol = solve(problem(resources=[machine("M1")], orders=[order("O1", 2)], operations=[operation("O1/10", "O1", 10, ["M1"], 300)]))
    o = sol.orders[0]
    assert o.status == "LATE"
    assert o.earliest_possible_end.isoformat() == at(5)
    assert sol.kpi_details["late_orders"][0]["cause"]["code"] == "DUE_DATE_IMPOSSIBLE"


def test_operation_without_resource_is_reported():
    sol = solve(problem(resources=[machine("M1")], orders=[order("O1", 10)], operations=[operation("O1/10", "O1", 10, ["NOPE"], 60)]))
    assert sol.unscheduled[0].reason == "NO_COMPATIBLE_RESOURCE"
    types = {v.type for v in sol.violations}
    assert "DATA_UNKNOWN_RESOURCE" in types and "DATA_OPERATION_WITHOUT_RESOURCE" in types


def test_validator_flags_conflicting_fixed_operations():
    p = problem(
        resources=[machine("M1")],
        orders=[order("O1", 48), order("O2", 48)],
        operations=[
            operation("O1/10", "O1", 10, ["M1"], 120, fixed={"resource_id": "M1", "start": at(1), "reason": "MANUAL"}),
            operation("O2/10", "O2", 10, ["M1"], 120, fixed={"resource_id": "M1", "start": at(2), "reason": "MANUAL"}),
        ],
    )
    sol = solve(p)
    assert any(v.type == "CAPACITY_OVERLAP" for v in sol.violations)
    assert not sol.feasible


def test_deterministic_output():
    orders = [order(f"O{k}", 20 + 3 * (k % 7), item="AB"[k % 2], family="AB"[k % 2]) for k in range(20)]
    ops = []
    for k in range(20):
        ops.append(operation(f"O{k}/10", f"O{k}", 10, ["M1", "M2"], 60 + 7 * k))
        ops.append(operation(f"O{k}/20", f"O{k}", 20, ["M3"], 30 + 5 * (k % 4)))
    kw = dict(resources=[machine("M1"), machine("M2"), machine("M3")], orders=orders, operations=ops, solver={"local_search": True, "multi_start": True, "time_limit_s": 30})
    a = solve(problem(**kw))
    b = solve(problem(**kw))
    assert [(s.op_id, s.resource_id, s.start) for s in a.schedule] == [(s.op_id, s.resource_id, s.start) for s in b.schedule]
    assert a.solver_metadata.input_hash == b.solver_metadata.input_hash


def test_long_operation_with_shift_labour_pauses_over_nights_and_breaks():
    """Regression: labour pools with shift calendars (capacity 0 at night and in breaks) must not block
    multi-day operations — the operation pauses there and only consumes labour while working."""
    cal = {"id": "C", "timezone": "UTC", "shifts": [{"weekday": d, "start": "06:00", "end": "14:00", "breaks": [{"start": "10:00", "end": "10:20"}]} for d in range(5)]}
    pool = {"id": "POOL", "code": "POOL", "kind": "LABOR_POOL", "capacity": 1, "calendar_id": "C"}
    tool = {"id": "T", "code": "T", "kind": "TOOL", "capacity": 1}
    ops = []
    for k in range(3):
        op = operation(f"O{k}/10", f"O{k}", 10, ["M1", "M2"], 1500)  # 25 h each → spans several shifts
        for m in op["modes"]:
            m["secondary"] = [{"resource_id": "POOL"}, {"resource_id": "T"}]
        ops.append(op)
    sol = solve(problem(calendars=[cal], resources=[machine("M1", "C"), machine("M2", "C"), pool, tool], orders=[order(f"O{k}", 24 * 30) for k in range(3)], operations=ops))
    assert not sol.unscheduled, [u.message for u in sol.unscheduled]
    assert sol.feasible, [v.message for v in sol.violations if v.hardness == "HARD"]
    a = sorted(sol.schedule, key=lambda s: s.setup_start)
    for x, y in zip(a, a[1:], strict=False):
        assert y.setup_start >= x.end  # one operator and one tool: strictly sequential
