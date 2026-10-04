"""Optimisation tests on problems with known answers (spec §120/§121/§199)."""

from datetime import date

import pytest
from factory import T0, at, machine, operation, order, problem

from monxuplan_engine import solve
from monxuplan_engine.compile import compile_problem
from monxuplan_engine.feasibility import check
from monxuplan_engine.mrp import MrpProblem, run_mrp
from monxuplan_engine.providers import get_provider
from monxuplan_engine.providers.mip import AggregateProblem, MipProvider, NotSupported
from monxuplan_engine.simulation import monte_carlo


def _ab_problem(provider: str, time_limit: float = 10):
    matrix = {"id": "AB", "attribute": "item", "same_minutes": 5, "default_minutes": 60}
    orders, ops = [], []
    items = "ABABABABAB"
    for k, it in enumerate(items):
        orders.append(order(f"O{k}", 30 + 2 * k, item=it, family=it))
        ops.append(operation(f"O{k}/10", f"O{k}", 10, ["M1"], 90))
    return problem(
        resources=[machine("M1", setup_matrix_ids=["AB"], initial_state={"item": "A"})],
        setup_matrices=[matrix],
        orders=orders,
        operations=ops,
        solver={"provider": provider, "time_limit_s": time_limit, "local_search": True, "multi_start": True},
    )


@pytest.mark.parametrize("provider", ["heuristic", "cpsat", "hybrid"])
def test_setup_matrix_ab_campaigns_without_violating_due_dates(provider):
    """§199: A→A 5, A→B 60, B→A 60, B→B 5 — the optimiser groups A and B without late orders."""
    sol = solve(_ab_problem(provider))
    assert sol.feasible
    assert sol.kpis["late_orders"] == 0
    total = sum(s.setup_minutes for s in sol.schedule)
    assert total == 5 * 9 + 60  # one changeover only: the minimum
    if provider != "heuristic":
        assert sol.solver_metadata.status in ("OPTIMAL", "FEASIBLE")
        assert sol.solver_metadata.details.get("cp_status") in ("OPTIMAL", "FEASIBLE")


def test_job1_before_job2_when_job2_due_later():
    """Problem A: two jobs on one machine; job 2 can wait, job 1 cannot → job 1 first."""
    p = problem(
        resources=[machine("M1")],
        orders=[order("J1", 3), order("J2", 20)],
        operations=[operation("J1/10", "J1", 10, ["M1"], 150), operation("J2/10", "J2", 10, ["M1"], 150)],
        solver={"provider": "cpsat", "time_limit_s": 5, "reproducible": True},
    )
    sol = solve(p)
    s = {x.op_id: x for x in sol.schedule}
    assert s["J1/10"].end <= s["J2/10"].setup_start
    assert sol.kpis["late_orders"] == 0
    assert sol.solver_metadata.status == "OPTIMAL"
    assert sol.solver_metadata.proven_optimal


def test_cpsat_uses_alternative_to_meet_due_dates():
    p = problem(
        resources=[machine("CNC-03"), machine("CNC-02")],
        orders=[order(f"O{k}", 4) for k in range(3)],
        operations=[operation(f"O{k}/10", f"O{k}", 10, ["CNC-03", "CNC-02"], 120) for k in range(3)],
        solver={"provider": "cpsat", "time_limit_s": 5},
    )
    sol = solve(p)
    used = {s.resource_id for s in sol.schedule}
    assert used == {"CNC-03", "CNC-02"}
    ex = sol.explanations[sol.schedule[0].op_id]
    assert ex.alternatives and any(a.chosen for a in ex.alternatives)


def test_cpsat_respects_calendar_and_material_reservoir():
    cal = {"id": "C", "timezone": "UTC", "shifts": [{"weekday": d, "start": "06:00", "end": "14:00"} for d in range(5)]}
    mats = [{"id": "RAW", "code": "RAW", "supplies": [{"id": "OH", "quantity": 10, "kind": "ON_HAND"}, {"id": "PO", "quantity": 10, "kind": "PURCHASE", "time": at(26)}]}]
    ops = [operation(f"O{k}/10", f"O{k}", 10, ["M1"], 300, materials=[{"material_id": "RAW", "quantity": 10}]) for k in range(2)]
    p = problem(
        calendars=[cal],
        resources=[machine("M1", "C")],
        materials=mats,
        orders=[order("O0", 48), order("O1", 60)],
        operations=ops,
        solver={"provider": "cpsat", "time_limit_s": 5},
    )
    sol = solve(p)
    assert sol.feasible
    starts = sorted(s.setup_start for s in sol.schedule)
    assert starts[1].isoformat() >= at(26)  # second job waits for the receipt
    assert not [v for v in sol.violations if v.type == "MATERIAL_SHORTAGE"]


def test_lexicographic_objective_prioritises_lateness_over_setup():
    p = _ab_problem("cpsat")
    data = p.model_dump(mode="json", by_alias=True)
    data["objectives"]["mode"] = "LEXICOGRAPHIC"
    data["objectives"]["levels"] = [["late_orders"], ["setup"]]
    from monxuplan_engine.contract import Problem

    sol = solve(Problem.model_validate(data))
    assert sol.kpis["late_orders"] == 0
    assert sum(s.setup_minutes for s in sol.schedule) == 105
    assert len(sol.solver_metadata.objective_vector) == 3


def test_time_limit_is_respected_and_status_is_honest():
    orders, ops = [], []
    for k in range(60):
        orders.append(order(f"O{k}", 24 + (k % 10) * 6, item="ABC"[k % 3], family="ABC"[k % 3]))
        ops.append(operation(f"O{k}/10", f"O{k}", 10, ["M1", "M2"], 60 + (k * 7) % 50))
        ops.append(operation(f"O{k}/20", f"O{k}", 20, ["M3"], 30 + (k * 5) % 40))
    matrix = {"id": "F", "attribute": "family", "same_minutes": 5, "default_minutes": 45}
    p = problem(
        resources=[machine(m, setup_matrix_ids=["F"]) for m in ("M1", "M2", "M3")],
        setup_matrices=[matrix],
        orders=orders,
        operations=ops,
        solver={"provider": "hybrid", "time_limit_s": 4, "cpsat_max_ops": 60},
    )
    sol = solve(p)
    assert sol.feasible
    md = sol.solver_metadata
    assert md.runtime_s < 4 + 6  # limit + explanation/assembly overhead
    assert md.status in ("FEASIBLE", "HEURISTIC")
    assert not md.proven_optimal


def test_feasibility_check_reports_capacity_shortage_and_impossible_dates():
    p = problem(
        resources=[machine("CNC-03")],
        orders=[order("O1", 2), order("O2", 3), order("O3", 4)],
        operations=[operation(f"O{k}/10", f"O{k}", 10, ["CNC-03"], 120) for k in (1, 2, 3)],
    )
    res = check(compile_problem(p))
    assert res["status"] in ("PARTIALLY", "NO")
    assert res["capacity_shortages"] and res["capacity_shortages"][0]["resources"] == ["CNC-03"]
    impossible = {x["order_id"] for x in res["impossible_orders"]}
    assert impossible == set()  # each alone fits: the problem is capacity, not lead time
    p2 = problem(resources=[machine("M1")], orders=[order("O1", 1)], operations=[operation("O1/10", "O1", 10, ["M1"], 120)])
    r2 = check(compile_problem(p2))
    assert r2["impossible_orders"][0]["earliest_achievable"] == at(2)


def test_mrp_explosion_netting_lot_sizing_and_cycle_detection():
    pb = MrpProblem(
        start=date(2026, 9, 28),
        bucket_days=7,
        periods=4,
        items=[
            {"id": "FG", "code": "FG", "on_hand": 10, "lead_time_days": 7},
            {"id": "SUB", "code": "SUB", "lead_time_days": 7, "lot_policy": "MULTIPLE", "multiple": 50},
            {"id": "RAW", "code": "RAW", "make_or_buy": "BUY", "on_hand": 100, "lead_time_days": 14},
        ],
        bom=[{"parent_id": "FG", "component_id": "SUB", "qty_per": 2}, {"parent_id": "SUB", "component_id": "RAW", "qty_per": 1}],
        demands=[
            {"item_id": "FG", "date": "2026-10-12", "quantity": 40, "type": "CUSTOMER_ORDER", "ref": "SO-1"},
            {"item_id": "FG", "date": "2026-10-12", "quantity": 30, "type": "FORECAST"},
        ],
    )
    out = run_mrp(pb)
    fg = next(r for r in out["items"] if r["item_id"] == "FG")
    assert fg["gross_requirements"][2] == 40  # forecast consumed by customer orders (MAX)
    po_fg = [p for p in out["planned_orders"] if p["item_id"] == "FG"]
    assert po_fg[0]["quantity"] == 30 and po_fg[0]["release_period"] == 1
    sub = [p for p in out["planned_orders"] if p["item_id"] == "SUB"]
    assert sub[0]["quantity"] == 100  # 60 needed, multiple of 50
    raw = next(r for r in out["items"] if r["item_id"] == "RAW")
    assert raw["dependent_demand"][0] == 100
    pb2 = MrpProblem(start=date(2026, 9, 28), items=[{"id": "X", "code": "X"}, {"id": "Y", "code": "Y"}], bom=[{"parent_id": "X", "component_id": "Y", "qty_per": 1}, {"parent_id": "Y", "component_id": "X", "qty_per": 1}])
    assert run_mrp(pb2)["bom_cycles"]


def test_mip_aggregate_planning_builds_ahead_of_capacity_peak():
    pb = AggregateProblem(
        periods=["W1", "W2", "W3"],
        families=[{"id": "A", "demand": [50, 50, 200], "hours_per_unit": {"CNC": 1.0}, "holding_cost": 1, "backlog_cost": 20}],
        groups=[{"id": "CNC", "capacity_hours": [100, 100, 100], "overtime_max_hours": [0, 0, 20], "overtime_cost_per_hour": 5}],
    )
    out = MipProvider().solve_aggregate(pb)
    assert out["status"] == "OPTIMAL"
    fam = out["families"][0]
    assert sum(fam["backlog"]) == pytest.approx(0, abs=1e-6)
    assert fam["production"][0] + fam["production"][1] >= 180 - 1e-6  # builds ahead
    with pytest.raises(NotSupported):
        get_provider("mip").solve(None, None, None)


def test_monte_carlo_gives_on_time_probabilities():
    p = problem(
        resources=[machine("M1")],
        orders=[order("O1", 3), order("O2", 100)],
        operations=[operation("O1/10", "O1", 10, ["M1"], 170), operation("O2/10", "O2", 10, ["M1"], 60)],
    )
    sol = solve(p)
    mc = monte_carlo(p, sol, runs=20, runtime_cv=0.2, breakdowns_per_week=0, seed=7)
    probs = {o["order_id"]: o["on_time_probability"] for o in mc["orders"]}
    assert probs["O2"] == 1.0
    assert 0.0 < probs["O1"] < 1.0  # tight order is at risk under variance
    assert mc["throughput_units"]["p50"] is not None
    assert T0  # factory import used


@pytest.mark.parametrize("provider", ["cpsat", "hybrid"])
def test_exact_providers_never_return_a_worse_plan_than_the_heuristic(provider):
    """CP-SAT and hybrid start from the heuristic (with its local search) and keep it when CP-SAT does
    not beat it: asking for the stronger solver must not give a worse plan (the 50-order benchmark
    instance used to come back 0.25 worse than the heuristic alone)."""
    import os
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "benchmarks"))
    from run_benchmarks import generate

    def objective(name: str) -> float:
        p = generate("M")
        p.solver.provider = name
        p.solver.time_limit_s = 4
        sol = solve(p)
        assert sol.feasible
        return sol.solver_metadata.objective

    assert objective(provider) <= objective("heuristic") + 1e-6
