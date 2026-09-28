"""Integration framework: connectors, import/export jobs, webhooks, audit and saved views."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ..core.clock import now
from ..core.db import Base, IdMixin, JSONType, TenantMixin, TimestampMixin, VersionMixin


class Integration(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    """A configured connector (ERP/MES/WMS/PLM). Secrets are stored encrypted."""

    __tablename__ = "integration"
    code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200))
    system: Mapped[str] = mapped_column(String(40))  # SAP/ORACLE/DYNAMICS/ODOO/SAGE/INFOR/MES/GENERIC_REST/FILE
    direction: Mapped[str] = mapped_column(String(10), default="BOTH")
    settings: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    secret_encrypted: Mapped[str | None] = mapped_column(Text)
    schedule_cron: Mapped[str | None] = mapped_column(String(60))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(20))


class ImportJob(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "import_job"
    entity: Mapped[str] = mapped_column(String(40))
    filename: Mapped[str] = mapped_column(String(300))
    file_format: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default="UPLOADED")  # UPLOADED/MAPPED/VALIDATED/IMPORTED/FAILED
    columns: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    mapping: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    rows: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    errors: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    warnings: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    options: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    source: Mapped[str] = mapped_column(String(20), default="UPLOAD")


class ExportJob(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "export_job"
    kind: Mapped[str] = mapped_column(String(40))
    file_format: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default="DONE")
    params: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    rows: Mapped[int] = mapped_column(Integer, default=0)
    target: Mapped[str | None] = mapped_column(String(300))


class WebhookSubscription(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "webhook_subscription"
    name: Mapped[str] = mapped_column(String(120))
    url: Mapped[str] = mapped_column(String(500))
    events: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    secret_encrypted: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class WebhookDelivery(IdMixin, TenantMixin, Base):
    __tablename__ = "webhook_delivery"
    __table_args__ = (Index("ix_webhook_delivery_sub", "subscription_id", "created_at"),)
    subscription_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("webhook_subscription.id", ondelete="CASCADE"))
    event_type: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="PENDING")  # PENDING/DELIVERED/FAILED
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    response_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(IdMixin, TenantMixin, Base):
    """Who, when, what, before, after, why — append only."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_entity", "tenant_id", "entity_type", "entity_id", "at"),)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    user: Mapped[str | None] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(60))
    entity_type: Mapped[str] = mapped_column(String(60))
    entity_id: Mapped[str | None] = mapped_column(String(120))
    entity_label: Mapped[str | None] = mapped_column(String(200))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    reason: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str | None] = mapped_column(String(60))
    ip: Mapped[str | None] = mapped_column(String(60))


class SavedView(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "saved_view"
    owner: Mapped[str] = mapped_column(String(120), index=True)
    page: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(120))
    filters: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    columns: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    is_shared: Mapped[bool] = mapped_column(Boolean, default=False)
