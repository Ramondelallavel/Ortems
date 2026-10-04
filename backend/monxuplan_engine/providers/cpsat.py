"""CP-SAT provider (OR-Tools).

Model (see docs/OPTIMIZATION.md §3):

* mode choice ``x[o,m]`` (exactly one), optional intervals per mode;
* exact working-calendar stretch: for every mode, the piecewise-linear map start → end of the mode's
  effective calendar is encoded with segment literals (``lo ≤ S ≤ hi ∧ E = S + offset``);
* unary resources: NoOverlap, and — when setups are sequence dependent and the resource has at most
  ``circuit_max_ops`` free operations — AddCircuit with setup gaps on the chosen arcs;
* cumulative resources (labour pools, tools, multi-capacity machines): AddCumulative with fixed
  blocker intervals for reduced capacity and for operations outside the neighbourhood;
* precedences (FS/SS/FF/SF/overlap) with move/queue/wait/buffer lags;
* materials: AddReservoirConstraint (level never negative);
* objective: weighted sum with normalised integer coefficients, or lexicographic levels.

The solution (modes + per-resource sequences) is decoded by the exact schedule builder, which puts
setups inside working time and re-checks every HARD constraint. The CP model is a relaxation of the
exact problem in two places (setups as elapsed gaps; labour/tools not held during circuit setups),
so the reported status is ``OPTIMAL`` only when CP-SAT proved optimality of the full model *and* the
decoded schedule reproduces the model's times exactly; otherwise ``FEASIBLE`` with the model's gap.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from ..builder import BuildResult
from ..compile import CMode, CompiledProblem
from ..objectives import levels_of
from ..timing import Timing
from .base import Evaluator, OptimizationProvider, ProviderCapabilities, ProviderResult, SolveContext, decode
from .heuristic import HeuristicProvider

K_SCALE = 100_000  # integer scaling of normalised objective coefficients
QTY_SCALE = 1000  # material quantities → integers
BIG = 10**7


@dataclass
class Neighbourhood:
    free: set[int]
    lo: int
    hi: int
    label: str = "full"


@dataclass
class CpOutcome:
    status: str
    objective: float | None
    bound: float | None
    modes: dict[int, int] = field(default_factory=dict)
    starts: dict[int, int] = field(default_factory=dict)
    ends: dict[int, int] = field(default_factory=dict)
    wall: float = 0.0
    num_booleans: int = 0
    num_constraints: int = 0
    circuit_res: set[int] = field(default_factory=set)
    wall_limit_hit: bool = False


class _Model:
    def __init__(self, cp: CompiledProblem, ref: BuildResult | None, nb: Neighbourhood, ev: Evaluator, circuit_max: int) -> None:
        self.cp = cp
        self.ref = ref
        self.nb = nb
        self.ev = ev
        self.m = cp_model.CpModel()
        self.x: dict[tuple[int, int], cp_model.IntVar] = {}
        self.S: dict[int, cp_model.IntVar] = {}
        self.E: dict[int, cp_model.IntVar] = {}
        self.iv: dict[tuple[int, int], cp_model.IntervalVar] = {}
        self.Sm: dict[tuple[int, int], cp_model.IntVar] = {}
        self.R: dict[int, cp_model.IntVar] = {}  # run-start proxy (setup excluded) for precedences, release, material
        self.Em: dict[tuple[int, int], cp_model.IntVar] = {}
        self.circuit_max = circuit_max
        self.circuit_res: set[int] = set()
        self.setup_terms: list = []
        self.components: dict[str, list] = defaultdict(list)  # component -> list of (coef, expr) with exact units
        self.fixed_placements = {}
        from ..setups import state_key

        self.sk = [state_key(op.state) or () for op in cp.ops]
        if ref is not None:
            for p in ref.placements:
                if p is not None and p.op not in nb.free:
                    self.fixed_placements[p.op] = p

    # ------------------------------------------------------------------ helpers
    def _uses_circuit(self, res_idx: int, count: int) -> bool:
        return self.cp.resources[res_idx].unary and self.cp.setup.has_matrices(res_idx) and 2 <= count <= self.circuit_max

    def _base_setup(self, op_idx: int, mode: CMode) -> int:
        """Setup included in the interval when no circuit is used: the routing setup, or — with
        sequence-dependent matrices — the same-state changeover (an optimistic, i.e. relaxing, value)."""
        if not self.cp.setup.has_matrices(mode.res):
            return mode.setup_base
        sk = self.sk[op_idx]
        return self.cp.setup.setup(mode.res, sk, sk, mode.setup_base)

    def _duration(self, op_idx: int, mode: CMode, in_circuit: bool) -> int:
        if mode.sub:
            return mode.run
        return (0 if in_circuit else self._base_setup(op_idx, mode)) + mode.run + mode.teardown

    def _segments(self, mode: CMode, d: int, lo: int, hi: int, interruptible: bool) -> list[tuple[int, int, int]]:
        """(s_lo, s_hi, offset) such that for S in [s_lo, s_hi], E = S + offset on the mode calendar."""
        out: list[tuple[int, int, int]] = []
        if mode.sub:
            return [(lo, hi, d)]
        blocks = mode.cal.segments()
        if not blocks:
            return out
        # cumulative working minutes at block starts
        cs = [0]
        for a, b in blocks:
            cs.append(cs[-1] + (b - a))
        n = len(blocks)
        for i, (a_i, b_i) in enumerate(blocks):
            if b_i <= lo or a_i > hi:
                continue
            if d == 0:
                s_lo, s_hi = max(a_i, lo), min(b_i - 1, hi)
                if s_lo <= s_hi:
                    out.append((s_lo, s_hi, 0))
                continue
            if not interruptible:
                s_lo, s_hi = max(a_i, lo), min(b_i - d, hi)
                if s_lo <= s_hi:
                    out.append((s_lo, s_hi, d))
                continue
            for j in range(i, n):
                a_j, b_j = blocks[j]
                cs_j, ce_j = cs[j], cs[j + 1]
                off_lo = cs_j - d - cs[i] + 1
                off_hi = ce_j - d - cs[i]
                s_lo = a_i + max(0, off_lo)
                s_hi = a_i + min(b_i - a_i - 1, off_hi)
                s_lo, s_hi = max(s_lo, lo), min(s_hi, hi)
                if s_lo <= s_hi:
                    out.append((s_lo, s_hi, d + (a_j - cs_j) - (a_i - cs[i])))
                if cs_j - d - cs[i] > b_i - a_i:
                    break
        return out

    # ------------------------------------------------------------------ build
    def build(self, lower_bounds: dict[int, int], ref_hints: bool = True) -> bool:
        cp, m, nb = self.cp, self.m, self.nb
        H = nb.hi
        free = sorted(nb.free)
        # count free ops per unary resource with setup matrices (circuit decision)
        per_res: dict[int, set[int]] = defaultdict(set)
        for i in free:
            for md in cp.ops[i].modes:
                per_res[md.res].add(i)
        self.circuit_res = {r for r, s in per_res.items() if self._uses_circuit(r, len(s))}

        res_intervals: dict[int, list[cp_model.IntervalVar]] = defaultdict(list)
        cum_intervals: dict[int, list[tuple[cp_model.IntervalVar, int]]] = defaultdict(list)
        circuit_nodes: dict[int, list[tuple[int, int]]] = defaultdict(list)  # res -> (op, mode)

        for i in free:
            op = cp.ops[i]
            run_lb = lower_bounds.get(i, cp.work_lb)
            detached = op.interruptible and any(cp.resources[md.res].detached for md in op.modes)
            lb = max(cp.work_lb if detached else run_lb, nb.lo)
            if lb > H:
                return False
            S = m.NewIntVar(lb, H, f"S{i}")
            E = m.NewIntVar(lb, H + 10 * 1440, f"E{i}")
            R = m.NewIntVar(lb, H + 10 * 1440, f"R{i}")
            self.S[i], self.E[i], self.R[i] = S, E, R
            m.Add(run_lb <= R)
            lits = []
            offsets = []
            mode_off: dict[int, int] = {}
            for md in op.modes:
                in_circ = md.res in self.circuit_res
                d = self._duration(i, md, in_circ)
                segs = self._segments(md, d, lb, H, op.interruptible)
                if not segs:
                    continue
                x = m.NewBoolVar(f"x{i}_{md.idx}")
                self.x[(i, md.idx)] = x
                lits.append(x)
                s_lo = min(s[0] for s in segs)
                s_hi = max(s[1] for s in segs)
                Sm = m.NewIntVar(s_lo, s_hi, f"S{i}_{md.idx}")
                Em = m.NewIntVar(s_lo, s_hi + max(s[2] for s in segs), f"E{i}_{md.idx}")
                if len(segs) == 1:
                    s_lo_, s_hi_, off = segs[0]
                    m.Add(Em == Sm + off).OnlyEnforceIf(x)
                else:
                    ys = []
                    for k, (a, b, off) in enumerate(segs):
                        y = m.NewBoolVar(f"y{i}_{md.idx}_{k}")
                        ys.append(y)
                        m.Add(Sm >= a).OnlyEnforceIf(y)
                        m.Add(Sm <= b).OnlyEnforceIf(y)
                        m.Add(Em == Sm + off).OnlyEnforceIf(y)
                    m.AddExactlyOne(ys + [x.Not()])
                size = m.NewIntVar(0, H + 10 * 1440, f"z{i}_{md.idx}")
                m.Add(size == Em - Sm)
                itv = m.NewOptionalIntervalVar(Sm, size, Em, x, f"I{i}_{md.idx}")
                self.iv[(i, md.idx)] = itv
                self.Sm[(i, md.idx)], self.Em[(i, md.idx)] = Sm, Em
                m.Add(Sm == S).OnlyEnforceIf(x)
                m.Add(Em == E).OnlyEnforceIf(x)
                # the run starts after the setup included in the interval (none for circuit resources,
                # whose setup is a gap before S); detached setups may precede predecessors/material
                off = 0 if (in_circ or md.sub) else (self._base_setup(i, md) if (cp.resources[md.res].detached and op.interruptible) else 0)
                offsets.append((x, off))
                mode_off[md.idx] = off
                res = cp.resources[md.res]
                if res.finite:
                    if res.unary:
                        res_intervals[md.res].append(itv)
                        if in_circ:
                            circuit_nodes[md.res].append((i, md.idx))
                    else:
                        cum_intervals[md.res].append((itv, 1))
                for r2, u in md.sec:
                    cum_intervals[r2].append((itv, u))
                # objective: preference / cost / base setup for non-circuit resources
                if md.pref > 0:
                    self.components["preference"].append((md.pref * md.nominal, x))
                if md.cost_per_min or md.sub_cost:
                    self.components["cost"].append((md.nominal * md.cost_per_min + md.sub_cost, x))
                if not in_circ and not md.sub and res.unary:
                    base = self._base_setup(i, md)
                    if base:
                        self.components["setup"].append((base, x))
            if not lits:
                return False
            m.AddExactlyOne(lits)
            m.Add(S + sum(off * x for x, off in offsets if off) == R)
            rp = self.ref.placements[i] if (ref_hints and self.ref is not None) else None
            if rp is not None:
                for (oi, mi), x in self.x.items():
                    if oi == i:
                        m.AddHint(x, 1 if mi == rp.mode else 0)
                off = mode_off.get(rp.mode, 0)
                if rp.res in self.circuit_res:
                    hint_s = rp.start  # S is the run start; the setup is a gap before it
                elif off:
                    hint_s = rp.start - off  # detached setup: the interval holds only the base setup
                else:
                    hint_s = rp.setup_start
                m.AddHint(S, min(max(hint_s, lb), H))

        # ---- fixed operations outside the neighbourhood occupy capacity
        for p in self.fixed_placements.values():
            op = cp.ops[p.op]
            md = op.modes[p.mode]
            res = cp.resources[p.res]
            if p.end <= nb.lo - 1440 or p.setup_start >= H + 1440:
                continue
            itv = m.NewFixedSizeIntervalVar(p.setup_start, max(p.end - p.setup_start, 0), f"F{p.op}")
            if res.finite:
                if res.unary:
                    res_intervals[p.res].append(itv)
                else:
                    cum_intervals[p.res].append((itv, 1))
            for r2, u in md.sec:
                cum_intervals[r2].append((itv, u))

        # ---- unary resources
        for r, ivs in res_intervals.items():
            if len(ivs) > 1:
                m.AddNoOverlap(ivs)
        for r, nodes in circuit_nodes.items():
            self._circuit(r, nodes)

        # ---- cumulative resources with time-varying capacity
        for r, lst in cum_intervals.items():
            res = cp.resources[r]
            prof = res.profile
            cap_max = max((c for _, c in prof), default=0)
            if cap_max <= 0:
                return False
            blockers = []
            for k, (t, c) in enumerate(prof):
                e = prof[k + 1][0] if k + 1 < len(prof) else H + 20 * 1440
                a, b = max(t, nb.lo - 1), min(e, H + 20 * 1440)
                # closed periods (c == 0) are outside every effective calendar: operations pause
                # there, so only partial reductions (fewer operators, absences) become blockers
                if b > a and 0 < c < cap_max:
                    blockers.append((m.NewFixedSizeIntervalVar(a, b - a, f"B{r}_{k}"), cap_max - c))
            ivs = [iv for iv, _ in lst] + [iv for iv, _ in blockers]
            dem = [u for _, u in lst] + [u for _, u in blockers]
            m.AddCumulative(ivs, dem, cap_max)

        # ---- precedences (constrain the run start proxy R)
        for i in free:
            op = cp.ops[i]
            for pi, kind, lag, frac in op.preds:
                po = cp.ops[pi]
                gap = po.move + po.wait + op.queue + po.buf_after + op.buf_before + lag
                if pi in self.S:
                    Sa, Ea = self.R[pi], self.E[pi]
                elif pi in self.fixed_placements:
                    fp = self.fixed_placements[pi]
                    Sa, Ea = fp.start, fp.end
                else:
                    continue
                if kind == "FS":
                    m.Add(self.R[i] >= Ea + gap)
                elif kind == "SS":
                    m.Add(self.R[i] >= Sa + lag)
                elif kind == "OVL":
                    pr = min(md.run for md in po.modes) if po.modes else 0
                    my = min(md.run for md in op.modes) if op.modes else 0
                    m.Add(self.R[i] >= Sa + int(frac * pr) + po.move + lag)
                    m.Add(self.E[i] >= Ea + po.move + int(frac * my))
                elif kind == "FF":
                    m.Add(self.E[i] >= Ea + lag)
                else:
                    m.Add(self.E[i] >= Sa + lag)
            for si, kind, lag, _frac in op.succs:
                if si in self.S or si not in self.fixed_placements:
                    continue
                so = cp.ops[si]
                fp = self.fixed_placements[si]
                ready = fp.start if (cp.resources[fp.res].detached and so.interruptible) else fp.setup_start
                if kind == "FS":
                    m.Add(self.E[i] + op.move + op.wait + so.queue + op.buf_after + so.buf_before + lag <= ready)
                elif kind == "SS":
                    m.Add(self.R[i] + lag <= fp.start)

        # ---- materials (reservoir, level never negative)
        if cp.constraints.materials == "HARD":
            self._materials()

        # ---- objective components
        self._order_terms()
        return True

    def _circuit(self, r: int, nodes: list[tuple[int, int]]) -> None:
        cp, m = self.cp, self.m
        res = cp.resources[r]
        # initial state: last fixed job on the resource before the neighbourhood
        init_state = res.initial_state
        init_end = None
        for p in self.fixed_placements.values():
            if p.res == r and p.end <= self.nb.lo + 1:
                if init_end is None or p.end > init_end:
                    init_end, init_state = p.end, self.sk[p.op]
        arcs = []
        n = len(nodes)
        for a in range(n):
            ia, ma = nodes[a]
            xa = self.x[(ia, ma)]
            arcs.append((a + 1, a + 1, xa.Not()))
            lit0 = m.NewBoolVar(f"c0_{r}_{a}")
            arcs.append((0, a + 1, lit0))
            su0 = cp.setup.setup(r, init_state, self.sk[ia], cp.ops[ia].modes[ma].setup_base)
            if init_end is not None:
                m.Add(self.Sm[(ia, ma)] >= init_end + su0).OnlyEnforceIf(lit0)
            else:
                m.Add(self.Sm[(ia, ma)] >= cp.work_lb + su0).OnlyEnforceIf(lit0)
            if su0:
                self.components["setup"].append((su0, lit0))
            arcs.append((a + 1, 0, m.NewBoolVar(f"cE_{r}_{a}")))
            for b in range(n):
                if a == b:
                    continue
                ib, mb = nodes[b]
                if ia == ib:
                    continue
                lit = m.NewBoolVar(f"c{r}_{a}_{b}")
                arcs.append((a + 1, b + 1, lit))
                su = cp.setup.setup(r, self.sk[ia], self.sk[ib], cp.ops[ib].modes[mb].setup_base)
                m.Add(self.Sm[(ib, mb)] >= self.Em[(ia, ma)] + su).OnlyEnforceIf(lit)
                if su:
                    self.components["setup"].append((su, lit))
                if _forbidden(cp, r, self.sk[ia], self.sk[ib]):
                    m.Add(lit == 0)
        # the empty circuit (no node present) is allowed through the depot self loop
        arcs.append((0, 0, m.NewBoolVar(f"cempty_{r}")))
        m.AddCircuit(arcs)

    def _materials(self) -> None:
        cp, m = self.cp, self.m
        by_mat: dict[int, list] = defaultdict(list)
        for i in self.S:
            for mi, q in cp.ops[i].materials:
                by_mat[mi].append(("C", i, q))
            if cp.ops[i].produces is not None:
                mi, q = cp.ops[i].produces
                if cp.materials[mi].consumers:
                    by_mat[mi].append(("P", i, q))
        for mi, events in by_mat.items():
            times, deltas = [], []
            for t, q, *_ in cp.materials[mi].supplies:
                times.append(t)
                deltas.append(int(round(q * QTY_SCALE)))
            for p in self.fixed_placements.values():
                op = cp.ops[p.op]
                for mj, q in op.materials:
                    if mj == mi:
                        times.append(p.start)
                        deltas.append(-int(round(q * QTY_SCALE)))
                if op.produces is not None and op.produces[0] == mi:
                    times.append(p.end + op.move + op.wait)
                    deltas.append(int(round(op.produces[1] * QTY_SCALE)))
            for kind, i, q in events:
                if kind == "C":
                    times.append(self.R[i])
                    deltas.append(-int(round(q * QTY_SCALE)))
                else:
                    times.append(self.E[i] + cp.ops[i].move + cp.ops[i].wait)
                    deltas.append(int(round(q * QTY_SCALE)))
            total_in = sum(d for d in deltas if d > 0)
            if total_in + sum(d for d in deltas if d < 0) < 0:
                # not enough supply at all: the neighbourhood excludes shortage ops, so this is data
                continue
            m.AddReservoirConstraint(times, deltas, 0, max(total_in, 1))

    def _order_terms(self) -> None:
        cp, m = self.cp, self.m
        H = self.nb.hi + 10 * 1440
        ends_all = []
        for o in cp.orders:
            if not o.ops:
                continue
            last_vars = [self.E[i] for i in o.last_ops if i in self.E]
            if not last_vars:
                continue
            last_const = [self.fixed_placements[i].end for i in o.last_ops if i in self.fixed_placements]
            if len(last_vars) + len(last_const) < len(o.last_ops):
                continue  # some last op unscheduled in the reference → excluded
            C = m.NewIntVar(0, H, f"C{o.idx}")
            m.AddMaxEquality(C, last_vars + [int(c) for c in last_const])
            ends_all.append(C)
            target = o.due - o.safety
            T = m.NewIntVar(0, H, f"T{o.idx}")
            m.Add(C - target <= T)
            L = m.NewBoolVar(f"L{o.idx}")
            m.Add(target >= C).OnlyEnforceIf(L.Not())
            self.components["tardiness"].append((o.weight, T))
            self.components["late_orders"].append((o.weight, L))
            if o.critical:
                self.components["critical_tardiness"].append((o.weight, T))
            early = m.NewIntVar(0, H, f"Er{o.idx}")
            m.Add(early >= target - C)
            self.components["inventory"].append((o.qty / 60.0, early))
            starts = [self.S[i] for i in o.ops if i in self.S] + [self.fixed_placements[i].setup_start for i in o.ops if i in self.fixed_placements]
            if starts:
                F = m.NewIntVar(-H, H, f"F{o.idx}")
                m.AddMinEquality(F, starts)
                # flow time as an explicitly non-negative variable: keeps the relaxation (and so the
                # reported bound / gap) meaningful
                W = m.NewIntVar(0, 2 * H, f"W{o.idx}")
                m.Add(W == C - F)
                self.components["wip"].append((1.0, W))
        if ends_all:
            M = m.NewIntVar(0, H, "makespan")
            m.AddMaxEquality(M, ends_all)
            Ms = m.NewIntVar(0, H, "makespan_span")
            m.Add(Ms >= M - cp.as_of)
            self.components["makespan"].append((1.0, Ms))
        # stability versus baseline
        if cp.baseline:
            pen = cp.objectives.stability_resource_change_minutes
            for i in self.S:
                b = cp.baseline.get(i)
                if b is None:
                    continue
                b_res, _bss, b_start, _be = b
                dev = m.NewIntVar(0, H, f"dev{i}")
                m.AddAbsEquality(dev, self.S[i] - b_start)
                self.components["stability"].append((1.0, dev))
                for (oi, mi), x in self.x.items():
                    if oi == i and cp.ops[i].modes[mi].res != b_res:
                        self.components["stability"].append((pen, x))

    def component_expr(self, name: str):
        terms = self.components.get(name, [])
        return terms

    def objective(self, comps: list[str], weights: dict[str, float] | None) -> None:
        expr_terms = []
        for c in comps:
            w = (weights or {}).get(c, 1.0)
            if not w:
                continue
            scale = self.ev.scale.get(c, 1.0)
            for coef, e in self.components.get(c, []):
                k = int(round(K_SCALE * w * coef / scale))
                if k:
                    expr_terms.append(k * e)
        self.m.Minimize(sum(expr_terms) if expr_terms else 0)
        self._objective_terms = expr_terms


def _forbidden(cp: CompiledProblem, r: int, ska, skb) -> bool:
    from ..setups import matches

    for rs, pm, nm, _ in cp.forbidden:
        if rs is not None and r not in rs:
            continue
        if matches(ska, pm) and matches(skb, nm):
            return True
    return False


def lower_bounds(cp: CompiledProblem, ref: BuildResult | None) -> dict[int, int]:
    out = {}
    for op in cp.ops:
        o = cp.orders[op.order]
        lb = cp.work_lb
        if o.release is not None:
            lb = max(lb, o.release)
        if op.earliest is not None:
            lb = max(lb, op.earliest)
        out[op.idx] = lb
    return out


def solve_neighbourhood(cp: CompiledProblem, ref: BuildResult | None, nb: Neighbourhood, ev: Evaluator, time_limit: float, seed: int, workers: int, reproducible: bool, circuit_max: int) -> CpOutcome:
    mdl = _Model(cp, ref, nb, ev, circuit_max)
    ok = mdl.build(lower_bounds(cp, ref), ref_hints=True)
    if not ok:
        return CpOutcome("MODEL_INVALID", None, None)
    spec = cp.objectives
    solver = cp_model.CpSolver()
    params = solver.parameters
    params.random_seed = seed
    params.num_workers = workers
    params.repair_hint = True
    params.max_time_in_seconds = max(time_limit, 0.5)
    if reproducible:
        # deterministic: one worker, fixed seed, deterministic-time budget; wall time is only a safety
        # net (hitting it is reported because the result may then differ between runs). Interleaved
        # multi-worker search is avoided on purpose: combined with solution hints it can abort inside
        # OR-Tools (heuristics.fixed_search check).
        params.num_workers = 1
        # repair_hint in single-worker mode triggers the same OR-Tools check (no fixed search is
        # instantiated for the lone worker); the hint is still used as the first branching guide.
        params.repair_hint = False
        params.max_deterministic_time = max(time_limit * 0.5, 0.25)
        # the user's time limit is a hard wall-clock cap; hitting it before the deterministic budget
        # is reported (the result may then differ between runs on machines of different speed)
        params.max_time_in_seconds = max(time_limit, 0.5)
    params.log_search_progress = False
    if spec.mode == "LEXICOGRAPHIC":
        levels = levels_of(spec)
        per_level = max(time_limit / max(len(levels), 1), 0.3)
        status = None
        for lvl in levels:
            mdl.objective(lvl, None)
            params.max_time_in_seconds = per_level
            if reproducible:
                params.max_deterministic_time = per_level * 0.5
            status = solver.Solve(mdl.m)
            if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                break
            best = int(solver.ObjectiveValue())
            tol = spec.tolerance
            if mdl._objective_terms:
                mdl.m.Add(sum(mdl._objective_terms) <= int(math.floor(best * (1 + tol))))
            # warm start next level
            mdl.m.ClearHints()
            for key, x in mdl.x.items():
                mdl.m.AddHint(x, solver.Value(x))
            for i, S in mdl.S.items():
                mdl.m.AddHint(S, solver.Value(S))
    else:
        comps = [c for c, w in spec.weights.items() if w]
        mdl.objective(comps, spec.weights)
        status = solver.Solve(mdl.m)
    st = {cp_model.OPTIMAL: "OPTIMAL", cp_model.FEASIBLE: "FEASIBLE", cp_model.INFEASIBLE: "INFEASIBLE", cp_model.MODEL_INVALID: "MODEL_INVALID"}.get(status, "UNKNOWN")
    out = CpOutcome(st, None, None, wall=solver.WallTime(), circuit_res=set(mdl.circuit_res))
    out.wall_limit_hit = reproducible and solver.WallTime() >= params.max_time_in_seconds * 0.98
    out.num_booleans = solver.NumBooleans()
    out.num_constraints = len(mdl.m.Proto().constraints)
    if st in ("OPTIMAL", "FEASIBLE"):
        out.objective = solver.ObjectiveValue()
        out.bound = solver.BestObjectiveBound()
        for (i, mi), x in mdl.x.items():
            if solver.Value(x):
                out.modes[i] = mi
                out.starts[i] = solver.Value(mdl.S[i])
                out.ends[i] = solver.Value(mdl.E[i])
    return out


def decode_outcome(cp: CompiledProblem, timing: Timing, ref: BuildResult | None, out: CpOutcome, explain: bool = False) -> BuildResult:
    """Materialise a CP outcome with the exact builder (modes + per-resource sequences)."""
    prio: dict[int, float] = {}
    forced: dict[int, int] = {}
    if ref is not None:
        for p in ref.placements:
            if p is not None:
                prio[p.op] = float(p.setup_start)
                forced[p.op] = p.mode
    for i, mi in out.modes.items():
        forced[i] = mi
        prio[i] = float(out.starts[i])
    chains: dict[int, list[int]] = defaultdict(list)
    for i, mi in forced.items():
        r = cp.ops[i].modes[mi].res
        if cp.resources[r].unary:
            chains[r].append(i)
    for r in chains:
        chains[r].sort(key=lambda i: (prio[i], i))
    return decode(cp, timing, rules=cp.solver.dispatch_rules, priority=prio, forced=forced, chains=dict(chains), explain=explain)


def is_exact(cp: CompiledProblem, dec: BuildResult, out: CpOutcome) -> bool:
    """True when the builder reproduced the CP model's times for every operation of the model."""
    for i, mi in out.modes.items():
        p = dec.placements[i]
        if p is None or p.mode != mi or p.end != out.ends[i]:
            return False
        start = p.start if cp.ops[i].modes[mi].res in out.circuit_res else p.setup_start
        if start != out.starts[i]:
            return False
    return True


class CpSatProvider(OptimizationProvider):
    """Full CP-SAT model (warm-started with the heuristic). Larger problems go through LNS."""

    name = "cpsat"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(detailed_scheduling=True, proves_optimality=True, max_recommended_ops=400)

    def solve(self, cp: CompiledProblem, timing: Timing, ctx: SolveContext) -> ProviderResult:
        if len(cp.ops) > cp.solver.cpsat_max_ops:
            from .hybrid import HybridProvider

            ctx.messages.append(f"{len(cp.ops)} operations > cpsat_max_ops={cp.solver.cpsat_max_ops}: solved by CP-SAT large-neighbourhood search")
            res = HybridProvider().solve(cp, timing, ctx)
            res.details["delegated"] = "hybrid-lns"
            return res
        return full_model(cp, timing, ctx)


def full_model(cp: CompiledProblem, timing: Timing, ctx: SolveContext, heuristic_share: float = 0.15) -> ProviderResult:
    total = ctx.time_limit_s
    sub = SolveContext(time_limit_s=max(total * heuristic_share, 0.5), seed=ctx.seed, progress=ctx.progress, cancelled=ctx.cancelled)
    # heuristic warm start, with its local search: the warm start is both CP-SAT's hint and the plan
    # kept when CP-SAT does not beat it, so the result is never worse than what the heuristic alone
    # finds in this share of the budget (the local search stops on convergence or on the share)
    h = HeuristicProvider().solve(cp, timing, sub)
    ev = Evaluator(cp)
    ev.scale = h.details["scales"]
    ref = h.result
    free = {p.op for p in ref.placements if p is not None and not p.fixed}
    if not free:
        return h
    horizon_hi = max([p.end for p in ref.placements if p is not None] + [cp.h_end]) + 2 * 1440
    nb = Neighbourhood(free=free, lo=cp.work_lb, hi=horizon_hi, label="full")
    ctx.report("Optimizing", 0.2, f"CP-SAT full model: {len(free)} operations")
    out = solve_neighbourhood(cp, ref, nb, ev, ctx.remaining(), ctx.seed, cp.solver.workers, cp.solver.reproducible, cp.solver.circuit_max_ops)
    if out.wall_limit_hit:
        ctx.messages.append("CP-SAT stopped on the wall-clock safety limit: this result may not be exactly reproducible")
    details = {
        "cp_objective_units": "normalised objective × 100000",
        "cp_status": out.status,
        "model_objective": out.objective,
        "model_bound": out.bound,
        "cp_wall_time_s": round(out.wall, 3),
        "cp_booleans": out.num_booleans,
        "cp_constraints": out.num_constraints,
        "scales": ev.scale,
        "warm_start": "heuristic",
    }
    if out.status not in ("OPTIMAL", "FEASIBLE"):
        ctx.messages.append(f"CP-SAT returned {out.status}; heuristic schedule kept")
        h.details.update(details)
        return h
    dec = decode_outcome(cp, timing, ref, out, explain=False)
    comp, vec = ev.evaluate(dec)
    h_comp, h_vec = ev.evaluate(ref)
    exact = is_exact(cp, dec, out)
    overtime_exact = not cp.objectives.weights.get("overtime") or comp.get("overtime", 0) == 0
    if len(dec.unscheduled) > len(ref.unscheduled) or ev.better(h_vec, vec):
        ctx.messages.append("decoded CP-SAT schedule not better than the heuristic warm start; heuristic kept")
        best, best_comp, best_vec = ref, h_comp, h_vec
    else:
        best, best_comp, best_vec = dec, comp, vec
    # Bound and gap are reported on the scale of the returned objective. In weighted mode the CP
    # objective is the normalised objective × K_SCALE (rounded coefficients), so bound / K_SCALE is a
    # valid lower bound of the reported value; the gap refers to the schedule actually returned. In
    # lexicographic mode the bound belongs to the last level only and is not reported.
    gap = None
    bound_report = None
    weighted = cp.objectives.mode != "LEXICOGRAPHIC"
    if weighted and out.bound is not None and len(best_vec) >= 2 and best_vec[0] == 0:
        bound_report = round(out.bound / K_SCALE, 6)
        value = best_vec[1]
        if abs(value) > 1e-9:
            gap = max(0.0, (value - bound_report) / abs(value))
    proven = out.status == "OPTIMAL" and best is dec and exact and overtime_exact
    status = "OPTIMAL" if proven else "FEASIBLE"
    if out.status == "OPTIMAL" and not proven:
        ctx.messages.append("CP-SAT proved the model optimal, but the exact re-timing differs from the model (setups/overlaps in working time): reported as FEASIBLE")
    if cp.solver.explain:
        best = decode_outcome(cp, timing, ref, out, explain=True) if best is dec else decode(cp, timing, explain=True, rules=h.details.get("rules"))
    details["decoded_exact"] = exact
    return ProviderResult(
        result=best,
        status=status,
        components=best_comp,
        vector=best_vec,
        best_bound=bound_report,
        gap=round(gap, 6) if gap is not None else None,
        proven_optimal=proven,
        iterations=h.iterations + 1,
        details=details,
    )
