"""KPIs, drill-down, bottlenecks, capacity, comparisons, trends, plan vs actual."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query

from ...services import analytics as an
from ...services import views
from ...services.context import Ctx
from ..deps import get_ctx, get_db

router = APIRouter(tags=["analytics"])


@router.get("/kpis")
def kpis(plan_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return an.kpis(s, ctx, plan_id)


@router.get("/kpis/{code}/drilldown")
def drilldown(code: str, plan_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return an.drilldown(s, ctx, plan_id, code)


@router.get("/bottlenecks")
def bottlenecks(plan_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return an.bottlenecks(s, ctx, plan_id)


@router.get("/capacity/load")
def load(plan_id: uuid.UUID, bucket: str = "day", group_by: str = Query("resource", pattern="^(resource|group|area|plant)$"), start: datetime | None = None, end: datetime | None = None, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return an.capacity(s, ctx, plan_id, bucket, group_by, start, end)


@router.get("/analytics/compare")
def compare(plan_ids: list[uuid.UUID] = Query(...), ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return an.compare_plans(s, ctx, plan_ids)


@router.get("/analytics/trends")
def trends(plant_id: uuid.UUID, codes: list[str] = Query(["otif", "late_orders", "setup_h", "utilization"]), limit: int = 30, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return an.trends(s, ctx, plant_id, codes, limit)


@router.get("/analytics/plan-vs-actual")
def plan_vs_actual(plant_id: uuid.UUID, days: int = Query(14, ge=1, le=120), ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return views.plan_vs_actual(s, ctx, plant_id, days)
