"""Planning service: runs, plan versions, manual changes, undo/redo, repair and publication."""

from __future__ import annotations

import gzip
import logging
import threading
import time
import traceback
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from monxuplan_engine.contract import (
    OrderResult,
    Problem,
    ScheduledOperation as EngScheduled,
    Solution,
    SolverMetadata,
    Violation,
)
from monxuplan_engine.diff import compare_solutions
from monxuplan_engine.pipeline import PIPELINE_STEPS, solve
from monxuplan_engine.providers.mip import NotSupported
from monxuplan_engine.repair import move as engine_move
from monxuplan_engine.repair import repair as engine_repair

from ..core.clock import now
from ..core.db import new_session
from ..core.errors import Conflict, DomainError, Forbidden, NotFound, PlanningBlocked, ValidationFailed
from ..core.events import bus
from ..core.observability import PLANNING_RUNS, SOLVER_SECONDS
from ..models import ConstraintViolation, KpiValue, Plan, PlanningRun, Plant, ProblemSnapshot, Scenario, ScheduledOperation
from . import audit
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
    s.add(run)
    s.flush()
    audit.record(s, ctx, "PLANNING_RUN_QUEUED", "scenario", sc.id, sc.name, after={"run_id": str(run.id), "kind": kind, "params": params})
    bus().publish("planning.run.queued", str(ctx.tenant_id), {"run_id": str(run.id), "scenario_id": str(sc.id), "kind": kind}, str(sc.plant_id))
    return run


def cancel_run(s: Session, ctx: Ctx, run_id: uuid.UUID) -> PlanningRun:
    run = s.get(PlanningRun, run_id)
    if run is None:
        raise NotFound("Run not found")
    ctx.require("plan:run")
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
        try:
            params = run.params or {}
            baseline = head_plan(s, sc)
            frozen = published_plan(s, plant_id) if sc.is_live else baseline
            overrides = {k: params[k] for k in ("objectives", "constraints", "solver", "horizon_days", "frozen_hours", "flexible_days") if k in params}
            if run.kind == "PLAN":
                overrides.setdefault("solver", {})
                overrides["solver"] = {**overrides["solver"], "provider": "heuristic", "local_search": False}
            problem, info = build_problem(s, sc, baseline_plan=baseline, frozen_plan=frozen, overrides=overrides)
            provider = problem.solver.provider
            bus().publish("planning.run.started", str(tenant_id), {"run_id": str(run.id), "scenario_id": str(sc.id)}, str(plant_id))
            solution = solve(problem, progress=prog, cancelled=prog.is_cancelled)
            prog.mark("Save schedule", "RUNNING")
            plan = persist_solution(s, ctx, sc, problem, solution, info, kind="OPTIMIZED" if run.kind != "REPAIR" else "REPAIR", run=run, parent=baseline, note=params.get("note"))
            if baseline is not None:
                try:
                    plan.change_summary = _summary(compare_solutions(solution_from_plan(s, baseline), solution))
                except Exception:  # noqa: BLE001 - comparison is informative only
                    log.exception("comparison with previous plan failed")
            run.plan_id = plan.id
            run.status = "SUCCEEDED"
            md = solution.solver_metadata
            run.provider, run.solver_status, run.objective, run.best_bound, run.gap, run.input_hash = md.provider, md.status, md.objective, md.best_bound, md.gap, md.input_hash
            run.result = {
                "plan_id": str(plan.id),
                "plan_number": plan.number,
                "feasible": solution.feasible,
                "kpis": solution.kpis,
                "messages": md.messages + (["stopped by the user: best plan found so far"] if prog.cancelled else []),
                "change_log": info.change_log,
                "unscheduled": len(solution.unscheduled),
            }
            from .alerts import generate_alerts

            generate_alerts(s, ctx, plan, solution, sc)
            prog.mark("Save schedule", "DONE", plan.number)
            prog.mark("Publish result", "DONE", "draft plan available")
        except NotSupported as exc:
            _fail(run, "PROVIDER_NOT_SUPPORTED", str(exc), None)
        except DomainError as exc:
            _fail(run, exc.code, exc.message, None)
        except Exception as exc:  # noqa: BLE001
            error_id = uuid.uuid4().hex[:10]
            log.exception("planning run failed", extra={"planning_run": str(run_id), "error_id": error_id})
            _fail(run, "ENGINE_ERROR", f"MonxuPlan couldn't generate the schedule (error {error_id}). The technical details were logged for the administrator.", traceback.format_exc()[-8000:])
            prog.mark(prog.current or "Optimize", "FAILED", str(exc)[:200])
        finally:
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


def store_snapshot(s: Session, tenant_id: uuid.UUID, problem: Problem, sha: str) -> ProblemSnapshot:
    snap = s.scalar(select(ProblemSnapshot).where(ProblemSnapshot.sha256 == sha))
    if snap is None:
        blob = gzip.compress(problem.model_dump_json(by_alias=True).encode(), compresslevel=6)
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
) -> Plan:
    md = sol.solver_metadata
    sha = md.input_hash or ""
    # the snapshot never carries the baseline of a previous version: it is rebuilt from the plan chain
    data = problem.model_dump(mode="json", by_alias=True)
    data["baseline"] = []
    clean = Problem.model_validate(data)
    from monxuplan_engine.compile import problem_hash

    snap = store_snapshot(s, ctx.tenant_id, clean, problem_hash(clean))
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
            status="DRAFT",
            feasible=sol.feasible,
            horizon_start=problem.horizon.start,
            horizon_end=problem.horizon.end,
            frozen_until=problem.horizon.frozen_until,
            kpis=sol.kpis,
            kpi_details=sol.kpi_details,
            solver_metadata=md.model_dump(mode="json"),
            analysis={
                "orders": [o.model_dump(mode="json") for o in sol.orders],
                "bottlenecks": [b.model_dump(mode="json") for b in sol.bottlenecks],
                "unscheduled": [u.model_dump(mode="json") for u in sol.unscheduled],
                "pegging": [p.model_dump(mode="json") for p in sol.pegging],
                "data_issues": (info.issues if info else []),
                "excluded_orders": (info.excluded_orders if info else []),
                "change_log": (info.change_log if info else []),
            },
            params={"objectives": problem.objectives.model_dump(mode="json"), "constraints": problem.constraints.model_dump(mode="json"), "solver": problem.solver.model_dump(mode="json")},
            snapshot_id=snap.id,
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
    order_ids = {o.id: o.number for o in problem.orders}
    op_rows = info.op_rows if info else {}
    rows = []
    for x in sol.schedule:
        oid, ooid = op_rows.get(x.op_id, (None, None))
        rows.append(
            {
                "id": uuid.uuid4(),
                "tenant_id": ctx.tenant_id,
                "plan_id": plan.id,
                "op_key": x.op_id,
                "order_id": oid if oid is not None else _uuid_or_none(x.order_id),
                "order_key": x.order_id,
                "order_operation_id": ooid,
                "resource_id": _uuid_or_none(x.resource_id),
                "resource_key": x.resource_id,
                "secondary": [a.model_dump() for a in x.secondary],
                "setup_start": x.setup_start,
                "start": x.start,
                "end": x.end,
                "setup_minutes": x.setup_minutes,
                "run_minutes": x.run_minutes,
                "working_minutes": x.working_minutes,
                "overtime_minutes": x.overtime_minutes,
                "quantity": x.quantity,
                "is_fixed": x.fixed,
                "fixed_reason": x.fixed_reason,
                "is_locked": x.fixed_reason == "LOCKED",
                "is_late": x.late,
                "zone": x.zone,
                "subcontracted": x.subcontracted,
                "prev_op_key": x.prev_op_id,
                "material_ready": x.material_ready,
                "binding": x.binding.model_dump(mode="json"),
                "explanation": sol.explanations[x.op_id].model_dump(mode="json") if x.op_id in sol.explanations else None,
                "cost": x.cost,
            }
        )
    _ = order_ids
    if rows:
        s.execute(ScheduledOperation.__table__.insert(), rows)
    vrows = [
        {
            "id": uuid.uuid4(),
            "tenant_id": ctx.tenant_id,
            "plan_id": plan.id,
            "severity": v.severity,
            "hardness": v.hardness,
            "type": v.type,
            "message": v.message[:4000],
            "order_key": v.order_id,
            "op_key": v.op_id,
            "resource_key": v.resource_id,
            "material_key": v.material_id,
            "start": v.start,
            "end": v.end,
            "details": v.details,
        }
        for v in sol.violations
    ]
    if vrows:
        s.execute(ConstraintViolation.__table__.insert(), vrows)
    krows = [{"id": uuid.uuid4(), "tenant_id": ctx.tenant_id, "plan_id": plan.id, "code": k, "value": float(v) if isinstance(v, int | float) else None, "computed_at": now()} for k, v in sol.kpis.items()]
    if krows:
        s.execute(KpiValue.__table__.insert(), krows)
    # move the scenario head; a new version invalidates the redo stack
    sc.head_plan_id = plan.id
    sc.redo_stack = []
    audit.record(s, ctx, "PLAN_CREATED", "plan", plan.id, plan.number, after={"kind": kind, "scenario": sc.name, "feasible": sol.feasible, "solver": md.provider, "status": md.status, "objective": md.objective, "gap": md.gap})
    bus().publish("plan.created", str(ctx.tenant_id), {"plan_id": str(plan.id), "number": plan.number, "scenario_id": str(sc.id), "kind": kind}, str(sc.plant_id))
    return plan


def _uuid_or_none(v: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(v) if v else None
    except ValueError:
        return None


def solution_from_plan(s: Session, plan: Plan) -> Solution:
    """Rebuild the engine Solution of a stored plan (for comparisons and baselines)."""
    rows = list(s.scalars(select(ScheduledOperation).where(ScheduledOperation.plan_id == plan.id)))
    schedule = []
    for r in rows:
        schedule.append(
            EngScheduled(
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
                binding=r.binding or {"type": "NONE"},
            )
        )
    violations = [
        Violation(severity=v.severity, hardness=v.hardness, type=v.type, message=v.message, order_id=v.order_key, op_id=v.op_key, resource_id=v.resource_key, material_id=v.material_key, details=v.details or {})
        for v in s.scalars(select(ConstraintViolation).where(ConstraintViolation.plan_id == plan.id))
    ]
    orders = [OrderResult.model_validate(o) for o in (plan.analysis or {}).get("orders", [])]
    md = SolverMetadata.model_validate(plan.solver_metadata) if plan.solver_metadata else SolverMetadata(provider="?", status="HEURISTIC")
    return Solution(schedule=schedule, violations=violations, feasible=plan.feasible, orders=orders, kpis=plan.kpis or {}, solver_metadata=md)


def problem_for_plan(s: Session, plan: Plan) -> Problem:
    """The plan's own problem with the plan itself as baseline (for moves and repairs)."""
    problem = load_problem(s, plan)
    data = problem.model_dump(mode="json", by_alias=True)
    data["baseline"] = [
        {"op_id": r.op_key, "resource_id": r.resource_key, "start": _aware(r.start).isoformat(), "end": _aware(r.end).isoformat(), "setup_start": _aware(r.setup_start).isoformat()}
        for r in s.scalars(select(ScheduledOperation).where(ScheduledOperation.plan_id == plan.id))
    ]
    # manual/locked positions of the plan remain fixed
    locked = {r.op_key: r for r in s.scalars(select(ScheduledOperation).where(ScheduledOperation.plan_id == plan.id, ScheduledOperation.is_locked.is_(True)))}
    for op in data["operations"]:
        r = locked.get(op["id"])
        if r is not None and op.get("fixed") is None:
            op["fixed"] = {"resource_id": r.resource_key, "start": _aware(r.setup_start).isoformat(), "end": _aware(r.end).isoformat(), "reason": "LOCKED", "setup_minutes": r.setup_minutes}
    return Problem.model_validate(data)


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


def preview_move(s: Session, ctx: Ctx, plan_id: uuid.UUID, op_key: str, resource_id: str, start: datetime, replan: str = "DOWNSTREAM", allow_frozen: bool = False) -> dict[str, Any]:
    ctx.require("plan:read")
    plan = s.get(Plan, plan_id)
    if plan is None:
        raise NotFound("Plan not found", code="PLAN_NOT_FOUND")
    get_scenario(s, ctx, plan.scenario_id)
    if replan not in REPLAN_MODES:
        raise ValidationFailed(f"Unknown replan mode {replan}")
    if allow_frozen:
        ctx.require("plan:frozen")
    problem = problem_for_plan(s, plan)
    try:
        out = engine_move(problem, op_key, resource_id, start, replan=replan, allow_frozen=allow_frozen, baseline_solution=solution_from_plan(s, plan))
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
        "_problem": problem,
    }


def apply_move(s: Session, ctx: Ctx, plan_id: uuid.UUID, op_key: str, resource_id: str, start: datetime, replan: str, reason: str | None, allow_frozen: bool = False, expected_version: int | None = None, accept_violations: bool = False) -> Plan:
    ctx.require("plan:edit")
    plan, sc = _require_head(s, ctx, plan_id, expected_version)
    check_edit(ctx, sc)
    prev = preview_move(s, ctx, plan_id, op_key, resource_id, start, replan, allow_frozen)
    sol: Solution = prev["_solution"]
    if not sol.feasible and prev["comparison"]["new_hard_violation_count"] and not accept_violations:
        raise ValidationFailed(
            "The move creates hard constraint violations. Choose another replan mode, use automatic repair, or confirm explicitly.",
            code="MOVE_CREATES_VIOLATIONS",
            context={"violations": prev["hard_violations"][:10]},
        )
    problem: Problem = prev["_problem"]
    new_plan = persist_solution(s, ctx, sc, problem, sol, _info_from_plan(s, plan), kind="MANUAL_EDIT", parent=plan, note=reason, change_summary=_summary(prev["comparison"]))
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
    return new_plan


def _pos(s: Session, plan_id: uuid.UUID, op_key: str) -> dict[str, Any]:
    so = s.scalar(select(ScheduledOperation).where(ScheduledOperation.plan_id == plan_id, ScheduledOperation.op_key == op_key))
    if so is None:
        return {}
    return {"resource": so.resource_key, "start": _aware(so.setup_start).isoformat()}


def _info_from_plan(s: Session, plan: Plan):
    from .problem_builder import BuildInfo

    info = BuildInfo(plant_id=plan.plant_id, scenario_id=plan.scenario_id)
    for so in s.scalars(select(ScheduledOperation).where(ScheduledOperation.plan_id == plan.id)):
        if so.order_id is not None and so.order_operation_id is not None:
            info.op_rows[so.op_key] = (so.order_id, so.order_operation_id)
    info.issues = (plan.analysis or {}).get("data_issues", [])
    return info


def set_locks(s: Session, ctx: Ctx, plan_id: uuid.UUID, op_keys: list[str], locked: bool) -> int:
    ctx.require("plan:edit")
    plan, sc = _require_head(s, ctx, plan_id)
    check_edit(ctx, sc)
    n = 0
    for so in s.scalars(select(ScheduledOperation).where(ScheduledOperation.plan_id == plan.id, ScheduledOperation.op_key.in_(op_keys))):
        so.is_locked = locked
        n += 1
    audit.record(s, ctx, "LOCK" if locked else "UNLOCK", "plan", plan.id, plan.number, after={"operations": op_keys})
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
    data = problem.model_dump(mode="json", by_alias=True)
    pos = {r.op_key: r for r in s.scalars(select(ScheduledOperation).where(ScheduledOperation.plan_id == plan.id))}
    for op in data["operations"]:
        r = pos.get(op["id"])
        if r is not None:
            op["fixed"] = {"resource_id": r.resource_key, "start": _aware(r.setup_start).isoformat(), "end": _aware(r.end).isoformat(), "reason": "KEPT", "setup_minutes": r.setup_minutes}
    data["solver"].update({"provider": "heuristic", "local_search": False, "multi_start": False, "explain": False})
    sol = solve(Problem.model_validate(data))
    hard = [v for v in sol.violations if v.hardness == "HARD" and v.severity == "CRITICAL"]
    new_ops = [op["id"] for op in data["operations"] if op["id"] not in pos]
    missing = [k for k in pos if k not in {op["id"] for op in data["operations"]}]
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


def publish(s: Session, ctx: Ctx, plan_id: uuid.UUID, reason: str | None = None, force: bool = False) -> Plan:
    ctx.require("plan:publish")
    plan = s.get(Plan, plan_id)
    if plan is None:
        raise NotFound("Plan not found")
    sc = get_scenario(s, ctx, plan.scenario_id)
    if plan.status == "PUBLISHED":
        raise Conflict("This plan is already published.", code="ALREADY_PUBLISHED")
    hard = s.scalar(
        select(func.count()).select_from(ConstraintViolation).where(ConstraintViolation.plan_id == plan.id, ConstraintViolation.hardness == "HARD", ConstraintViolation.severity == "CRITICAL", ~ConstraintViolation.type.like("DATA_%"), ConstraintViolation.type != "UNSCHEDULED")
    )
    if hard and not force:
        raise ValidationFailed(f"The plan has {hard} hard constraint violation(s). Resolve them or publish with an explicit override and reason.", code="PLAN_HAS_VIOLATIONS")
    if force and hard and not reason:
        raise ValidationFailed("A reason is required to publish a plan with violations.", code="REASON_REQUIRED")
    plant = s.get(Plant, plan.plant_id)
    prev_id = plant.published_plan_id
    if prev_id:
        prev = s.get(Plan, prev_id)
        if prev is not None:
            prev.status = "SUPERSEDED"
    plan.status = "PUBLISHED"
    plan.published_at = now()
    plan.published_by = ctx.username
    plan.publish_reason = reason
    plant.published_plan_id = plan.id
    if not sc.is_live:
        # publishing a what-if makes it the plant's live plan from now on
        audit.record(s, ctx, "SCENARIO_PROMOTED", "scenario", sc.id, sc.name, reason=reason)
    audit.record(s, ctx, "PLAN_PUBLISHED", "plan", plan.id, plan.number, before={"published": str(prev_id) if prev_id else None}, after={"published": plan.number}, reason=reason)
    bus().publish("plan.published", str(ctx.tenant_id), {"plan_id": str(plan.id), "number": plan.number}, str(plan.plant_id))
    from .webhooks import emit

    emit(s, ctx.tenant_id, "plan.published", {"plan_id": str(plan.id), "number": plan.number, "plant": plant.code, "published_at": plan.published_at.isoformat(), "published_by": ctx.username})
    return plan
