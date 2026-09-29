"""Property-based tests with an oracle independent of the engine.

Random problems (alternative modes, calendars, downtime, sequence-dependent setups, labour pools,
tools, fractional material quantities, precedences, fixed operations) are solved by every provider;
the returned schedule is then checked by

1. the engine's own validator (no HARD violation except explicit UNSCHEDULED and data issues), and
2. an oracle written here from the *output* only (published start/end/resource) and the *input*
   problem: resource compatibility, no duplicates, no overlap on a machine, finish-to-start
   precedences, predecessors of a scheduled operation scheduled, and an exact material balance
   (integer micro-units; supplies at their time, consumption at operation start, production — the
   earliest it could count — at the producing operation's end).

The oracle shares no code with the builder or the validator, so a bug common to both is caught.
``MONXU_FUZZ_SEEDS`` raises the number of problems (CI: 60; nightly: thousands).
"""

from __future__ import annotations

import os
import random
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import pytest

from monxuplan_engine import solve
from monxuplan_engine.contract import Problem

SEEDS = int(os.environ.get("MONXU_FUZZ_SEEDS", "60"))
T0 = datetime(2026, 10, 19, 5, 0, tzinfo=UTC)  # the horizon crosses the October clock change (Madrid)


def at(h: float) -> str:
    return (T0 + timedelta(hours=h)).isoformat()


def gen(seed: int, provider: str = "heuristic") -> Problem:
    rng = random.Random(seed)
    machines = [f"M{k}" for k in range(rng.randint(2, 5))]
    cals = [
        {"id": "DAY", "timezone": "Europe/Madrid", "shifts": [{"weekday": d, "start": "06:00", "end": "22:00", "breaks": [{"start": "12:00", "end": "12:30"}]} for d in range(6)]},
        {"id": "NIGHT", "timezone": "Europe/Madrid", "shifts": [{"weekday": d, "start": "22:00", "end": "06:00"} for d in range(7)]},
    ]
    resources = []
    for m in machines:
        r = {"id": m, "code": m, "kind": "MACHINE", "calendar_id": rng.choice(["DAY", "NIGHT", None]), "setup_matrix_ids": ["FAM"] if rng.random() < 0.7 else []}
        if rng.random() < 0.4:
            s = rng.randint(0, 100)
            r["unavailability"] = [{"start": at(s), "end": at(s + rng.randint(1, 12)), "kind": rng.choice(["MAINTENANCE_PLANNED", "BREAKDOWN"])}]
        resources.append(r)
    resources += [
        {"id": "POOL", "code": "POOL", "kind": "LABOR_POOL", "capacity": rng.randint(1, 3), "calendar_id": "DAY"},
        {"id": "TOOL", "code": "TOOL", "kind": "TOOL", "capacity": rng.randint(1, 2)},
    ]
    step = rng.choice([1, 0.1, 0.001])
    mats = [
        {
            "id": "RAW",
            "code": "RAW",
            "supplies": [
                {"id": "OH", "quantity": round(rng.randint(10, 400) * step, 6), "kind": "ON_HAND"},
                {"id": "PO", "quantity": round(rng.randint(10, 400) * step, 6), "kind": "PURCHASE", "time": at(rng.randint(5, 80))},
            ],
        },
        {"id": "SUB", "code": "SUB", "make_or_buy": "MAKE"},
    ]
    orders, ops, precs = [], [], []
    for k in range(rng.randint(6, 18)):
        oid = f"O{k}"
        fam = rng.choice("ABC")
        qty = rng.randint(2, 40)
        o = {"id": oid, "number": oid, "item_id": f"I{fam}", "item_code": f"I{fam}", "family": fam, "quantity": qty, "due": at(rng.randint(6, 140)), "priority": rng.randint(1, 10)}
        if k < 2:
            o["produces_material_id"] = "SUB"
        orders.append(o)
        prev = None
        for j in range(rng.randint(1, 4)):
            opid = f"{oid}/{10 * (j + 1)}"
            modes = []
            for m in rng.sample(machines, rng.randint(1, min(3, len(machines)))):
                mode = {"resource_id": m, "preference": len(modes)}
                if rng.random() < 0.3:
                    mode["secondary"] = [{"resource_id": "POOL"}]
                if rng.random() < 0.2:
                    mode.setdefault("secondary", []).append({"resource_id": "TOOL"})
                modes.append(mode)
            op = {
                "id": opid,
                "order_id": oid,
                "seq": 10 * (j + 1),
                "quantity": qty,
                "duration": {"setup_minutes": rng.choice([0, 15, 30]), "run_minutes_per_unit": rng.uniform(0.5, 8), "move_minutes": rng.choice([0, 0, 20])},
                "modes": modes,
                "interruptible": rng.random() > 0.2,
            }
            if j == 0 and rng.random() < 0.4:
                op["materials"] = [{"material_id": "RAW", "quantity": round(rng.randint(1, 60) * step, 6)}]
            if j == 0 and k >= 2 and rng.random() < 0.15:
                op.setdefault("materials", []).append({"material_id": "SUB", "quantity": 1})
            ops.append(op)
            if prev:
                precs.append({"pred": prev, "succ": opid, "lag_minutes": rng.choice([0, 0, 60])})
            prev = opid
    return Problem.model_validate(
        {
            "horizon": {"start": T0.isoformat(), "end": (T0 + timedelta(days=8)).isoformat(), "timezone": "Europe/Madrid", "overflow_days": 20},
            "calendars": cals,
            "resources": resources,
            "materials": mats,
            "orders": orders,
            "operations": ops,
            "precedences": precs,
            "setup_matrices": [{"id": "FAM", "attribute": "family", "same_minutes": 5, "default_minutes": 45, "entries": [{"from": "A", "to": "C", "minutes": 120}]}],
            "sequence_constraints": [{"id": "S", "type": "NOT_IMMEDIATELY_AFTER", "prev_match": {"family": "C"}, "next_match": {"family": "A"}}] if seed % 3 == 0 else [],
            "solver": {"provider": provider, "time_limit_s": 3, "local_search": seed % 2 == 0, "multi_start": False, "cpsat_max_ops": 40, "explain": False, "reproducible": True},
        }
    )


def oracle(problem: Problem, sol) -> list[str]:
    """Independent checks of a solution against its problem. Returns the list of broken properties."""
    errs: list[str] = []
    ops = {o.id: o for o in problem.operations}
    kinds = {r.id: r for r in problem.resources}
    seen: set[str] = set()
    by_res: dict[str, list] = defaultdict(list)
    placed = {}
    for x in sol.schedule:
        if x.op_id in seen:
            errs.append(f"{x.op_id} scheduled twice")
        seen.add(x.op_id)
        op = ops[x.op_id]
        allowed = {m.resource_id for m in op.modes} | ({op.fixed.resource_id} if op.fixed else set())
        if x.resource_id not in allowed:
            errs.append(f"{x.op_id} on incompatible resource {x.resource_id}")
        if not (x.setup_start <= x.start <= x.end):
            errs.append(f"{x.op_id} times out of order")
        r = kinds[x.resource_id]
        if r.kind == "MACHINE" and (r.capacity or 1) == 1 and not r.detached_setup:
            by_res[x.resource_id].append((x.setup_start, x.end, x.op_id))
        placed[x.op_id] = x
    for rid, ivs in by_res.items():
        ivs.sort()
        for (a0, a1, ao), (b0, _b1, bo) in zip(ivs, ivs[1:], strict=False):
            if b0 < a1:
                errs.append(f"overlap on {rid}: {ao} [{a0}-{a1}) and {bo} from {b0}")
    for p in problem.precedences:
        if p.succ in placed:
            if p.pred not in placed:
                errs.append(f"{p.succ} scheduled while its predecessor {p.pred} is not")
            elif p.type == "FS" and placed[p.succ].start < placed[p.pred].end:
                errs.append(f"FS {p.pred} -> {p.succ}: successor starts before predecessor ends")
    # exact material balance (micro-units)
    scale = 1_000_000
    far_past = datetime(1970, 1, 1, tzinfo=UTC)
    moves: dict[str, list] = defaultdict(list)
    for m in problem.materials:
        for sp in m.supplies:
            moves[m.id].append((sp.time or far_past, 0, round(sp.quantity * scale)))
    orders = {o.id: o for o in problem.orders}
    last_end: dict[str, datetime] = {}
    for x in sol.schedule:
        last_end[x.order_id] = max(last_end.get(x.order_id, x.end), x.end)
        for mu in ops[x.op_id].materials:
            moves[mu.material_id].append((x.start, 1, -round(mu.quantity * scale)))
    for oid, end in last_end.items():
        o = orders[oid]
        n_ops = sum(1 for op in problem.operations if op.order_id == oid)
        n_done = sum(1 for x in sol.schedule if x.order_id == oid)
        if o.produces_material_id and n_ops == n_done:
            moves[o.produces_material_id].append((end, 0, round(o.quantity * scale)))
    for mid, mv in moves.items():
        level = 0
        for t, _k, u in sorted(mv, key=lambda e: (e[0], e[1])):
            level += u
            if level < 0:
                errs.append(f"material {mid} negative ({level / scale}) at {t}")
                break
    return errs


def _engine_hard(sol):
    return [(v.type, v.message) for v in sol.violations if v.hardness == "HARD" and v.type != "UNSCHEDULED" and not v.type.startswith("DATA_")]


@pytest.mark.parametrize("seed", range(SEEDS))
def test_heuristic_schedules_satisfy_the_oracle(seed):
    p = gen(seed)
    sol = solve(p)
    assert not _engine_hard(sol), _engine_hard(sol)[:5]
    errs = oracle(p, sol)
    assert not errs, errs[:5]


@pytest.mark.parametrize("seed", range(max(4, SEEDS // 10)))
@pytest.mark.parametrize("provider", ["cpsat", "hybrid"])
def test_exact_providers_satisfy_the_oracle(seed, provider):
    p = gen(1000 + seed, provider)
    sol = solve(p)
    assert not _engine_hard(sol), (provider, _engine_hard(sol)[:5])
    errs = oracle(p, sol)
    assert not errs, (provider, errs[:5])


@pytest.mark.parametrize("seed", range(max(4, SEEDS // 10)))
def test_reproducible_mode_is_deterministic(seed):
    a, b = solve(gen(2000 + seed)), solve(gen(2000 + seed))
    key = lambda s: sorted((x.op_id, x.resource_id, x.setup_start, x.end) for x in s.schedule)  # noqa: E731
    assert key(a) == key(b)


def test_oracle_detects_what_it_claims():
    """The oracle must reject corrupted schedules (otherwise it proves nothing)."""
    p = gen(7)
    sol = solve(p)
    assert not oracle(p, sol)
    xs = sorted(sol.schedule, key=lambda x: (x.resource_id, x.setup_start))
    a, b = next((a, b) for a, b in zip(xs, xs[1:], strict=False) if a.resource_id == b.resource_id)
    bad = sol.model_copy(deep=True)
    y = next(x for x in bad.schedule if x.op_id == b.op_id)
    y.setup_start = a.setup_start
    assert any("overlap" in e for e in oracle(p, bad))
