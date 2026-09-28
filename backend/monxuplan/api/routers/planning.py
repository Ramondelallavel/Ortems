"""Planning runs and engine utilities."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from monxuplan_engine.contract import Problem
from monxuplan_engine.objectives import PRESETS
from monxuplan_engine.pipeline import PIPELINE_STEPS, PROFILE_LIMITS
from monxuplan_engine.providers import available, get_provider

from ...core.errors import NotFound
from ...models import PlanningRun
from ...services import planning as planning_svc
from ...services.context import Ctx
from ...services.masterdata import row_dict
from ..deps import get_ctx, get_db

router = APIRouter(tags=["planning"])


class RunIn(BaseModel):
    scenario_id: uuid.UUID
    mode: str = Field(default="OPTIMIZE", pattern="^(OPTIMIZE|PLAN|REPAIR|VALIDATE)$")
    horizon_days: float | None = Field(default=None, gt=0, le=400)
    frozen_hours: float | None = Field(default=None, ge=0)
    objectives: dict[str, Any] | None = None
    constraints: dict[str, Any] | None = None
    solver: dict[str, Any] | None = None
    force: bool = False
    note: str | None = None


@router.post("/planning/run", status_code=202)
def run(body: RunIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    params = {k: v for k, v in body.model_dump().items() if k not in ("scenario_id", "mode") and v is not None}
    r = planning_svc.enqueue_run(s, ctx, body.scenario_id, body.mode, params)
    return {"run_id": str(r.id), "status": r.status, "steps": PIPELINE_STEPS}


@router.get("/planning/runs/{run_id}")
def get_run(run_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    ctx.require("plan:read")
    r = s.get(PlanningRun, run_id)
    if r is None:
        raise NotFound("Run not found")
    out = row_dict(r)
    if "admin:config" not in ctx.permissions:
        out.pop("error_detail", None)
    return out


@router.get("/planning/runs")
def list_runs(scenario_id: uuid.UUID, limit: int = 20, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from sqlalchemy import select

    ctx.require("plan:read")
    rows = s.scalars(select(PlanningRun).where(PlanningRun.scenario_id == scenario_id).order_by(PlanningRun.created_at.desc()).limit(limit))
    return [{k: v for k, v in row_dict(r).items() if k != "error_detail"} for r in rows]


@router.post("/planning/runs/{run_id}/cancel")
def cancel(run_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return row_dict(planning_svc.cancel_run(s, ctx, run_id))


class FeasIn(BaseModel):
    scenario_id: uuid.UUID


@router.post("/planning/feasibility")
def feasibility(body: FeasIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from monxuplan_engine.compile import compile_problem
    from monxuplan_engine.feasibility import check

    from ...services.problem_builder import build_problem

    ctx.require("plan:read")
    sc = planning_svc.get_scenario(s, ctx, body.scenario_id)
    problem, info = build_problem(s, sc, baseline_plan=None, frozen_plan=None)
    res = check(compile_problem(problem))
    res["data_issues"] = info.issues
    return res


@router.get("/planning/contract")
def contract():
    """JSON schema of the Planning Engine input contract."""
    return Problem.model_json_schema()


@router.get("/planning/presets")
def presets():
    return {"presets": PRESETS, "time_limits": PROFILE_LIMITS, "steps": PIPELINE_STEPS}


@router.get("/planning/providers")
def providers():
    out = []
    for name in available():
        cap = get_provider(name).capabilities()
        out.append({"name": name, "detailed_scheduling": cap.detailed_scheduling, "aggregate_planning": cap.aggregate_planning, "proves_optimality": cap.proves_optimality, "max_recommended_ops": cap.max_recommended_ops})
    return out


class MpsIn(BaseModel):
    plant_id: uuid.UUID
    weeks: int = Field(default=12, ge=1, le=52)
    start: date | None = None


@router.post("/planning/mrp")
def mrp(body: MpsIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services.mps import run_mps

    return run_mps(s, ctx, body.plant_id, body.weeks, body.start)


class FirmIn(BaseModel):
    plant_id: uuid.UUID
    planned_orders: list[dict[str, Any]]


@router.post("/planning/mrp/firm")
def firm(body: FirmIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services.mps import firm_planned_orders

    return firm_planned_orders(s, ctx, body.plant_id, body.planned_orders)


class AggIn(BaseModel):
    plant_id: uuid.UUID
    weeks: int = Field(default=12, ge=2, le=52)
    integer: bool = False


@router.post("/planning/aggregate")
def aggregate(body: AggIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services.mps import aggregate_plan

    return aggregate_plan(s, ctx, body.plant_id, body.weeks, body.integer)
