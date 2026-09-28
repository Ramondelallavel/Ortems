"""Dispatch lists, supervisor and operator views, execution feedback."""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from ...services import views
from ...services.context import Ctx
from ..deps import get_ctx, get_db
from ..fastjson import fast_json

router = APIRouter(tags=["shop-floor"])


@router.get("/dispatch")
def dispatch(plant_id: uuid.UUID, resource_id: str | None = None, date_from: datetime | None = None, hours: int = Query(24, ge=1, le=24 * 14), ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return fast_json(views.dispatch_list(s, ctx, plant_id, resource_id, date_from, hours))


@router.get("/supervisor")
def supervisor(plant_id: uuid.UUID, area_id: uuid.UUID | None = None, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return fast_json(views.supervisor_view(s, ctx, plant_id, area_id))


@router.get("/operator")
def operator(plant_id: uuid.UUID, resource_id: str, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return views.operator_view(s, ctx, plant_id, resource_id)


class ReportIn(BaseModel):
    order_operation_id: uuid.UUID
    action: str = Field(pattern="^(START|PAUSE|RESUME|FINISH|QUANTITY)$")
    resource_id: uuid.UUID | None = None
    good_quantity: float = Field(default=0, ge=0)
    scrap_quantity: float = Field(default=0, ge=0)
    at: datetime | None = None
    note: str | None = None


@router.post("/execution/report")
def report(body: ReportIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services.events_in import report_execution

    return report_execution(s, ctx, body.model_dump())
