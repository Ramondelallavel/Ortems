"""Randomised consistency tests: every schedule produced by any provider must pass the independent
PlanValidator (no HARD violation other than data issues and explicitly unscheduled operations)."""

import random
from datetime import timedelta

import pytest
from factory import T0, at, problem

from monxuplan_engine import solve

ALLOWED = {"UNSCHEDULED"}


def random_problem(seed: int, provider: str):
    rng = random.Random(seed)
    n_mach = rng.randint(2, 5)
    machines = [f"M{k}" for k in range(n_mach)]
    cals = [
        {"id": "C2", "timezone": "Europe/Madrid", "shifts": [{"weekday": d, "start": "06:00", "end": "22:00", "breaks": [{"start": "12:00", "end": "12:30"}]} for d in range(5)]},
        {"id": "C1", "timezone": "Europe/Madrid", "shifts": [{"weekday": d, "start": "06:00", "end": "14:00"} for d in range(5)] + [{"weekday": d, "start": "14:00", "end": "18:00", "kind": "OVERTIME"} for d in range(5)]},
    ]
    resources = []
    for m in machines:
        r = {"id": m, "code": m, "kind": "MACHINE", "calendar_id": rng.choice(["C2", "C1", None]), "groups": ["G"], "setup_matrix_ids": ["FAM"] if rng.random() < 0.6 else []}
        if rng.random() < 0.3:
            s = rng.randint(0, 72)
            r["unavailability"] = [{"start": at(s), "end": at(s + rng.randint(2, 10)), "kind": "MAINTENANCE_PLANNED"}]
        resources.append(r)
    resources.append({"id": "POOL", "code": "POOL", "kind": "LABOR_POOL", "capacity": rng.randint(1, 3), "calendar_id": "C2"})
    resources.append({"id": "TOOL", "code": "TOOL", "kind": "TOOL", "capacity": rng.randint(1, 2)})
    mats = [
        {"id": "RAW", "code": "RAW", "supplies": [{"id": "OH", "quantity": rng.randint(20, 60), "kind": "ON_HAND"}, {"id": "PO", "quantity": 100, "kind": "PURCHASE", "time": at(rng.randint(10, 60))}]},
        {"id": "SUB", "code": "SUB", "make_or_buy": "MAKE"},
    ]
    orders, ops, precs = [], [], []
    n_orders = rng.randint(8, 22)
    for k in range(n_orders):
        oid = f"O{k}"
        fam = rng.choice("ABC")
        o = {"id": oid, "number": oid, "item_id": f"I{fam}", "item_code": f"I{fam}", "family": fam, "quantity": rng.randint(5, 50), "due": at(rng.randint(8, 120)), "priority": rng.randint(1, 10)}
        if k < 2:
            o["produces_material_id"] = "SUB"
        orders.append(o)
        n_ops = rng.randint(1, 4)
        prev = None
        for j in range(n_ops):
            opid = f"{oid}/{10 * (j + 1)}"
            modes = []
            for m in rng.sample(machines, rng.randint(1, min(3, n_mach))):
                mode = {"resource_id": m, "preference": len(modes), "speed_factor": rng.choice([1.0, 0.9, 1.1])}
                if rng.random() < 0.3:
                    mode["secondary"] = [{"resource_id": "POOL"}]
                if rng.random() < 0.15:
                    mode.setdefault("secondary", []).append({"resource_id": "TOOL"})
                modes.append(mode)
            op = {
                "id": opid,
                "order_id": oid,
                "seq": 10 * (j + 1),
                "quantity": o["quantity"],
                "duration": {"setup_minutes": rng.choice([0, 10, 20]), "run_minutes_per_unit": rng.uniform(0.5, 6), "move_minutes": rng.choice([0, 0, 30])},
                "modes": modes,
                "interruptible": rng.random() > 0.1,
            }
            if j == 0 and rng.random() < 0.3:
                op["materials"] = [{"material_id": "RAW", "quantity": rng.randint(5, 25)}]
            if j == 0 and k >= 2 and rng.random() < 0.15:
                op.setdefault("materials", []).append({"material_id": "SUB", "quantity": 1})
            if rng.random() < 0.15:
                op["transfer_batch"] = max(1, o["quantity"] // 4)
            ops.append(op)
            if prev:
                precs.append({"pred": prev, "succ": opid})
            prev = opid
    matrix = {"id": "FAM", "attribute": "family", "same_minutes": 5, "default_minutes": 40, "entries": [{"from": "A", "to": "C", "minutes": 90}]}
    return problem(
        calendars=cals,
        resources=resources,
        materials=mats,
        orders=orders,
        operations=ops,
        precedences=precs,
        setup_matrices=[matrix],
        sequence_constraints=[{"id": "S", "type": "NOT_IMMEDIATELY_AFTER", "prev_match": {"family": "C"}, "next_match": {"family": "A"}}] if seed % 2 else [],
        constraints={"allow_overtime": bool(seed % 3 == 0)},
        solver={"provider": provider, "time_limit_s": 3, "local_search": True, "multi_start": True, "cpsat_max_ops": 40, "explain": seed % 4 == 0},
        horizon={"start": T0.isoformat(), "end": (T0 + timedelta(days=10)).isoformat(), "timezone": "Europe/Madrid"},
    )


@pytest.mark.parametrize("seed", range(12))
def test_heuristic_schedules_are_always_valid(seed):
    sol = solve(random_problem(seed, "heuristic"))
    hard = [v for v in sol.violations if v.hardness == "HARD" and not v.type.startswith("DATA_") and v.type not in ALLOWED]
    assert not hard, [(v.type, v.message) for v in hard[:5]]


@pytest.mark.parametrize("seed", range(6))
def test_cpsat_and_hybrid_schedules_are_always_valid(seed):
    for provider in ("cpsat", "hybrid"):
        sol = solve(random_problem(100 + seed, provider))
        hard = [v for v in sol.violations if v.hardness == "HARD" and not v.type.startswith("DATA_") and v.type not in ALLOWED]
        assert not hard, (provider, [(v.type, v.message) for v in hard[:5]])
