"""Master data: calendars, resources, labour, tools, items, BOMs, routings, setups, planning rules."""

from __future__ import annotations

import uuid
from datetime import datetime, time
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, Time, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db import Base, IdMixin, JSONType, TenantMixin, TimestampMixin, VersionMixin

Qty = Numeric(18, 6, asdecimal=False)


class _Master(IdMixin, TenantMixin, TimestampMixin, VersionMixin):
    pass


# ---------------------------------------------------------------------------------------------
# Calendars
# ---------------------------------------------------------------------------------------------


class Calendar(_Master, Base):
    __tablename__ = "calendar"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)
    code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("calendar.id", ondelete="SET NULL"))
    always_available: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(Text)


class CalendarShift(IdMixin, TenantMixin, Base):
    __tablename__ = "calendar_shift"
    calendar_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("calendar.id", ondelete="CASCADE"), index=True)
    weekday: Mapped[int] = mapped_column(Integer)
    shift_code: Mapped[str] = mapped_column(String(40), default="S1")
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    kind: Mapped[str] = mapped_column(String(20), default="REGULAR")
    breaks: Mapped[list[Any]] = mapped_column(JSONType, default=list)


class CalendarException(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "calendar_exception"
    calendar_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("calendar.id", ondelete="CASCADE"), index=True)
    start_local: Mapped[datetime] = mapped_column(DateTime(timezone=False))
    end_local: Mapped[datetime] = mapped_column(DateTime(timezone=False))
    kind: Mapped[str] = mapped_column(String(20))  # HOLIDAY/CLOSURE → CLOSED; EXTRA_WORK → WORKING; OVERTIME
    reason: Mapped[str | None] = mapped_column(String(300))


# ---------------------------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------------------------


class Resource(_Master, Base):
    __tablename__ = "resource"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)
    plant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("plant.id", ondelete="CASCADE"), index=True)
    area_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("planning_area.id", ondelete="SET NULL"))
    work_center_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("work_center.id", ondelete="SET NULL"))
    code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20), default="MACHINE")
    capacity: Mapped[int] = mapped_column(Integer, default=1)
    calendar_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("calendar.id", ondelete="SET NULL"))
    efficiency: Mapped[float] = mapped_column(Float, default=1.0)
    speed_factor: Mapped[float] = mapped_column(Float, default=1.0)
    is_finite: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    cost_per_hour: Mapped[float] = mapped_column(Float, default=0.0)
    overtime_cost_per_hour: Mapped[float] = mapped_column(Float, default=0.0)
    setup_cost_per_hour: Mapped[float] = mapped_column(Float, default=0.0)
    energy_kw: Mapped[float] = mapped_column(Float, default=0.0)
    co2_kg_per_kwh: Mapped[float] = mapped_column(Float, default=0.0)
    setup_combine: Mapped[str] = mapped_column(String(3), default="MAX")
    detached_setup: Mapped[bool] = mapped_column(Boolean, default=False)
    initial_state: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    available_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    available_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="AVAILABLE")  # live machine state from MES
    description: Mapped[str | None] = mapped_column(Text)


class ResourceGroup(_Master, Base):
    __tablename__ = "resource_group"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)
    plant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("plant.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200))


class ResourceGroupMember(IdMixin, TenantMixin, Base):
    __tablename__ = "resource_group_member"
    __table_args__ = (UniqueConstraint("group_id", "resource_id"),)
    group_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("resource_group.id", ondelete="CASCADE"), index=True)
    resource_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("resource.id", ondelete="CASCADE"), index=True)


class Maintenance(_Master, Base):
    __tablename__ = "maintenance"
    resource_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("resource.id", ondelete="CASCADE"), index=True)
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(20), default="PREVENTIVE")  # PREVENTIVE/CORRECTIVE/PLANNED/UNPLANNED
    status: Mapped[str] = mapped_column(String(20), default="PLANNED")
    description: Mapped[str | None] = mapped_column(String(300))


class Downtime(_Master, Base):
    __tablename__ = "downtime"
    resource_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("resource.id", ondelete="CASCADE"), index=True)
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(300))
    source: Mapped[str] = mapped_column(String(20), default="MANUAL")


class Skill(_Master, Base):
    __tablename__ = "skill"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)
    code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200))


class LaborPool(_Master, Base):
    __tablename__ = "labor_pool"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)
    plant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("plant.id", ondelete="CASCADE"), index=True)
    resource_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("resource.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200))
    skill_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("skill.id", ondelete="SET NULL"))
    min_skill_level: Mapped[int] = mapped_column(Integer, default=1)
    machines_per_operator: Mapped[int] = mapped_column(Integer, default=1)


class Operator(_Master, Base):
    __tablename__ = "operator"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)
    plant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("plant.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200))
    labor_pool_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("labor_pool.id", ondelete="SET NULL"), index=True)
    calendar_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("calendar.id", ondelete="SET NULL"))
    cost_per_hour: Mapped[float] = mapped_column(Float, default=0.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("app_user.id", ondelete="SET NULL"))


class OperatorSkill(IdMixin, TenantMixin, Base):
    __tablename__ = "operator_skill"
    __table_args__ = (UniqueConstraint("operator_id", "skill_id"),)
    operator_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("operator.id", ondelete="CASCADE"), index=True)
    skill_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("skill.id", ondelete="CASCADE"))
    level: Mapped[int] = mapped_column(Integer, default=1)
    certified_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OperatorAbsence(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "operator_absence"
    operator_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("operator.id", ondelete="CASCADE"), index=True)
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(200))


class ToolCompatibility(IdMixin, TenantMixin, Base):
    __tablename__ = "tool_compatibility"
    __table_args__ = (UniqueConstraint("tool_id", "machine_id"),)
    tool_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("resource.id", ondelete="CASCADE"), index=True)
    machine_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("resource.id", ondelete="CASCADE"), index=True)


# ---------------------------------------------------------------------------------------------
# Items, BOM, routings
# ---------------------------------------------------------------------------------------------


class ProductFamily(_Master, Base):
    __tablename__ = "product_family"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)
    code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200))
    color_hint: Mapped[str | None] = mapped_column(String(9))
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class UnitOfMeasure(_Master, Base):
    __tablename__ = "unit_of_measure"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)
    code: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(80))
    dimension: Mapped[str] = mapped_column(String(20), default="count")  # count/mass/length/volume/time
    integer: Mapped[bool] = mapped_column(Boolean, default=False)


class UomConversion(IdMixin, TenantMixin, Base):
    __tablename__ = "uom_conversion"
    __table_args__ = (UniqueConstraint("tenant_id", "from_code", "to_code", "item_id"),)
    from_code: Mapped[str] = mapped_column(String(20))
    to_code: Mapped[str] = mapped_column(String(20))
    factor: Mapped[float] = mapped_column(Float)
    item_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("item.id", ondelete="CASCADE"))


class Item(_Master, Base):
    __tablename__ = "item"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)
    code: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(250))
    item_type: Mapped[str] = mapped_column(String(20), default="FINISHED")  # FINISHED/SEMI_FINISHED/RAW/PACKAGING
    make_or_buy: Mapped[str] = mapped_column(String(10), default="MAKE")
    uom: Mapped[str] = mapped_column(String(20), default="pcs")
    quantity_type: Mapped[str] = mapped_column(String(10), default="INTEGER")
    family_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("product_family.id", ondelete="SET NULL"), index=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    lot_policy: Mapped[str] = mapped_column(String(10), default="LFL")
    min_lot: Mapped[float | None] = mapped_column(Qty)
    max_lot: Mapped[float | None] = mapped_column(Qty)
    lot_multiple: Mapped[float | None] = mapped_column(Qty)
    fixed_lot: Mapped[float | None] = mapped_column(Qty)
    economic_lot: Mapped[float | None] = mapped_column(Qty)
    safety_stock: Mapped[float] = mapped_column(Qty, default=0)
    purchase_lead_time_days: Mapped[float] = mapped_column(Float, default=0)
    production_lead_time_days: Mapped[float] = mapped_column(Float, default=0)
    unit_cost: Mapped[float] = mapped_column(Float, default=0)
    unit_price: Mapped[float] = mapped_column(Float, default=0)
    shelf_life_days: Mapped[int | None] = mapped_column(Integer)
    safety_time_minutes: Mapped[float] = mapped_column(Float, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str | None] = mapped_column(Text)


class ItemPlant(IdMixin, TenantMixin, Base):
    __tablename__ = "item_plant"
    __table_args__ = (UniqueConstraint("item_id", "plant_id", "sourcing"),)
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("item.id", ondelete="CASCADE"), index=True)
    plant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("plant.id", ondelete="CASCADE"))
    sourcing: Mapped[str] = mapped_column(String(20), default="MAKE")  # MAKE/BUY/TRANSFER/SUBCONTRACT
    priority: Mapped[int] = mapped_column(Integer, default=1)


class Bom(_Master, Base):
    __tablename__ = "bom"
    __table_args__ = (UniqueConstraint("tenant_id", "item_id", "version_code"),)
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("item.id", ondelete="CASCADE"), index=True)
    version_code: Mapped[str] = mapped_column(String(20), default="1")
    base_quantity: Mapped[float] = mapped_column(Qty, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BomLine(IdMixin, TenantMixin, Base):
    __tablename__ = "bom_line"
    bom_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("bom.id", ondelete="CASCADE"), index=True)
    component_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("item.id", ondelete="RESTRICT"), index=True)
    quantity_per: Mapped[float] = mapped_column(Qty)
    scrap_pct: Mapped[float] = mapped_column(Float, default=0)
    operation_seq: Mapped[int | None] = mapped_column(Integer)
    is_phantom: Mapped[bool] = mapped_column(Boolean, default=False)
    position: Mapped[int] = mapped_column(Integer, default=10)


class Routing(_Master, Base):
    __tablename__ = "routing"
    __table_args__ = (UniqueConstraint("tenant_id", "item_id", "version_code"),)
    item_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("item.id", ondelete="CASCADE"), index=True)
    plant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("plant.id", ondelete="CASCADE"))
    version_code: Mapped[str] = mapped_column(String(20), default="1")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class RoutingOperation(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "routing_operation"
    __table_args__ = (UniqueConstraint("routing_id", "seq"),)
    routing_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("routing.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200))
    setup_minutes: Mapped[float] = mapped_column(Float, default=0)
    run_minutes_per_unit: Mapped[float] = mapped_column(Float, default=0)
    run_tiers: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    fixed_minutes: Mapped[float] = mapped_column(Float, default=0)
    batch_size: Mapped[float | None] = mapped_column(Qty)
    minutes_per_batch: Mapped[float] = mapped_column(Float, default=0)
    teardown_minutes: Mapped[float] = mapped_column(Float, default=0)
    queue_minutes: Mapped[float] = mapped_column(Float, default=0)
    move_minutes: Mapped[float] = mapped_column(Float, default=0)
    wait_minutes: Mapped[float] = mapped_column(Float, default=0)
    buffer_before_minutes: Mapped[float] = mapped_column(Float, default=0)
    buffer_after_minutes: Mapped[float] = mapped_column(Float, default=0)
    overlap_percent: Mapped[float | None] = mapped_column(Float)
    transfer_batch: Mapped[float | None] = mapped_column(Qty)
    splittable: Mapped[bool] = mapped_column(Boolean, default=False)
    min_split_quantity: Mapped[float | None] = mapped_column(Qty)
    interruptible: Mapped[bool] = mapped_column(Boolean, default=True)
    labor_pool_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("labor_pool.id", ondelete="SET NULL"))
    labor_units: Mapped[int] = mapped_column(Integer, default=1)
    tool_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("resource.id", ondelete="SET NULL"))
    tool_units: Mapped[int] = mapped_column(Integer, default=1)
    setup_attributes: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    instructions: Mapped[str | None] = mapped_column(Text)


class OperationResource(IdMixin, TenantMixin, Base):
    __tablename__ = "operation_resource"
    routing_operation_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("routing_operation.id", ondelete="CASCADE"), index=True)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("resource.id", ondelete="CASCADE"))
    group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("resource_group.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(20), default="PRIMARY")  # PRIMARY/ALTERNATIVE/SUBCONTRACT
    preference: Mapped[int] = mapped_column(Integer, default=0)
    speed_factor: Mapped[float] = mapped_column(Float, default=1.0)
    setup_minutes: Mapped[float | None] = mapped_column(Float)
    run_minutes_per_unit: Mapped[float | None] = mapped_column(Float)
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("supplier.id", ondelete="SET NULL"))
    subcontract_lead_time_minutes: Mapped[int | None] = mapped_column(Integer)
    subcontract_cost: Mapped[float | None] = mapped_column(Float)


class OperationPrecedence(IdMixin, TenantMixin, Base):
    __tablename__ = "operation_precedence"
    routing_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("routing.id", ondelete="CASCADE"), index=True)
    pred_seq: Mapped[int] = mapped_column(Integer)
    succ_seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(2), default="FS")
    lag_minutes: Mapped[float] = mapped_column(Float, default=0)


# ---------------------------------------------------------------------------------------------
# Setups, rules, optimisation profiles
# ---------------------------------------------------------------------------------------------


class SetupMatrix(_Master, Base):
    __tablename__ = "setup_matrix"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)
    code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200))
    attribute: Mapped[str] = mapped_column(String(60))
    same_minutes: Mapped[float] = mapped_column(Float, default=0)
    default_minutes: Mapped[float] = mapped_column(Float, default=0)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("resource.id", ondelete="CASCADE"))
    group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("resource_group.id", ondelete="CASCADE"))


class SetupMatrixEntry(IdMixin, TenantMixin, Base):
    __tablename__ = "setup_matrix_entry"
    __table_args__ = (UniqueConstraint("matrix_id", "from_value", "to_value"),)
    matrix_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("setup_matrix.id", ondelete="CASCADE"), index=True)
    from_value: Mapped[str] = mapped_column(String(80))
    to_value: Mapped[str] = mapped_column(String(80))
    minutes: Mapped[float] = mapped_column(Float)
    cost: Mapped[float | None] = mapped_column(Float)


class SetupRule(_Master, Base):
    __tablename__ = "setup_rule"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)
    code: Mapped[str] = mapped_column(String(60))
    description: Mapped[str] = mapped_column(String(300), default="")
    resource_ids: Mapped[list[Any] | None] = mapped_column(JSONType)
    when_prev: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    when_next: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    add_minutes: Mapped[float] = mapped_column(Float, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class SequenceRule(_Master, Base):
    __tablename__ = "sequence_rule"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)
    code: Mapped[str] = mapped_column(String(60))
    type: Mapped[str] = mapped_column(String(30), default="NOT_IMMEDIATELY_AFTER")
    description: Mapped[str] = mapped_column(String(300), default="")
    resource_ids: Mapped[list[Any] | None] = mapped_column(JSONType)
    prev_match: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    next_match: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class PlanningRule(_Master, Base):
    __tablename__ = "planning_rule"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)
    code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200))
    condition: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    actions: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str | None] = mapped_column(Text)


class OptimizationProfile(_Master, Base):
    __tablename__ = "optimization_profile"
    __table_args__ = (UniqueConstraint("tenant_id", "code"),)
    code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200))
    preset: Mapped[str | None] = mapped_column(String(40))
    objectives: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    constraints: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    solver: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(Text)
