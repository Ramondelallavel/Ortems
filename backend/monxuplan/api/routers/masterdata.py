"""Master data (generic registry) and convenience endpoints."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request

from ...services import masterdata as md
from ...services.context import Ctx
from ..deps import get_ctx, get_db

router = APIRouter(tags=["master-data"])


@router.get("/master-data")
def entities(ctx: Ctx = Depends(get_ctx)):
    ctx.require("masterdata:read")
    return {"entities": [md.schema(d) for d in md.REGISTRY.values()]}


@router.get("/master-data/{entity}")
def list_entity(
    entity: str,
    request: Request,
    q: str | None = None,
    sort: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=5000),
    plant_id: uuid.UUID | None = None,
    ctx: Ctx = Depends(get_ctx),
    s=Depends(get_db),
):
    reserved = {"q", "sort", "offset", "limit", "plant_id"}
    filters = {k: v for k, v in request.query_params.items() if k not in reserved}
    return md.list_rows(s, ctx, entity, q=q, filters=filters, sort=sort, offset=offset, limit=limit, plant_id=plant_id)


@router.get("/master-data/{entity}/schema")
def entity_schema(entity: str, ctx: Ctx = Depends(get_ctx)):
    ctx.require("masterdata:read")
    return md.schema(md.get_def(entity))


@router.get("/master-data/{entity}/{id_}")
def get_entity(entity: str, id_: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return md.get_row(s, ctx, entity, id_)


@router.post("/master-data/{entity}", status_code=201)
def create_entity(entity: str, body: dict[str, Any] = Body(...), reason: str | None = None, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return md.create_row(s, ctx, entity, body, reason)


@router.put("/master-data/{entity}/{id_}")
@router.patch("/master-data/{entity}/{id_}")
def update_entity(entity: str, id_: uuid.UUID, body: dict[str, Any] = Body(...), reason: str | None = None, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return md.update_row(s, ctx, entity, id_, body, reason)


@router.delete("/master-data/{entity}/{id_}")
def delete_entity(entity: str, id_: uuid.UUID, reason: str | None = None, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return md.delete_row(s, ctx, entity, id_, reason)


# convenience aliases from the API contract of the brief
@router.get("/orders")
def orders(request: Request, plant_id: uuid.UUID | None = None, q: str | None = None, offset: int = 0, limit: int = 500, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services.orders import list_orders

    filters = {k: v for k, v in request.query_params.items() if k not in {"plant_id", "q", "offset", "limit", "plan_id"}}
    plan_id = request.query_params.get("plan_id")
    return list_orders(s, ctx, plant_id, q, filters, offset, limit, uuid.UUID(plan_id) if plan_id else None)


@router.post("/orders", status_code=201)
def create_order(body: dict[str, Any] = Body(...), ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return md.create_row(s, ctx, "production-orders", body)


@router.put("/orders/{id_}")
def update_order(id_: uuid.UUID, body: dict[str, Any] = Body(...), ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return md.update_row(s, ctx, "production-orders", id_, body)


@router.get("/resources")
def resources(plant_id: uuid.UUID | None = None, q: str | None = None, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return md.list_rows(s, ctx, "resources", q=q, plant_id=plant_id, limit=2000)


@router.get("/items/{id_}/bom-tree")
def bom_tree(id_: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services.orders import bom_tree as tree

    ctx.require("masterdata:read")
    return tree(s, id_)
