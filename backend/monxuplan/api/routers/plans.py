"""Plans: Gantt, detail, explainability, manual moves, validation, publication, export, robustness."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...models import ConstraintViolation, ScheduledOperation
from ...services import planning as planning_svc
from ...services import views
from ...services.context import Ctx
from ...services.masterdata import row_dict
from ..deps import get_ctx, get_db

router = APIRouter(tags=["plans"])


@router.get("/plans/{plan_id}")
def plan(plan_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return views.plan_header(s, ctx, plan_id)


@router.get("/plans/{plan_id}/gantt")
def gantt(plan_id: uuid.UUID, start: datetime | None = None, end: datetime | None = None, resource_ids: list[str] | None = Query(None), include_secondary: bool = False, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return views.gantt(s, ctx, plan_id, start, end, resource_ids, include_secondary)


@router.get("/plans/{plan_id}/schedule")
def schedule(plan_id: uuid.UUID, resource_id: str | None = None, order_id: str | None = None, offset: int = 0, limit: int = Query(2000, le=20000), ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    views._get_plan(s, ctx, plan_id)
    q = select(ScheduledOperation).where(ScheduledOperation.plan_id == plan_id).order_by(ScheduledOperation.resource_key, ScheduledOperation.start)
    if resource_id:
        q = q.where(ScheduledOperation.resource_key == resource_id)
    if order_id:
        q = q.where(ScheduledOperation.order_key == order_id)
    return [row_dict(r, skip={"explanation"}) for r in s.scalars(q.offset(offset).limit(limit))]


@router.get("/plans/{plan_id}/violations")
def violations(plan_id: uuid.UUID, hardness: str | None = None, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    views._get_plan(s, ctx, plan_id)
    q = select(ConstraintViolation).where(ConstraintViolation.plan_id == plan_id)
    if hardness:
        q = q.where(ConstraintViolation.hardness == hardness)
    sev = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
    rows = [row_dict(v) for v in s.scalars(q)]
    rows.sort(key=lambda v: (sev.get(v["severity"], 3), v["type"]))
    return rows


@router.get("/plans/{plan_id}/unscheduled")
def unscheduled(plan_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    p = views._get_plan(s, ctx, plan_id)
    return (p.analysis or {}).get("unscheduled", [])


@router.get("/plans/{plan_id}/orders")
def plan_orders(plan_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    p = views._get_plan(s, ctx, plan_id)
    return (p.analysis or {}).get("orders", [])


@router.get("/plans/{plan_id}/operations/{op_key:path}/explore")
def explore(plan_id: uuid.UUID, op_key: str, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return views.explore(s, ctx, plan_id, op_key)


class CheckIn(BaseModel):
    resource_id: str
    start: datetime


@router.post("/plans/{plan_id}/operations/{op_key:path}/check")
def check(plan_id: uuid.UUID, op_key: str, body: CheckIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return views.position_check(s, ctx, plan_id, op_key, body.resource_id, body.start)


@router.get("/plans/{plan_id}/operations/{op_key:path}")
def operation(plan_id: uuid.UUID, op_key: str, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return views.operation_detail(s, ctx, plan_id, op_key)


@router.get("/plans/{plan_id}/order-detail/{order_key}")
def order_detail(plan_id: uuid.UUID, order_key: str, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return views.order_detail(s, ctx, plan_id, order_key)


@router.get("/plans/{plan_id}/order-chain/{order_key}")
def order_chain(plan_id: uuid.UUID, order_key: str, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return views.order_chain(s, ctx, plan_id, order_key)


class MoveIn(BaseModel):
    op_id: str
    resource_id: str
    start: datetime
    replan: str = Field(default="DOWNSTREAM", pattern="^(NO_REPLAN|THIS_ORDER|DOWNSTREAM|RESOURCE|AREA|SCENARIO)$")
    allow_frozen: bool = False
    reason: str | None = Field(default=None, max_length=1000)
    expected_version: int | None = None
    accept_violations: bool = False


@router.post("/plans/{plan_id}/moves/preview")
def preview(plan_id: uuid.UUID, body: MoveIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    out = planning_svc.preview_move(s, ctx, plan_id, body.op_id, body.resource_id, body.start, body.replan, body.allow_frozen)
    out.pop("_solution", None)
    out.pop("_problem", None)
    return out


@router.post("/plans/{plan_id}/moves")
def apply_move(plan_id: uuid.UUID, body: MoveIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    p = planning_svc.apply_move(s, ctx, plan_id, body.op_id, body.resource_id, body.start, body.replan, body.reason, body.allow_frozen, body.expected_version, body.accept_violations)
    return views.plan_summary(p)


class LocksIn(BaseModel):
    op_ids: list[str]
    locked: bool = True


@router.post("/plans/{plan_id}/locks")
def locks(plan_id: uuid.UUID, body: LocksIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return {"updated": planning_svc.set_locks(s, ctx, plan_id, body.op_ids, body.locked)}


class LockSeqIn(BaseModel):
    resource_id: str
    op_ids: list[str] = Field(min_length=2)


@router.post("/plans/{plan_id}/lock-sequence")
def lock_sequence(plan_id: uuid.UUID, body: LockSeqIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services.scenarios import add_change

    p = views._get_plan(s, ctx, plan_id)
    add_change(s, ctx, p.scenario_id, "LOCK_SEQUENCE", {"resource_id": body.resource_id, "op_ids": body.op_ids}, f"Lock sequence of {len(body.op_ids)} operations")
    return {"ok": True, "message": "Sequence locked: it will be respected by the next planning run."}


@router.post("/plans/{plan_id}/validate")
def validate(plan_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return planning_svc.validate_plan(s, ctx, plan_id)


class PublishIn(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)
    force: bool = False


@router.post("/plans/{plan_id}/publish")
def publish(plan_id: uuid.UUID, body: PublishIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return views.plan_summary(planning_svc.publish(s, ctx, plan_id, body.reason, body.force))


class RestoreIn(BaseModel):
    reason: str | None = None


@router.post("/plans/{plan_id}/restore")
def restore(plan_id: uuid.UUID, body: RestoreIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return views.plan_summary(planning_svc.restore(s, ctx, plan_id, body.reason))


@router.get("/plans/{plan_id}/export")
def export(plan_id: uuid.UUID, format: str = Query("xlsx", pattern="^(xlsx|csv|json)$"), sheet: str = "Schedule", ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services.exports import export_plan

    ctx.require("integration:export")
    data, media, filename = export_plan(s, ctx, plan_id, format, sheet)
    return Response(content=data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


class SimIn(BaseModel):
    runs: int = Field(default=30, ge=5, le=200)
    runtime_cv: float = Field(default=0.10, ge=0, le=1)
    supplier_delay_prob: float = Field(default=0.10, ge=0, le=1)
    supplier_delay_days: float = Field(default=3, ge=0, le=60)
    breakdowns_per_week: float = Field(default=0.2, ge=0, le=10)
    breakdown_hours: float = Field(default=4, ge=0, le=200)
    seed: int = 42


@router.post("/plans/{plan_id}/simulate")
def simulate(plan_id: uuid.UUID, body: SimIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from monxuplan_engine.simulation import monte_carlo

    ctx.require("analytics:read")
    p = views._get_plan(s, ctx, plan_id)
    problem = planning_svc.load_problem(s, p)
    return monte_carlo(problem, planning_svc.solution_from_plan(s, p), **body.model_dump())


@router.post("/plans/{plan_id}/sensitivity")
def sensitivity(plan_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from monxuplan_engine.contract import Bottleneck
    from monxuplan_engine.sensitivity import analyse

    ctx.require("analytics:read")
    p = views._get_plan(s, ctx, plan_id)
    problem = planning_svc.load_problem(s, p)
    sol = planning_svc.solution_from_plan(s, p)
    sol.bottlenecks = [Bottleneck.model_validate(b) for b in (p.analysis or {}).get("bottlenecks", [])]
    return analyse(problem, sol)


@router.get("/plans/{plan_id}/diff/{other_id}")
def diff(plan_id: uuid.UUID, other_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from monxuplan_engine.diff import compare_solutions

    a = views._get_plan(s, ctx, plan_id)
    b = views._get_plan(s, ctx, other_id)
    return compare_solutions(planning_svc.solution_from_plan(s, a), planning_svc.solution_from_plan(s, b))


_ = Any
