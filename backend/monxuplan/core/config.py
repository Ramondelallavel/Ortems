"""Settings (environment variables, prefix ``MONXU_``)."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _secret() -> str:
    v = os.environ.get("MONXU_SECRET_KEY")
    if v:
        return v
    path = BASE_DIR / "data" / ".secret_key"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_text().strip()
    key = secrets.token_urlsafe(48)
    path.write_text(key)
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover
        pass
    return key


@dataclass(frozen=True)
class Settings:
    env: str = field(default_factory=lambda: os.environ.get("MONXU_ENV", "development"))
    database_url: str = field(default_factory=lambda: os.environ.get("MONXU_DATABASE_URL", f"sqlite:///{BASE_DIR / 'data' / 'monxuplan.db'}"))
    redis_url: str | None = field(default_factory=lambda: os.environ.get("MONXU_REDIS_URL") or None)
    secret_key: str = field(default_factory=_secret)
    session_hours: int = field(default_factory=lambda: int(os.environ.get("MONXU_SESSION_HOURS", "12")))
    cookie_secure: bool = field(default_factory=lambda: _bool("MONXU_COOKIE_SECURE", False))
    cors_origins: tuple[str, ...] = field(default_factory=lambda: tuple(o for o in os.environ.get("MONXU_CORS_ORIGINS", "http://localhost:3000").split(",") if o))
    worker_in_process: bool = field(default_factory=lambda: _bool("MONXU_WORKER_IN_PROCESS", True))
    worker_threads: int = field(default_factory=lambda: int(os.environ.get("MONXU_WORKER_THREADS", "2")))
    now_override: str | None = field(default_factory=lambda: os.environ.get("MONXU_NOW") or None)
    rate_limit_per_minute: int = field(default_factory=lambda: int(os.environ.get("MONXU_RATE_LIMIT_PER_MINUTE", "600")))
    login_rate_limit_per_minute: int = field(default_factory=lambda: int(os.environ.get("MONXU_LOGIN_RATE_LIMIT_PER_MINUTE", "20")))
    max_upload_mb: int = field(default_factory=lambda: int(os.environ.get("MONXU_MAX_UPLOAD_MB", "25")))
    oidc_issuer: str | None = field(default_factory=lambda: os.environ.get("MONXU_OIDC_ISSUER") or None)
    oidc_audience: str | None = field(default_factory=lambda: os.environ.get("MONXU_OIDC_AUDIENCE") or None)
    oidc_jwks_url: str | None = field(default_factory=lambda: os.environ.get("MONXU_OIDC_JWKS_URL") or None)
    log_level: str = field(default_factory=lambda: os.environ.get("MONXU_LOG_LEVEL", "INFO"))
    log_json: bool = field(default_factory=lambda: _bool("MONXU_LOG_JSON", True))
    anthropic_api_key: str | None = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY") or None)
    assistant_model: str = field(default_factory=lambda: os.environ.get("MONXU_ASSISTANT_MODEL", "claude-sonnet-5"))
    webhook_timeout_s: float = field(default_factory=lambda: float(os.environ.get("MONXU_WEBHOOK_TIMEOUT_S", "5")))
    default_time_limits: dict = field(default_factory=lambda: {"QUICK": 10.0, "NORMAL": 60.0, "DEEP": 600.0})

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_production(self) -> bool:
        return self.env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings() -> None:
    get_settings.cache_clear()
