"""Solver pipeline — the single entry point ``solve(problem) -> solution``.

Steps (reported through the progress callback and in ``solver_metadata.phases``):

 1 Load scenario              7 Feasible resource alternatives   13 Repair / re-time
 2 Validate master data       8 Build calendars                  14 Calculate KPIs
 3 Validate constraints       9 Build setup matrices             15 Generate explanations
 4 Explode BOM / pegging     10 Identify bottlenecks             16 Save schedule (platform)
 5 Material availability     11 Initial feasible schedule        17 Publish result (platform)
 6 Generate operations       12 Optimize

Steps 16 and 17 belong to the platform (database and event bus) and are reported by it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import contextmanager

from .assemble import assemble
from .compile import ENGINE_VERSION, compile_problem
from .contract import PhaseLog, Problem, Solution, SolverMetadata
from .providers import get_provider
from .providers.base import SolveContext
from .timing import compute_timing
from .validator import validate

PROFILE_LIMITS = {"QUICK": 10.0, "NORMAL": 60.0, "DEEP": 600.0, "CUSTOM": 60.0}

PIPELINE_STEPS = [
    "Load scenario",
    "Validate master data",
    "Validate constraints",
    "Explode BOM",
    "Calculate material availability",
    "Generate operations",
    "Generate feasible resource alternatives",
    "Build calendars",
    "Build setup matrices",
    "Identify bottlenecks",
    "Generate initial feasible schedule",
    "Optimize",
    "Repair violations",
    "Calculate KPIs",
    "Generate explanations",
    "Save schedule",
    "Publish result",
]

Progress = Callable[[str, float | None, str | None], None]


def time_limit_for(problem: Problem) -> float:
    s = problem.solver
    return float(s.time_limit_s) if s.time_limit_s else PROFILE_LIMITS[s.profile]


def solve(problem: Problem, progress: Progress | None = None, cancelled: Callable[[], bool] | None = None) -> Solution:
    t0 = time.monotonic()
    phases: list[PhaseLog] = []

    def report(step: str, fraction: float | None = None, detail: str | None = None) -> None:
        if progress:
            progress(step, fraction, detail)

    @contextmanager
    def phase(name: str, detail: str | None = None):
        report(name, 0.0, detail)
        start = time.monotonic()
        rec = PhaseLog(name=name, detail=detail)
        try:
            yield rec
        except Exception:
            rec.status = "FAILED"
            rec.runtime_s = round(time.monotonic() - start, 4)
            phases.append(rec)
            raise
        rec.runtime_s = round(time.monotonic() - start, 4)
        phases.append(rec)
        report(name, 1.0, rec.detail)

    with phase("Load scenario") as rec:
        cp = compile_problem(problem)
        rec.detail = f"{len(cp.orders)} orders, {len(cp.ops)} operations, {len(cp.resources)} resources, {len(cp.materials)} materials"
    crit = [i for i in cp.issues if i.severity == "CRITICAL"]
    with phase("Validate master data") as rec:
        rec.detail = f"{len(cp.issues)} data issues ({len(crit)} critical)"
    with phase("Validate constraints") as rec:
        rec.detail = f"{len(cp.forbidden)} sequence rules, frozen until {cp.dt(cp.frozen_until).isoformat() if cp.frozen_until is not None else '—'}"
    with phase("Explode BOM") as rec:
        rec.detail = f"{len(cp.static_pegs)} component links (make items)"
    with phase("Calculate material availability") as rec:
        rec.detail = f"{sum(len(m.supplies) for m in cp.materials)} supplies for {len(cp.materials)} materials"
    with phase("Generate operations") as rec:
        rec.detail = f"{len(cp.ops)} operations to schedule ({sum(1 for o in cp.ops if o.fixed is not None)} fixed)"
    with phase("Generate feasible resource alternatives") as rec:
        rec.detail = f"{sum(len(o.modes) for o in cp.ops)} modes"
    with phase("Build calendars") as rec:
        rec.detail = f"{len(cp.calendars)} calendars expanded"
    with phase("Build setup matrices") as rec:
        rec.detail = f"{len(cp.setup.matrices)} matrices, {len(cp.setup.rules)} setup rules"
    with phase("Identify bottlenecks") as rec:
        timing = compute_timing(cp)
        impossible = sum(1 for o in cp.orders if timing.order_eft[o.idx] is not None and timing.order_eft[o.idx] > o.due)
        rec.detail = f"{impossible} orders cannot meet their due date even at infinite capacity"

    limit = time_limit_for(problem)
    provider = get_provider(problem.solver.provider)
    if not provider.capabilities().detailed_scheduling:
        from .providers.mip import NotSupported

        raise NotSupported(f"provider {provider.name!r} does not support detailed scheduling")
    # keep time for validation, KPIs and explanations so the whole run honours the limit
    elapsed = time.monotonic() - t0
    reserve = min(0.3 * limit, 1.5 + 0.0015 * len(cp.ops) * (2.0 if problem.solver.explain else 1.0))
    budget = max(limit - elapsed - reserve, 0.2 * limit)
    ctx = SolveContext(time_limit_s=budget, seed=problem.solver.seed, progress=progress, cancelled=cancelled)
    with phase("Optimize", provider.name) as rec:
        pres = provider.solve(cp, timing, ctx)
        rec.detail = f"{provider.name}: {pres.status}, {pres.iterations} iterations"

    with phase("Repair violations") as rec:
        validation = validate(cp, pres.result.placements, pres.result.unscheduled)
        hard = [v for v in validation.violations if v.hardness == "HARD" and v.severity == "CRITICAL" and not v.type.startswith("DATA_")]
        rec.detail = f"{len(hard)} hard violations after build ({len(pres.result.unscheduled)} unscheduled operations)"
    with phase("Calculate KPIs"):
        pass
    with phase("Generate explanations") as rec:
        rec.detail = "per operation: reasons, alternatives, binding constraint" if problem.solver.explain else "disabled"

    meta = SolverMetadata(
        provider=provider.name,
        status=pres.status,
        objective=_objective_scalar(pres.vector),
        objective_breakdown={k: round(v, 3) for k, v in pres.components.items()},
        objective_vector=[round(v, 6) for v in pres.vector],
        best_bound=pres.best_bound,
        gap=pres.gap,
        proven_optimal=pres.proven_optimal,
        runtime_s=round(time.monotonic() - t0, 3),
        time_limit_s=limit,
        iterations=pres.iterations,
        seed=problem.solver.seed,
        engine_version=ENGINE_VERSION,
        phases=phases,
        messages=ctx.messages,
        details=pres.details,
    )
    sol = assemble(pres.result, meta, validation, explain=problem.solver.explain)
    sol.solver_metadata.runtime_s = round(time.monotonic() - t0, 3)
    return sol


def _objective_scalar(vec: list[float]) -> float | None:
    if not vec:
        return None
    return round(vec[-1] if len(vec) == 2 else sum(vec[1:]), 6)
