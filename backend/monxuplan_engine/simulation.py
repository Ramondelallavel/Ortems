"""Stochastic evaluation of a plan (Monte Carlo over the exact schedule builder).

The plan's decisions — resource assignment and sequence on every resource — are kept; each run
samples uncertainty and re-times the plan with the builder:

* run-time variance: log-normal multiplier with coefficient of variation ``runtime_cv``,
* supplier delays: each purchase receipt is late with probability ``supplier_delay_prob`` by up to
  ``supplier_delay_days`` days,
* breakdowns: Poisson arrivals per machine (``breakdowns_per_week``) with duration ``breakdown_hours``.

Outputs distributions (P10/P50/P90) of throughput, lead time, WIP and late orders, and each order's
probability of being on time — a robustness measure of the plan, not a new plan. Seeded → repeatable.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import mean
from typing import Any

from .builder import BuildConfig, ScheduleBuilder
from .compile import compile_problem
from .contract import Problem, Solution
from .timing import compute_timing


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    k = max(0, min(len(v) - 1, int(round(q * (len(v) - 1)))))
    return v[k]


def _perturb(problem: Problem, rng: random.Random, runtime_cv: float, supplier_delay_prob: float, supplier_delay_days: float, breakdowns_per_week: float, breakdown_hours: float) -> Problem:
    data = problem.model_dump(mode="json", by_alias=True)
    sigma = math.sqrt(math.log(1 + runtime_cv**2)) if runtime_cv > 0 else 0.0
    mu = -0.5 * sigma * sigma
    for op in data["operations"]:
        if op.get("status") in ("COMPLETED",):
            continue
        f = math.exp(rng.gauss(mu, sigma)) if sigma else 1.0
        d = op.setdefault("duration", {})
        for k in ("run_minutes_per_unit", "fixed_minutes", "minutes_per_batch"):
            if d.get(k):
                d[k] = d[k] * f
        for t in d.get("run_tiers", []) or []:
            t["minutes_per_unit"] *= f
    for m in data["materials"]:
        for s in m.get("supplies", []):
            if s.get("kind") in ("PURCHASE", "TRANSFER", "PROJECTED") and s.get("time") and rng.random() < supplier_delay_prob:
                s["time"] = (datetime.fromisoformat(s["time"]) + timedelta(days=rng.uniform(0.5, supplier_delay_days))).isoformat()
    start = datetime.fromisoformat(data["horizon"]["start"])
    end = datetime.fromisoformat(data["horizon"]["end"])
    weeks = max((end - start).total_seconds() / (7 * 86400), 0.1)
    for r in data["resources"]:
        if r.get("kind") != "MACHINE" or not r.get("finite", True):
            continue
        n = _poisson(rng, breakdowns_per_week * weeks)
        for _ in range(n):
            t = start + timedelta(seconds=rng.uniform(0, (end - start).total_seconds()))
            r.setdefault("unavailability", []).append(
                {"start": t.isoformat(), "end": (t + timedelta(hours=breakdown_hours)).isoformat(), "kind": "BREAKDOWN", "reason": "simulated"}
            )
    data["solver"]["explain"] = False
    return Problem.model_validate(data)


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    L = math.exp(-lam)
    k, p = 0, 1.0
    while True:
        p *= rng.random()
        if p <= L:
            return k
        k += 1


def monte_carlo(
    problem: Problem,
    plan: Solution,
    runs: int = 30,
    runtime_cv: float = 0.10,
    supplier_delay_prob: float = 0.10,
    supplier_delay_days: float = 3.0,
    breakdowns_per_week: float = 0.2,
    breakdown_hours: float = 4.0,
    seed: int = 42,
) -> dict[str, Any]:
    rng = random.Random(seed)
    plan_pos = {s.op_id: s for s in plan.schedule}
    on_time: dict[str, int] = defaultdict(int)
    ends: dict[str, list[float]] = defaultdict(list)
    late_counts: list[int] = []
    throughput: list[float] = []
    lead: list[float] = []
    wip: list[float] = []
    for _ in range(runs):
        pb = _perturb(problem, rng, runtime_cv, supplier_delay_prob, supplier_delay_days, breakdowns_per_week, breakdown_hours)
        cp = compile_problem(pb)
        tm = compute_timing(cp)
        prio: dict[int, float] = {}
        forced: dict[int, int] = {}
        chains: dict[int, list[int]] = defaultdict(list)
        for op in cp.ops:
            s = plan_pos.get(op.id)
            if s is None:
                continue
            prio[op.idx] = cp.axis.to_min(s.setup_start)
            mi = next((m.idx for m in op.modes if cp.resources[m.res].id == s.resource_id), None)
            if mi is not None:
                forced[op.idx] = mi
                if cp.resources[op.modes[mi].res].unary:
                    chains[op.modes[mi].res].append(op.idx)
        for r in chains:
            chains[r].sort(key=lambda i: prio[i])
        res = ScheduleBuilder(cp, BuildConfig(priority=prio, forced_modes=forced, resource_chains=dict(chains), explain=False), tm).build()
        n_late = 0
        units = 0.0
        leads = []
        for o in cp.orders:
            c = res.order_completion(o.idx)
            if c is None:
                n_late += 1
                continue
            ends[o.id].append(c)
            if c <= o.due:
                on_time[o.id] += 1
            else:
                n_late += 1
            if c <= cp.h_end:
                units += o.qty
            starts = [res.placements[i].setup_start for i in o.ops if res.placements[i] is not None]
            if starts:
                leads.append(c - min(starts))
        late_counts.append(n_late)
        throughput.append(units)
        lead.append(mean(leads) / 60.0 if leads else 0.0)
        wip.append(sum(leads) / max(cp.h_end - cp.as_of, 1))
    orders = []
    for o in plan.orders:
        vals = ends.get(o.order_id, [])
        orders.append(
            {
                "order_id": o.order_id,
                "number": o.number,
                "planned_end": o.end.isoformat() if o.end else None,
                "on_time_probability": round(on_time.get(o.order_id, 0) / runs, 3),
                "p50_delay_h": None,
                "risk": "HIGH" if on_time.get(o.order_id, 0) / runs < 0.5 else "MEDIUM" if on_time.get(o.order_id, 0) / runs < 0.9 else "LOW",
                "samples": len(vals),
            }
        )
    orders.sort(key=lambda r: r["on_time_probability"])

    def dist(v):
        return {"p10": _pct(v, 0.1), "p50": _pct(v, 0.5), "p90": _pct(v, 0.9), "mean": round(mean(v), 3) if v else None}

    return {
        "runs": runs,
        "seed": seed,
        "assumptions": {
            "runtime_cv": runtime_cv,
            "supplier_delay_prob": supplier_delay_prob,
            "supplier_delay_days": supplier_delay_days,
            "breakdowns_per_week": breakdowns_per_week,
            "breakdown_hours": breakdown_hours,
        },
        "late_orders": dist(late_counts),
        "throughput_units": dist(throughput),
        "lead_time_h": dist(lead),
        "wip_orders": dist(wip),
        "orders": orders,
    }
