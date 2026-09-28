"""Engine views of stored plans: compiled problem and exact replay (cached per plan version).

Used for on-demand engine computations on an existing plan — constraint explorer, position checks,
capacity profiles, material projections — without re-optimising.
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from datetime import UTC

from sqlalchemy import select
from sqlalchemy.orm import Session

from monxuplan_engine.builder import BuildConfig, BuildResult, ScheduleBuilder
from monxuplan_engine.compile import CompiledProblem, compile_problem
from monxuplan_engine.timing import compute_timing

from ..models import Plan, ScheduledOperation
from .planning import problem_for_plan

_CACHE: OrderedDict[tuple, tuple[CompiledProblem, BuildResult]] = OrderedDict()
_LOCK = threading.Lock()
MAX_ENTRIES = 6


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def replay(s: Session, plan: Plan) -> tuple[CompiledProblem, BuildResult]:
    key = (plan.id, plan.version, plan.snapshot_id)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
            return hit
    problem = problem_for_plan(s, plan)
    problem.solver.explain = False
    cp = compile_problem(problem)
    tm = compute_timing(cp)
    overrides = {}
    for r in s.scalars(select(ScheduledOperation).where(ScheduledOperation.plan_id == plan.id)):
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
    from monxuplan_engine.builder import BuildResult as BR

    unscheduled = {}
    from monxuplan_engine.builder import Unsched

    for op in cp.ops:
        if op.idx not in placed:
            unscheduled[op.idx] = Unsched(op.idx, "UNSCHEDULED", f"{op.id} is not scheduled in this plan", {})
    result = BR(cp=cp, timing=tm, placements=res.placements, unscheduled=unscheduled, unary=res.unary, cumulative=res.cumulative, ledger=res.ledger, config=res.cfg)
    with _LOCK:
        _CACHE[key] = (cp, result)
        while len(_CACHE) > MAX_ENTRIES:
            _CACHE.popitem(last=False)
    return cp, result


def invalidate(plan_id: uuid.UUID) -> None:
    with _LOCK:
        for k in [k for k in _CACHE if k[0] == plan_id]:
            _CACHE.pop(k, None)


__all__ = ["replay", "invalidate", "BuildResult"]
