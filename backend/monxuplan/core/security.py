"""Authentication primitives.

* Passwords: scrypt (N=2^15, r=8, p=1) with per-user salt, constant-time comparison.
* Sessions: HS256 JWT signed with ``MONXU_SECRET_KEY`` — in an httpOnly ``SameSite=Lax`` cookie for
  browsers (plus double-submit CSRF token) or as a bearer token for API clients.
* API keys: ``mxk_<prefix>_<secret>``; only a scrypt hash is stored.
* OIDC: bearer tokens from the configured issuer are verified against its JWKS.
* Secrets at rest (connector credentials, webhook secrets): AES-GCM with a key derived from the secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import uuid
from datetime import timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .clock import now
from .config import get_settings

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
PERMISSIONS: dict[str, str] = {
    "masterdata:read": "Read master data",
    "masterdata:write": "Create/update master data",
    "orders:read": "Read orders, demand and inventory",
    "orders:write": "Create/update orders",
    "scenario:read": "Read scenarios",
    "scenario:write": "Create/modify scenarios",
    "plan:read": "Read plans and schedules",
    "plan:run": "Run planning / optimisation",
    "plan:edit": "Edit plans (move, lock, split)",
    "plan:publish": "Publish plans",
    "plan:frozen": "Change operations in the frozen zone",
    "analytics:read": "Read KPIs and analytics",
    "integration:import": "Import data",
    "integration:export": "Export data",
    "integration:manage": "Manage connectors and webhooks",
    "execution:report": "Report production (MES feedback)",
    "alerts:manage": "Acknowledge/resolve alerts",
    "admin:users": "Manage users and roles",
    "admin:config": "Manage rules, profiles and settings",
    "admin:audit": "Read the audit log",
    "tenant:manage": "Manage tenants",
}

READ = ["masterdata:read", "orders:read", "scenario:read", "plan:read", "analytics:read"]
ROLES: dict[str, tuple[str, list[str]]] = {
    "SUPER_ADMIN": ("Super Admin", list(PERMISSIONS)),
    "COMPANY_ADMIN": ("Company Admin", [p for p in PERMISSIONS if p != "tenant:manage"]),
    "PLANNER": (
        "Planner",
        READ + ["masterdata:write", "orders:write", "scenario:write", "plan:run", "plan:edit", "plan:publish", "integration:import", "integration:export", "alerts:manage"],
    ),
    "PRODUCTION_MANAGER": ("Production Manager", READ + ["scenario:write", "plan:run", "plan:publish", "plan:frozen", "alerts:manage", "integration:export", "admin:audit"]),
    "SUPERVISOR": ("Supervisor", ["plan:read", "orders:read", "masterdata:read", "analytics:read", "execution:report", "alerts:manage"]),
    "OPERATOR": ("Operator", ["plan:read", "execution:report"]),
    "VIEWER": ("Viewer", READ),
    "INTEGRATION_SERVICE": ("Integration Service", READ + ["masterdata:write", "orders:write", "integration:import", "integration:export", "execution:report"]),
}


# ------------------------------------------------------------------ passwords
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=128 * 1024 * 1024)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    try:
        _, n, r, p, salt_b64, dk_b64 = stored.split("$")
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        dk = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), maxmem=128 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


def password_problems(password: str) -> list[str]:
    problems = []
    if len(password) < 10:
        problems.append("at least 10 characters")
    if password.lower() == password or password.upper() == password:
        problems.append("upper and lower case letters")
    if not any(c.isdigit() for c in password):
        problems.append("a digit")
    return problems


# ------------------------------------------------------------------ session tokens
def create_token(user_id: uuid.UUID, tenant_id: uuid.UUID, hours: int | None = None, extra: dict[str, Any] | None = None) -> str:
    s = get_settings()
    t = now()
    payload = {
        "sub": str(user_id),
        "tid": str(tenant_id),
        "iat": int(t.timestamp()),
        "exp": int((t + timedelta(hours=hours or s.session_hours)).timestamp()),
        "iss": "monxuplan",
        "jti": secrets.token_hex(8),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, s.secret_key, algorithm="HS256")


def decode_token(token: str) -> dict[str, Any] | None:
    try:
        return jwt.decode(token, get_settings().secret_key, algorithms=["HS256"], issuer="monxuplan", options={"require": ["exp", "sub", "tid"]}, leeway=5)
    except jwt.PyJWTError:
        return None


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


# ------------------------------------------------------------------ API keys
def new_api_key() -> tuple[str, str, str]:
    """Returns (full key shown once, prefix, hash to store)."""
    prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    full = f"mxk_{prefix}_{secret}"
    return full, prefix, hash_api_key(full)


def hash_api_key(full: str) -> str:
    return hashlib.sha256((get_settings().secret_key + "|" + full).encode()).hexdigest()


def parse_api_key(full: str) -> str | None:
    if not full.startswith("mxk_"):
        return None
    parts = full.split("_", 2)
    return parts[1] if len(parts) == 3 else None


# ------------------------------------------------------------------ OIDC
_jwks_client = None


def verify_oidc(token: str) -> dict[str, Any] | None:
    s = get_settings()
    if not s.oidc_issuer:
        return None
    global _jwks_client
    try:
        if _jwks_client is None:
            url = s.oidc_jwks_url or s.oidc_issuer.rstrip("/") + "/.well-known/jwks.json"
            _jwks_client = jwt.PyJWKClient(url, cache_keys=True)
        key = _jwks_client.get_signing_key_from_jwt(token).key
        return jwt.decode(token, key, algorithms=["RS256", "ES256"], audience=s.oidc_audience, issuer=s.oidc_issuer)
    except Exception:  # noqa: BLE001 - any failure means "not a valid OIDC token"
        return None


# ------------------------------------------------------------------ secrets at rest
def _aes_key() -> bytes:
    return hashlib.sha256(("monxuplan-secrets|" + get_settings().secret_key).encode()).digest()


def encrypt_secret(plain: str) -> str:
    nonce = os.urandom(12)
    ct = AESGCM(_aes_key()).encrypt(nonce, plain.encode(), b"monxuplan")
    return "v1:" + base64.urlsafe_b64encode(nonce + ct).decode()


def decrypt_secret(token: str) -> str:
    if not token.startswith("v1:"):
        raise ValueError("unknown secret format")
    raw = base64.urlsafe_b64decode(token[3:])
    return AESGCM(_aes_key()).decrypt(raw[:12], raw[12:], b"monxuplan").decode()


def sign_payload(secret: str, body: bytes, timestamp: str) -> str:
    """HMAC-SHA256 signature for webhook deliveries (``X-Monxu-Signature``)."""
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
