"""Planning service: runs, plan versions, manual changes, undo/redo, repair and publication."""

from __future__ import annotations

import copy
import gc
import gzip
import hashlib
import logging
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from monxuplan_engine.compile import DAY, CompiledProblem, compile_problem
from monxuplan_engine.contract import (
    BaselineOpSpec,
    FixedAssignmentSpec,
    Problem,
    Solution,
    SolverMetadata,
    Violation,
)
from monxuplan_engine.contract import (
    ScheduledOperation as EngScheduled,
)
from monxuplan_engine.diff import compare_solutions
from monxuplan_engine.perf import paused_gc
from monxuplan_engine.pipeline import PIPELINE_STEPS, solve
from monxuplan_engine.providers.mip import NotSupported
from monxuplan_engine.repair import move as engine_move
from monxuplan_engine.repair import repair as engine_repair
from monxuplan_engine.timing import Timing, compute_timing

from ..core.clock import now
from ..core.db import new_session
from ..core.errors import Conflict, DomainError, Forbidden, NotFound, PlanningBlocked, ValidationFailed
from ..core.events import bus
from ..core.observability import PLANNING_RUNS, SOLVER_SECONDS
from ..models import ConstraintViolation, Plan, PlanningRun, Plant, ProblemSnapshot, Scenario, ScheduledOperation, revision
from . import audit, plan_store
from .context import Ctx, system_ctx
from .problem_builder import build_problem

log = logging.getLogger("monxuplan.planning")
REPLAN_MODES = ("NO_REPLAN", "THIS_ORDER", "DOWNSTREAM", "RESOURCE", "AREA", "SCENARIO")


# =============================================================================================
# Scenario access helpers
# =============================================================================================


def get_scenario(s: Session, ctx: Ctx, scenario_id: uuid.UUID) -> Scenario:
    sc = s.get(Scenario, scenario_id)
    if sc is None:
        raise NotFound("Scenario not found", code="SCENARIO_NOT_FOUND")
    ctx.require_plant(sc.plant_id)
    if sc.visibility == "PRIVATE" and sc.owner != ctx.username and "COMPANY_ADMIN" not in ctx.roles and "SUPER_ADMIN" not in ctx.roles:
        raise Forbidden("This scenario is private.", code="SCENARIO_PRIVATE")
    return sc


def check_edit(ctx: Ctx, sc: Scenario) -> None:
    """Edit permission and collaborative lock."""
    if sc.status == "ARCHIVED":
        raise Conflict("The scenario is archived.", code="SCENARIO_ARCHIVED")
    if sc.editors and ctx.username not in sc.editors and sc.owner != ctx.username and not ({"COMPANY_ADMIN", "SUPER_ADMIN", "SYSTEM"} & ctx.roles):
        raise Forbidden("You are not an editor of this scenario.", code="SCENARIO_NOT_EDITOR")
    if sc.locked_by and sc.locked_by != ctx.username and sc.lock_expires_at and _aware(sc.lock_expires_at) > now():
        raise Conflict(f"Scenario is being edited by {sc.locked_by}.", code="SCENARIO_LOCKED", context={"locked_by": sc.locked_by, "until": sc.lock_expires_at.isoformat()})


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def head_plan(s: Session, sc: Scenario) -> Plan | None:
    return s.get(Plan, sc.head_plan_id) if sc.head_plan_id else None


def published_plan(s: Session, plant_id: uuid.UUID) -> Plan | None:
    plant = s.get(Plant, plant_id)
    return s.get(Plan, plant.published_plan_id) if plant and plant.published_plan_id else None


# =============================================================================================
# Runs
# =============================================================================================


def enqueue_run(s: Session, ctx: Ctx, scenario_id: uuid.UUID, kind: str = "OPTIMIZE", params: dict[str, Any] | None = None) -> PlanningRun:
    ctx.require("plan:run")
    sc = get_scenario(s, ctx, scenario_id)
    check_edit(ctx, sc)
    params = dict(params or {})
    if kind not in ("OPTIMIZE", "PLAN", "REPAIR", "VALIDATE"):
        raise ValidationFailed(f"Unknown run kind {kind}")
    provider = (params.get("solver") or {}).get("provider")
    if provider == "mip":
        raise ValidationFailed("The MIP provider performs aggregate planning only; choose heuristic, cpsat or hybrid for detailed scheduling.", code="PROVIDER_NOT_SUPPORTED")
    if not params.get("force"):
        from .dataquality import blocking_issues

        blockers = blocking_issues(s, sc.plant_id)
        if blockers:
            raise PlanningBlocked(
                f"Planning is blocked by {len(blockers)} critical data problem(s). Fix them in the Data Quality Center or run with 'force'.",
                code="DATA_QUALITY_BLOCK",
                context={"issues": blockers[:20]},
            )
    running = s.scalar(select(func.count()).select_from(PlanningRun).where(PlanningRun.scenario_id == sc.id, PlanningRun.status.in_(["QUEUED", "RUNNING"])))
    if running:
        raise Conflict("A planning run is already queued or running for this scenario.", code="RUN_IN_PROGRESS")
    run = PlanningRun(tenant_id=ctx.tenant_id, scenario_id=sc.id, kind=kind, params=params, progress=[{"step": st, "status": "PENDING"} for st in PIPELINE_STEPS], created_by=ctx.username)
    try:
        # the count above is only a friendly early answer: the unique index on active runs is what
        # makes two concurrent requests unable to queue two runs for one scenario
        with s.begin_nested():
            s.add(run)
            s.flush()
    except IntegrityError as exc:
        raise Conflict("A planning run is already queued or running for this scenario.", code="RUN_IN_PROGRESS") from exc
    audit.record(s, ctx, "PLANNING_RUN_QUEUED", "scenario", sc.id, sc.name, after={"run_id": str(run.id), "kind": kind, "params": params})
    bus().publish("planning.run.queued", str(ctx.tenant_id), {"run_id": str(run.id), "scenario_id": str(sc.id), "kind": kind}, str(sc.plant_id))
    return run


def cancel_run(s: Session, ctx: Ctx, run_id: uuid.UUID) -> PlanningRun:
    run = s.get(PlanningRun, run_id)
    if run is None:
        raise NotFound("Run not found")
    ctx.require("plan:run")
    check_edit(ctx, get_scenario(s, ctx, run.scenario_id))
    if run.status == "QUEUED":
        run.status = "CANCELLED"
        run.finished_at = now()
    elif run.status == "RUNNING":
        run.cancel_requested = True
    audit.record(s, ctx, "PLANNING_RUN_CANCEL", "planning_run", run.id)
    return run


class _Progress:
    """Throttled persistence of engine progress + SSE events."""

    def __init__(self, run_id: uuid.UUID, tenant_id: uuid.UUID, plant_id: uuid.UUID) -> None:
        self.run_id, self.tenant_id, self.plant_id = run_id, tenant_id, plant_id
        self.steps: dict[str, dict[str, Any]] = {st: {"step": st, "status": "PENDING"} for st in PIPELINE_STEPS}
        self.last_write = 0.0
        self.last_cancel_check = 0.0
        self.cancelled = False
        self.lock = threading.Lock()
        self.current: str | None = None

    def __call__(self, step: str, fraction: float | None, detail: str | None) -> None:
        with self.lock:
            st = self.steps.setdefault(step, {"step": step, "status": "PENDING"})
            if fraction is not None and fraction >= 1.0:
                st["status"] = "DONE"
            else:
                st["status"] = "RUNNING"
                if fraction is not None:
                    st["fraction"] = round(fraction, 3)
            if detail:
                st["detail"] = detail[:300]
            self.current = step
        self.flush()

    def mark(self, step: str, status: str, detail: str | None = None) -> None:
        with self.lock:
            st = self.steps.setdefault(step, {"step": step})
            st["status"] = status
            if detail:
                st["detail"] = detail
        self.flush(force=True)

    def flush(self, force: bool = False) -> None:
        t = time.monotonic()
        if not force and t - self.last_write < 0.4:
            return
        self.last_write = t
        progress = list(self.steps.values())
        with new_session(self.tenant_id, "worker") as s:
            s.execute(update(PlanningRun).where(PlanningRun.id == self.run_id).values(progress=progress, current_step=self.current, heartbeat_at=now()))
            s.commit()
        bus().publish("planning.run.progress", str(self.tenant_id), {"run_id": str(self.run_id), "current_step": self.current, "progress": progress}, str(self.plant_id))

    def is_cancelled(self) -> bool:
        t = time.monotonic()
        if t - self.last_cancel_check > 1.0:
            self.last_cancel_check = t
            with new_session(self.tenant_id, "worker") as s:
                self.cancelled = bool(s.scalar(select(PlanningRun.cancel_requested).where(PlanningRun.id == self.run_id)))
        return self.cancelled


def execute_run(run_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    """Executed by a worker (thread or process). Never raises: failures are stored on the run."""
    # a 100 000-order run allocates millions of long-lived objects: one collection at the end
    with paused_gc():
        _execute_run(run_id, tenant_id)


def _execute_run(run_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    t0 = time.monotonic()
    with new_session(tenant_id, "worker") as s:
        run = s.get(PlanningRun, run_id)
        if run is None:
            return
        sc = s.get(Scenario, run.scenario_id)
        plant_id = sc.plant_id
        ctx = system_ctx(tenant_id, run.created_by or "worker")
        prog = _Progress(run.id, tenant_id, plant_id)
        provider = None
        stale: str | None = None
        owner = run.worker  # the claim; a run re-queued and claimed by another worker is not ours anymore
        try:
            params = run.params or {}
            baseline = head_plan(s, sc)
            # the inputs this result will be computed from (checked again before it becomes the head)
            run.input_revision = revision.current(s, tenant_id)
            run.baseline_plan_id = baseline.id if baseline else None
            frozen = published_plan(s, plant_id) if sc.is_live else baseline
            overrides = {k: params[k] for k in ("objectives", "constraints", "solver", "horizon_days", "frozen_hours", "flexible_days") if k in params}
            if run.kind == "PLAN":
                overrides.setdefault("solver", {})
                overrides["solver"] = {**overrides["solver"], "provider": "heuristic", "local_search": False}
            problem, info = build_problem(s, sc, baseline_plan=baseline, frozen_plan=frozen, overrides=overrides)
            provider = problem.solver.provider
            s.commit()  # release any write lock before the long solve (progress is written from other sessions)
            bus().publish("planning.run.started", str(tenant_id), {"run_id": str(run.id), "scenario_id": str(sc.id)}, str(plant_id))
            solution = solve(problem, progress=prog, cancelled=prog.is_cancelled)
            prog.mark("Save schedule", "RUNNING")
            stale = _promotion_check(s, run, sc, owner, tenant_id)
            if stale == "NOT_OWNER":
                s.rollback()
                log.warning("planning run result discarded: the run was re-queued and belongs to another worker", extra={"planning_run": str(run_id)})
                return
            plan = persist_solution(
                s, ctx, sc, problem, solution, info, kind="OPTIMIZED" if run.kind != "REPAIR" else "REPAIR", run=run, parent=baseline, note=params.get("note"), promote=stale is None
            )
            solution._state = None  # the engine state (gigabytes on a large plant) is no longer needed
            if baseline is not None:
                try:
                    plan.change_summary = _summary(compare_solutions(solution_from_plan(s, baseline), solution))
                except Exception:  # noqa: BLE001 - comparison is informative only
                    log.exception("comparison with previous plan failed")
            run.plan_id = plan.id
            run.status = "SUCCEEDED" if stale is None else "STALE"
            md = solution.solver_metadata
            run.provider, run.solver_status, run.objective, run.best_bound, run.gap, run.input_hash = md.provider, md.status, md.objective, md.best_bound, md.gap, md.input_hash
            run.result = {
                "plan_id": str(plan.id),
                "plan_number": plan.number,
                "feasible": solution.feasible,
                "kpis": solution.kpis,
                "messages": md.messages + (["stopped by the user: best plan found so far"] if prog.cancelled else []) + ([stale] if stale else []),
                "stale": stale,
                "change_log": info.change_log,
                "unscheduled": len(solution.unscheduled),
            }
            from .alerts import generate_alerts

            generate_alerts(s, ctx, plan, solution, sc)
            s.commit()  # commit before writing progress from another session (avoids lock waits)
            prog.mark("Save schedule", "DONE", plan.number)
            prog.mark("Publish result", "DONE", "draft plan available" if stale is None else "not applied: the inputs changed while planning")
        except NotSupported as exc:
            s.rollback()
            _fail(run, "PROVIDER_NOT_SUPPORTED", str(exc), None)
        except DomainError as exc:
            s.rollback()
            _fail(run, exc.code, exc.message, None)
        except Exception as exc:  # noqa: BLE001
            s.rollback()  # never keep a half-written plan
            error_id = uuid.uuid4().hex[:10]
            log.exception("planning run failed", extra={"planning_run": str(run_id), "error_id": error_id})
            _fail(run, "ENGINE_ERROR", f"MonxuPlan couldn't generate the schedule (error {error_id}). The technical details were logged for the administrator.", traceback.format_exc()[-8000:])
            prog.mark(prog.current or "Optimize", "FAILED", str(exc)[:200])
        finally:
            if stale == "NOT_OWNER":
                return  # noqa: B012 - the run row belongs to the worker that re-claimed it: leave it alone
            run.finished_at = now()
            run.duration_s = round(time.monotonic() - t0, 3)
            s.commit()
            PLANNING_RUNS.labels(run.kind, run.status, provider or "-").inc()
            SOLVER_SECONDS.labels(provider or "-").observe(run.duration_s)
            log.info(
                "planning run finished",
                extra={"planning_run": str(run.id), "scenario": str(sc.id), "plan": str(run.plan_id), "status": run.status, "duration_s": run.duration_s, "extra_data": {"provider": run.provider, "solver_status": run.solver_status, "gap": run.gap, "input_hash": run.input_hash}},
            )
            bus().publish("planning.run.finished", str(tenant_id), {"run_id": str(run.id), "status": run.status, "plan_id": str(run.plan_id) if run.plan_id else None, "error": run.error_message}, str(plant_id))


def _promotion_check(s: Session, run: PlanningRun, sc: Scenario, owner: str | None, tenant_id: uuid.UUID) -> str | None:
    """Before a run's result becomes the scenario head: lock the run and scenario rows and compare the
    inputs the run started from with the current ones. Returns None (promote), a reason (keep the result
    as a stale version; the head is left alone) or ``NOT_OWNER`` (another worker owns the run now)."""
    fresh_run = s.execute(select(PlanningRun.worker, PlanningRun.status).where(PlanningRun.id == run.id).with_for_update()).one()
    if fresh_run.worker != owner or fresh_run.status != "RUNNING":
        return "NOT_OWNER"
    s.refresh(sc, with_for_update=True)
    if sc.head_plan_id != run.baseline_plan_id:
        return "The scenario's current plan changed while this run was computing (manual edit, undo, restore or another run); the result was kept as a separate version and not applied."
    if revision.current(s, tenant_id) != run.input_revision:
        return "Planning data changed while this run was computing (master data, orders, inventory, calendars or scenario changes); the result was kept as a separate version and not applied. Run again to plan with the current data."
    return None


def _fail(run: PlanningRun, code: str, message: str, detail: str | None) -> None:
    run.status = "FAILED"
    run.error_code = code
    run.error_message = message
    run.error_detail = detail


def _summary(cmp: dict[str, Any]) -> dict[str, Any]:
    keep = ("orders_delayed", "orders_advanced", "operations_moved", "average_shift_minutes", "sequence_changes", "setup_delta_minutes", "overtime_delta_minutes", "new_hard_violation_count", "resolved_hard_violation_count")
    out = {k: cmp.get(k) for k in keep}
    out["resource_changes"] = len(cmp.get("resource_changes", []))
    out["kpis"] = {k: v for k, v in cmp.get("kpis", {}).items() if k in ("otif", "late_orders", "setup_h", "overtime_h", "utilization", "orders_unscheduled")}
    return out


# =============================================================================================
# Persistence of plans
# =============================================================================================


def _next_number(s: Session, tenant_id: uuid.UUID) -> tuple[str, int]:
    day = now().date().isoformat()
    prefix = f"PLAN-{day}-V"
    n = s.scalar(select(func.count()).select_from(Plan).where(Plan.tenant_id == tenant_id, Plan.number.like(prefix + "%"))) or 0
    return f"{prefix}{n + 1:03d}", n + 1


def store_snapshot(s: Session, tenant_id: uuid.UUID, problem: Problem) -> ProblemSnapshot:
    """The problem of a plan version, stored once per content hash. The baseline of the previous
    version is left out (it is rebuilt from the plan chain); one serialisation feeds the hash and
    the compressed blob."""
    data = problem.model_copy(update={"baseline": []}).model_dump_json(by_alias=True).encode()
    sha = hashlib.sha256(data).hexdigest()
    snap = s.scalar(select(ProblemSnapshot).where(ProblemSnapshot.sha256 == sha))
    if snap is None:
        blob = gzip.compress(data, compresslevel=1 if len(data) > 8_000_000 else 6)
        snap = ProblemSnapshot(tenant_id=tenant_id, sha256=sha, data=blob, size_bytes=len(blob))
        s.add(snap)
        s.flush()
    return snap


def load_problem(s: Session, plan: Plan) -> Problem:
    if plan.snapshot_id is None:
        raise NotFound("This plan has no problem snapshot", code="NO_SNAPSHOT")
    snap = s.get(ProblemSnapshot, plan.snapshot_id)
    return Problem.model_validate_json(gzip.decompress(snap.data))


def persist_solution(
    s: Session,
    ctx: Ctx,
    sc: Scenario,
    problem: Problem,
    sol: Solution,
    info=None,
    kind: str = "OPTIMIZED",
    run: PlanningRun | None = None,
    parent: Plan | None = None,
    note: str | None = None,
    change_summary: dict | None = None,
    explanations_from: Plan | None = None,
    snapshot_id: uuid.UUID | None = None,
    reuse: plan_store.Reuse | None = None,
    promote: bool = True,
) -> Plan:
    md = sol.solver_metadata
    sha = md.input_hash or ""
    snap_id = snapshot_id or store_snapshot(s, ctx.tenant_id, problem).id
    analysis = {
        "storage": plan_store.STORAGE_VERSION,
        "bottlenecks": [b.model_dump(mode="json") for b in sol.bottlenecks],
        "data_issues": (info.issues if info else []),
        "excluded_orders": (info.excluded_orders if info else []),
        "change_log": (info.change_log if info else []),
        "counts": {"operations": len(sol.schedule), "orders": len(sol.orders), "unscheduled": len(sol.unscheduled), "pegging": len(sol.pegging)},
    }
    for attempt in range(5):
        number, _ = _next_number(s, ctx.tenant_id)
        if attempt:
            number = f"{number}-{uuid.uuid4().hex[:4]}"
        version_no = (s.scalar(select(func.max(Plan.version_no)).where(Plan.scenario_id == sc.id)) or 0) + 1
        plan = Plan(
            tenant_id=ctx.tenant_id,
            scenario_id=sc.id,
            plant_id=sc.plant_id,
            run_id=run.id if run else None,
            parent_id=parent.id if parent else None,
            number=number,
            version_no=version_no,
            kind=kind,
            status="DRAFT" if promote else "STALE",
            feasible=sol.feasible,
            horizon_start=problem.horizon.start,
            horizon_end=problem.horizon.end,
            frozen_until=problem.horizon.frozen_until,
            kpis=sol.kpis,
            kpi_details=plan_store.slim_kpi_details(sol.kpi_details),
            solver_metadata=md.model_dump(mode="json"),
            analysis=analysis,
            params={"objectives": problem.objectives.model_dump(mode="json"), "constraints": problem.constraints.model_dump(mode="json"), "solver": problem.solver.model_dump(mode="json")},
            snapshot_id=snap_id,
            input_hash=sha,
            note=note,
            change_summary=change_summary or {},
            created_by=ctx.username,
        )
        try:
            with s.begin_nested():
                s.add(plan)
                s.flush()
            break
        except IntegrityError:
            continue
    else:  # pragma: no cover
        raise Conflict("Could not allocate a plan number", code="PLAN_NUMBER")
    plan_store.write_results(s, ctx.tenant_id, plan, sol, info.op_rows if info else None, explanations_from, reuse)
    if promote:
        # move the scenario head; a new version invalidates the redo stack
        sc.head_plan_id = plan.id
        sc.redo_stack = []
    audit.record(s, ctx, "PLAN_CREATED", "plan", plan.id, plan.number, after={"kind": kind, "scenario": sc.name, "feasible": sol.feasible, "solver": md.provider, "status": md.status, "objective": md.objective, "gap": md.gap})
    bus().publish("plan.created", str(ctx.tenant_id), {"plan_id": str(plan.id), "number": plan.number, "scenario_id": str(sc.id), "kind": kind}, str(sc.plant_id))
    return plan


def solution_from_plan(s: Session, plan: Plan) -> Solution:
    """Rebuild the engine Solution of a stored plan (for comparisons and baselines). Stored rows
    were validated when the plan was written: they are read as plain columns and not re-validated."""
    SO, CV = ScheduledOperation, ConstraintViolation
    binding = plan_store.binding_from
    schedule = [
        EngScheduled.fast(
            op_id=r.op_key,
            order_id=r.order_key,
            resource_id=r.resource_key,
            mode_index=0,
            setup_start=_aware(r.setup_start),
            start=_aware(r.start),
            end=_aware(r.end),
            setup_minutes=r.setup_minutes,
            run_minutes=r.run_minutes,
            working_minutes=r.working_minutes,
            overtime_minutes=r.overtime_minutes,
            quantity=r.quantity,
            fixed=r.is_fixed,
            fixed_reason=r.fixed_reason,
            late=r.is_late,
            zone=r.zone,
            binding=binding(r.binding),
        )
        for r in s.execute(
            select(SO.op_key, SO.order_key, SO.resource_key, SO.setup_start, SO.start, SO.end, SO.setup_minutes, SO.run_minutes, SO.working_minutes, SO.overtime_minutes, SO.quantity, SO.is_fixed, SO.fixed_reason, SO.is_late, SO.zone, SO.binding).where(SO.plan_id == plan.id)
        )
    ]
    violations = [
        Violation.fast(severity=v.severity, hardness=v.hardness, type=v.type, message=v.message, order_id=v.order_key, op_id=v.op_key, resource_id=v.resource_key, material_id=v.material_key, details=v.details or {})
        for v in s.execute(select(CV.severity, CV.hardness, CV.type, CV.message, CV.order_key, CV.op_key, CV.resource_key, CV.material_key, CV.details).where(CV.plan_id == plan.id))
    ]
    orders = plan_store.order_results_models(s, plan)
    md = SolverMetadata.model_validate(plan.solver_metadata) if plan.solver_metadata else SolverMetadata(provider="?", status="HEURISTIC")
    return Solution.fast(schedule=schedule, violations=violations, feasible=plan.feasible, orders=orders, kpis=plan.kpis or {}, solver_metadata=md)


def problem_for_plan(s: Session, plan: Plan) -> Problem:
    """The plan's own problem with the plan itself as baseline (for moves and repairs)."""
    problem = load_problem(s, plan)
    SO = ScheduledOperation
    rows = s.execute(select(SO.op_key, SO.resource_key, SO.setup_start, SO.start, SO.end, SO.is_locked, SO.setup_minutes).where(SO.plan_id == plan.id)).all()
    placed = {r.op_key for r in rows}
    problem.baseline = [BaselineOpSpec.fast(op_id=r.op_key, resource_id=r.resource_key, start=_aware(r.start), end=_aware(r.end), setup_start=_aware(r.setup_start)) for r in rows]
    # the plan's locks are its rows' locks: locked positions are fixed, an operation unlocked since the
    # problem was stored is free again
    locked = {r.op_key: r for r in rows if r.is_locked}
    for op in problem.operations:
        r = locked.get(op.id)
        if r is not None and op.fixed is None:
            op.fixed = FixedAssignmentSpec.fast(resource_id=r.resource_key, start=_aware(r.setup_start), end=_aware(r.end), reason="LOCKED", setup_minutes=r.setup_minutes)
        elif r is None and op.fixed is not None and op.fixed.reason == "LOCKED" and op.id in placed:
            op.fixed = None
    return problem


# =============================================================================================
# Manual changes
# =============================================================================================


def _require_head(s: Session, ctx: Ctx, plan_id: uuid.UUID, expected_version: int | None = None) -> tuple[Plan, Scenario]:
    plan = s.get(Plan, plan_id)
    if plan is None:
        raise NotFound("Plan not found", code="PLAN_NOT_FOUND")
    sc = get_scenario(s, ctx, plan.scenario_id)
    if sc.head_plan_id != plan.id:
        raise Conflict(
            "This plan version is no longer the current version of the scenario (someone changed it). Reload to see the latest version.",
            code="PLAN_VERSION_CONFLICT",
            context={"head_plan_id": str(sc.head_plan_id) if sc.head_plan_id else None},
        )
    if expected_version is not None and plan.version != expected_version:
        raise Conflict("The plan was modified concurrently.", code="PLAN_VERSION_CONFLICT")
    return plan, sc


# A manual move on a 100 000-order plan must not reload, re-validate and recompile the whole plan
# each time (that alone takes over a minute). The problem, its compiled form and the plan's solution
# are kept per head plan version; applying a move derives the next version's entry from the new
# schedule, so consecutive moves only pay for the re-placement, the checks and the storage.
@dataclass
class MoveBase:
    problem: Problem
    cp: CompiledProblem
    baseline: Solution
    snapshot_id: uuid.UUID | None = None  # stored problem of the plan (locks come from its rows)
    timing: Timing | None = None  # computed on first use
    op_rows: dict[str, tuple] | None = None  # op key -> (order id, order operation id), on first apply


_MOVE_BASES: OrderedDict[tuple, MoveBase] = OrderedDict()
_MOVE_LOCK = threading.Lock()
MOVE_BASE_MAX_OPERATIONS = 300_000  # one 100 000-order day plan (or several small ones)
MOVE_BASE_MAX_ENTRIES = 4


def _base_key(plan: Plan) -> tuple:
    # a version's schedule and problem never change (publishing only changes its status); the one
    # in-place change, locks, drops the entry (forget_move_base)
    return (plan.id, plan.snapshot_id)


def _remember_base(key: tuple, base: MoveBase) -> None:
    with _MOVE_LOCK:
        _MOVE_BASES[key] = base
        _MOVE_BASES.move_to_end(key)
        total = sum(len(b.cp.ops) for b in _MOVE_BASES.values())
        evicted = False
        while len(_MOVE_BASES) > 1 and (len(_MOVE_BASES) > MOVE_BASE_MAX_ENTRIES or total > MOVE_BASE_MAX_OPERATIONS):
            _k, old = _MOVE_BASES.popitem(last=False)
            total -= len(old.cp.ops)
            evicted = True
    if evicted:
        gc.unfreeze()  # let the collector see what was frozen with the evicted base again


def forget_move_base(plan_id: uuid.UUID) -> None:
    """Drop the cached move base of a plan whose rows changed in place (locks)."""
    with _MOVE_LOCK:
        for k in [k for k in _MOVE_BASES if k[0] == plan_id]:
            _MOVE_BASES.pop(k, None)


def move_base(s: Session, plan: Plan) -> MoveBase:
    key = _base_key(plan)
    with _MOVE_LOCK:
        hit = _MOVE_BASES.get(key)
        if hit is not None:
            _MOVE_BASES.move_to_end(key)
            return hit
    with paused_gc():  # one full collection when done …
        problem = problem_for_plan(s, plan)
        base = MoveBase(problem, compile_problem(problem), solution_from_plan(s, plan), plan.snapshot_id)
    _remember_base(key, base)
    # … then the millions of long-lived objects of the base are moved out of the collector's sight:
    # later collections do not rescan a 100 000-order plan (seconds each time)
    gc.freeze()
    return base


def _next_move_base(base: MoveBase, sol: Solution, op_key: str) -> MoveBase | None:
    """The move base of the version created by applying a move: same problem, the new schedule as
    baseline and the moved operation locked where the planner put it — what ``problem_for_plan``
    and ``compile_problem`` return for that version, without reading and compiling it again."""
    result = getattr(sol, "_state", None)
    cp = base.cp
    i = cp.op_index.get(op_key)
    if result is None or i is None or result.placements[i] is None:
        return None
    p = result.placements[i]
    if p.setup_start - DAY < cp.lo:  # the compiled horizon start depends on fixed operations
        return None
    x = next((x for x in sol.schedule if x.op_id == op_key), None)
    if x is None:
        return None
    op = copy.copy(cp.ops[i])
    op.fixed = (next(m.idx for m in op.modes if m.res == p.res), p.setup_start, p.end, "LOCKED", p.setup)
    ops = list(cp.ops)
    ops[i] = op
    specs = list(base.problem.operations)
    k = next(k for k, o in enumerate(specs) if o.id == op_key)
    specs[k] = specs[k].model_copy(update={"fixed": FixedAssignmentSpec.fast(resource_id=x.resource_id, start=x.setup_start, end=x.end, reason="LOCKED", setup_minutes=x.setup_minutes)})
    baseline = [BaselineOpSpec.fast(op_id=y.op_id, resource_id=y.resource_id, start=y.start, end=y.end, setup_start=y.setup_start) for y in sol.schedule]
    problem = base.problem.model_copy(update={"operations": specs, "baseline": baseline})
    ncp = copy.copy(cp)
    ncp.problem = problem
    ncp.ops = ops
    ncp.baseline = {q.op: (q.res, q.setup_start, q.start, q.end) for q in result.placements if q is not None}
    ncp.input_hash = None  # not recomputed for a derived version (it hashes the whole problem)
    return MoveBase(problem, ncp, sol, base.snapshot_id, None, base.op_rows)


def _move(s: Session, ctx: Ctx, plan: Plan, op_key: str, resource_id: str, start: datetime, replan: str, allow_frozen: bool, explain: str) -> dict[str, Any]:
    if replan not in REPLAN_MODES:
        raise ValidationFailed(f"Unknown replan mode {replan}")
    if allow_frozen:
        ctx.require("plan:frozen")
    base = move_base(s, plan)
    try:
        with paused_gc(collect=False):
            if base.timing is None:
                base.timing = compute_timing(base.cp)
            out = engine_move(base.problem, op_key, resource_id, start, replan=replan, allow_frozen=allow_frozen, baseline_solution=base.baseline, cp=base.cp, explain=explain, timing=base.timing)
    except PermissionError as exc:
        raise Forbidden(str(exc), code="FROZEN_OPERATION") from exc
    except ValueError as exc:
        raise ValidationFailed(str(exc), code="INVALID_MOVE") from exc
    moved = next((x for x in out.solution.schedule if x.op_id == op_key), None)
    return {
        "op_id": op_key,
        "replan": replan,
        "resource_id": resource_id,
        "requested_start": start.isoformat(),
        "planned": moved.model_dump(mode="json") if moved else None,
        "feasible": out.solution.feasible,
        "hard_violations": [v.model_dump(mode="json") for v in out.solution.violations if v.hardness == "HARD" and not v.type.startswith("DATA_")][:50],
        "comparison": out.comparison,
        "freed_operations": len(out.freed_ops),
        "messages": out.messages,
        "_solution": out.solution,
        "_problem": base.problem,
        "_base": base,
        "_replaced": _replaced_keys(out.solution),
    }


def preview_move(s: Session, ctx: Ctx, plan_id: uuid.UUID, op_key: str, resource_id: str, start: datetime, replan: str = "DOWNSTREAM", allow_frozen: bool = False) -> dict[str, Any]:
    ctx.require("plan:read")
    plan = s.get(Plan, plan_id)
    if plan is None:
        raise NotFound("Plan not found", code="PLAN_NOT_FOUND")
    get_scenario(s, ctx, plan.scenario_id)
    out = _move(s, ctx, plan, op_key, resource_id, start, replan, allow_frozen, explain="NONE")
    return {k: v for k, v in out.items() if not k.startswith("_")}


def apply_move(s: Session, ctx: Ctx, plan_id: uuid.UUID, op_key: str, resource_id: str, start: datetime, replan: str, reason: str | None, allow_frozen: bool = False, expected_version: int | None = None, accept_violations: bool = False) -> Plan:
    # the collector would rescan the new 200 000-operation solution again and again while it is stored
    with paused_gc(collect=False):
        return _apply_move(s, ctx, plan_id, op_key, resource_id, start, replan, reason, allow_frozen, expected_version, accept_violations)


def _apply_move(s: Session, ctx: Ctx, plan_id: uuid.UUID, op_key: str, resource_id: str, start: datetime, replan: str, reason: str | None, allow_frozen: bool, expected_version: int | None, accept_violations: bool) -> Plan:
    ctx.require("plan:edit")
    plan, sc = _require_head(s, ctx, plan_id, expected_version)
    check_edit(ctx, sc)
    # one computation: the re-placed operations are explained, the others keep their explanation
    prev = _move(s, ctx, plan, op_key, resource_id, start, replan, allow_frozen, explain="CHANGED")
    sol: Solution = prev["_solution"]
    if not sol.feasible and prev["comparison"]["new_hard_violation_count"] and not accept_violations:
        raise ValidationFailed(
            "The move creates hard constraint violations. Choose another replan mode, use automatic repair, or confirm explicitly.",
            code="MOVE_CREATES_VIOLATIONS",
            context={"violations": prev["hard_violations"][:10]},
        )
    problem: Problem = prev["_problem"]
    base: MoveBase = prev["_base"]
    if base.op_rows is None:
        base.op_rows = _info_from_plan(s, plan).op_rows
    info = _info_from_plan(s, plan, base.op_rows)
    # same input problem as the parent version: its snapshot is reused (the locks are in the rows)
    new_plan = persist_solution(
        s,
        ctx,
        sc,
        problem,
        sol,
        info,
        kind="MANUAL_EDIT",
        parent=plan,
        note=reason,
        change_summary=_summary(prev["comparison"]),
        explanations_from=plan,
        snapshot_id=base.snapshot_id,
        reuse=_reuse_for(s, plan, sol, prev["_replaced"] | {op_key}),
    )
    # the moved operation is kept where the planner put it in later replans
    so = s.scalar(select(ScheduledOperation).where(ScheduledOperation.plan_id == new_plan.id, ScheduledOperation.op_key == op_key))
    if so is not None:
        so.is_locked = True
        so.fixed_reason = "MANUAL"
    audit.record(
        s,
        ctx,
        "OPERATION_MOVED",
        "plan",
        new_plan.id,
        new_plan.number,
        before={"op": op_key, "plan": plan.number, **_pos(s, plan.id, op_key)},
        after={"op": op_key, "resource": resource_id, "start": start.isoformat(), "replan": replan},
        reason=reason,
        compact=False,
    )
    nxt = _next_move_base(base, sol, op_key) if so is not None else None
    sol._state = None
    if nxt is not None:
        _remember_base(_base_key(new_plan), nxt)
    return new_plan


def _replaced_keys(sol: Solution) -> set[str]:
    state = getattr(sol, "_state", None)
    if state is None:
        return set()
    from monxuplan_engine.repair import replaced_ops

    return {state.cp.ops[i].id for i in replaced_ops(state)}


def _reuse_for(s: Session, parent: Plan, sol: Solution, replaced: set[str]) -> plan_store.Reuse | None:
    """Rows of a moved plan that differ from the parent version's stored rows (the others are copied
    from the parent). Operations whose position, setup, sequence and flags are unchanged keep their
    stored row — including its binding constraint, as their explanation is kept."""
    SO, PO = ScheduledOperation, plan_store.PlanOrder
    before = {
        r.op_key: (r.resource_key, _aware(r.setup_start), _aware(r.start), _aware(r.end), r.setup_minutes, r.is_fixed, r.fixed_reason, r.is_locked, r.is_late, r.zone, r.prev_op_key)
        for r in s.execute(select(SO.op_key, SO.resource_key, SO.setup_start, SO.start, SO.end, SO.setup_minutes, SO.is_fixed, SO.fixed_reason, SO.is_locked, SO.is_late, SO.zone, SO.prev_op_key).where(SO.plan_id == parent.id))
    }
    ops = set(replaced)
    for x in sol.schedule:
        if before.pop(x.op_id, None) != (x.resource_id, x.setup_start, x.start, x.end, x.setup_minutes, x.fixed, x.fixed_reason, x.fixed_reason == "LOCKED", x.late, x.zone, x.prev_op_id):
            ops.add(x.op_id)
    ops |= set(before)  # no longer scheduled
    def when(t):
        return _aware(t) if t else None

    old_orders = {
        r.order_key: (r.status, when(r.start), when(r.end), when(r.due), r.lateness_minutes, r.material_status, when(r.earliest_possible_end), r.weight, list(r.rules_applied or []))
        for r in s.execute(
            select(PO.order_key, PO.status, PO.start, PO.end, PO.due, PO.lateness_minutes, PO.material_status, PO.earliest_possible_end, PO.weight, PO.rules_applied).where(PO.plan_id == parent.id)
        )
    }
    orders = {
        o.order_id
        for o in sol.orders
        if old_orders.get(o.order_id) != (o.status, o.start, o.end, o.due, o.lateness_minutes, o.material_status, o.earliest_possible_end, o.weight, list(o.rules_applied))
    }
    orders |= set(old_orders) - {o.order_id for o in sol.orders}
    state = getattr(sol, "_state", None)
    if state is None or len(ops) > plan_store.REUSE_MAX_KEYS or len(orders) > plan_store.REUSE_MAX_KEYS:
        return None
    cp = state.cp
    materials: set[str] = set()
    for k in ops:
        i = cp.op_index.get(k)
        if i is None:
            continue
        op = cp.ops[i]
        materials.update(cp.materials[mi].id for mi, _q in op.materials)
        if op.produces is not None:
            materials.add(cp.materials[op.produces[0]].id)
    return plan_store.Reuse(parent.id, ops, orders, materials)


def _pos(s: Session, plan_id: uuid.UUID, op_key: str) -> dict[str, Any]:
    so = s.scalar(select(ScheduledOperation).where(ScheduledOperation.plan_id == plan_id, ScheduledOperation.op_key == op_key))
    if so is None:
        return {}
    return {"resource": so.resource_key, "start": _aware(so.setup_start).isoformat()}


def _info_from_plan(s: Session, plan: Plan, op_rows: dict[str, tuple] | None = None):
    from .problem_builder import BuildInfo

    info = BuildInfo(plant_id=plan.plant_id, scenario_id=plan.scenario_id)
    SO = ScheduledOperation
    if op_rows is not None:
        info.op_rows = op_rows
    else:
        for r in s.execute(select(SO.op_key, SO.order_id, SO.order_operation_id).where(SO.plan_id == plan.id, SO.order_operation_id.is_not(None))):
            if r.order_id is not None:
                info.op_rows[r.op_key] = (r.order_id, r.order_operation_id)
    info.issues = (plan.analysis or {}).get("data_issues", [])
    return info


def set_locks(s: Session, ctx: Ctx, plan_id: uuid.UUID, op_keys: list[str], locked: bool) -> int:
    ctx.require("plan:edit")
    plan, sc = _require_head(s, ctx, plan_id)
    check_edit(ctx, sc)
    n = 0
    for part in plan_store.chunks(op_keys):
        res = s.execute(update(ScheduledOperation).where(ScheduledOperation.plan_id == plan.id, ScheduledOperation.op_key.in_(part)).values(is_locked=locked).execution_options(synchronize_session=False))
        n += res.rowcount or 0
    forget_move_base(plan.id)  # locked operations are fixed in the plan's problem
    audit.record(s, ctx, "LOCK" if locked else "UNLOCK", "plan", plan.id, plan.number, after={"operations": op_keys[:500], "count": len(op_keys)})
    return n


def undo(s: Session, ctx: Ctx, scenario_id: uuid.UUID) -> Plan:
    ctx.require("plan:edit")
    sc = get_scenario(s, ctx, scenario_id)
    check_edit(ctx, sc)
    cur = head_plan(s, sc)
    if cur is None or cur.parent_id is None:
        raise Conflict("Nothing to undo.", code="NOTHING_TO_UNDO")
    parent = s.get(Plan, cur.parent_id)
    sc.redo_stack = [str(cur.id)] + list(sc.redo_stack or [])
    sc.head_plan_id = parent.id
    audit.record(s, ctx, "UNDO", "scenario", sc.id, sc.name, before={"head": cur.number}, after={"head": parent.number})
    bus().publish("plan.head_changed", str(ctx.tenant_id), {"scenario_id": str(sc.id), "plan_id": str(parent.id)}, str(sc.plant_id))
    return parent


def redo(s: Session, ctx: Ctx, scenario_id: uuid.UUID) -> Plan:
    ctx.require("plan:edit")
    sc = get_scenario(s, ctx, scenario_id)
    check_edit(ctx, sc)
    stack = list(sc.redo_stack or [])
    if not stack:
        raise Conflict("Nothing to redo.", code="NOTHING_TO_REDO")
    nxt = s.get(Plan, uuid.UUID(stack[0]))
    sc.redo_stack = stack[1:]
    sc.head_plan_id = nxt.id
    audit.record(s, ctx, "REDO", "scenario", sc.id, sc.name, after={"head": nxt.number})
    bus().publish("plan.head_changed", str(ctx.tenant_id), {"scenario_id": str(sc.id), "plan_id": str(nxt.id)}, str(sc.plant_id))
    return nxt


def restore(s: Session, ctx: Ctx, plan_id: uuid.UUID, reason: str | None = None) -> Plan:
    """Make an older version the head again (history is kept)."""
    ctx.require("plan:edit")
    plan = s.get(Plan, plan_id)
    if plan is None:
        raise NotFound("Plan not found")
    sc = get_scenario(s, ctx, plan.scenario_id)
    check_edit(ctx, sc)
    old = sc.head_plan_id
    sc.head_plan_id = plan.id
    sc.redo_stack = []
    audit.record(s, ctx, "RESTORE_VERSION", "scenario", sc.id, sc.name, before={"head": str(old)}, after={"head": plan.number}, reason=reason)
    return plan


# =============================================================================================
# Repair / rescheduling
# =============================================================================================


def reschedule(s: Session, ctx: Ctx, scenario_id: uuid.UUID, scope: str = "LOCAL", allow_frozen: bool = False, note: str | None = None) -> tuple[Plan, dict[str, Any]]:
    """Re-plan the scenario's head plan under current data + scenario changes (event reaction)."""
    ctx.require("plan:run")
    sc = get_scenario(s, ctx, scenario_id)
    check_edit(ctx, sc)
    if allow_frozen:
        ctx.require("plan:frozen")
    base = head_plan(s, sc)
    if base is None:
        raise Conflict("The scenario has no plan yet: run the optimiser first.", code="NO_PLAN")
    frozen = published_plan(s, sc.plant_id) if sc.is_live else base
    overrides = {"constraints": {"frozen": "ALLOW_CHANGES"}} if allow_frozen else None
    problem, info = build_problem(s, sc, baseline_plan=base, frozen_plan=None if allow_frozen else frozen, overrides=overrides)
    out = engine_repair(problem, scope=scope, allow_frozen=allow_frozen, baseline_solution=solution_from_plan(s, base))
    plan = persist_solution(s, ctx, sc, problem, out.solution, info, kind="REPAIR", parent=base, note=note or f"{scope.lower()} reschedule", change_summary=_summary(out.comparison or {}))
    from .alerts import generate_alerts

    generate_alerts(s, ctx, plan, out.solution, sc)
    result = {
        "plan_id": str(plan.id),
        "plan_number": plan.number,
        "scope": scope,
        "affected_operations": out.affected_ops,
        "affected_orders": out.affected_orders,
        "freed_operations": len(out.freed_ops),
        "messages": out.messages + info.change_log,
        "comparison": out.comparison,
    }
    audit.record(s, ctx, "RESCHEDULE", "scenario", sc.id, sc.name, after={"plan": plan.number, "scope": scope, "affected": len(out.affected_ops)}, reason=note)
    return plan, result


# =============================================================================================
# Validation and publication
# =============================================================================================


def validate_plan(s: Session, ctx: Ctx, plan_id: uuid.UUID) -> dict[str, Any]:
    """Re-validate the plan's schedule against the *current* data (master data may have changed)."""
    ctx.require("plan:read")
    plan = s.get(Plan, plan_id)
    if plan is None:
        raise NotFound("Plan not found")
    sc = get_scenario(s, ctx, plan.scenario_id)
    problem, _info = build_problem(s, sc, as_of=_aware(plan.horizon_start), baseline_plan=plan, frozen_plan=None)
    SO = ScheduledOperation
    pos = {r.op_key: r for r in s.execute(select(SO.op_key, SO.resource_key, SO.setup_start, SO.end, SO.setup_minutes).where(SO.plan_id == plan.id))}
    for op in problem.operations:
        r = pos.get(op.id)
        if r is not None:
            op.fixed = FixedAssignmentSpec.fast(resource_id=r.resource_key, start=_aware(r.setup_start), end=_aware(r.end), reason="KEPT", setup_minutes=r.setup_minutes)
    problem.solver = problem.solver.model_copy(update={"provider": "heuristic", "local_search": False, "multi_start": False, "explain": False})
    sol = solve(problem)
    hard = [v for v in sol.violations if v.hardness == "HARD" and v.severity == "CRITICAL"]
    op_ids = {op.id for op in problem.operations}
    new_ops = [op.id for op in problem.operations if op.id not in pos]
    missing = [k for k in pos if k not in op_ids]
    result = {
        "plan_id": str(plan.id),
        "feasible": not hard,
        "violations": [v.model_dump(mode="json") for v in sol.violations][:500],
        "hard_count": len(hard),
        "soft_count": sum(1 for v in sol.violations if v.hardness == "SOFT"),
        "operations_not_in_plan": new_ops[:200],
        "operations_no_longer_needed": missing[:200],
        "checked_at": now().isoformat(),
    }
    if not hard and plan.status == "DRAFT":
        plan.status = "VALIDATED"
        plan.validated_at = now()
    audit.record(s, ctx, "PLAN_VALIDATED", "plan", plan.id, plan.number, after={"feasible": result["feasible"], "hard": len(hard)})
    return result


def final_validation(s: Session, plan: Plan) -> dict[str, Any]:
    """Independent validation of a stored plan version against its own problem snapshot: the stored
    positions are replayed exactly and checked by the validator (not by the builder that produced
    them). Operations kept from a previous plan are validated like any other placement (release, start
    in the past…); only the plan's genuine fixings (in progress, locked, frozen) are exempt from them."""
    from monxuplan_engine.builder import Placement
    from monxuplan_engine.validator import validate

    from .engine_view import replay

    cp, res = replay(s, plan)
    placements: list[Placement | None] = []
    for p in res.placements:
        if p is not None and p.fixed_reason == "KEPT" and cp.ops[p.op].fixed is None:
            q = copy.copy(p)  # the replay is cached: never mutate its placements
            q.fixed, q.fixed_reason = False, None
            placements.append(q)
        else:
            placements.append(p)
    val = validate(cp, placements, res.unscheduled, include_data_issues=True)
    hard = [v for v in val.violations if v.hardness == "HARD" and v.type != "UNSCHEDULED" and not v.type.startswith("DATA_")]
    data_critical = [v for v in val.violations if v.type.startswith("DATA_") and v.severity == "CRITICAL"]
    return {
        "hard": hard,
        "unscheduled": len(res.unscheduled),
        "data_critical": data_critical,
        "operations": sum(1 for p in placements if p is not None),
        "describe": lambda vs: [{"type": v.type, "message": v.message, "op": cp.ops[v.op].id if v.op is not None else None} for v in vs[:20]],
    }


PUBLISH_BLOCKERS = ("NOT_CURRENT_VERSION", "NOT_VALIDATED", "HARD_VIOLATIONS", "UNSCHEDULED_OPERATIONS", "CRITICAL_DATA_ISSUES")


def publish(s: Session, ctx: Ctx, plan_id: uuid.UUID, reason: str | None = None, force: bool = False) -> Plan:
    """Publication gate (PLANNING_ENGINE.md, "Publication"). A plan becomes the plant's live plan only if

    1. it is the current version (head) of its scenario and not stale,
    2. it is VALIDATED when the plant requires it (setting ``publish_requires_validation``),
    3. the independent validator, re-run now on the stored schedule, finds no HARD violation,
    4. no operation is unscheduled, and
    5. no critical data issue affects its input.

    Conditions 2–5 can be overridden only with ``force`` and a reason; the overrides are recorded on the
    plan and in the audit log. Condition 1 is never overridden (restore the version first). The plant row
    is locked for the duration, so two publications of one plant are serialised.
    """
    ctx.require("plan:publish")
    plan = s.get(Plan, plan_id)
    if plan is None:
        raise NotFound("Plan not found")
    sc = get_scenario(s, ctx, plan.scenario_id)
    if force and not (reason or "").strip():
        raise ValidationFailed("A reason is required to override the publication checks.", code="REASON_REQUIRED")
    # serialise publishers of one plant, and read the current state under that lock
    plant = s.get(Plant, plan.plant_id, with_for_update=True)
    s.refresh(plan, with_for_update=True)
    s.refresh(sc)
    if plan.status == "PUBLISHED":
        raise Conflict("This plan is already published.", code="ALREADY_PUBLISHED")
    if plan.status == "STALE" or sc.head_plan_id != plan.id:
        raise Conflict(
            "Only the current version of a scenario can be published. Restore this version first if it is the one to publish.",
            code="NOT_CURRENT_VERSION",
            context={"head_plan_id": str(sc.head_plan_id) if sc.head_plan_id else None},
        )
    blockers: list[dict[str, Any]] = []
    if (plant.settings or {}).get("publish_requires_validation") and plan.status != "VALIDATED":
        blockers.append({"code": "NOT_VALIDATED", "message": "This plant requires plans to be validated against the current data before publication."})
    fv = final_validation(s, plan)
    if fv["hard"]:
        blockers.append({"code": "HARD_VIOLATIONS", "message": f"{len(fv['hard'])} hard constraint violation(s) in the stored schedule.", "details": fv["describe"](fv["hard"])})
    if fv["unscheduled"]:
        blockers.append({"code": "UNSCHEDULED_OPERATIONS", "message": f"{fv['unscheduled']} operation(s) of the plan's orders are not scheduled: the plan is incomplete."})
    if fv["data_critical"]:
        blockers.append({"code": "CRITICAL_DATA_ISSUES", "message": f"{len(fv['data_critical'])} critical data issue(s) in the plan's input.", "details": fv["describe"](fv["data_critical"])})
    if blockers and not force:
        raise ValidationFailed(
            "The plan cannot be published: " + " ".join(b["message"] for b in blockers) + " Resolve them, or override explicitly with a reason.",
            code="PUBLISH_BLOCKED",
            context={"blockers": blockers},
        )
    prev_id = plant.published_plan_id
    if prev_id:
        prev = s.get(Plan, prev_id)
        if prev is not None:
            prev.status = "SUPERSEDED"
    plan.status = "PUBLISHED"
    plan.published_at = now()
    plan.published_by = ctx.username
    plan.publish_reason = reason
    plan.publish_overrides = [{"code": b["code"], "message": b["message"]} for b in blockers]
    plant.published_plan_id = plan.id
    if not sc.is_live:
        # publishing a what-if makes it the plant's live plan from now on
        audit.record(s, ctx, "SCENARIO_PROMOTED", "scenario", sc.id, sc.name, reason=reason)
    audit.record(
        s,
        ctx,
        "PLAN_PUBLISHED_FORCED" if blockers else "PLAN_PUBLISHED",
        "plan",
        plan.id,
        plan.number,
        before={"published": str(prev_id) if prev_id else None},
        after={"published": plan.number, "overrides": plan.publish_overrides, "operations_validated": fv["operations"]},
        reason=reason,
    )
    bus().publish("plan.published", str(ctx.tenant_id), {"plan_id": str(plan.id), "number": plan.number}, str(plan.plant_id))
    from .webhooks import emit

    emit(s, ctx.tenant_id, "plan.published", {"plan_id": str(plan.id), "number": plan.number, "plant": plant.code, "published_at": plan.published_at.isoformat(), "published_by": ctx.username})
    return plan
