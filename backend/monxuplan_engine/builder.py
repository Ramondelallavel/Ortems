"""Exact, calendar-aware schedule builder (serial schedule-generation scheme).

The builder is the single place where operations are given concrete times. Every provider
(dispatching heuristic, local search, CP-SAT decoding, LNS, repair, manual moves) produces
*decisions* — priorities, modes, per-resource sequences, fixed placements — and the builder turns them
into a schedule that satisfies all HARD constraints:

* modes / compatibility, effective calendars (primary ∩ labour ∩ tools − maintenance),
* unary capacity with sequence-dependent setups (inserting into a gap re-checks the successor's setup),
* cumulative capacity (labour pools, tools, multi-capacity machines),
* precedences (FS/SS/FF/SF, lags, overlap/transfer batches, move/queue/wait, buffers),
* materials (ledger: no stock is ever created), releases, frozen zone, fixed operations,
* sequence rules (forbidden immediate transitions, locked sequences).

For each operation it records *why* it was placed there: the binding constraint and every evaluated
alternative with its finish time or the constraint that blocked it. Operations that cannot be placed
are returned as unscheduled with an explicit reason — never forced into an infeasible position.
"""

from __future__ import annotations

import heapq
from collections.abc import Sequence
from dataclasses import dataclass, field

from .compile import CMode, CompiledProblem, COp
from .contract import ModeSelection
from .materials import MaterialLedger
from .setups import matches
from .timelines import POS_INF, Block, CumulativeTimeline, UnaryTimeline
from .timing import Timing, compute_timing, initial_ledger

POS = 1 << 50
MAX_SLOT_ITERATIONS = 20000
DYNAMIC_RULES = {"SHORTEST_SETUP", "FAMILY_GROUPING", "MATERIAL_AVAILABILITY"}


# =============================================================================================
# Records
# =============================================================================================


class Push:
    """A constraint that moved the candidate start of an operation forward."""

    __slots__ = ("type", "ref", "detail", "at", "data")

    def __init__(self, type_: str, ref: str | None, at: int, detail: str | None = None, data: dict | None = None) -> None:
        self.type = type_
        self.ref = ref
        self.at = at
        self.detail = detail
        self.data = data or {}


class Slot:
    __slots__ = ("mode", "feasible", "setup_start", "start", "end", "setup", "prev_op", "pushes", "reason", "detail", "score", "next_adjust")

    def __init__(self, mode: CMode) -> None:
        self.mode = mode
        self.feasible = False
        self.setup_start = 0
        self.start = 0
        self.end = 0
        self.setup = 0
        self.prev_op: int | None = None
        self.pushes: list[Push] = []
        self.reason: str | None = None
        self.detail: str | None = None
        self.score: tuple = ()
        self.next_adjust: tuple[int, int] | None = None  # (new setup start, new setup) of the next job


class Placement:
    __slots__ = (
        "op", "mode", "res", "setup_start", "start", "end", "setup", "prev_op", "binding", "lb", "mat_ready",
        "mat_wait", "res_wait", "alternatives", "fixed", "fixed_reason", "overtime", "cost", "shortage", "earliest", "lb_src",
    )

    def __init__(self) -> None:
        self.alternatives: list[Slot] = []
        self.fixed = False
        self.fixed_reason: str | None = None
        self.mat_ready: int | None = None
        self.mat_wait = 0
        self.res_wait = 0
        self.overtime = 0
        self.cost = 0.0
        self.shortage: list[tuple[int, float, float]] = []  # (material, required, available)
        self.prev_op: int | None = None
        self.earliest = 0  # earliest allowed setup start (release, predecessors, material)
        self.lb_src: BindingRec | None = None  # what determined the earliest start (predecessor, material…)


class BindingRec:
    __slots__ = ("type", "ref", "detail", "at", "wait", "data")

    def __init__(self, type_: str, ref: str | None = None, detail: str | None = None, at: int | None = None, wait: int = 0, data: dict | None = None):
        self.type = type_
        self.ref = ref
        self.detail = detail
        self.at = at
        self.wait = wait
        self.data = data or {}


@dataclass
class Unsched:
    op: int
    reason: str
    message: str
    details: dict = field(default_factory=dict)


@dataclass
class BuildConfig:
    rules: Sequence[str] = ("HYBRID_APS",)
    priority: dict[int, float] | None = None  # explicit priority (lower first) — overrides rules
    forced_modes: dict[int, int] | None = None
    resource_chains: dict[int, list[int]] | None = None  # explicit sequence per resource
    overrides: dict[int, tuple[int, int, int | None, str]] | None = None  # op -> (mode, setup_start, end, reason)
    targets: dict[int, int] | None = None  # extra lower bound per op (backward / JIT targets)
    mode_selection: ModeSelection = field(default_factory=ModeSelection)
    explain: bool = True
    lookahead: int = 8
    slack_bucket: int = 480
    direction: str = "FORWARD"
    hybrid_buffer: int = 2 * 1440


@dataclass
class BuildResult:
    cp: CompiledProblem
    timing: Timing
    placements: list[Placement | None]
    unscheduled: dict[int, Unsched]
    unary: dict[int, UnaryTimeline]
    cumulative: dict[int, CumulativeTimeline]
    ledger: MaterialLedger
    config: BuildConfig
    fixed_conflicts: list[dict] = field(default_factory=list)

    def order_completion(self, oi: int) -> int | None:
        o = self.cp.orders[oi]
        if not o.last_ops:
            return None
        ends = []
        for i in o.last_ops:
            p = self.placements[i]
            if p is None:
                return None
            ends.append(p.end)
        return max(ends)


# =============================================================================================
# Builder
# =============================================================================================


class ScheduleBuilder:
    def __init__(self, cp: CompiledProblem, config: BuildConfig | None = None, timing: Timing | None = None) -> None:
        self.cp = cp
        self.cfg = config or BuildConfig(rules=tuple(cp.solver.dispatch_rules), mode_selection=cp.solver.mode_selection, direction=cp.solver.direction)
        self.timing = timing or compute_timing(cp)
        n = len(cp.ops)
        self.placements: list[Placement | None] = [None] * n
        self.unscheduled: dict[int, Unsched] = {}
        self.unary: dict[int, UnaryTimeline] = {}
        self.cumulative: dict[int, CumulativeTimeline] = {}
        for r in cp.resources:
            if not r.finite:
                continue
            if r.unary:
                self.unary[r.idx] = UnaryTimeline(r.idx)
            else:
                self.cumulative[r.idx] = CumulativeTimeline(r.idx, r.profile)
        self.ledger = initial_ledger(cp)
        self.use_materials = cp.constraints.materials != "IGNORE"
        self.last_state: dict[int, tuple[int, object, str | None]] = {}  # res -> (end, state_key, family)
        self.fixed_conflicts: list[dict] = []
        self.chain_prev: dict[int, int] = {}
        if self.cfg.resource_chains:
            for _r, seq in self.cfg.resource_chains.items():
                for a, b in zip(seq, seq[1:], strict=False):
                    self.chain_prev[b] = a
        self._bottleneck_score = self._compute_bottleneck_scores()
        self._allow_defer = True
        self._state_keys = [_freeze(op.state) for op in cp.ops]

    # ------------------------------------------------------------------ public API
    def build(self) -> BuildResult:
        cp = self.cp
        self._place_fixed()
        n = len(cp.ops)
        indeg = [0] * n
        for op in cp.ops:
            if self.placements[op.idx] is not None:
                continue
            cnt = 0
            for p, *_ in op.preds:
                if self.placements[p] is None:
                    cnt += 1
            if op.idx in self.chain_prev and self.placements[self.chain_prev[op.idx]] is None:
                cnt += 1
            indeg[op.idx] = cnt
        chain_next: dict[int, int] = {a: b for b, a in self.chain_prev.items()}
        heap: list[tuple] = []
        seq = 0
        for op in cp.ops:
            if self.placements[op.idx] is None and indeg[op.idx] == 0 and not op.cycle:
                heapq.heappush(heap, (self._static_key(op), seq, op.idx))
                seq += 1
        deferred: list[tuple] = []
        while heap or deferred:
            if not heap:
                # only deferred operations left: place them without further deferral
                self._allow_defer = False
                for d in deferred:
                    heapq.heappush(heap, d)
                deferred = []
            i = self._select(heap)
            op = cp.ops[i]
            ok = self._place(op)
            if ok is None:
                deferred.append((self._static_key(op), seq, i))
                seq += 1
                continue
            if not ok:
                self._propagate_unscheduled(i)
                continue
            if deferred:
                for d in deferred:
                    heapq.heappush(heap, d)
                deferred = []
            succs = [s for s, *_ in op.succs]
            if i in chain_next:
                succs.append(chain_next[i])
            for s in succs:
                if self.placements[s] is not None or s in self.unscheduled:
                    continue
                indeg[s] -= 1
                if indeg[s] == 0 and not cp.ops[s].cycle:
                    heapq.heappush(heap, (self._static_key(cp.ops[s]), seq, s))
                    seq += 1
        # anything left: cycles or blocked by an unscheduled predecessor
        for op in cp.ops:
            if self.placements[op.idx] is None and op.idx not in self.unscheduled:
                if op.cycle:
                    self.unscheduled[op.idx] = Unsched(op.idx, "PRECEDENCE_CYCLE", f"{op.id} is part of a precedence cycle", {})
                else:
                    blocker = next((p for p, *_ in op.preds if self.placements[p] is None), None)
                    ref = cp.ops[blocker].id if blocker is not None else None
                    self.unscheduled[op.idx] = Unsched(
                        op.idx, "PREDECESSOR_UNSCHEDULED", f"{op.id} waits for {ref}, which could not be scheduled", {"predecessor": ref}
                    )
        return BuildResult(
            cp=cp,
            timing=self.timing,
            placements=self.placements,
            unscheduled=self.unscheduled,
            unary=self.unary,
            cumulative=self.cumulative,
            ledger=self.ledger,
            config=self.cfg,
            fixed_conflicts=self.fixed_conflicts,
        )

    # ------------------------------------------------------------------ fixed operations
    def _place_fixed(self) -> None:
        cp = self.cp
        fixed: list[tuple[int, int, int, int | None, str, int | None]] = []
        for op in cp.ops:
            ov = self.cfg.overrides.get(op.idx) if self.cfg.overrides else None
            if ov is not None:
                mi, s, e, reason = ov
                fixed.append((s, op.idx, mi, e, reason, None))
            elif op.fixed is not None:
                mi, s, e, reason, su = op.fixed
                fixed.append((s, op.idx, mi, e, reason, su))
        fixed.sort()
        for s, i, mi, e, reason, su in fixed:
            op = cp.ops[i]
            if mi >= len(op.modes):
                continue
            m = op.modes[mi]
            res = cp.resources[m.res]
            in_progress = op.status == "IN_PROGRESS"
            prev_op = None
            if su is None:
                if res.unary:
                    tl = self.unary[m.res]
                    k = tl.prev_index(s)
                    prev_state = tl.blocks[k].state_key if k >= 0 else res.initial_state
                    prev_op = tl.blocks[k].op if k >= 0 else None
                    su = 0 if in_progress else cp.setup.setup(res.idx, prev_state, self._state_keys[i], m.setup_base)
                else:
                    su = 0 if in_progress else m.setup_base
            if e is None:
                if m.sub:
                    e = s + m.run
                elif in_progress:
                    e = m.cal.add_work(max(s, cp.as_of), m.run + m.teardown) or cp.hi
                else:
                    e = m.cal.add_work(s, su + m.run + m.teardown) or cp.hi
            start = s if su == 0 else (m.cal.next_work(m.cal.add_work(s, su) or s) or s)
            if in_progress:
                start = s
            self._commit(op, m, s, start, e, su, prev_op, fixed_reason=reason, check_conflicts=True)
            pl = self.placements[i]
            assert pl is not None
            pl.binding = BindingRec("FIXED", reason, f"{reason.lower()} by {'MES actuals' if in_progress else 'planner/frozen zone'}", s)

    # ------------------------------------------------------------------ dispatching
    def _static_key(self, op: COp) -> tuple:
        cfg = self.cfg
        if cfg.priority is not None:
            return (cfg.priority.get(op.idx, POS), op.order, op.seq)
        key: list = []
        for rule in self._expanded_rules():
            if rule in DYNAMIC_RULES:
                break
            key.append(self._rule_value(rule, op))
        key.append(op.order)
        key.append(op.seq)
        return tuple(key)

    def _expanded_rules(self) -> list[str]:
        out: list[str] = []
        for r in self.cfg.rules:
            if r == "HYBRID_APS":
                out += ["_EXPEDITE", "_WEIGHTED_SLACK_BUCKET", "SHORTEST_SETUP", "MIN_SLACK", "EDD"]
            else:
                out.append(r)
        if not out:
            out = ["EDD"]
        return out

    def _rule_value(self, rule: str, op: COp):
        cp, tm = self.cp, self.timing
        o = cp.orders[op.order]
        if rule == "EDD":
            return o.due - o.safety
        if rule == "EDD_PRIORITY":
            return (-o.priority, o.due - o.safety)
        if rule == "DUE_DATE_FIRST":
            return tm.lst[op.idx]
        if rule == "SPT":
            return tm.nominal[op.idx]
        if rule == "LPT":
            return -tm.nominal[op.idx]
        if rule == "FIFO":
            return (o.release if o.release is not None else -POS, o.idx)
        if rule == "CRITICAL_RATIO":
            return (o.due - cp.as_of) / max(tm.tail[op.idx], 1)
        if rule == "MIN_SLACK":
            return (tm.lst[op.idx] - cp.as_of) // 60
        if rule == "CUSTOMER_PRIORITY":
            return (-o.customer_priority, 0 if o.strategic else 1)
        if rule == "BOTTLENECK_FIRST":
            return -self._bottleneck_score[op.idx]
        if rule == "_EXPEDITE":
            return 0 if o.expedite else 1
        if rule == "_WEIGHTED_SLACK_BUCKET":
            slack = tm.lst[op.idx] - cp.as_of
            ws = slack / o.weight if slack >= 0 else slack * o.weight
            return int(ws // self.cfg.slack_bucket)
        if rule == "SHORTEST_SETUP":
            return self._setup_estimate(op)
        if rule == "FAMILY_GROUPING":
            return self._family_break(op)
        if rule == "MATERIAL_AVAILABILITY":
            return self._material_time(op)
        return 0

    def _select(self, heap: list[tuple]) -> int:
        rules = self._expanded_rules()
        if self.cfg.priority is not None or not any(r in DYNAMIC_RULES for r in rules):
            return heapq.heappop(heap)[2]
        first = heapq.heappop(heap)
        prefix = first[0][:-2]
        cands = [first]
        while heap and len(cands) < self.cfg.lookahead and heap[0][0][:-2] == prefix:
            cands.append(heapq.heappop(heap))
        if len(cands) == 1:
            return first[2]
        k = len(prefix)
        tail_rules = rules[k:]

        def full_key(entry):
            op = self.cp.ops[entry[2]]
            return tuple(self._rule_value(r, op) for r in tail_rules) + (entry[1],)

        cands.sort(key=full_key)
        best = cands[0]
        for c in cands[1:]:
            heapq.heappush(heap, c)
        return best[2]

    def _setup_estimate(self, op: COp) -> int:
        best = POS
        sk = self._state_keys[op.idx]
        for m in op.modes:
            res = self.cp.resources[m.res]
            if not res.unary:
                best = min(best, m.setup_base)
                continue
            last = self.last_state.get(m.res)
            prev = last[1] if last else res.initial_state
            best = min(best, self.cp.setup.setup(res.idx, prev, sk, m.setup_base) + m.pref)
        return best

    def _family_break(self, op: COp) -> int:
        fam = op.family
        for m in sorted(op.modes, key=lambda m: m.pref):
            last = self.last_state.get(m.res)
            if last and last[2] == fam and fam is not None:
                return 0
        return 1

    def _material_time(self, op: COp) -> int:
        t = self.cp.work_lb
        for mi, q in op.materials:
            e = self.ledger.earliest(mi, q, self.cp.work_lb)
            t = max(t, e if e is not None else POS)
        return t

    def _compute_bottleneck_scores(self) -> list[float]:
        cp, tm = self.cp, self.timing
        horizon = max(cp.h_end - cp.as_of, 1)
        req: dict[int, float] = {}
        for op in cp.ops:
            if not op.modes:
                continue
            share = 1.0 / len(op.modes)
            for m in op.modes:
                req[m.res] = req.get(m.res, 0.0) + share * m.nominal
        util: dict[int, float] = {}
        for r in cp.resources:
            cap = r.cal.working_between(cp.as_of, cp.as_of + horizon) * max(r.capacity, 1)
            util[r.idx] = req.get(r.idx, 0.0) / cap if cap > 0 else 0.0
        _ = tm
        return [max((util.get(m.res, 0.0) for m in op.modes), default=0.0) for op in cp.ops]

    # ------------------------------------------------------------------ placement
    def _lower_bound(self, op: COp) -> tuple[int, BindingRec, int]:
        """Earliest start ignoring the operation's own resources, and the end lower bound."""
        cp = self.cp
        order = cp.orders[op.order]
        lb = cp.work_lb
        src = BindingRec("FROZEN_ZONE" if cp.frozen_until is not None and lb == cp.frozen_until and lb > cp.as_of else "HORIZON_START", at=lb)
        if order.release is not None and order.release > lb:
            lb, src = order.release, BindingRec("RELEASE", order.number, "order release date", order.release)
        if op.earliest is not None and op.earliest > lb:
            lb, src = op.earliest, BindingRec("RELEASE", op.id, "operation earliest start", op.earliest)
        tg = self.cfg.targets.get(op.idx) if self.cfg.targets else None
        if tg is not None and tg > lb:
            lb, src = tg, BindingRec("RELEASE", op.id, "just-in-time target (backward scheduling)", tg)
        end_lb = -POS
        for p, kind, lag, frac in op.preds:
            pp = self.placements[p]
            if pp is None:
                continue
            po = cp.ops[p]
            if kind == "FS":
                t = pp.end + po.move + po.wait + op.queue + po.buf_after + op.buf_before + lag
            elif kind == "SS":
                t = pp.start + lag
            elif kind == "OVL":
                pm = po.modes[pp.mode]
                part = int(frac * pm.run)
                t = (pm.cal.add_work(pp.start, part) or pp.end) + po.move + lag
                my_run = min((m.run for m in op.modes), default=0)
                end_lb = max(end_lb, pp.end + po.move + int(frac * my_run))
            elif kind == "FF":
                end_lb = max(end_lb, pp.end + lag)
                continue
            else:  # SF
                end_lb = max(end_lb, pp.start + lag)
                continue
            if t > lb:
                lb, src = t, BindingRec("PREDECESSOR", po.id, f"{kind} after {po.id}", t)
        cprev = self.chain_prev.get(op.idx)
        if cprev is not None:
            pp = self.placements[cprev]
            if pp is not None and pp.end > lb:
                lb, src = pp.end, BindingRec("RESOURCE", cp.ops[cprev].id, "sequence on resource", pp.end)
        return lb, src, end_lb

    def _place(self, op: COp) -> bool | None:
        cp = self.cp
        if not op.modes:
            self.unscheduled[op.idx] = Unsched(op.idx, "NO_COMPATIBLE_RESOURCE", f"{op.id} has no compatible resource (check routing / planning rules)", {})
            return False
        lb, src, end_lb = self._lower_bound(op)
        base_lb = lb
        # ---- materials
        mat_ready = lb
        mat_ref: tuple[int, int] | None = None
        shortage: list[tuple[int, float, float]] = []
        if self.use_materials and op.materials:
            for mi, q in op.materials:
                t = self.ledger.earliest(mi, q, lb)
                if t is None:
                    avail = self.ledger.accounts[mi].max_available_after(lb)
                    shortage.append((mi, q, max(avail, 0.0)))
                    continue
                if t > mat_ready:
                    mat_ready, mat_ref = t, (mi, t)
            if shortage and cp.constraints.materials == "HARD":
                details = [
                    {
                        "material_id": cp.materials[mi].id,
                        "material": cp.materials[mi].code,
                        "required": q,
                        "available": round(a, 6),
                        "shortfall": round(q - a, 6),
                        "uom": cp.materials[mi].uom,
                        "replenishment_lead_time_minutes": cp.materials[mi].lead,
                    }
                    for mi, q, a in shortage
                ]
                names = ", ".join(f"{d['material']} (need {d['required']:g}, available {d['available']:g})" for d in details)
                self.unscheduled[op.idx] = Unsched(op.idx, "MATERIAL_SHORTAGE", f"{op.id} cannot start: material shortage {names}", {"materials": details})
                return False
        lb = max(lb, mat_ready)
        # ---- modes
        forced = self.cfg.forced_modes.get(op.idx) if self.cfg.forced_modes else None
        evaluate_all = self.cfg.explain or forced is None
        slots: list[Slot] = []
        for m in op.modes:
            if not evaluate_all and m.idx != forced:
                continue
            detached = cp.resources[m.res].detached and op.interruptible
            slots.append(self._slot(op, m, lb, end_lb, setup_lb=cp.work_lb if detached else None))
        feasible = [s for s in slots if s.feasible]
        if forced is not None:
            chosen = next((s for s in slots if s.mode.idx == forced and s.feasible), None)
            if chosen is None and feasible:
                chosen = self._choose(op, feasible)
        else:
            chosen = self._choose(op, feasible) if feasible else None
        if chosen is None and self._allow_defer and slots and all(s.reason == "SEQUENCE_RULE" for s in slots):
            return None  # try again once another job has been placed on the resource
        if chosen is None:
            reasons = [{"resource": cp.resources[s.mode.res].code, "reason": s.reason, "detail": s.detail} for s in slots]
            text = "; ".join(f"{r['resource']}: {r['detail'] or r['reason']}" for r in reasons)
            self.unscheduled[op.idx] = Unsched(op.idx, "NO_FEASIBLE_SLOT", f"{op.id} cannot be placed on any resource — {text}", {"modes": reasons})
            return False
        self._commit(op, chosen.mode, chosen.setup_start, chosen.start, chosen.end, chosen.setup, chosen.prev_op)
        pl = self.placements[op.idx]
        assert pl is not None
        pl.lb = base_lb
        pl.earliest = cp.work_lb if (cp.resources[chosen.mode.res].detached and op.interruptible) else lb
        pl.mat_ready = mat_ready if (op.materials and self.use_materials) else None
        pl.mat_wait = max(0, mat_ready - base_lb)
        pl.res_wait = max(0, chosen.setup_start - lb)
        pl.shortage = shortage
        pl.alternatives = slots if self.cfg.explain else []
        cal = chosen.mode.cal
        if mat_ref is not None and mat_ready > base_lb:
            mi, t = mat_ref
            lb_src = BindingRec("MATERIAL", cp.materials[mi].id, f"{cp.materials[mi].code} available", t, cal.working_between(base_lb, mat_ready))
        else:
            lb_src = src
        pl.lb_src = lb_src
        if chosen.setup_start > lb and chosen.pushes:
            # waiting is measured in working minutes of the operation's calendar (nights and
            # weekends are not "waiting for a resource")
            wait = cal.working_between(lb, chosen.setup_start) if not chosen.mode.sub else chosen.setup_start - lb
            last = chosen.pushes[-1]
            if wait == 0:
                pl.binding = BindingRec("CALENDAR", None, "next working time", chosen.setup_start, 0)
            else:
                pl.binding = BindingRec(last.type, last.ref, last.detail, last.at, wait, last.data)
        else:
            pl.binding = lb_src
        return True

    def _choose(self, op: COp, feasible: list[Slot]) -> Slot:
        ms = self.cfg.mode_selection
        for s in feasible:
            m = s.mode
            cost = (s.end - s.setup_start) * m.cost_per_min + m.sub_cost
            if ms.strategy == "PREFERRED":
                s.score = (m.pref, s.end, s.setup)
            elif ms.strategy == "LEAST_SETUP":
                s.score = (s.setup, s.end, m.pref)
            elif ms.strategy == "LOWEST_COST":
                s.score = (cost, s.end, m.pref)
            else:
                s.score = (
                    s.end
                    + ms.preference_penalty_minutes * m.pref
                    + ms.setup_weight * s.setup
                    + ms.cost_weight_minutes_per_currency * cost,
                    m.pref,
                    s.end,
                )
            s.score = s.score + (m.idx,)
        return min(feasible, key=lambda s: s.score)

    # ------------------------------------------------------------------ slot search
    def _slot(self, op: COp, m: CMode, lb: int, end_lb: int, setup_lb: int | None = None) -> Slot:
        """Earliest feasible placement of ``op`` in mode ``m``.

        ``lb`` bounds the *run* start (predecessors, release, material). With a detached setup
        (``setup_lb`` given) the changeover may start earlier — just in time before the run — as soon
        as the machine is free; otherwise the setup itself starts no earlier than ``lb``.
        """
        cp = self.cp
        res = cp.resources[m.res]
        cal = m.cal
        slot = Slot(m)
        pushes = slot.pushes
        if m.sub:
            slot.feasible = True
            slot.setup_start = slot.start = lb
            slot.end = max(lb + m.run, end_lb)
            return slot
        sk = self._state_keys[op.idx]
        if cal.total_minutes == 0 or cal.next_work(lb) is None:
            slot.reason, slot.detail = self._calendar_culprit(m, lb)
            return slot
        detached = setup_lb is not None
        t = min(lb, setup_lb) if detached else lb
        for _ in range(MAX_SLOT_ITERATIONS):
            base_work = m.setup_base + m.run + m.teardown
            if op.interruptible:
                t2 = cal.next_work(t)
            else:
                t2 = cal.next_uninterrupted_start(t, base_work)
            if t2 is None:
                slot.reason = "NO_WINDOW" if not op.interruptible else "CALENDAR_END"
                slot.detail = (
                    f"no continuous working window of {base_work} min"
                    if not op.interruptible
                    else "no working time left in the calendar"
                )
                return slot
            if t2 > t:
                pushes.append(self._calendar_push(m, t, t2))
                t = t2
            setup = m.setup_base
            prev_op = None
            nb = None
            if res.unary:
                tl = self.unary[m.res]
                k = tl.prev_index(t)
                if k >= 0 and tl.blocks[k].end > t:
                    b = tl.blocks[k]
                    pushes.append(Push("RESOURCE", cp.ops[b.op].id, b.end, f"{res.code} busy with {cp.ops[b.op].id}", {"order": cp.orders[cp.ops[b.op].order].number}))
                    t = b.end
                    continue
                prev_state = tl.blocks[k].state_key if k >= 0 else res.initial_state
                prev_op = tl.blocks[k].op if k >= 0 else None
                nb = tl.blocks[k + 1] if k + 1 < len(tl.blocks) else None
                if self._forbidden(m.res, prev_state, sk):
                    if nb is None:
                        slot.reason = "SEQUENCE_RULE"
                        slot.detail = "sequence rule forbids following the last job on this resource"
                        return slot
                    pushes.append(Push("RESOURCE", cp.ops[nb.op].id, nb.end, "sequence rule: cannot immediately follow previous job"))
                    t = nb.end
                    continue
                setup = cp.setup.setup(res.idx, prev_state, sk, m.setup_base)
            work = setup + m.run + m.teardown
            run_start = None
            if detached:
                if setup > 0:
                    jit = cal.sub_work(lb, setup)
                    if jit is not None and jit > t:
                        t = jit  # prepare the machine just in time before the job is ready
                        continue
                    s_end = cal.add_work(t, setup)
                    run_start = cal.next_work(max(s_end, lb)) if s_end is not None else None
                else:
                    if t < lb:
                        t = lb
                        continue
                    run_start = t
                e = cal.add_work(run_start, m.run + m.teardown) if run_start is not None else None
            elif op.interruptible:
                e = cal.add_work(t, work)
            else:
                if not cal.fits_uninterrupted(t, work):
                    t3 = cal.next_uninterrupted_start(t + 1, work)
                    if t3 is None:
                        slot.reason = "NO_WINDOW"
                        slot.detail = f"no continuous working window of {work} min"
                        return slot
                    pushes.append(self._calendar_push(m, t, t3))
                    t = t3
                    continue
                e = t + work
            if e is None:
                slot.reason = "CALENDAR_END"
                slot.detail = "not enough working time before the end of the planning calendar"
                return slot
            if e < end_lb:
                t_new = cal.sub_work(end_lb, work) if op.interruptible else end_lb - work
                t_new = max(t + 1, t_new if t_new is not None else t + 1)
                pushes.append(Push("PREDECESSOR", None, t_new, "must finish after predecessor (overlap / FF)"))
                t = t_new
                continue
            if res.unary and nb is not None:
                if nb.setup_start < e:
                    pushes.append(Push("RESOURCE", cp.ops[nb.op].id, nb.end, f"{res.code} busy with {cp.ops[nb.op].id}", {"order": cp.orders[cp.ops[nb.op].order].number}))
                    t = nb.end
                    continue
                if self._forbidden(m.res, sk, nb.state_key):
                    pushes.append(Push("RESOURCE", cp.ops[nb.op].id, nb.end, "sequence rule: next job cannot follow"))
                    t = nb.end
                    continue
                ns = cp.setup.setup(res.idx, sk, nb.state_key, nb.setup_base)
                adjust = None
                if ns > nb.setup:
                    # the next job's changeover grows: it may start earlier into idle time only if its
                    # own precedences, material, labour and tools still allow it
                    adjust = self._can_extend_setup(nb, ns, e)
                    if adjust is None:
                        pushes.append(Push("SETUP", cp.ops[nb.op].id, nb.end, f"changeover to {cp.ops[nb.op].id} would not fit"))
                        t = nb.end
                        continue
                slot.next_adjust = adjust
            pieces = cal.pieces(t, e) if op.interruptible else [(t, e)]
            if not res.unary and res.finite:
                nf = _fit(self.cumulative[m.res], pieces, 1)
                if nf is not None:
                    if nf >= POS_INF:
                        slot.reason = "NO_CAPACITY"
                        slot.detail = f"{res.code} has no free capacity for the rest of the calendar"
                        return slot
                    pushes.append(Push("RESOURCE", res.id, nf, f"{res.code} at full capacity"))
                    t = nf
                    continue
            blocked = False
            for r_i, units in m.sec:
                nf = _fit(self.cumulative[r_i], pieces, units)
                if nf is not None:
                    sr = cp.resources[r_i]
                    kind = "LABOR" if sr.kind in ("LABOR_POOL", "HUMAN") else "TOOL" if sr.kind == "TOOL" else "RESOURCE"
                    if nf >= POS_INF:
                        slot.reason = f"NO_{kind}"
                        slot.detail = f"{sr.code} has no free capacity ({units} unit(s) required) for the rest of the calendar"
                        return slot
                    pushes.append(Push(kind, sr.id, nf, f"{sr.code}: {units} unit(s) not available"))
                    t = nf
                    blocked = True
                    break
            if blocked:
                continue
            slot.feasible = True
            slot.setup_start = t
            if run_start is not None:
                slot.start = run_start
            elif setup > 0:
                s_end = cal.add_work(t, setup) if op.interruptible else t + setup
                slot.start = (cal.next_work(s_end) if op.interruptible else s_end) or s_end
            else:
                slot.start = t
            slot.end = e
            slot.setup = setup
            slot.prev_op = prev_op
            return slot
        slot.reason = "SEARCH_LIMIT"
        slot.detail = "slot search limit reached"
        return slot

    def _can_extend_setup(self, nb: Block, ns: int, after: int) -> tuple[int, int] | None:
        """Can the next job's setup start earlier (to hold a longer changeover) without breaking it?"""
        if nb.fixed:
            return None
        new_ss = nb.cal.sub_work(nb.start, ns) if ns > 0 else nb.start
        if new_ss is None or new_ss < after:
            return None
        npl = self.placements[nb.op]
        if npl is None or new_ss < npl.earliest:
            return None
        nop = self.cp.ops[nb.op]
        if not nop.interruptible and not nb.cal.fits_uninterrupted(new_ss, nb.end - new_ss):
            return None
        nm = nop.modes[npl.mode]
        extra = nm.cal.pieces(new_ss, nb.setup_start)
        for r_i, units in nm.sec:
            if _fit(self.cumulative[r_i], extra, units) is not None:
                return None
        return new_ss, ns

    def _calendar_culprit(self, m: CMode, lb: int) -> tuple[str, str]:
        """Explain why a mode has no working time left: which of its resources is never available."""
        cp = self.cp
        primary = cp.resources[m.res]
        if primary.cal.next_work(lb) is None:
            return "NO_WORKING_TIME", f"{primary.code} has no working time after {cp.dt(lb).isoformat()} (calendar, maintenance or availability dates)"
        for ri, units in m.sec:
            sr = cp.resources[ri]
            kind = "NO_LABOR" if sr.kind in ("LABOR_POOL", "HUMAN") else "NO_TOOL" if sr.kind == "TOOL" else "NO_RESOURCE"
            if sr.cal.next_work(lb) is None:
                return kind, f"{sr.code} is never available after {cp.dt(lb).isoformat()} (capacity {sr.capacity}, calendar or unavailability)"
            if primary.cal.intersect(sr.cal).next_work(lb) is None:
                return kind, f"{sr.code} is never available while {primary.code} works (no common working time)"
        return "NO_COMMON_TIME", f"{primary.code} and its required labour/tools never have common working time"

    def _calendar_push(self, m: CMode, t0: int, t1: int) -> Push:
        cp = self.cp
        for ri in (m.res, *(r for r, _ in m.sec)):
            res = cp.resources[ri]
            for a, b, kind, reason, uid, _loss in res.unavail:
                if a < t1 and b > t0:
                    label = kind.replace("_", " ").lower()
                    return Push(
                        "CALENDAR",
                        uid or res.id,
                        t1,
                        f"{res.code} {label}" + (f": {reason}" if reason else ""),
                        {"kind": kind, "resource": res.code, "from": a, "to": b},
                    )
        return Push("CALENDAR", None, t1, "non-working time")

    def _forbidden(self, res: int, prev_state, next_state) -> bool:
        for rs, pm, nm, _id in self.cp.forbidden:
            if rs is not None and res not in rs:
                continue
            if matches(prev_state, pm) and matches(next_state, nm):
                return True
        return False

    # ------------------------------------------------------------------ commit
    def _commit(self, op: COp, m: CMode, setup_start: int, start: int, end: int, setup: int, prev_op: int | None, fixed_reason: str | None = None, check_conflicts: bool = False) -> None:
        cp = self.cp
        res = cp.resources[m.res]
        if res.unary:
            tl = self.unary[m.res]
            if check_conflicts:
                k = tl.prev_index(setup_start)
                clash = []
                if k >= 0 and tl.blocks[k].end > setup_start:
                    clash.append(tl.blocks[k].op)
                if k + 1 < len(tl.blocks) and tl.blocks[k + 1].setup_start < end:
                    clash.append(tl.blocks[k + 1].op)
                for c in clash:
                    self.fixed_conflicts.append({"op": op.idx, "other": c, "resource": m.res})
            block = Block(op.idx, setup_start, start, end, setup, m.setup_base, self._state_keys[op.idx], m.cal, fixed=fixed_reason is not None)
            k = tl.insert(block)
            if k + 1 < len(tl.blocks) and not check_conflicts:
                nb = tl.blocks[k + 1]
                ns = cp.setup.setup(res.idx, block.state_key, nb.state_key, nb.setup_base)
                npl = self.placements[nb.op]
                if npl is not None:
                    npl.prev_op = op.idx
                if ns != nb.setup and not nb.fixed:
                    old_ss = nb.setup_start
                    new_ss = nb.start if ns == 0 else (nb.cal.sub_work(nb.start, ns) or nb.start)
                    if new_ss < old_ss and npl is not None:
                        # longer changeover pulled into idle time: hold labour/tools and material earlier
                        nop = cp.ops[nb.op]
                        nm = nop.modes[npl.mode]
                        for r_i, units in nm.sec:
                            for a, b in nm.cal.pieces(new_ss, old_ss):
                                self.cumulative[r_i].reserve(a, b, units)
                    nb.setup = ns
                    nb.setup_start = new_ss
                    tl.starts[k + 1] = nb.setup_start
                    if npl is not None:
                        npl.setup = ns
                        npl.setup_start = nb.setup_start
            last = self.last_state.get(m.res)
            if last is None or end >= last[0]:
                self.last_state[m.res] = (end, block.state_key, op.family)
        pieces = [(setup_start, end)] if (m.sub or not op.interruptible) else (m.cal.pieces(setup_start, end) or [(setup_start, end)])
        if not res.unary and res.finite:
            for a, b in pieces:
                self.cumulative[m.res].reserve(a, b, 1)
        for r_i, units in m.sec:
            for a, b in pieces:
                self.cumulative[r_i].reserve(a, b, units)
        if self.use_materials:
            for mi, q in op.materials:
                self.ledger.consume(mi, start, q, op.id, order=op.order)
            if op.produces is not None:
                mat, qty = op.produces
                o = cp.orders[op.order]
                self.ledger.supply(mat, end + op.move + op.wait, qty, f"PROD:{o.id}", kind="PRODUCTION", ref=o.number, order=o.idx)
        pl = Placement()
        pl.op = op.idx
        pl.mode = m.idx
        pl.res = m.res
        pl.setup_start = setup_start
        pl.start = start
        pl.end = end
        pl.setup = setup
        pl.prev_op = prev_op
        pl.lb = setup_start
        pl.earliest = setup_start
        pl.fixed = fixed_reason is not None
        pl.fixed_reason = fixed_reason
        pl.binding = BindingRec("NONE")
        pl.overtime = m.cal.overtime_between(setup_start, end) if not m.sub else 0
        minutes = max(end - setup_start, 0) if m.sub else m.cal.working_between(setup_start, end)
        pl.cost = (
            minutes * m.cost_per_min
            + pl.overtime * (res.ot_cost_per_min - res.cost_per_min if res.ot_cost_per_min > res.cost_per_min else 0)
            + setup * res.setup_cost_per_min
            + m.sub_cost
        )
        self.placements[op.idx] = pl

    def _propagate_unscheduled(self, i: int) -> None:
        cp = self.cp
        stack = [i]
        while stack:
            j = stack.pop()
            for s, *_ in cp.ops[j].succs:
                if self.placements[s] is None and s not in self.unscheduled:
                    self.unscheduled[s] = Unsched(
                        s, "PREDECESSOR_UNSCHEDULED", f"{cp.ops[s].id} waits for {cp.ops[j].id}, which could not be scheduled", {"predecessor": cp.ops[j].id}
                    )
                    stack.append(s)


def _fit(tl: CumulativeTimeline, pieces: list[tuple[int, int]], units: int) -> int | None:
    """None if ``units`` are free in every working piece, else the next candidate start."""
    for a, b in pieces:
        nf = tl.next_fit(a, b, units)
        if nf is not None:
            return nf
    return None


def _freeze(state: dict) -> tuple:
    from .setups import state_key

    return state_key(state) or ()


def build(cp: CompiledProblem, config: BuildConfig | None = None, timing: Timing | None = None) -> BuildResult:
    cfg = config
    if cfg is None:
        cfg = BuildConfig(
            rules=tuple(cp.solver.dispatch_rules),
            mode_selection=cp.solver.mode_selection,
            explain=cp.solver.explain,
            direction=cp.solver.direction,
        )
    tm = timing or compute_timing(cp)
    if cfg.direction in ("BACKWARD", "HYBRID") and cfg.targets is None:
        buf = 0 if cfg.direction == "BACKWARD" else cfg.hybrid_buffer
        cfg.targets = {i: tm.lst[i] - buf for i in range(len(cp.ops)) if tm.lst[i] < POS and tm.lst[i] - buf > cp.work_lb}
    return ScheduleBuilder(cp, cfg, tm).build()
