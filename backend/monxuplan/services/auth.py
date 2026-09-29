"""Authentication and authorisation services."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.errors import Conflict, NotFound, Unauthorized, ValidationFailed
from ..core.security import ROLES, create_token, hash_api_key, hash_password, new_api_key, parse_api_key, password_problems, verify_oidc, verify_password
from ..models import ApiKey, Role, Tenant, User, UserRole
from . import audit
from .context import Ctx

MAX_FAILED = 8
LOCK_MINUTES = 15


def ensure_roles(s: Session, tenant_id: uuid.UUID) -> dict[str, Role]:
    existing = {r.code: r for r in s.scalars(select(Role).where(Role.tenant_id == tenant_id).execution_options(skip_tenant_filter=True))}
    for code, (name, perms) in ROLES.items():
        r = existing.get(code)
        if r is None:
            r = Role(tenant_id=tenant_id, code=code, name=name, permissions=sorted(perms), is_system=True)
            s.add(r)
            existing[code] = r
        elif r.is_system and sorted(r.permissions) != sorted(perms):
            r.permissions = sorted(perms)
    s.flush()
    return existing


def create_user(s: Session, tenant_id: uuid.UUID, username: str, email: str, full_name: str, password: str | None, roles: list[str], check_password: bool = True) -> User:
    if password and check_password:
        problems = password_problems(password)
        if problems:
            raise ValidationFailed("Password needs " + ", ".join(problems), code="WEAK_PASSWORD")
    dup = s.scalar(select(User).where(User.tenant_id == tenant_id, User.username == username).execution_options(skip_tenant_filter=True))
    if dup is not None:
        raise Conflict(f"User {username} already exists", code="DUPLICATE_USER")
    u = User(tenant_id=tenant_id, username=username, email=email, full_name=full_name, password_hash=hash_password(password) if password else None)
    s.add(u)
    s.flush()
    all_roles = ensure_roles(s, tenant_id)
    for rc in roles:
        role = all_roles.get(rc)
        if role is None:
            raise ValidationFailed(f"Unknown role {rc}")
        s.add(UserRole(tenant_id=tenant_id, user_id=u.id, role_id=role.id))
    s.flush()
    return u


def load_ctx(s: Session, user: User, via: str = "session", request_id: str | None = None, ip: str | None = None) -> Ctx:
    rows = s.execute(
        select(Role, UserRole.plant_id).join(UserRole, UserRole.role_id == Role.id).where(UserRole.user_id == user.id).execution_options(skip_tenant_filter=True)
    ).all()
    perms: set[str] = set()
    roles: set[str] = set()
    plants: set[uuid.UUID] | None = set()
    for role, plant_id in rows:
        perms.update(role.permissions or [])
        roles.add(role.code)
        if plant_id is None:
            plants = None
        elif plants is not None:
            plants.add(plant_id)
    if plants is not None and not plants:
        plants = None
    return Ctx(tenant_id=user.tenant_id, user_id=user.id, username=user.username, permissions=perms, roles=roles, plant_ids=plants, request_id=request_id, ip=ip, via=via, locale=user.locale)


def login(s: Session, tenant_slug: str | None, username: str, password: str, ip: str | None = None) -> tuple[User, str]:
    q = select(User, Tenant).join(Tenant, Tenant.id == User.tenant_id).where(User.username == username, Tenant.is_active.is_(True))
    if tenant_slug:
        q = q.where(Tenant.slug == tenant_slug)
    rows = s.execute(q.execution_options(skip_tenant_filter=True)).all()
    if len(rows) != 1:
        raise Unauthorized("Invalid username or password.", code="INVALID_CREDENTIALS")
    user, _tenant = rows[0]
    t = now()
    if user.locked_until and user.locked_until > t:
        raise Unauthorized("Account temporarily locked after repeated failed logins. Try again later.", code="ACCOUNT_LOCKED")
    if not user.is_active or not verify_password(password, user.password_hash):
        user.failed_logins = (user.failed_logins or 0) + 1
        if user.failed_logins >= MAX_FAILED:
            user.locked_until = t + timedelta(minutes=LOCK_MINUTES)
            user.failed_logins = 0
        s.flush()
        raise Unauthorized("Invalid username or password.", code="INVALID_CREDENTIALS")
    user.failed_logins = 0
    user.last_login_at = t
    s.flush()
    token = create_token(user.id, user.tenant_id)
    return user, token


def user_from_claims(s: Session, claims: dict) -> User | None:
    try:
        uid = uuid.UUID(claims["sub"])
        tid = uuid.UUID(claims["tid"])
    except (KeyError, ValueError):
        return None
    u = s.get(User, uid)
    if u is None or u.tenant_id != tid or not u.is_active:
        return None
    return u


def user_from_api_key(s: Session, key: str) -> tuple[User | None, ApiKey | None, Ctx | None]:
    prefix = parse_api_key(key)
    if prefix is None:
        return None, None, None
    ak = s.scalar(select(ApiKey).where(ApiKey.prefix == prefix, ApiKey.is_active.is_(True)).execution_options(skip_tenant_filter=True))
    if ak is None or ak.key_hash != hash_api_key(key):
        return None, None, None
    if ak.expires_at and ak.expires_at < now():
        return None, None, None
    ak.last_used_at = now()
    role = s.scalar(select(Role).where(Role.tenant_id == ak.tenant_id, Role.code == ak.role_code).execution_options(skip_tenant_filter=True))
    perms = set(role.permissions) if role else set()
    ctx = Ctx(tenant_id=ak.tenant_id, user_id=None, username=f"apikey:{ak.name}", permissions=perms, roles={ak.role_code}, via="api_key")
    return None, ak, ctx


def user_from_oidc(s: Session, token: str) -> User | None:
    claims = verify_oidc(token)
    if not claims:
        return None
    """The user bound to the token's subject (issuer and audience are verified by ``verify_oidc``).

    An account is never matched by e-mail alone: an e-mail is linked only when the identity provider
    says it is verified, the account has no subject yet, exactly one active account has that e-mail, and
    linking is enabled (``MONXU_OIDC_LINK_BY_EMAIL``). The account is then bound to the subject."""
    from ..core.config import get_settings

    sub = claims.get("sub")
    if not sub:
        return None
    users = list(s.scalars(select(User).where(User.external_subject == str(sub)).execution_options(skip_tenant_filter=True)))
    if len(users) == 1:
        return users[0] if users[0].is_active else None
    if users:  # the same subject on several accounts: refuse rather than guess
        return None
    email = claims.get("email")
    if not (get_settings().oidc_link_by_email and email and claims.get("email_verified") is True):
        return None
    cands = list(s.scalars(select(User).where(func.lower(User.email) == str(email).lower(), User.external_subject.is_(None), User.is_active.is_(True)).execution_options(skip_tenant_filter=True)))
    if len(cands) != 1:
        return None
    u = cands[0]
    u.external_subject = str(sub)
    audit.record(s, load_ctx(s, u, via="oidc"), "OIDC_LINKED", "user", u.id, u.username, after={"subject": str(sub), "email": email})
    return u


def create_api_key(s: Session, ctx: Ctx, name: str, role_code: str = "INTEGRATION_SERVICE", days: int | None = None) -> tuple[ApiKey, str]:
    ctx.require("admin:users")
    if role_code not in ROLES:
        raise ValidationFailed(f"Unknown role {role_code}")
    full, prefix, h = new_api_key()
    ak = ApiKey(tenant_id=ctx.tenant_id, name=name, prefix=prefix, key_hash=h, role_code=role_code, expires_at=(now() + timedelta(days=days)) if days else None)
    s.add(ak)
    s.flush()
    audit.record(s, ctx, "CREATE", "api_key", ak.id, name, after={"name": name, "role": role_code})
    return ak, full


def change_password(s: Session, ctx: Ctx, user_id: uuid.UUID, old: str | None, new: str) -> None:
    u = s.get(User, user_id)
    if u is None:
        raise NotFound("User not found")
    if ctx.user_id == user_id:
        if not verify_password(old or "", u.password_hash):
            raise Unauthorized("Current password is incorrect.", code="INVALID_CREDENTIALS")
    else:
        ctx.require("admin:users")
    problems = password_problems(new)
    if problems:
        raise ValidationFailed("Password needs " + ", ".join(problems), code="WEAK_PASSWORD")
    u.password_hash = hash_password(new)
    audit.record(s, ctx, "PASSWORD_CHANGE", "user", u.id, u.username)
