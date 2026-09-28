"""Heuristic provider: multi-start dispatching + local search.

1. **Multi-start construction** — the configured dispatching rules plus a portfolio of classical
   rules (EDD, minimum slack, critical ratio, customer priority, setup/family-aware hybrids) and mode
   strategies. Each construction is an exact schedule (builder).
2. **Local search** on the decisions (priority list and modes) of the best construction:
   * pull a late order forward in front of the jobs it waited behind,
   * swap adjacent jobs on a resource,
   * batch a job next to a job of the same family on its resource (setup reduction),
   * move a job to an alternative resource.
   Every neighbour is re-built exactly and accepted only if the objective vector improves
   (lexicographic comparison; weighted objectives are a 2-level vector). Seeded RNG → reproducible.

Status is always ``HEURISTIC``: this provider never claims optimality.
"""

from __future__ import annotations

import random
from collections import defaultdict

from ..compile import CompiledProblem
from ..contract import ModeSelection
from ..timing import Timing
from .base import Evaluator, OptimizationProvider, ProviderCapabilities, ProviderResult, SolveContext, decisions_of, decode

PORTFOLIO: list[tuple[str, ...]] = [
    ("HYBRID_APS",),
    ("EDD",),
    ("MIN_SLACK", "EDD"),
    ("DUE_DATE_FIRST",),
    ("CRITICAL_RATIO",),
    ("EDD_PRIORITY",),
    ("CUSTOMER_PRIORITY", "MIN_SLACK"),
    ("MIN_SLACK", "SHORTEST_SETUP", "EDD"),
    ("MIN_SLACK", "FAMILY_GROUPING", "EDD"),
    ("BOTTLENECK_FIRST", "MIN_SLACK"),
    ("SPT",),
]


class HeuristicProvider(OptimizationProvider):
    name = "heuristic"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(detailed_scheduling=True, proves_optimality=False)

    def solve(self, cp: CompiledProblem, timing: Timing, ctx: SolveContext) -> ProviderResult:
        ev = Evaluator(cp)
        configured = tuple(cp.solver.dispatch_rules)
        starts: list[tuple[tuple[str, ...], ModeSelection]] = [(configured, cp.solver.mode_selection)]
        if cp.solver.multi_start:
            for rs in PORTFOLIO:
                if rs != configured:
                    starts.append((rs, cp.solver.mode_selection))
            if cp.solver.mode_selection.strategy != "PREFERRED":
                starts.append((configured, ModeSelection(strategy="PREFERRED")))
        best = None
        best_vec = None
        best_comp = None
        best_rules = configured
        best_msel = cp.solver.mode_selection
        tried = 0
        ctx.report("Generating initial plan", 0.0, f"{len(starts)} dispatching strategies")
        for k, (rules, msel) in enumerate(starts):
            if k > 0 and ctx.expired():
                break
            res = decode(cp, timing, rules=rules, mode_selection=msel)
            tried += 1
            comp, _vec = ev.evaluate(res)
            if best is None:
                ev.set_reference(comp)
            comp, vec = ev.evaluate(res)
            if best_vec is None or ev.better(vec, best_vec):
                best, best_vec, best_comp, best_rules, best_msel = res, vec, comp, rules, msel
            ctx.report("Generating initial plan", (k + 1) / len(starts), f"{'/'.join(rules)}: {comp['late_orders']:.0f} weighted late, {comp['setup'] / 60:.1f} h setup")
        assert best is not None and best_vec is not None and best_comp is not None
        iterations = tried
        improved = 0
        decisions = {"rules": best_rules, "mode_selection": best_msel, "priority": None, "forced": None}
        if cp.solver.local_search and not ctx.expired():
            best, best_vec, best_comp, it, improved, prio, forced = local_search(cp, timing, ev, best, best_vec, best_comp, ctx, best_rules, best_msel)
            iterations += it
            if improved:
                decisions.update(priority=prio, forced=forced or None)
        if cp.solver.explain:
            best = decode(cp, timing, explain=True, **decisions)
        return ProviderResult(
            result=best,
            status="HEURISTIC",
            components=best_comp,
            vector=best_vec,
            iterations=iterations,
            details={"constructions": tried, "rules": list(best_rules), "local_search_improvements": improved, "scales": ev.scale},
        )


def local_search(cp: CompiledProblem, timing: Timing, ev: Evaluator, best, best_vec, best_comp, ctx: SolveContext, rules, msel=None, max_iter: int = 100000):
    rng = random.Random(ctx.seed)
    prio, _modes = decisions_of(best)
    forced: dict[int, int] = {}
    best_prio = dict(prio)
    it = 0
    improved = 0
    stall = 0
    n_ops = len(cp.ops)
    max_stall = max(60, min(400, 4 * n_ops))
    ctx.report("Optimizing", 0.0, "local search")
    while it < max_iter and not ctx.expired() and stall < max_stall:
        it += 1
        move = rng.random()
        new_prio = dict(prio)
        new_forced = dict(forced)
        desc = ""
        if move < 0.40:
            desc = _pull_late_order(cp, best, new_prio, rng)
        elif move < 0.60:
            desc = _swap_adjacent(cp, best, new_prio, rng)
        elif move < 0.80:
            desc = _batch_family(cp, best, new_prio, rng)
        else:
            desc = _change_mode(cp, best, new_forced, rng)
        if not desc:
            stall += 1
            continue
        cand = decode(cp, timing, rules=rules, priority=new_prio, forced=new_forced or None, mode_selection=msel)
        comp, vec = ev.evaluate(cand)
        if ev.better(vec, best_vec):
            best, best_vec, best_comp = cand, vec, comp
            best_prio = new_prio
            prio, _m = decisions_of(cand)
            forced = new_forced
            improved += 1
            stall = 0
            ctx.report("Optimizing", None, f"improvement {improved}: {desc}")
        else:
            stall += 1
    return best, best_vec, best_comp, it, improved, best_prio, forced


def _resource_sequences(res) -> dict[int, list]:
    seqs: dict[int, list] = defaultdict(list)
    for p in res.placements:
        if p is not None and not p.fixed:
            seqs[p.res].append(p)
    for lst in seqs.values():
        lst.sort(key=lambda p: p.setup_start)
    return seqs


def _pull_late_order(cp, res, prio, rng) -> str:
    late = []
    for o in cp.orders:
        c = res.order_completion(o.idx)
        if c is not None and c > o.due - o.safety:
            late.append((c - o.due) * o.weight)
        else:
            late.append(0)
    cands = [i for i, v in enumerate(late) if v > 0]
    if not cands:
        return ""
    oi = rng.choices(cands, weights=[late[i] for i in cands])[0]
    order = cp.orders[oi]
    # find the op of this order that waited longest for a resource and jump before its blocker
    waits = []
    for i in order.ops:
        p = res.placements[i]
        if p is not None and not p.fixed and p.binding is not None and p.binding.type in ("RESOURCE", "SETUP", "LABOR", "TOOL") and p.binding.wait > 0:
            waits.append((p.binding.wait, i))
    if not waits:
        return ""
    _w, i = max(waits)
    p = res.placements[i]
    seqs = _resource_sequences(res)
    lst = seqs.get(p.res, [])
    pos = next((k for k, q in enumerate(lst) if q.op == i), None)
    if pos is None or pos == 0:
        return ""
    jump = rng.randint(1, min(pos, 4))
    target = lst[pos - jump]
    new_val = prio.get(target.op, target.setup_start) - 1e-3
    prio[i] = new_val
    # predecessors inside the order must come first: pull them along (backwards along the routing)
    ordered = sorted(order.ops, key=lambda j: cp.ops[j].seq)
    for a, b in reversed(list(zip(ordered, ordered[1:], strict=False))):
        if prio.get(a, 0.0) >= prio.get(b, 0.0):
            prio[a] = prio[b] - 1e-4
    return f"pull {order.number} before {cp.ops[target.op].id}"


def _swap_adjacent(cp, res, prio, rng) -> str:
    seqs = [lst for lst in _resource_sequences(res).values() if len(lst) >= 2]
    if not seqs:
        return ""
    lst = rng.choice(seqs)
    k = rng.randrange(len(lst) - 1)
    a, b = lst[k], lst[k + 1]
    pa, pb = prio.get(a.op, a.setup_start), prio.get(b.op, b.setup_start)
    prio[a.op], prio[b.op] = pb, pa
    return f"swap {cp.ops[a.op].id}/{cp.ops[b.op].id}"


def _batch_family(cp, res, prio, rng) -> str:
    seqs = [lst for lst in _resource_sequences(res).values() if len(lst) >= 3]
    if not seqs:
        return ""
    lst = rng.choice(seqs)
    k = rng.randrange(len(lst) - 1)
    anchor = lst[k]
    fam = cp.ops[anchor.op].family
    if fam is None:
        return ""
    for q in lst[k + 2 : k + 12]:
        if cp.ops[q.op].family == fam:
            nxt = lst[k + 1]
            prio[q.op] = (prio.get(anchor.op, anchor.setup_start) + prio.get(nxt.op, nxt.setup_start)) / 2.0
            return f"batch {cp.ops[q.op].id} after {cp.ops[anchor.op].id} (family {fam})"
    return ""


def _change_mode(cp, res, forced, rng) -> str:
    cands = [p for p in res.placements if p is not None and not p.fixed and len(cp.ops[p.op].modes) > 1]
    if not cands:
        return ""
    p = rng.choice(cands)
    op = cp.ops[p.op]
    alts = [m.idx for m in op.modes if m.idx != p.mode]
    forced[p.op] = rng.choice(alts)
    return f"move {op.id} to {cp.resources[op.modes[forced[p.op]].res].code}"
