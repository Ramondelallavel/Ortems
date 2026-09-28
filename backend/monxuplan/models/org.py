"""Organisation: tenant → company → site → plant → planning area → work centre."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base, IdMixin, JSONType, TenantMixin, TimestampMixin, VersionMixin


class Tenant(IdMixin, TimestampMixin, Base):
    __tablename__ = "tenant"
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class Company(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "company"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)
    code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200))
    currency: Mapped[str] = mapped_column(String(3), default="EUR")
    locale: Mapped[str] = mapped_column(String(10), default="en")
    unit_system: Mapped[str] = mapped_column(String(10), default="metric")


class Site(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "site"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("company.id", ondelete="RESTRICT"))
    code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200))
    country: Mapped[str | None] = mapped_column(String(2))


class Plant(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "plant"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)
    site_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("site.id", ondelete="SET NULL"))
    code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    country: Mapped[str | None] = mapped_column(String(2))
    default_calendar_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("calendar.id", ondelete="SET NULL", use_alter=True))
    published_plan_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("plan.id", ondelete="SET NULL", use_alter=True))
    live_scenario_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("scenario.id", ondelete="SET NULL", use_alter=True))
    settings: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class PlanningArea(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "planning_area"
    __table_args__ = (UniqueConstraint("tenant_id", "plant_id", "code"),)
    plant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("plant.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200))
    sort_order: Mapped[int] = mapped_column(default=0)


class WorkCenter(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "work_center"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)
    plant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("plant.id", ondelete="CASCADE"), index=True)
    area_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("planning_area.id", ondelete="SET NULL"))
    code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200))
    calendar_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("calendar.id", ondelete="SET NULL"))
