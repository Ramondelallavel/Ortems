"""Hybrid provider: heuristic construction + CP-SAT.

* Small problems (``len(ops) <= cpsat_max_ops``): full CP-SAT model warm-started by the heuristic.
* Larger problems: Large Neighbourhood Search. Repeatedly free a neighbourhood — late orders and the
  jobs they queue behind, a time window, a bottleneck resource, a pair of alternative resources —,
  fix everything else, re-optimise it with CP-SAT (with the incumbent as hint), re-time the whole plan
  with the exact builder and accept only strict improvements of the objective vector.

LNS never claims global optimality: status is ``FEASIBLE`` and ``gap`` is left empty (the proven gap
of a sub-problem says nothing about the whole plan); the neighbourhood log is returned in details.
"""

from __future__ import annotations

import random
from collections import defaultdict

from ..builder import BuildResult
from ..compile import CompiledProblem
from ..timing import Timing
from .base import Evaluator, OptimizationProvider, ProviderCapabilities, ProviderResult, SolveContext, decode
from .cpsat import Neighbourhood, decode_outcome, full_model, solve_neighbourhood
from .heuristic import HeuristicProvider


class HybridProvider(OptimizationProvider):
    name = "hybrid"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(detailed_scheduling=True, proves_optimality=True)

    def solve(self, cp: CompiledProblem, timing: Timing, ctx: SolveContext) -> ProviderResult:
        free_ops = sum(1 for o in cp.ops if o.fixed is None)
        if free_ops <= cp.solver.cpsat_max_ops:
            res = full_model(cp, timing, ctx)
            res.details["strategy"] = "full-cpsat"
            return res
        return lns(cp, timing, ctx)


def lns(cp: CompiledProblem, timing: Timing, ctx: SolveContext) -> ProviderResult:
    total = ctx.time_limit_s
    sub = SolveContext(time_limit_s=max(total * 0.4, 0.5), seed=ctx.seed, progress=ctx.progress, cancelled=ctx.cancelled)
    h = HeuristicProvider().solve(cp, timing, sub)
    ev = Evaluator(cp)
    ev.scale = h.details["scales"]
    best: BuildResult = h.result
    best_comp, best_vec = ev.evaluate(best)
    rng = random.Random(ctx.seed)
    strategies = ["late_orders", "time_window", "bottleneck", "resource_pair"]
    log: list[dict] = []
    it = 0
    improved = 0
    target = min(cp.solver.lns_neighbourhood_ops, 60)
    reproducible = cp.solver.reproducible
    per_iter = max(1.0, total / 12)
    # reproducible mode: a fixed number of neighbourhoods with deterministic budgets; the wall clock
    # (limit + 20 %) is only a safety net and hitting it is reported
    max_iter = max(4, int(ctx.remaining() / per_iter)) if reproducible else 10**9
    import time as _time

    hard_deadline = ctx.deadline + 0.2 * total
    wall_hit = False
    while it < max_iter:
        if reproducible:
            if _time.monotonic() >= hard_deadline or (ctx.cancelled and ctx.cancelled()):
                wall_hit = _time.monotonic() >= hard_deadline
                break
        elif ctx.expired():
            break
        it += 1
        strat = strategies[(it - 1) % len(strategies)]
        nb = _neighbourhood(cp, best, strat, target, rng)
        if nb is None or len(nb.free) < 2:
            if it > 4 * len(strategies) and not log:
                break
            continue
        limit = per_iter if reproducible else min(ctx.remaining() - 1.0, per_iter)
        if limit < 0.5:
            break
        out = solve_neighbourhood(cp, best, nb, ev, limit, ctx.seed + it, cp.solver.workers, cp.solver.reproducible, cp.solver.circuit_max_ops)
        entry = {"iteration": it, "strategy": strat, "label": nb.label, "ops": len(nb.free), "cp_status": out.status, "accepted": False}
        # adaptive neighbourhood size: shrink when CP-SAT runs out of time, grow when it proves optimality
        if out.status in ("UNKNOWN", "MODEL_INVALID"):
            target = max(12, int(target * 0.7))
        elif out.status == "OPTIMAL" and out.wall < 0.5 * limit:
            target = min(cp.solver.lns_neighbourhood_ops * 2, int(target * 1.25) + 2)
        if out.status in ("OPTIMAL", "FEASIBLE"):
            dec = decode_outcome(cp, timing, best, out)
            comp, vec = ev.evaluate(dec)
            if len(dec.unscheduled) <= len(best.unscheduled) and ev.better(vec, best_vec):
                best, best_comp, best_vec = dec, comp, vec
                improved += 1
                entry["accepted"] = True
                ctx.report("Optimizing", None, f"LNS {strat}: improvement {improved}")
        log.append(entry)
    if wall_hit:
        ctx.messages.append("LNS stopped on the wall-clock safety limit: this result may not be exactly reproducible")
    if cp.solver.explain:
        best = _rebuild_with_explanations(cp, timing, best)
        best_comp, best_vec = ev.evaluate(best)
    return ProviderResult(
        result=best,
        status="FEASIBLE" if improved or it else "HEURISTIC",
        components=best_comp,
        vector=best_vec,
        iterations=h.iterations + it,
        details={"strategy": "lns", "neighbourhoods": log[-50:], "lns_iterations": it, "lns_improvements": improved, "scales": ev.scale, "heuristic_rules": h.details.get("rules")},
    )


def _rebuild_with_explanations(cp: CompiledProblem, timing: Timing, res: BuildResult) -> BuildResult:
    prio: dict[int, float] = {}
    forced: dict[int, int] = {}
    chains: dict[int, list[int]] = defaultdict(list)
    for p in res.placements:
        if p is None:
            continue
        prio[p.op] = float(p.setup_start)
        forced[p.op] = p.mode
        if cp.resources[p.res].unary:
            chains[p.res].append(p.op)
    for r in chains:
        chains[r].sort(key=lambda i: (prio[i], i))
    again = decode(cp, timing, priority=prio, forced=forced, chains=dict(chains), explain=True)
    # the rebuild must reproduce the plan; if it does not (should not happen) keep the original
    same = all((a is None) == (b is None) and (a is None or (a.res == b.res and a.start == b.start)) for a, b in zip(res.placements, again.placements, strict=True))
    return again if same else res


def _free_ops(res: BuildResult) -> list:
    return [p for p in res.placements if p is not None and not p.fixed]


def _neighbourhood(cp: CompiledProblem, res: BuildResult, strat: str, target: int, rng: random.Random) -> Neighbourhood | None:
    placed = _free_ops(res)
    if not placed:
        return None
    placed.sort(key=lambda p: p.setup_start)
    chosen: set[int] = set()
    label = strat
    if strat == "late_orders":
        late = []
        for o in cp.orders:
            c = res.order_completion(o.idx)
            if c is not None and c > o.due - o.safety:
                late.append((o, (c - o.due) * o.weight))
        if not late:
            return _neighbourhood(cp, res, "time_window", target, rng)
        k = min(len(late), max(1, target // 12))
        picks = rng.choices([o for o, _ in late], weights=[w for _, w in late], k=k)
        resources: set[int] = set()
        lo, hi = None, None
        for o in picks:
            for i in o.ops:
                p = res.placements[i]
                if p is not None and not p.fixed:
                    chosen.add(i)
                    for m in cp.ops[i].modes:
                        resources.add(m.res)
                    lo = p.setup_start if lo is None else min(lo, p.setup_start)
                    hi = p.end if hi is None else max(hi, p.end)
        if lo is not None:
            span = max(hi - lo, 480)
            for p in placed:
                if len(chosen) >= target:
                    break
                if p.res in resources and lo - span <= p.setup_start <= hi:
                    chosen.add(p.op)
        label = f"late orders ({len(picks)})"
    elif strat == "time_window":
        start = rng.randrange(max(1, len(placed) - target // 2))
        for p in placed[start : start + target]:
            chosen.add(p.op)
        label = "time window"
    elif strat == "bottleneck":
        load: dict[int, int] = defaultdict(int)
        for p in placed:
            load[p.res] += p.end - p.setup_start
        if not load:
            return None
        ranked = sorted(load, key=lambda r: -load[r])[:3]
        r = rng.choice(ranked)
        seq = [p for p in placed if p.res == r]
        start = rng.randrange(max(1, len(seq) - target // 2))
        for p in seq[start : start + target]:
            chosen.add(p.op)
        label = f"bottleneck {cp.resources[r].code}"
    else:  # resource_pair: resources sharing alternative operations
        pairs: dict[tuple[int, int], int] = defaultdict(int)
        for p in placed:
            ms = sorted({m.res for m in cp.ops[p.op].modes})
            for a in ms:
                for b in ms:
                    if a < b:
                        pairs[(a, b)] += 1
        if not pairs:
            return _neighbourhood(cp, res, "time_window", target, rng)
        a, b = rng.choice(sorted(pairs, key=lambda k: -pairs[k])[:5])
        seq = [p for p in placed if p.res in (a, b)]
        start = rng.randrange(max(1, len(seq) - target // 2))
        for p in seq[start : start + target]:
            chosen.add(p.op)
        label = f"resources {cp.resources[a].code}+{cp.resources[b].code}"
    if len(chosen) < 2:
        return None
    lo = min(res.placements[i].setup_start for i in chosen)
    hi = max(res.placements[i].end for i in chosen)
    span = max(hi - lo, 1440)
    return Neighbourhood(free=chosen, lo=max(cp.work_lb, lo - span // 2), hi=hi + span, label=label)
