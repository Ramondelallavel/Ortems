"""Scenarios, planning runs and immutable plan versions."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from ..core.clock import now
from ..core.db import Base, IdMixin, JSONType, PlanRowTenantMixin, TenantMixin, TimestampMixin, VersionMixin


class Scenario(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "scenario"
    __table_args__ = (UniqueConstraint("tenant_id", "plant_id", "name"),)
    plant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("plant.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("scenario.id", ondelete="SET NULL"))
    base_plan_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("plan.id", ondelete="SET NULL", use_alter=True))
    head_plan_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("plan.id", ondelete="SET NULL", use_alter=True))
    is_live: Mapped[bool] = mapped_column(Boolean, default=False)
    kind: Mapped[str] = mapped_column(String(20), default="WHAT_IF")  # LIVE/WHAT_IF/RESCHEDULE
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")  # ACTIVE/ARCHIVED
    owner: Mapped[str | None] = mapped_column(String(120))
    visibility: Mapped[str] = mapped_column(String(10), default="SHARED")  # PRIVATE/SHARED
    editors: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    config: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    redo_stack: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    locked_by: Mapped[str | None] = mapped_column(String(120))
    lock_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScenarioChange(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "scenario_change"
    __table_args__ = (UniqueConstraint("scenario_id", "seq"),)
    scenario_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("scenario.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    description: Mapped[str] = mapped_column(String(400), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ProblemSnapshot(IdMixin, TenantMixin, Base):
    """Exact engine input of a plan (gzip JSON), content addressed — shared by plan versions."""

    __tablename__ = "problem_snapshot"
    __table_args__ = (UniqueConstraint("tenant_id", "sha256"),)
    sha256: Mapped[str] = mapped_column(String(64))
    data: Mapped[bytes] = mapped_column(LargeBinary)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


ACTIVE_RUN_STATUSES = ("QUEUED", "RUNNING")


class PlanningRun(IdMixin, TenantMixin, TimestampMixin, Base):
    __tablename__ = "planning_run"
    __table_args__ = (
        Index("ix_planning_run_queue", "status", "created_at"),
        # at most one queued or running run per scenario, enforced by the database (not a count-then-insert)
        Index(
            "uq_planning_run_active_scenario",
            "scenario_id",
            unique=True,
            postgresql_where=text("status IN ('QUEUED', 'RUNNING')"),
            sqlite_where=text("status IN ('QUEUED', 'RUNNING')"),
        ),
    )
    scenario_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("scenario.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="OPTIMIZE")  # OPTIMIZE/PLAN/REPAIR/VALIDATE/SIMULATE
    # QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED, or STALE: computed, but its inputs changed meanwhile —
    # the result is kept as a version that did not become the scenario's current plan
    status: Mapped[str] = mapped_column(String(20), default="QUEUED")
    input_revision: Mapped[int | None] = mapped_column(Integer)  # tenant input revision the problem was built from
    baseline_plan_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # scenario head when the run started
    params: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    progress: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    current_step: Mapped[str | None] = mapped_column(String(80))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_s: Mapped[float | None] = mapped_column(Float)
    provider: Mapped[str | None] = mapped_column(String(20))
    solver_status: Mapped[str | None] = mapped_column(String(20))
    objective: Mapped[float | None] = mapped_column(Float)
    best_bound: Mapped[float | None] = mapped_column(Float)
    gap: Mapped[float | None] = mapped_column(Float)
    input_hash: Mapped[str | None] = mapped_column(String(64))
    plan_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("plan.id", ondelete="SET NULL", use_alter=True))
    error_code: Mapped[str | None] = mapped_column(String(60))
    error_message: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)
    worker: Mapped[str | None] = mapped_column(String(120))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class Plan(IdMixin, TenantMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "plan"
    __table_args__ = (UniqueConstraint("tenant_id", "number"), Index("ix_plan_scenario", "scenario_id", "version_no"))
    scenario_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("scenario.id", ondelete="CASCADE"))
    plant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("plant.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("planning_run.id", ondelete="SET NULL"))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("plan.id", ondelete="SET NULL"))
    number: Mapped[str] = mapped_column(String(60))
    version_no: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20), default="OPTIMIZED")  # OPTIMIZED/MANUAL_EDIT/REPAIR/IMPORTED
    # DRAFT/VALIDATED/PUBLISHED/SUPERSEDED/ARCHIVED, or STALE: result of a run whose inputs changed before
    # it finished (never the scenario head, never publishable)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")
    feasible: Mapped[bool] = mapped_column(Boolean, default=True)
    horizon_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    horizon_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    frozen_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kpis: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    # the two larger documents load only when read (headers, lists and trends never need them)
    kpi_details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, deferred=True)
    solver_metadata: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    # bottlenecks, data issues, excluded orders, change log and counts; per-order results, pegging and
    # unscheduled operations live in plan_order / plan_peg / plan_unscheduled
    analysis: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, deferred=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("problem_snapshot.id", ondelete="SET NULL"))
    input_hash: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    change_summary: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_by: Mapped[str | None] = mapped_column(String(120))
    publish_reason: Mapped[str | None] = mapped_column(Text)
    # publication checks overridden with ``force`` (code, message), empty for a clean publication
    publish_overrides: Mapped[list[Any]] = mapped_column(JSONType, default=list)


class ScheduledOperation(IdMixin, PlanRowTenantMixin, Base):
    """One scheduled operation of a plan version. Versions are immutable snapshots, so order,
    operation and resource ids are plain references (no foreign keys to check on 200 000-row inserts
    or to cascade when master data is deleted); ``*_key`` columns keep the engine identifiers."""

    __tablename__ = "scheduled_operation"
    __table_args__ = (
        Index("ix_sched_op_lane", "plan_id", "resource_key", "setup_start"),
        Index("ix_sched_op_order_key", "plan_id", "order_key"),
        UniqueConstraint("plan_id", "op_key"),
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("plan.id", ondelete="CASCADE"))
    op_key: Mapped[str] = mapped_column(String(120))
    order_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    order_key: Mapped[str] = mapped_column(String(120))
    order_operation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    resource_key: Mapped[str] = mapped_column(String(120))
    secondary: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    setup_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    setup_minutes: Mapped[int] = mapped_column(Integer, default=0)
    run_minutes: Mapped[int] = mapped_column(Integer, default=0)
    working_minutes: Mapped[int] = mapped_column(Integer, default=0)
    overtime_minutes: Mapped[int] = mapped_column(Integer, default=0)
    quantity: Mapped[float] = mapped_column(Float)
    is_fixed: Mapped[bool] = mapped_column(Boolean, default=False)
    fixed_reason: Mapped[str | None] = mapped_column(String(20))
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    is_late: Mapped[bool] = mapped_column(Boolean, default=False)
    zone: Mapped[str] = mapped_column(String(10), default="PLANNING")
    subcontracted: Mapped[bool] = mapped_column(Boolean, default=False)
    prev_op_key: Mapped[str | None] = mapped_column(String(120))
    material_ready: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    binding: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    cost: Mapped[float] = mapped_column(Float, default=0)


class PlanOrder(IdMixin, PlanRowTenantMixin, Base):
    """Result of one order in a plan version: status, planned dates, lateness and its first cause."""

    __tablename__ = "plan_order"
    __table_args__ = (
        UniqueConstraint("plan_id", "order_key"),
        Index("ix_plan_order_status", "plan_id", "status", "lateness_minutes"),
        Index("ix_plan_order_order", "plan_id", "order_id"),
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("plan.id", ondelete="CASCADE"))
    order_key: Mapped[str] = mapped_column(String(120))
    order_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # the production order (joins the order book)
    number: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(20))
    start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lateness_minutes: Mapped[int] = mapped_column(Integer, default=0)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    earliest_possible_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    limiting: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    material_status: Mapped[str] = mapped_column(String(20), default="NONE")
    rules_applied: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    cause: Mapped[dict[str, Any] | None] = mapped_column(JSONType)  # first cause of a late / unscheduled order


class PlanPeg(IdMixin, PlanRowTenantMixin, Base):
    """Material pegging of a plan version: which supply covers which consuming operation."""

    __tablename__ = "plan_peg"
    __table_args__ = (
        Index("ix_plan_peg_consumer", "plan_id", "consumer_order_id"),
        Index("ix_plan_peg_material", "plan_id", "material_id"),
        Index("ix_plan_peg_supply_order", "plan_id", "supply_order_id"),
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("plan.id", ondelete="CASCADE"))
    material_id: Mapped[str] = mapped_column(String(120))
    supply_id: Mapped[str] = mapped_column(String(200))
    supply_kind: Mapped[str] = mapped_column(String(30))
    supply_ref: Mapped[str | None] = mapped_column(String(200))
    supply_order_id: Mapped[str | None] = mapped_column(String(120))
    supply_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumer_op_id: Mapped[str] = mapped_column(String(120))
    consumer_order_id: Mapped[str] = mapped_column(String(120))
    need_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quantity: Mapped[float] = mapped_column(Float)


class PlanUnscheduled(IdMixin, PlanRowTenantMixin, Base):
    """An operation the plan version could not schedule, with the engine's reason."""

    __tablename__ = "plan_unscheduled"
    __table_args__ = (Index("ix_plan_unsched_order", "plan_id", "order_key"), Index("ix_plan_unsched_op", "plan_id", "op_key"))
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("plan.id", ondelete="CASCADE"))
    op_key: Mapped[str] = mapped_column(String(120))
    order_key: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(String(60))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class PlanDocument(IdMixin, PlanRowTenantMixin, Base):
    """Read models of a plan version, computed once when it is stored: gzip-compressed JSON
    documents keyed by ``(kind, key)`` — operation explanations per resource (``EXPLANATIONS``),
    capacity profiles per bucket size (``CAPACITY``), resource calendars (``CALENDAR``) and material
    projections (``MATERIAL``). A screen reads the few small documents it shows instead of
    rebuilding the engine state of a 200 000-operation plan."""

    __tablename__ = "plan_document"
    __table_args__ = (UniqueConstraint("plan_id", "kind", "key"),)
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("plan.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))
    key: Mapped[str] = mapped_column(String(120))
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    data: Mapped[bytes] = mapped_column(LargeBinary)


class ConstraintViolation(IdMixin, PlanRowTenantMixin, Base):
    __tablename__ = "constraint_violation"
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("plan.id", ondelete="CASCADE"), index=True)
    severity: Mapped[str] = mapped_column(String(10))
    hardness: Mapped[str] = mapped_column(String(4))
    type: Mapped[str] = mapped_column(String(60))
    message: Mapped[str] = mapped_column(Text)
    order_key: Mapped[str | None] = mapped_column(String(120))
    op_key: Mapped[str | None] = mapped_column(String(120))
    resource_key: Mapped[str | None] = mapped_column(String(120))
    material_key: Mapped[str | None] = mapped_column(String(120))
    start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class KpiValue(IdMixin, TenantMixin, Base):
    __tablename__ = "kpi_value"
    __table_args__ = (UniqueConstraint("plan_id", "code"),)
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("plan.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(60))
    value: Mapped[float | None] = mapped_column(Float)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
