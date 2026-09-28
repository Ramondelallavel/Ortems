"""Materials: availability, projected inventory, pegging, receipts, MPS."""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query

from ...services import materials as mat
from ...services.context import Ctx
from ..deps import get_ctx, get_db

router = APIRouter(tags=["materials"])


@router.get("/materials/availability")
def availability(plan_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return mat.availability(s, ctx, plan_id)


@router.get("/materials/{material_id}/projection")
def projection(material_id: str, plan_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return mat.projection(s, ctx, plan_id, material_id)


@router.get("/materials/{material_id}/impact")
def impact(material_id: str, plan_id: uuid.UUID, supply_ref: str | None = None, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return mat.impact_of_material(s, ctx, plan_id, material_id, supply_ref)


@router.get("/pegging/order/{order_id}")
def pegging(order_id: str, plan_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return mat.pegging_for_order(s, ctx, plan_id, order_id)


@router.get("/receipts")
def receipts(plant_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return mat.receipts(s, ctx, plant_id)


@router.get("/mps")
def mps(plant_id: uuid.UUID, weeks: int = Query(12, ge=1, le=52), start: date | None = None, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services.mps import run_mps

    return run_mps(s, ctx, plant_id, weeks, start)
