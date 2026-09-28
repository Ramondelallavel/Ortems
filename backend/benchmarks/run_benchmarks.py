"""Solver benchmarks (spec §121): runtime, feasibility, objective and memory on reference instances.

    python benchmarks/run_benchmarks.py                 # all instances, all providers
    python benchmarks/run_benchmarks.py --only S M      # a subset
    python benchmarks/run_benchmarks.py --markdown      # table for docs/OPTIMIZATION.md

Instances are generated deterministically (seeded): two-shift calendars, family setup matrices,
alternative resources, labour pools, tools, raw materials with late receipts, multi-step routings.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import resource
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from monxuplan_engine import solve  # noqa: E402
from monxuplan_engine.contract import Problem  # noqa: E402

T0 = datetime(2026, 9, 28, 4, 0, tzinfo=UTC)

INSTANCES = {
    "S": {"label": "10 orders / 2 machines", "orders": 10, "machines": 2, "ops_per_order": (1, 2), "days": 7},
    "M": {"label": "50 orders / 5 machines", "orders": 50, "machines": 5, "ops_per_order": (1, 3), "days": 14},
    "L": {"label": "500 orders / 20 machines", "orders": 500, "machines": 20, "ops_per_order": (2, 4), "days": 42},
    "XL": {"label": "5000 operations / 100 resources", "orders": 1700, "machines": 80, "ops_per_order": (2, 4), "days": 60, "pools": 12, "tools": 8},
}


def generate(key: str, seed: int = 1) -> Problem:
    spec = INSTANCES[key]
    rng = random.Random(seed)
    n_m = spec["machines"]
    groups = max(1, n_m // 4)
    machines = [f"M{k:03d}" for k in range(n_m)]
    grp_of = {m: f"G{k % groups}" for k, m in enumerate(machines)}
    cal = {
        "id": "CAL2",
        "timezone": "Europe/Madrid",
        "shifts": [{"weekday": d, "start": "06:00", "end": "22:00", "breaks": [{"start": "13:00", "end": "13:30"}]} for d in range(5)],
    }
    resources = [
        {"id": m, "code": m, "kind": "MACHINE", "calendar_id": "CAL2", "groups": [grp_of[m]], "setup_matrix_ids": ["FAM"], "efficiency": rng.choice([0.85, 0.9, 1.0])}
        for m in machines
    ]
    pools = [f"POOL{k}" for k in range(spec.get("pools", 1))]
    tools = [f"TOOL{k}" for k in range(spec.get("tools", 1))]
    for p in pools:
        resources.append({"id": p, "code": p, "kind": "LABOR_POOL", "capacity": max(2, n_m // (2 * len(pools)) + 1), "calendar_id": "CAL2"})
    for t in tools:
        resources.append({"id": t, "code": t, "kind": "TOOL", "capacity": 2})
    mats = [
        {"id": f"RAW{k}", "code": f"RAW{k}", "supplies": [{"id": f"OH{k}", "quantity": 10_000, "kind": "ON_HAND"}, {"id": f"PO{k}", "quantity": 50_000, "kind": "PURCHASE", "time": (T0 + timedelta(days=rng.randint(2, 10))).isoformat()}]}
        for k in range(10)
    ]
    orders, ops, precs = [], [], []
    horizon_days = spec["days"]
    for k in range(spec["orders"]):
        oid = f"WO{k:05d}"
        fam = rng.choice("ABCDE")
        qty = rng.randint(10, 120)
        orders.append({"id": oid, "number": oid, "item_id": f"P{fam}{k % 7}", "family": fam, "quantity": qty, "due": (T0 + timedelta(hours=rng.uniform(24, horizon_days * 24))).isoformat(), "priority": rng.randint(1, 10)})
        n_ops = rng.randint(*spec["ops_per_order"])
        prev = None
        for j in range(n_ops):
            g = rng.randrange(groups)
            cands = [m for m in machines if grp_of[m] == f"G{g}"]
            chosen = rng.sample(cands, min(len(cands), rng.randint(1, 3)))
            modes = []
            for pref, m in enumerate(chosen):
                mode = {"resource_id": m, "preference": pref}
                if rng.random() < 0.3:
                    mode["secondary"] = [{"resource_id": rng.choice(pools)}]
                if rng.random() < 0.1:
                    mode.setdefault("secondary", []).append({"resource_id": rng.choice(tools)})
                modes.append(mode)
            opid = f"{oid}/{(j + 1) * 10}"
            op = {"id": opid, "order_id": oid, "seq": (j + 1) * 10, "quantity": qty, "duration": {"setup_minutes": 15, "run_minutes_per_unit": rng.uniform(0.3, 2.5)}, "modes": modes}
            if j == 0:
                op["materials"] = [{"material_id": f"RAW{rng.randrange(10)}", "quantity": qty}]
            ops.append(op)
            if prev:
                precs.append({"pred": prev, "succ": opid})
            prev = opid
    matrix = {"id": "FAM", "attribute": "family", "same_minutes": 5, "default_minutes": 35}
    return Problem.model_validate(
        {
            "horizon": {"start": T0.isoformat(), "end": (T0 + timedelta(days=horizon_days)).isoformat(), "timezone": "Europe/Madrid"},
            "calendars": [cal],
            "resources": resources,
            "materials": mats,
            "orders": orders,
            "operations": ops,
            "precedences": precs,
            "setup_matrices": [matrix],
        }
    )


def run(key: str, provider: str, time_limit: float) -> dict:
    """One benchmark in a fresh process: wall time around solve() and the process peak RSS (includes
    native solver memory, which Python-level tracing would miss)."""
    cmd = [sys.executable, __file__, "--single", key, provider, str(time_limit)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def _single(key: str, provider: str, time_limit: float) -> dict:
    p = generate(key)
    p.solver.provider = provider
    p.solver.time_limit_s = time_limit
    p.solver.explain = key in ("S", "M")
    base_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t = time.monotonic()
    sol = solve(p)
    wall = time.monotonic() - t
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    md = sol.solver_metadata
    return {
        "instance": key,
        "label": INSTANCES[key]["label"],
        "operations": len(p.operations),
        "resources": len(p.resources),
        "provider": provider,
        "time_limit_s": time_limit,
        "runtime_s": round(wall, 2),
        "status": md.status,
        "feasible": sol.feasible,
        "hard_violations": sum(1 for v in sol.violations if v.hardness == "HARD" and v.type != "UNSCHEDULED" and not v.type.startswith("DATA_")),
        "unscheduled": len(sol.unscheduled),
        "late_orders": sol.kpis.get("late_orders"),
        "setup_h": sol.kpis.get("setup_h"),
        "objective": md.objective,
        "gap": md.gap,
        "peak_rss_mb": round(peak_rss / 1024, 0),
        "rss_growth_mb": round((peak_rss - base_rss) / 1024, 0),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=list(INSTANCES))
    ap.add_argument("--providers", nargs="*", default=["heuristic", "hybrid"])
    ap.add_argument("--time-limit", type=float, default=30)
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--single", nargs=3, metavar=("KEY", "PROVIDER", "TIME_LIMIT"), help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.single:
        print(json.dumps(_single(args.single[0], args.single[1], float(args.single[2]))))
        return
    rows = []
    for key in args.only:
        for prov in args.providers:
            r = run(key, prov, args.time_limit)
            rows.append(r)
            print(json.dumps(r), file=sys.stderr)
    os.makedirs(os.path.join(os.path.dirname(__file__), "results"), exist_ok=True)
    with open(os.path.join(os.path.dirname(__file__), "results", "latest.json"), "w") as fh:
        json.dump(rows, fh, indent=2)
    if args.markdown:
        print("| Instance | Ops | Resources | Provider | Runtime (s) | Status | Hard violations | Unscheduled | Late | Setup (h) | Gap | Peak RSS (MB) |")
        print("|---|---:|---:|---|---:|---|---:|---:|---:|---:|---:|---:|")
        for r in rows:
            gap = f"{r['gap']:.1%}" if r["gap"] is not None else "—"
            print(
                f"| {r['label']} | {r['operations']} | {r['resources']} | {r['provider']} | {r['runtime_s']} | {r['status']} | {r['hard_violations']} | {r['unscheduled']} | {r['late_orders']:.0f} | {r['setup_h']} | {gap} | {r['peak_rss_mb']:.0f} |"
            )


if __name__ == "__main__":
    main()
