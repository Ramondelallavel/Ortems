"""Users, roles, permissions and API keys (RBAC, tenant scoped)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base, IdMixin, JSONType, TenantMixin, TimestampMixin, VersionMixin


class Permission(IdMixin, Base):
    __tablename__ = "permission"
    code: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str] = mapped_column(String(300), default="")


class Role(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "role"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)
    code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(120))
    is_system: Mapped[bool] = mapped_column(Boolean, default=True)
    permissions: Mapped[list[Any]] = mapped_column(JSONType, default=list)


class User(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "app_user"
    __table_args__ = (UniqueConstraint("tenant_id", "username"), UniqueConstraint("tenant_id", "email"))
    username: Mapped[str] = mapped_column(String(80))
    email: Mapped[str] = mapped_column(String(200))
    full_name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str | None] = mapped_column(String(300))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    locale: Mapped[str] = mapped_column(String(10), default="en")
    timezone: Mapped[str | None] = mapped_column(String(64))
    default_plant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("plant.id", ondelete="SET NULL"))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_subject: Mapped[str | None] = mapped_column(String(200), index=True)
    preferences: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    failed_logins: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # sessions and bearer tokens issued before this instant are refused (password change, deactivation,
    # "sign out everywhere"): tokens are stateless, this is their revocation point
    tokens_valid_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserRole(IdMixin, TenantMixin, Base):
    __tablename__ = "user_role"
    __table_args__ = (UniqueConstraint("user_id", "role_id", "plant_id"),)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("app_user.id", ondelete="CASCADE"), index=True)
    role_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("role.id", ondelete="CASCADE"))
    plant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("plant.id", ondelete="CASCADE"))


class ApiKey(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "api_key"
    name: Mapped[str] = mapped_column(String(120))
    prefix: Mapped[str] = mapped_column(String(16), index=True)
    key_hash: Mapped[str] = mapped_column(String(200))
    role_code: Mapped[str] = mapped_column(String(60), default="INTEGRATION_SERVICE")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
