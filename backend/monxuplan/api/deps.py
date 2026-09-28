"""FastAPI dependencies: database session bound to the caller's tenant, authentication, CSRF."""

from __future__ import annotations

import secrets
from collections.abc import Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from ..core.config import get_settings
from ..core.db import new_session
from ..core.errors import Forbidden, Unauthorized
from ..core.observability import tenant_var, user_var
from ..core.security import decode_token
from ..services.auth import load_ctx, user_from_api_key, user_from_claims, user_from_oidc
from ..services.context import Ctx

SESSION_COOKIE = "mx_session"
CSRF_COOKIE = "mx_csrf"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _auth(request: Request) -> tuple[Ctx, bool]:
    """Returns (context, via_cookie)."""
    auth = request.headers.get("authorization", "")
    rid = getattr(request.state, "request_id", None)
    ip = request.client.host if request.client else None
    with new_session(None, "auth") as s:
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            if token.startswith("mxk_"):
                _u, ak, ctx = user_from_api_key(s, token)
                if ctx is None:
                    raise Unauthorized("Invalid API key", code="INVALID_API_KEY")
                s.commit()
                ctx.request_id, ctx.ip = rid, ip
                return ctx, False
            claims = decode_token(token)
            user = user_from_claims(s, claims) if claims else user_from_oidc(s, token)
            if user is None:
                raise Unauthorized("Invalid or expired token", code="INVALID_TOKEN")
            ctx = load_ctx(s, user, via="bearer" if claims else "oidc", request_id=rid, ip=ip)
            s.commit()
            return ctx, False
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            raise Unauthorized("Please sign in.", code="NOT_AUTHENTICATED")
        claims = decode_token(token)
        user = user_from_claims(s, claims) if claims else None
        if user is None:
            raise Unauthorized("Your session has expired. Please sign in again.", code="SESSION_EXPIRED")
        return load_ctx(s, user, via="session", request_id=rid, ip=ip), True


def get_ctx(request: Request) -> Ctx:
    ctx, via_cookie = _auth(request)
    if via_cookie and request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER)
        cookie = request.cookies.get(CSRF_COOKIE)
        if not sent or not cookie or not secrets.compare_digest(sent, cookie):
            raise Forbidden("Missing or invalid CSRF token. Reload the page and try again.", code="CSRF")
    tenant_var.set(str(ctx.tenant_id))
    user_var.set(ctx.username)
    request.state.ctx = ctx
    return ctx


def get_db(ctx: Ctx = Depends(get_ctx)) -> Iterator[Session]:
    s = new_session(ctx.tenant_id, ctx.username)
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def cookie_kwargs() -> dict:
    st = get_settings()
    return {"httponly": True, "samesite": "lax", "secure": st.cookie_secure, "path": "/"}
