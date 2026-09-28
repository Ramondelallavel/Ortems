"""Scenarios, what-ifs, undo/redo and rescheduling."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, Field

from ...services import planning as planning_svc
from ...services import scenarios as sc_svc
from ...services.context import Ctx
from ...services.views import list_plans, plan_summary
from ..deps import get_ctx, get_db

router = APIRouter(tags=["scenarios"])


class ScenarioIn(BaseModel):
    plant_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    clone_from: uuid.UUID | None = None
    config: dict[str, Any] | None = None
    visibility: str = "SHARED"


@router.get("/scenarios")
def list_scenarios(plant_id: uuid.UUID, include_archived: bool = False, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return sc_svc.list_scenarios(s, ctx, plant_id, include_archived)


@router.post("/scenarios", status_code=201)
def create(body: ScenarioIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    sc = sc_svc.create_scenario(s, ctx, body.plant_id, body.name, body.description, body.clone_from, body.config, body.visibility)
    return sc_svc.scenario_dict(s, sc)


@router.get("/scenarios/{scenario_id}")
def get(scenario_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    ctx.require("scenario:read")
    return sc_svc.scenario_dict(s, planning_svc.get_scenario(s, ctx, scenario_id))


@router.patch("/scenarios/{scenario_id}")
def update(scenario_id: uuid.UUID, body: dict[str, Any] = Body(...), ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return sc_svc.scenario_dict(s, sc_svc.update_scenario(s, ctx, scenario_id, body))


class CloneIn(BaseModel):
    name: str
    description: str | None = None


@router.post("/scenarios/{scenario_id}/clone", status_code=201)
def clone(scenario_id: uuid.UUID, body: CloneIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    src = planning_svc.get_scenario(s, ctx, scenario_id)
    sc = sc_svc.create_scenario(s, ctx, src.plant_id, body.name, body.description, clone_from=src.id)
    return sc_svc.scenario_dict(s, sc)


class ChangeIn(BaseModel):
    type: str
    payload: dict[str, Any]
    description: str | None = None


@router.post("/scenarios/{scenario_id}/changes", status_code=201)
def add_change(scenario_id: uuid.UUID, body: ChangeIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    sc_svc.add_change(s, ctx, scenario_id, body.type, body.payload, body.description)
    return sc_svc.scenario_dict(s, planning_svc.get_scenario(s, ctx, scenario_id))


@router.delete("/scenarios/{scenario_id}/changes/{change_id}")
def remove_change(scenario_id: uuid.UUID, change_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    sc_svc.remove_change(s, ctx, scenario_id, change_id)
    return sc_svc.scenario_dict(s, planning_svc.get_scenario(s, ctx, scenario_id))


@router.post("/scenarios/{scenario_id}/lock")
def lock(scenario_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return sc_svc.scenario_dict(s, sc_svc.lock(s, ctx, scenario_id))


@router.post("/scenarios/{scenario_id}/unlock")
def unlock(scenario_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return sc_svc.scenario_dict(s, sc_svc.lock(s, ctx, scenario_id, release=True))


@router.post("/scenarios/{scenario_id}/archive")
def archive(scenario_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return sc_svc.scenario_dict(s, sc_svc.archive(s, ctx, scenario_id))


@router.get("/scenarios/{scenario_id}/plans")
def plans(scenario_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return list_plans(s, ctx, scenario_id)


@router.post("/scenarios/{scenario_id}/undo")
def undo(scenario_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return plan_summary(planning_svc.undo(s, ctx, scenario_id))


@router.post("/scenarios/{scenario_id}/redo")
def redo(scenario_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return plan_summary(planning_svc.redo(s, ctx, scenario_id))


class RescheduleIn(BaseModel):
    scope: str = Field(default="LOCAL", pattern="^(LOCAL|REGIONAL|GLOBAL)$")
    allow_frozen: bool = False
    note: str | None = None


@router.post("/scenarios/{scenario_id}/reschedule")
def reschedule(scenario_id: uuid.UUID, body: RescheduleIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    _plan, result = planning_svc.reschedule(s, ctx, scenario_id, body.scope, body.allow_frozen, body.note)
    return result


class WhatIfIn(BaseModel):
    plant_id: uuid.UUID
    kind: str = Field(pattern="^(NIGHT_SHIFT|ADD_MACHINE|RUSH_ORDER|MATERIAL_DELAY|BREAKDOWN|ADD_OPERATOR|OVERTIME)$")
    params: dict[str, Any] = Field(default_factory=dict)
    base_scenario_id: uuid.UUID | None = None
    name: str | None = None
    run: bool = True
    time_limit_s: float | None = None


@router.post("/scenarios/what-if", status_code=201)
def what_if(body: WhatIfIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    sc, changes = sc_svc.what_if(s, ctx, body.plant_id, body.kind, body.params, body.base_scenario_id, body.name)
    out = sc_svc.scenario_dict(s, sc)
    if body.run:
        params: dict[str, Any] = {"force": True, "note": f"what-if {body.kind}"}
        if body.time_limit_s:
            params["solver"] = {"time_limit_s": body.time_limit_s}
        run = planning_svc.enqueue_run(s, ctx, sc.id, "OPTIMIZE", params)
        out["run_id"] = str(run.id)
    return out
