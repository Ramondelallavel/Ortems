"""Integrations: import wizard, data exports, inbound events (MES/ERP), webhooks and connectors."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...core.config import get_settings
from ...core.errors import NotFound, ValidationFailed
from ...core.security import encrypt_secret
from ...models import Event, Integration, WebhookDelivery, WebhookSubscription
from ...services import audit, imports
from ...services.context import Ctx
from ...services.masterdata import row_dict
from ..deps import get_ctx, get_db

router = APIRouter(tags=["integrations"])


def _download(data: bytes, media: str, filename: str) -> Response:
    return Response(content=data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ------------------------------------------------------------------ import wizard
@router.get("/imports/templates")
def templates(ctx: Ctx = Depends(get_ctx)):
    ctx.require("integration:import")
    return [
        {"entity": t.entity, "label": t.label, "description": t.description, "key": list(t.key), "fields": [{"name": f.name, "type": f.type, "required": f.required, "description": f.description, "enum": list(f.enum), "ref": f.ref} for f in t.fields]}
        for t in imports.TEMPLATES.values()
    ]


@router.get("/imports/tables")
def tables(ctx: Ctx = Depends(get_ctx)):
    """Every table with all its columns (round-trip import/export, ``table:<entity>``)."""
    from ...services import tableio

    ctx.require("integration:import")
    out = []
    for sp in tableio.specs().values():
        if not ctx.has(sp.write_perm) and not ctx.has(sp.read_perm):
            continue
        out.append(
            {
                "entity": sp.name,
                "table": sp.entity,
                "label": sp.label,
                "group": sp.group,
                "parent": sp.parent.name if sp.parent else None,
                "key": list(sp.template.key),
                "writable": ctx.has(sp.write_perm),
                "fields": [{"name": f.name, "type": f.type, "description": f.description} for f in sp.template.fields],
            }
        )
    return out


class BatchIn(BaseModel):
    job_ids: list[uuid.UUID]
    options: dict[str, Any] | None = None


@router.post("/imports/workbook", status_code=201)
async def upload_workbook(file: UploadFile = File(...), plant_id: uuid.UUID | None = Form(None), ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    data = await file.read()
    if len(data) > get_settings().max_upload_mb * 1024 * 1024:
        raise ValidationFailed("File too large", code="PAYLOAD_TOO_LARGE")
    return imports.upload_workbook(s, ctx, file.filename or "workbook.xlsx", data, {"plant_id": str(plant_id) if plant_id else None})


@router.post("/imports/batch/validate")
def validate_batch(body: BatchIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return imports.validate_batch(s, ctx, body.job_ids, body.options)


@router.post("/imports/batch/commit")
def commit_batch(body: BatchIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return imports.commit_batch(s, ctx, body.job_ids, body.options)


@router.get("/imports/templates/{entity}")
def template_file(entity: str, format: str = Query("xlsx", pattern="^(xlsx|csv)$"), ctx: Ctx = Depends(get_ctx)):
    ctx.require("integration:import")
    return _download(*imports.template_file(entity, format))


@router.post("/imports", status_code=201)
async def upload(
    entity: str = Form(...),
    file: UploadFile = File(...),
    plant_id: uuid.UUID | None = Form(None),
    sheet: str | None = Form(None),
    ctx: Ctx = Depends(get_ctx),
    s=Depends(get_db),
):
    data = await file.read()
    if len(data) > get_settings().max_upload_mb * 1024 * 1024:
        raise ValidationFailed("File too large", code="PAYLOAD_TOO_LARGE")
    opts: dict[str, Any] = {"plant_id": str(plant_id) if plant_id else None}
    if sheet:
        opts["sheet"] = sheet
    return imports.upload(s, ctx, entity, file.filename or "upload", data, opts)


@router.get("/imports")
def list_imports(limit: int = Query(50, le=200), ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return imports.list_jobs(s, ctx, limit)


@router.get("/imports/{job_id}")
def get_import(job_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return imports.get_job(s, ctx, job_id)


class MappingIn(BaseModel):
    mapping: dict[str, str | None]
    options: dict[str, Any] | None = None


@router.put("/imports/{job_id}/mapping")
def set_mapping(job_id: uuid.UUID, body: MappingIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    opts = body.options or {}
    if "mode" in opts and opts["mode"] not in ("UPSERT", "CREATE_ONLY", "UPDATE_ONLY"):
        raise ValidationFailed("mode must be UPSERT, CREATE_ONLY or UPDATE_ONLY")
    if "date_format" in opts and opts["date_format"] not in ("DMY", "MDY"):
        raise ValidationFailed("date_format must be DMY or MDY")
    return imports.set_mapping(s, ctx, job_id, body.mapping, opts)


@router.post("/imports/{job_id}/validate")
def validate(job_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    return imports.validate(s, ctx, job_id)


@router.post("/imports/{job_id}/commit")
def commit(job_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    out = imports.commit(s, ctx, job_id)
    from ...services.webhooks import emit

    emit(s, ctx.tenant_id, "import.completed", {"import_id": str(job_id), "entity": out["entity"], "stats": out["stats"]})
    return out


# ------------------------------------------------------------------ exports
@router.get("/exports/workbook")
def export_workbook(plant_id: uuid.UUID | None = None, entities: list[str] | None = Query(None), ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    """Whole data set as one Excel workbook (one sheet per table), to edit and import back."""
    from ...services.exports import export_workbook as ex

    ctx.require("integration:export")
    return _download(*ex(s, ctx, entities, plant_id))


@router.get("/exports/{entity}")
def export_entity(entity: str, format: str = Query("csv", pattern="^(xlsx|csv|json)$"), plant_id: uuid.UUID | None = None, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services.exports import export_entity as ex

    ctx.require("integration:export")
    return _download(*ex(s, ctx, entity, format, plant_id))


# ------------------------------------------------------------------ inbound events
class EventIn(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime | None = None
    correlation_id: str | None = Field(default=None, max_length=120)
    source: str = Field(default="API", max_length=40)


@router.post("/events", status_code=202)
def post_event(body: EventIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services.events_in import ingest

    return ingest(s, ctx, body.type, body.payload, body.source, body.correlation_id, body.occurred_at)


@router.get("/events")
def list_events(plant_id: uuid.UUID | None = None, type: str | None = None, limit: int = Query(100, le=1000), ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    ctx.require("plan:read")
    q = select(Event).order_by(Event.received_at.desc()).limit(limit)
    if plant_id:
        q = q.where(Event.plant_id == plant_id)
    if type:
        q = q.where(Event.type == type)
    return [row_dict(e) for e in s.scalars(q)]


@router.get("/events/types")
def event_types(ctx: Ctx = Depends(get_ctx)):
    from ...services.events_in import EVENT_TYPES
    from ...services.webhooks import EVENT_TYPES as OUT

    return {"inbound": list(EVENT_TYPES), "outbound": list(OUT)}


# ------------------------------------------------------------------ webhooks
class WebhookIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    url: str = Field(pattern=r"^https?://", max_length=500)
    events: list[str]
    secret: str | None = Field(default=None, min_length=16, max_length=200)
    is_active: bool = True


@router.get("/webhooks")
def list_webhooks(ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    ctx.require("integration:manage")
    return [row_dict(w) for w in s.scalars(select(WebhookSubscription).order_by(WebhookSubscription.name))]


@router.post("/webhooks", status_code=201)
def create_webhook(body: WebhookIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    import secrets as _secrets

    from ...services.webhooks import EVENT_TYPES as OUT

    ctx.require("integration:manage")
    bad = [e for e in body.events if e not in OUT and e != "*"]
    if bad:
        raise ValidationFailed(f"Unknown event(s): {', '.join(bad)}")
    from ...core.netpolicy import check_url

    check_url(body.url, "Webhook")  # scheme, credentials and destination policy (checked again at every delivery)
    secret = body.secret or _secrets.token_urlsafe(32)
    w = WebhookSubscription(tenant_id=ctx.tenant_id, name=body.name, url=body.url, events=body.events, secret_encrypted=encrypt_secret(secret), is_active=body.is_active)
    s.add(w)
    s.flush()
    audit.record(s, ctx, "CREATE", "webhook", w.id, w.name, after={"url": w.url, "events": w.events})
    return {**row_dict(w), "secret": secret, "note": "Store the signing secret now; it will not be shown again."}


@router.delete("/webhooks/{webhook_id}")
def delete_webhook(webhook_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    ctx.require("integration:manage")
    w = s.get(WebhookSubscription, webhook_id)
    if w is None:
        raise NotFound("Webhook not found")
    audit.record(s, ctx, "DELETE", "webhook", w.id, w.name)
    s.delete(w)
    return {"deleted": True}


@router.get("/webhooks/{webhook_id}/deliveries")
def deliveries(webhook_id: uuid.UUID, limit: int = Query(50, le=500), ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    ctx.require("integration:manage")
    return [row_dict(d) for d in s.scalars(select(WebhookDelivery).where(WebhookDelivery.subscription_id == webhook_id).order_by(WebhookDelivery.created_at.desc()).limit(limit))]


# ------------------------------------------------------------------ connectors
CONNECTOR_SYSTEMS = {
    "DATABASE": "Database (PostgreSQL, MySQL/MariaDB, SQL Server, Oracle, SQLite): read tables or SELECT queries",
    "FILE": "File drop (CSV/Excel/JSON via the import wizard or API)",
    "GENERIC_REST": "Generic REST (push events to /api/v1/events, pull data with API keys)",
    "SAP": "SAP S/4HANA / ECC",
    "ORACLE": "Oracle ERP",
    "DYNAMICS": "Microsoft Dynamics 365",
    "ODOO": "Odoo",
    "SAGE": "Sage",
    "INFOR": "Infor",
    "MES": "MES (generic)",
}
AVAILABLE_SYSTEMS = {"DATABASE", "FILE", "GENERIC_REST"}


@router.get("/connectors")
def connectors(ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    ctx.require("integration:manage")
    return {
        "systems": [{"code": k, "label": v, "status": "AVAILABLE" if k in AVAILABLE_SYSTEMS else "COMING_SOON"} for k, v in CONNECTOR_SYSTEMS.items()],
        "configured": [{**row_dict(i), "has_password": bool(i.secret_encrypted)} for i in s.scalars(select(Integration).order_by(Integration.code))],
    }


class ConnectorIn(BaseModel):
    code: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=200)
    system: str
    direction: str = Field(default="BOTH", pattern="^(IN|OUT|BOTH)$")
    settings: dict[str, Any] = Field(default_factory=dict)
    secret: str | None = None
    is_active: bool = True


@router.post("/connectors", status_code=201)
def create_connector(body: ConnectorIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    ctx.require("integration:manage")
    if body.system == "DATABASE":
        from ...services import dbconnect

        return dbconnect.save(s, ctx, {"code": body.code, "name": body.name, "settings": body.settings, "password": body.secret, "is_active": body.is_active})
    if body.system not in CONNECTOR_SYSTEMS:
        raise ValidationFailed(f"Unknown system {body.system}")
    if body.system not in AVAILABLE_SYSTEMS:
        raise ValidationFailed(f"The {CONNECTOR_SYSTEMS[body.system]} connector is not available yet (coming soon). Use GENERIC_REST or FILE.", code="COMING_SOON")
    i = Integration(tenant_id=ctx.tenant_id, code=body.code, name=body.name, system=body.system, direction=body.direction, settings=body.settings, secret_encrypted=encrypt_secret(body.secret) if body.secret else None, is_active=body.is_active)
    s.add(i)
    s.flush()
    audit.record(s, ctx, "CREATE", "integration", i.id, i.code, after={"system": i.system})
    return row_dict(i)


# ------------------------------------------------------------------ database connectors
class DbConnectorIn(BaseModel):
    code: str = Field(min_length=1, max_length=60)
    name: str = Field(default="", max_length=200)
    settings: dict[str, Any] = Field(default_factory=dict)
    password: str | None = None
    clear_password: bool = False
    is_active: bool = True


class DbTestIn(BaseModel):
    settings: dict[str, Any]
    password: str | None = None
    connector_id: uuid.UUID | None = None


class DbPreviewIn(BaseModel):
    source: dict[str, Any]
    entity: str | None = None


class DbSyncIn(BaseModel):
    source_ids: list[str] | None = None
    plant_id: uuid.UUID | None = None
    commit: bool | None = None


@router.get("/connectors/database/drivers")
def db_drivers(ctx: Ctx = Depends(get_ctx)):
    from ...services import dbconnect

    ctx.require("integration:import")
    ok, why = dbconnect.runtime_supported()
    return {"supported": ok, "reason": why, "drivers": dbconnect.drivers()}


@router.post("/connectors/database/test")
def db_test(body: DbTestIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services import dbconnect

    ctx.require("integration:manage")
    pwd = body.password
    if not pwd and body.connector_id:
        i = s.get(Integration, body.connector_id)
        pwd = dbconnect._password(i) if i is not None and i.system == "DATABASE" else None
    return dbconnect.test_connection(body.settings, pwd)


@router.post("/connectors/database", status_code=201)
def db_create(body: DbConnectorIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services import dbconnect

    return dbconnect.save(s, ctx, body.model_dump())


@router.put("/connectors/database/{cid}")
def db_update(cid: uuid.UUID, body: DbConnectorIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services import dbconnect

    return dbconnect.save(s, ctx, body.model_dump(), cid)


@router.delete("/connectors/database/{cid}", status_code=204)
def db_delete(cid: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services import dbconnect

    dbconnect.delete(s, ctx, cid)
    return Response(status_code=204)


@router.get("/connectors/database/{cid}/objects")
def db_objects(cid: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services import dbconnect

    return dbconnect.objects(s, ctx, cid)


@router.post("/connectors/database/{cid}/preview")
def db_preview(cid: uuid.UUID, body: DbPreviewIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services import dbconnect

    return dbconnect.preview(s, ctx, cid, body.source, body.entity)


@router.post("/connectors/database/{cid}/sync")
def db_sync(cid: uuid.UUID, body: DbSyncIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services import dbconnect

    return dbconnect.sync(s, ctx, cid, body.source_ids, body.plant_id, body.commit)
