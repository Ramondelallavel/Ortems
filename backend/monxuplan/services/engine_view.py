"""Engine views of stored plans: compiled problem and exact replay (cached per plan version).

Used for on-demand engine computations on an existing plan — constraint explorer, position checks,
custom capacity windows, plans stored before the read models — without re-optimising. Screens read
the plan's stored read models instead (see ``plan_store``); a replay of a 200 000-operation plan
takes tens of seconds and gigabytes, so the cache is bounded by operations, not only by entries.
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from datetime import UTC

from sqlalchemy import select
from sqlalchemy.orm import Session

from monxuplan_engine.builder import BuildConfig, BuildResult, ScheduleBuilder, Unsched
from monxuplan_engine.compile import CompiledProblem, compile_problem
from monxuplan_engine.perf import paused_gc
from monxuplan_engine.timing import compute_timing

from ..models import Plan, ScheduledOperation
from .planning import problem_for_plan

_CACHE: OrderedDict[tuple, tuple[CompiledProblem, BuildResult]] = OrderedDict()
_LOCK = threading.Lock()
MAX_ENTRIES = 6
MAX_CACHED_OPERATIONS = 450_000  # ~2 replays of a 100 000-order day plus small plans


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _size(entry: tuple[CompiledProblem, BuildResult]) -> int:
    return len(entry[0].ops)


def _store(key: tuple, entry: tuple[CompiledProblem, BuildResult]) -> None:
    with _LOCK:
        _CACHE[key] = entry
        _CACHE.move_to_end(key)
        total = sum(_size(e) for e in _CACHE.values())
        while len(_CACHE) > 1 and (len(_CACHE) > MAX_ENTRIES or total > MAX_CACHED_OPERATIONS):
            _k, old = _CACHE.popitem(last=False)
            total -= _size(old)


def replay(s: Session, plan: Plan) -> tuple[CompiledProblem, BuildResult]:
    key = (plan.id, plan.version, plan.snapshot_id)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
            return hit
    with paused_gc():
        problem = problem_for_plan(s, plan)
        problem.solver.explain = False
        cp = compile_problem(problem)
        tm = compute_timing(cp)
        overrides = {}
        SO = ScheduledOperation
        for r in s.execute(select(SO.op_key, SO.resource_key, SO.setup_start, SO.end).where(SO.plan_id == plan.id)):
            i = cp.op_index.get(r.op_key)
            if i is None:
                continue
            mi = next((m.idx for m in cp.ops[i].modes if cp.resources[m.res].id == r.resource_key), None)
            if mi is None:
                continue
            overrides[i] = (mi, cp.axis.to_min(_aware(r.setup_start)), cp.axis.to_min_ceil(_aware(r.end)), "KEPT")
        placed = set(overrides)
        # operations the plan did not schedule stay unscheduled in the view
        res = ScheduleBuilder(cp, BuildConfig(overrides=overrides, explain=False), tm)
        res._place_fixed()
        unscheduled = {op.idx: Unsched(op.idx, "UNSCHEDULED", f"{op.id} is not scheduled in this plan", {}) for op in cp.ops if op.idx not in placed}
        result = BuildResult(cp=cp, timing=tm, placements=res.placements, unscheduled=unscheduled, unary=res.unary, cumulative=res.cumulative, ledger=res.ledger, config=res.cfg)
    _store(key, (cp, result))
    return cp, result


def invalidate(plan_id: uuid.UUID) -> None:
    with _LOCK:
        for k in [k for k in _CACHE if k[0] == plan_id]:
            _CACHE.pop(k, None)


__all__ = ["replay", "invalidate", "BuildResult"]
