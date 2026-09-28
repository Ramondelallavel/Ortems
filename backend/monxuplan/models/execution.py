"""Execution feedback (MES), industrial events and alerts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ..core.clock import now
from ..core.db import Base, IdMixin, JSONType, TenantMixin, TimestampMixin


class ActualProduction(IdMixin, TenantMixin, TimestampMixin, Base):
    """What really happened (never mixed with the schedule)."""

    __tablename__ = "actual_production"
    __table_args__ = (Index("ix_actual_order_op", "order_operation_id"),)
    order_operation_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("production_order_operation.id", ondelete="CASCADE"))
    resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("resource.id", ondelete="SET NULL"))
    operator_code: Mapped[str | None] = mapped_column(String(60))
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    good_quantity: Mapped[float] = mapped_column(Float, default=0)
    scrap_quantity: Mapped[float] = mapped_column(Float, default=0)
    source: Mapped[str] = mapped_column(String(20), default="MES")


class Event(IdMixin, TenantMixin, Base):
    """Industrial event store: OrderCreated, MachineDown, MaterialReceived, OperationStarted…"""

    __tablename__ = "event"
    __table_args__ = (Index("ix_event_type_time", "tenant_id", "type", "occurred_at"),)
    type: Mapped[str] = mapped_column(String(60))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    source: Mapped[str] = mapped_column(String(40), default="API")
    correlation_id: Mapped[str | None] = mapped_column(String(120), index=True)
    plant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("plant.id", ondelete="SET NULL"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    processing_result: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class Alert(IdMixin, TenantMixin, Base):
    __tablename__ = "alert"
    __table_args__ = (Index("ix_alert_open", "tenant_id", "plant_id", "status", "severity"),)
    plant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("plant.id", ondelete="CASCADE"))
    plan_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("plan.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(10))  # CRITICAL/WARNING/INFO
    title: Mapped[str] = mapped_column(String(300))
    message: Mapped[str] = mapped_column(Text, default="")
    context: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)  # entity refs to navigate to
    count: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(12), default="OPEN")  # OPEN/ACKNOWLEDGED/RESOLVED
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    acknowledged_by: Mapped[str | None] = mapped_column(String(120))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)
