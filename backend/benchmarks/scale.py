"""Scale benchmark: a factory with N production orders per day (default 100 000).

    python benchmarks/scale.py --orders 100000              # engine phases, one construction
    python benchmarks/scale.py --orders 100000 --full 120   # full solve() with a 120 s limit
    python benchmarks/scale.py --sweep 5000 20000 100000    # growth of every phase

The generated plant is sized so the work fits: ~one machine per 150 orders a day, grouped in cells
of 8 interchangeable machines, three shifts Monday–Saturday, routings of 1–3 operations with
alternative machines, family changeovers, labour pools on a third of the operations and raw
materials with stock and receipts. Everything is seeded and deterministic.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import resource
import sys
import time
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

T0 = datetime(2026, 9, 28, 0, 0, tzinfo=UTC)  # a Monday


def generate(orders: int, days: float = 1.0, seed: int = 7, machines: int | None = None) -> dict:
    """Problem as a plain dict (the JSON contract). ``days`` spreads the due dates."""
    rng = random.Random(seed)
    n_m = machines or max(4, orders // 100)
    cell = 8
    cells = max(1, n_m // cell)
    mach = [f"M{k:05d}" for k in range(n_m)]
    cell_of = {m: k % cells for k, m in enumerate(mach)}
    by_cell: dict[int, list[str]] = {}
    for m in mach:
        by_cell.setdefault(cell_of[m], []).append(m)
    cal = {"id": "3SH", "timezone": "Europe/Madrid", "shifts": [{"weekday": d, "start": a, "end": b} for d in range(6) for a, b in (("06:00", "14:00"), ("14:00", "22:00"), ("22:00", "06:00"))]}
    n_pools = max(1, cells // 4)
    resources = [{"id": m, "code": m, "kind": "MACHINE", "calendar_id": "3SH", "groups": [f"C{cell_of[m]}"], "setup_matrix_ids": ["FAM"], "efficiency": rng.choice([0.9, 1.0, 1.1])} for m in mach]
    for p in range(n_pools):
        resources.append({"id": f"POOL{p}", "code": f"POOL{p}", "kind": "LABOR_POOL", "capacity": 3 * cell, "calendar_id": "3SH"})
    n_mat = 50
    mats = [
        {
            "id": f"RAW{k}",
            "code": f"RAW{k}",
            "supplies": [
                {"id": f"OH{k}", "quantity": orders * 4, "kind": "ON_HAND"},
                {"id": f"PO{k}", "quantity": orders * 4, "kind": "PURCHASE", "time": (T0 + timedelta(hours=rng.randint(6, 30))).isoformat()},
            ],
        }
        for k in range(n_mat)
    ]
    ords, ops, precs = [], [], []
    horizon_days = max(3.0, days + 2)
    for k in range(orders):
        oid = f"WO{k:07d}"
        fam = rng.choice("ABCDEF")
        qty = rng.randint(1, 20)
        ords.append({"id": oid, "number": oid, "item_id": f"P{fam}{k % 97}", "family": fam, "quantity": qty, "due": (T0 + timedelta(hours=rng.uniform(8, 24 * days + 8))).isoformat(), "priority": rng.randint(1, 10)})
        n_ops = rng.choice((1, 2, 2, 3))
        prev = None
        c0 = rng.randrange(cells)
        for j in range(n_ops):
            c = (c0 + j) % cells
            cands = by_cell[c]
            chosen = rng.sample(cands, min(len(cands), rng.randint(1, 3)))
            modes = []
            for pref, m in enumerate(chosen):
                mode = {"resource_id": m, "preference": pref}
                if rng.random() < 0.3:
                    mode["secondary"] = [{"resource_id": f"POOL{c % n_pools}"}]
                modes.append(mode)
            opid = f"{oid}/{(j + 1) * 10}"
            op = {"id": opid, "order_id": oid, "seq": (j + 1) * 10, "quantity": qty, "duration": {"setup_minutes": 2, "run_minutes_per_unit": rng.uniform(0.05, 0.3)}, "modes": modes}
            if j == 0:
                op["materials"] = [{"material_id": f"RAW{rng.randrange(n_mat)}", "quantity": qty}]
            ops.append(op)
            if prev:
                precs.append({"pred": prev, "succ": opid})
            prev = opid
    return {
        "horizon": {"start": T0.isoformat(), "end": (T0 + timedelta(days=horizon_days)).isoformat(), "timezone": "Europe/Madrid"},
        "calendars": [cal],
        "resources": resources,
        "materials": mats,
        "orders": ords,
        "operations": ops,
        "precedences": precs,
        "setup_matrices": [{"id": "FAM", "attribute": "family", "same_minutes": 1, "default_minutes": 6}],
        "solver": {"provider": "heuristic", "profile": "QUICK", "explain": False, "multi_start": False},
    }


def _rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def phases(orders: int, full: float | None = None) -> dict:
    from monxuplan_engine.perf import paused_gc

    with paused_gc():
        return _phases(orders, full)


def _phases(orders: int, full: float | None = None) -> dict:
    from monxuplan_engine import solve
    from monxuplan_engine.assemble import assemble
    from monxuplan_engine.compile import compile_problem
    from monxuplan_engine.contract import Problem, SolverMetadata
    from monxuplan_engine.providers.base import decode
    from monxuplan_engine.timing import compute_timing
    from monxuplan_engine.validator import validate

    out: dict = {"orders": orders}
    t = time.monotonic()
    raw = generate(orders)
    out["generate_s"] = round(time.monotonic() - t, 2)
    t = time.monotonic()
    problem = Problem.model_validate(raw)
    out["operations"] = len(problem.operations)
    out["resources"] = len(problem.resources)
    out["parse_s"] = round(time.monotonic() - t, 2)
    del raw
    if full:
        problem.solver.time_limit_s = full
        t = time.monotonic()
        sol = solve(problem)
        out["solve_s"] = round(time.monotonic() - t, 2)
        out["phases"] = {p.name: p.runtime_s for p in sol.solver_metadata.phases}
        out["unscheduled"] = len(sol.unscheduled)
        out["late_orders"] = sol.kpis.get("late_orders")
        out["hard_violations"] = sum(1 for v in sol.violations if v.hardness == "HARD" and v.type != "UNSCHEDULED" and not v.type.startswith("DATA_"))
        out["iterations"] = sol.solver_metadata.iterations
        out["peak_rss_mb"] = round(_rss_mb())
        return out
    t = time.monotonic()
    cp = compile_problem(problem)
    out["compile_s"] = round(time.monotonic() - t, 2)
    t = time.monotonic()
    tm = compute_timing(cp)
    out["timing_s"] = round(time.monotonic() - t, 2)
    t = time.monotonic()
    res = decode(cp, tm, rules=tuple(cp.solver.dispatch_rules), mode_selection=cp.solver.mode_selection)
    out["construct_s"] = round(time.monotonic() - t, 2)
    t = time.monotonic()
    val = validate(cp, res.placements, res.unscheduled)
    out["validate_s"] = round(time.monotonic() - t, 2)
    t = time.monotonic()
    sol = assemble(res, SolverMetadata(provider="heuristic", status="HEURISTIC"), val, explain=False)
    out["assemble_kpis_s"] = round(time.monotonic() - t, 2)
    out["unscheduled"] = len(sol.unscheduled)
    out["late_orders"] = sol.kpis.get("late_orders")
    out["hard_violations"] = sum(1 for v in sol.violations if v.hardness == "HARD" and v.type != "UNSCHEDULED" and not v.type.startswith("DATA_"))
    out["peak_rss_mb"] = round(_rss_mb())
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--orders", type=int, default=100_000)
    ap.add_argument("--full", type=float, default=None, help="run the whole solve() with this time limit (s)")
    ap.add_argument("--sweep", type=int, nargs="*", help="run each size in a fresh process")
    args = ap.parse_args()
    if args.sweep:
        import subprocess

        for n in args.sweep:
            cmd = [sys.executable, __file__, "--orders", str(n)] + (["--full", str(args.full)] if args.full else [])
            r = subprocess.run(cmd, capture_output=True, text=True)
            print(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-2000:], flush=True)
        return
    print(json.dumps(phases(args.orders, args.full)))


if __name__ == "__main__":
    main()
