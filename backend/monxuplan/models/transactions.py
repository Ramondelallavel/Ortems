"""Transactional data (usually mirrored from the ERP): customers, suppliers, demand, orders, stock."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base, IdMixin, JSONType, TenantMixin, TimestampMixin, VersionMixin

Qty = Numeric(18, 6, asdecimal=False)


class _Tx(IdMixin, TenantMixin, TimestampMixin, VersionMixin):
    pass


class Customer(_Tx, Base):
    __tablename__ = "customer"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)
    code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200))
    priority: Mapped[int] = mapped_column(Integer, default=5)
    is_strategic: Mapped[bool] = mapped_column(Boolean, default=False)
    country: Mapped[str | None] = mapped_column(String(2))
    safety_time_minutes: Mapped[float] = mapped_column(Float, default=0)


class Supplier(_Tx, Base):
    __tablename__ = "supplier"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)
    code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200))
    lead_time_days: Mapped[float] = mapped_column(Float, default=0)
    reliability_pct: Mapped[float | None] = mapped_column(Float)
    is_subcontractor: Mapped[bool] = mapped_column(Boolean, default=False)


class SalesOrder(_Tx, Base):
    __tablename__ = "sales_order"
    __table_args__ = (UniqueConstraint("tenant_id", "number"),)
    number: Mapped[str] = mapped_column(String(60))
    customer_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("customer.id", ondelete="RESTRICT"), index=True)
    order_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    erp_ref: Mapped[str | None] = mapped_column(String(80))


class SalesOrderLine(_Tx, Base):
    __tablename__ = "sales_order_line"
    __table_args__ = (UniqueConstraint("sales_order_id", "line_no"),)
    sales_order_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("sales_order.id", ondelete="CASCADE"), index=True)
    line_no: Mapped[int] = mapped_column(Integer)
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("item.id", ondelete="RESTRICT"), index=True)
    plant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("plant.id", ondelete="SET NULL"))
    quantity: Mapped[float] = mapped_column(Qty)
    delivered_quantity: Mapped[float] = mapped_column(Qty, default=0)
    requested_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    promised_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    priority: Mapped[int] = mapped_column(Integer, default=5)
    unit_price: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")


class Demand(_Tx, Base):
    __tablename__ = "demand"
    __table_args__ = (Index("ix_demand_item_period", "tenant_id", "item_id", "period_start"),)
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("item.id", ondelete="CASCADE"))
    plant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("plant.id", ondelete="CASCADE"))
    period_start: Mapped[date] = mapped_column(Date)
    quantity: Mapped[float] = mapped_column(Qty)
    demand_type: Mapped[str] = mapped_column(String(20), default="FORECAST")
    source: Mapped[str | None] = mapped_column(String(60))
    ref: Mapped[str | None] = mapped_column(String(80))


class ProductionOrder(_Tx, Base):
    __tablename__ = "production_order"
    __table_args__ = (
        UniqueConstraint("tenant_id", "number"),
        Index("ix_prod_order_plan", "tenant_id", "plant_id", "status", "due_date"),
    )
    number: Mapped[str] = mapped_column(String(60))
    plant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("plant.id", ondelete="CASCADE"))
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("item.id", ondelete="RESTRICT"), index=True)
    quantity: Mapped[float] = mapped_column(Qty)
    completed_quantity: Mapped[float] = mapped_column(Qty, default=0)
    scrap_quantity: Mapped[float] = mapped_column(Qty, default=0)
    status: Mapped[str] = mapped_column(String(24), default="PLANNED")
    material_status: Mapped[str | None] = mapped_column(String(20))
    planning_status: Mapped[str | None] = mapped_column(String(20))
    release_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    requested_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    promised_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    need_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    priority: Mapped[int] = mapped_column(Integer, default=5)
    planner_priority: Mapped[int | None] = mapped_column(Integer)
    expedite: Mapped[bool] = mapped_column(Boolean, default=False)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("customer.id", ondelete="SET NULL"), index=True)
    sales_order_line_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("sales_order_line.id", ondelete="SET NULL"), index=True)
    parent_order_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("production_order.id", ondelete="SET NULL"))
    routing_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("routing.id", ondelete="SET NULL"))
    bom_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("bom.id", ondelete="SET NULL"))
    source: Mapped[str] = mapped_column(String(20), default="ERP")  # ERP/MRP/MANUAL/SCENARIO
    erp_ref: Mapped[str | None] = mapped_column(String(80))
    planner: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str | None] = mapped_column(Text)


class ProductionOrderOperation(_Tx, Base):
    __tablename__ = "production_order_operation"
    __table_args__ = (UniqueConstraint("order_id", "seq"),)
    order_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("production_order.id", ondelete="CASCADE"), index=True)
    routing_operation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("routing_operation.id", ondelete="SET NULL"))
    seq: Mapped[int] = mapped_column(Integer)
    code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="PLANNED")  # PLANNED/RELEASED/IN_PROGRESS/COMPLETED
    completed_quantity: Mapped[float] = mapped_column(Qty, default=0)
    scrap_quantity: Mapped[float] = mapped_column(Qty, default=0)
    actual_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("resource.id", ondelete="SET NULL"))
    pinned_resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("resource.id", ondelete="SET NULL"))
    overrides: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class PurchaseOrder(_Tx, Base):
    __tablename__ = "purchase_order"
    __table_args__ = (UniqueConstraint("tenant_id", "number"),)
    number: Mapped[str] = mapped_column(String(60))
    supplier_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("supplier.id", ondelete="RESTRICT"), index=True)
    plant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("plant.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    erp_ref: Mapped[str | None] = mapped_column(String(80))


class PurchaseOrderLine(_Tx, Base):
    __tablename__ = "purchase_order_line"
    __table_args__ = (UniqueConstraint("purchase_order_id", "line_no"),)
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("purchase_order.id", ondelete="CASCADE"), index=True)
    line_no: Mapped[int] = mapped_column(Integer)
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("item.id", ondelete="RESTRICT"), index=True)
    quantity: Mapped[float] = mapped_column(Qty)
    received_quantity: Mapped[float] = mapped_column(Qty, default=0)
    expected_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    original_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")


class Inventory(_Tx, Base):
    __tablename__ = "inventory"
    __table_args__ = (UniqueConstraint("tenant_id", "item_id", "plant_id", "location"),)
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("item.id", ondelete="CASCADE"), index=True)
    plant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("plant.id", ondelete="CASCADE"))
    location: Mapped[str] = mapped_column(String(60), default="MAIN")
    on_hand: Mapped[float] = mapped_column(Qty, default=0)
    reserved: Mapped[float] = mapped_column(Qty, default=0)
    blocked: Mapped[float] = mapped_column(Qty, default=0)
    quality_hold: Mapped[float] = mapped_column(Qty, default=0)

    @property
    def available(self) -> float:
        return max(0.0, (self.on_hand or 0) - (self.reserved or 0) - (self.blocked or 0) - (self.quality_hold or 0))


class MaterialLot(_Tx, Base):
    __tablename__ = "material_lot"
    __table_args__ = (UniqueConstraint("tenant_id", "item_id", "lot_number"),)
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("item.id", ondelete="CASCADE"), index=True)
    plant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("plant.id", ondelete="SET NULL"))
    lot_number: Mapped[str] = mapped_column(String(80))
    quantity: Mapped[float] = mapped_column(Qty)
    status: Mapped[str] = mapped_column(String(20), default="AVAILABLE")  # AVAILABLE/QUALITY_HOLD/BLOCKED
    expiry_date: Mapped[date | None] = mapped_column(Date)


class InventoryTransaction(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "inventory_transaction"
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("item.id", ondelete="CASCADE"), index=True)
    plant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("plant.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(20))  # RECEIPT/ISSUE/ADJUSTMENT/TRANSFER/PRODUCTION
    quantity: Mapped[float] = mapped_column(Qty)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ref: Mapped[str | None] = mapped_column(String(80))


class MaterialReservation(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "material_reservation"
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("item.id", ondelete="CASCADE"), index=True)
    order_operation_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("production_order_operation.id", ondelete="CASCADE"))
    quantity: Mapped[float] = mapped_column(Qty)
    lot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("material_lot.id", ondelete="SET NULL"))
    plan_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("plan.id", ondelete="CASCADE"))


class TransferLane(_Tx, Base):
    __tablename__ = "transfer_lane"
    __table_args__ = (UniqueConstraint("tenant_id", "from_code", "to_code"),)
    from_code: Mapped[str] = mapped_column(String(60))
    to_code: Mapped[str] = mapped_column(String(60))
    lead_time_minutes: Mapped[int] = mapped_column(Integer, default=0)
    cost_per_unit: Mapped[float] = mapped_column(Float, default=0)
