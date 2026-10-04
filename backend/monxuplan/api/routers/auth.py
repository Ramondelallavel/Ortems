"""Authentication endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from ...core.config import get_settings
from ...core.db import new_session
from ...core.errors import ValidationFailed
from ...core.security import create_token, new_csrf_token
from ...models import Plant, User
from ...services import audit
from ...services import auth as auth_svc
from ...services.context import Ctx
from ..deps import CSRF_COOKIE, SESSION_COOKIE, cookie_kwargs, get_ctx, get_db

router = APIRouter(tags=["auth"])


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)
    tenant: str | None = Field(default=None, description="Tenant slug (needed only if the username exists in several tenants)")


@router.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response):
    with new_session(None, "auth") as s:
        user, token = auth_svc.login(s, body.tenant, body.username, body.password, request.client.host if request.client else None)
        ctx = auth_svc.load_ctx(s, user, via="session")
        from ...services.audit import record

        s.info["tenant_id"] = user.tenant_id
        record(s, ctx, "LOGIN", "user", user.id, user.username)
        s.commit()
        csrf = _set_session(response, token)
        return {"user": _me(s, user, ctx), "csrf_token": csrf}


def _set_session(response: Response, token: str) -> str:
    """Session cookie (httpOnly) + CSRF cookie (double submit), both for the configured lifetime."""
    age = get_settings().session_hours * 3600
    csrf = new_csrf_token()
    response.set_cookie(SESSION_COOKIE, token, max_age=age, **cookie_kwargs())
    response.set_cookie(CSRF_COOKIE, csrf, max_age=age, httponly=False, samesite="lax", secure=cookie_kwargs()["secure"], path="/")
    return csrf


@router.post("/auth/token")
def token(body: LoginIn):
    """Bearer token for API clients (no cookies, no CSRF)."""
    with new_session(None, "auth") as s:
        user, tok = auth_svc.login(s, body.tenant, body.username, body.password)
        s.commit()
        return {"access_token": tok, "token_type": "bearer"}


@router.post("/auth/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me")
def me(ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    user = s.get(User, ctx.user_id) if ctx.user_id else None
    return _me(s, user, ctx)


class PasswordIn(BaseModel):
    current_password: str | None = None
    new_password: str = Field(min_length=10, max_length=200)


@router.post("/auth/password")
def change_password(body: PasswordIn, response: Response, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    """Changes the caller's password. Every other session and token of the user ends; this browser
    receives a fresh session."""
    if ctx.user_id is None:
        raise ValidationFailed("Only user accounts have a password.", code="NOT_A_USER")
    auth_svc.change_password(s, ctx, ctx.user_id, body.current_password, body.new_password)
    out: dict = {"ok": True}
    if ctx.via == "session":
        out["csrf_token"] = _set_session(response, create_token(ctx.user_id, ctx.tenant_id))
    return out


class MeIn(BaseModel):
    locale: str | None = Field(default=None, pattern="^(en|es|fr|de|pt)$")
    default_plant_id: uuid.UUID | None = None


@router.patch("/auth/me")
def update_me(body: MeIn, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    """The caller's own preferences: language and default plant."""
    user = s.get(User, ctx.user_id) if ctx.user_id else None
    if user is None:
        raise ValidationFailed("Only user accounts have preferences.", code="NOT_A_USER")
    if body.locale is not None:
        user.locale = body.locale
    if body.default_plant_id is not None:
        ctx.require_plant(body.default_plant_id)
        user.default_plant_id = body.default_plant_id
    return _me(s, user, ctx)


@router.post("/auth/logout-everywhere")
def logout_everywhere(response: Response, ctx: Ctx = Depends(get_ctx), s=Depends(get_db)):
    """Ends every session and bearer token of the caller, on every device."""
    user = s.get(User, ctx.user_id) if ctx.user_id else None
    if user is None:
        raise ValidationFailed("Only user accounts have sessions.", code="NOT_A_USER")
    auth_svc.revoke_tokens(user)
    audit.record(s, ctx, "LOGOUT_EVERYWHERE", "user", user.id, user.username)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}


def _me(s, user, ctx: Ctx) -> dict:
    plants = [
        {"id": str(p.id), "code": p.code, "name": p.name, "timezone": p.timezone, "live_scenario_id": str(p.live_scenario_id) if p.live_scenario_id else None, "published_plan_id": str(p.published_plan_id) if p.published_plan_id else None}
        for p in s.scalars(select(Plant).where(Plant.tenant_id == ctx.tenant_id).order_by(Plant.code).execution_options(skip_tenant_filter=True))
        if ctx.can_access_plant(p.id)
    ]
    return {
        "id": str(user.id) if user else None,
        "username": ctx.username,
        "full_name": user.full_name if user else ctx.username,
        "email": user.email if user else None,
        "locale": user.locale if user else "en",
        "tenant_id": str(ctx.tenant_id),
        "roles": sorted(ctx.roles),
        "permissions": sorted(ctx.permissions),
        "plants": plants,
        "default_plant_id": str(user.default_plant_id) if user and user.default_plant_id else (plants[0]["id"] if plants else None),
        "preferences": user.preferences if user else {},
    }
