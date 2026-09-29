"""Command Center, alerts, data quality, assistant, users/roles/API keys, audit, saved views, settings."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select

from ...core.clock import now
from ...core.config import get_settings
from ...core.errors import Forbidden, NotFound, ValidationFailed
from ...core.security import PERMISSIONS, ROLES
from ...models import Alert, ApiKey, AuditLog, Plant, Role, SavedView, User, UserRole
from ...services import audit
from ...services.context import Ctx
from ...services.masterdata import row_dict
from ..deps import get_ctx, get_db

router = APIRouter(tags=["admin"])


# ------------------------------------------------------------------ command center
@router.get("/plants")
def plants(ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services.overview import plants_overview

    return plants_overview(s, ctx)


@router.get("/dashboard")
def dashboard(plant_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services.overview import command_center

    return command_center(s, ctx, plant_id)


# ------------------------------------------------------------------ alerts
@router.get("/alerts")
def alerts(plant_id: uuid.UUID, status: str = Query("OPEN", pattern="^(OPEN|ACKNOWLEDGED|RESOLVED|ALL)$"), limit: int = Query(200, le=1000), ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    ctx.require("plan:read")
    ctx.require_plant(plant_id)
    q = select(Alert).where(Alert.plant_id == plant_id).order_by(Alert.created_at.desc()).limit(limit)
    if status != "ALL":
        q = q.where(Alert.status == status)
    sev = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
    out = [row_dict(a) for a in s.scalars(q)]
    out.sort(key=lambda a: (sev.get(a["severity"], 3), a["created_at"] and -datetime.fromisoformat(a["created_at"]).timestamp()))
    return out


class AckIn(BaseModel):
    note: str | None = Field(default=None, max_length=1000)
    resolve: bool = False


@router.post("/alerts/{alert_id}/acknowledge")
def acknowledge(alert_id: uuid.UUID, body: AckIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services.alerts import acknowledge as ack

    return row_dict(ack(s, ctx, alert_id, body.note, body.resolve))


# ------------------------------------------------------------------ data quality
@router.get("/data-quality")
def data_quality(plant_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services.dataquality import run_checks

    ctx.require("masterdata:read")
    ctx.require_plant(plant_id)
    return run_checks(s, plant_id)


# ------------------------------------------------------------------ assistant
class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    plant_id: uuid.UUID | None = None
    plan_id: uuid.UUID | None = None
    history: list[dict[str, str]] | None = None


@router.post("/assistant/ask")
def ask(body: AskIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services.assistant import ask as _ask

    return _ask(s, ctx, body.question, body.plant_id, body.plan_id, body.history)


@router.get("/assistant/status")
def assistant_status(ctx: Ctx = Depends(get_ctx)):
    from ...services.assistant import llm_enabled

    on = llm_enabled()
    return {"mode": "LLM" if on else "GROUNDED", "model": get_settings().assistant_model if on else None}


# ------------------------------------------------------------------ users & roles
@router.get("/roles")
def roles(ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    ctx.require("admin:users")
    return {"permissions": PERMISSIONS, "roles": [row_dict(r) for r in s.scalars(select(Role).order_by(Role.code))]}


def _user_out(s, u: User) -> dict[str, Any]:
    d = row_dict(u, skip={"failed_logins"})
    rows = s.execute(select(Role.code, UserRole.plant_id).join(UserRole, UserRole.role_id == Role.id).where(UserRole.user_id == u.id)).all()
    d["roles"] = sorted({c for c, _p in rows})
    d["plant_ids"] = sorted({str(p) for _c, p in rows if p})
    d["locked"] = bool(u.locked_until and (u.locked_until if u.locked_until.tzinfo else u.locked_until.replace(tzinfo=now().tzinfo)) > now())
    return d


@router.get("/users")
def users(ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    ctx.require("admin:users")
    return [_user_out(s, u) for u in s.scalars(select(User).order_by(User.username))]


class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=80, pattern=r"^[A-Za-z0-9._@-]+$")
    email: str = Field(min_length=3, max_length=200, pattern=r"^[^@\s]+@[^@\s]+$")
    full_name: str = Field(min_length=1, max_length=200)
    password: str | None = Field(default=None, max_length=200)
    roles: list[str] = Field(min_length=1)
    plant_ids: list[uuid.UUID] = Field(default_factory=list)
    locale: str = Field(default="en", pattern="^(en|es|fr|de|pt)$")


def _check_roles(ctx: Ctx, roles: list[str]) -> None:
    unknown = [r for r in roles if r not in ROLES]
    if unknown:
        raise ValidationFailed(f"Unknown role(s): {', '.join(unknown)}")
    if "SUPER_ADMIN" in roles and "SUPER_ADMIN" not in ctx.roles:
        raise Forbidden("Only a Super Admin can grant Super Admin.")


def _set_roles(s, ctx: Ctx, u: User, roles: list[str], plant_ids: list[uuid.UUID]) -> None:
    from ...services.auth import ensure_roles

    all_roles = ensure_roles(s, ctx.tenant_id)
    s.execute(delete(UserRole).where(UserRole.user_id == u.id))
    for rc in roles:
        for pid in plant_ids or [None]:
            s.add(UserRole(tenant_id=ctx.tenant_id, user_id=u.id, role_id=all_roles[rc].id, plant_id=pid))
    s.flush()


@router.post("/users", status_code=201)
def create_user(body: UserIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services.auth import create_user as _create

    ctx.require("admin:users")
    _check_roles(ctx, body.roles)
    u = _create(s, ctx.tenant_id, body.username, body.email, body.full_name, body.password, [])
    u.locale = body.locale
    _set_roles(s, ctx, u, body.roles, body.plant_ids)
    audit.record(s, ctx, "CREATE", "user", u.id, u.username, after={"roles": body.roles, "plants": [str(p) for p in body.plant_ids]})
    return _user_out(s, u)


class UserPatch(BaseModel):
    full_name: str | None = None
    email: str | None = None
    is_active: bool | None = None
    roles: list[str] | None = None
    plant_ids: list[uuid.UUID] | None = None
    locale: str | None = Field(default=None, pattern="^(en|es|fr|de|pt)$")
    unlock: bool = False
    version: int | None = None


@router.patch("/users/{user_id}")
def update_user(user_id: uuid.UUID, body: UserPatch, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    ctx.require("admin:users")
    u = s.get(User, user_id)
    if u is None:
        raise NotFound("User not found")
    if body.version is not None and body.version != u.version:
        from ...core.errors import Conflict

        raise Conflict("The user was changed meanwhile. Reload.", code="VERSION_CONFLICT")
    before = _user_out(s, u)
    if body.is_active is False and u.id == ctx.user_id:
        raise ValidationFailed("You cannot deactivate yourself")
    for f in ("full_name", "email", "is_active", "locale"):
        v = getattr(body, f)
        if v is not None:
            setattr(u, f, v)
    if body.unlock:
        u.failed_logins, u.locked_until = 0, None
    if body.roles is not None:
        _check_roles(ctx, body.roles)
        existing = next(iter(before["plant_ids"]), None)
        _set_roles(s, ctx, u, body.roles, body.plant_ids if body.plant_ids is not None else [uuid.UUID(p) for p in before["plant_ids"]] if existing else [])
    s.flush()
    after = _user_out(s, u)
    audit.record(s, ctx, "UPDATE", "user", u.id, u.username, before={k: before[k] for k in ("full_name", "email", "is_active", "roles", "plant_ids")}, after={k: after[k] for k in ("full_name", "email", "is_active", "roles", "plant_ids")})
    return after


class PasswordReset(BaseModel):
    new_password: str = Field(min_length=10, max_length=200)


@router.post("/users/{user_id}/password")
def reset_password(user_id: uuid.UUID, body: PasswordReset, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services.auth import change_password

    change_password(s, ctx, user_id, None, body.new_password)
    return {"ok": True}


# ------------------------------------------------------------------ API keys
@router.get("/api-keys")
def api_keys(ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    ctx.require("admin:users")
    return [row_dict(k) for k in s.scalars(select(ApiKey).order_by(ApiKey.created_at.desc()))]


class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    role_code: str = "INTEGRATION_SERVICE"
    days: int | None = Field(default=None, ge=1, le=3650)


@router.post("/api-keys", status_code=201)
def create_api_key(body: ApiKeyIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services.auth import create_api_key as _create

    _check_roles(ctx, [body.role_code])
    ak, full = _create(s, ctx, body.name, body.role_code, body.days)
    return {**row_dict(ak), "key": full, "note": "Copy the key now; only a hash is stored."}


@router.delete("/api-keys/{key_id}")
def revoke_api_key(key_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    ctx.require("admin:users")
    ak = s.get(ApiKey, key_id)
    if ak is None:
        raise NotFound("API key not found")
    ak.is_active = False
    audit.record(s, ctx, "REVOKE", "api_key", ak.id, ak.name)
    return {"revoked": True}


# ------------------------------------------------------------------ audit
@router.get("/audit")
def audit_log(
    entity_type: str | None = None,
    entity_id: str | None = None,
    user: str | None = None,
    action: str | None = None,
    q: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    ctx: Ctx = Depends(get_ctx),
    s=Depends(get_db),
):
    ctx.require("admin:audit")
    stmt = select(AuditLog)
    conds = []
    if entity_type:
        conds.append(AuditLog.entity_type == entity_type)
    if entity_id:
        conds.append(AuditLog.entity_id == entity_id)
    if user:
        conds.append(AuditLog.user == user)
    if action:
        conds.append(AuditLog.action == action)
    if date_from:
        conds.append(AuditLog.at >= date_from)
    if date_to:
        conds.append(AuditLog.at <= date_to)
    if q:
        like = f"%{q.lower()}%"
        conds.append(or_(func.lower(AuditLog.entity_label).like(like), func.lower(AuditLog.reason).like(like)))
    for c in conds:
        stmt = stmt.where(c)
    total = s.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = s.scalars(stmt.order_by(AuditLog.at.desc()).offset(offset).limit(limit))
    return {"total": total, "items": [row_dict(a) for a in rows]}


# ------------------------------------------------------------------ saved views
class ViewIn(BaseModel):
    page: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    filters: dict[str, Any] = Field(default_factory=dict)
    columns: list[Any] = Field(default_factory=list)
    is_shared: bool = False


@router.get("/views")
def views(page: str, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    rows = s.scalars(select(SavedView).where(SavedView.page == page, or_(SavedView.owner == ctx.username, SavedView.is_shared.is_(True))).order_by(SavedView.name))
    return [row_dict(v) for v in rows]


@router.post("/views", status_code=201)
def create_view(body: ViewIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    v = SavedView(tenant_id=ctx.tenant_id, owner=ctx.username, **body.model_dump())
    s.add(v)
    s.flush()
    return row_dict(v)


@router.delete("/views/{view_id}")
def delete_view(view_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    v = s.get(SavedView, view_id)
    if v is None:
        raise NotFound("View not found")
    if v.owner != ctx.username and "admin:config" not in ctx.permissions:
        raise Forbidden("Only the owner can delete this view")
    s.delete(v)
    return {"deleted": True}


# ------------------------------------------------------------------ plant settings
SETTINGS_KEYS = {"auto_reschedule", "default_profile", "units", "shift_handover_hours", "publish_requires_validation"}


@router.get("/plants/{plant_id}/settings")
def plant_settings(plant_id: uuid.UUID, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    ctx.require("plan:read")
    ctx.require_plant(plant_id)
    p = s.get(Plant, plant_id)
    if p is None:
        raise NotFound("Plant not found")
    return {"plant_id": str(p.id), "timezone": p.timezone, "settings": p.settings or {}}


@router.put("/plants/{plant_id}/settings")
def update_plant_settings(plant_id: uuid.UUID, body: dict[str, Any], ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    from ...services.events_in import EVENT_TYPES

    ctx.require("admin:config")
    ctx.require_plant(plant_id)
    p = s.get(Plant, plant_id)
    if p is None:
        raise NotFound("Plant not found")
    bad = [k for k in body if k not in SETTINGS_KEYS]
    if bad:
        raise ValidationFailed(f"Unknown setting(s): {', '.join(bad)}")
    if "publish_requires_validation" in body and not isinstance(body["publish_requires_validation"], bool):
        raise ValidationFailed("publish_requires_validation must be true or false")
    ar = body.get("auto_reschedule")
    if ar is not None:
        if not isinstance(ar, dict) or ar.get("scope", "LOCAL") not in ("LOCAL", "REGIONAL", "RESOURCE", "AREA", "GLOBAL") or any(e not in EVENT_TYPES for e in ar.get("events", [])):
            raise ValidationFailed("auto_reschedule must be {events: [event types], scope: LOCAL|REGIONAL|RESOURCE|AREA|GLOBAL}")
    before = dict(p.settings or {})
    p.settings = {**before, **body}
    audit.record(s, ctx, "UPDATE", "plant_settings", p.id, p.code, before=before, after=p.settings)
    return {"plant_id": str(p.id), "settings": p.settings}


# ------------------------------------------------------------------ demo
@router.post("/demo/reset")
def demo_reset(ctx: Ctx = Depends(get_ctx)):
    """Re-creates the demo dataset. Only in non-production environments and for administrators."""
    if get_settings().is_production:
        raise Forbidden("Demo reset is disabled in production.")
    ctx.require("admin:config")
    from ...seed.demo import seed_demo

    return seed_demo(reset=True)
