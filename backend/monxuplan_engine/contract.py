"""Planning Engine contract.

The engine is a pure function ``solve(Problem) -> Solution``. These Pydantic models are the stable,
versioned JSON contract between the platform (or any other client) and the engine. Nothing in this
module knows about databases or HTTP.

Conventions
-----------
* Datetimes are ISO-8601. Naive datetimes are interpreted in ``horizon.timezone`` (plant time);
  calendar exceptions without offset are interpreted in the calendar's own timezone.
* Durations are minutes (float in the input, integer minutes internally).
* Identifiers are opaque strings chosen by the caller (UUIDs or business codes).
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------------------------
# Enumerations (as Literals so that the JSON schema lists the allowed values)
# --------------------------------------------------------------------------------------------

ResourceKind = Literal[
    "MACHINE", "HUMAN", "TOOL", "LABOR_POOL", "WORK_CENTER", "SUBCONTRACTOR", "STORAGE", "TRANSPORT", "ROOM"
]
UnavailabilityKind = Literal[
    "MAINTENANCE_PREVENTIVE",
    "MAINTENANCE_CORRECTIVE",
    "MAINTENANCE_PLANNED",
    "BREAKDOWN",
    "DOWNTIME",
    "ABSENCE",
    "BLOCKED",
]
PrecedenceType = Literal["FS", "SS", "FF", "SF"]
OperationStatus = Literal["PLANNED", "RELEASED", "IN_PROGRESS", "COMPLETED"]
SupplyKind = Literal["ON_HAND", "PURCHASE", "TRANSFER", "PRODUCTION", "PROJECTED"]
Severity = Literal["CRITICAL", "WARNING", "INFO"]
Hardness = Literal["HARD", "SOFT"]
ProviderName = Literal["heuristic", "cpsat", "hybrid", "mip"]
SolverProfile = Literal["QUICK", "NORMAL", "DEEP", "CUSTOM"]
DispatchRule = Literal[
    "EDD",
    "EDD_PRIORITY",
    "SPT",
    "LPT",
    "FIFO",
    "CRITICAL_RATIO",
    "MIN_SLACK",
    "SHORTEST_SETUP",
    "FAMILY_GROUPING",
    "MATERIAL_AVAILABILITY",
    "BOTTLENECK_FIRST",
    "DUE_DATE_FIRST",
    "CUSTOMER_PRIORITY",
    "HYBRID_APS",
]
ObjectiveComponent = Literal[
    "unscheduled",
    "late_orders",
    "tardiness",
    "critical_tardiness",
    "setup",
    "wip",
    "inventory",
    "makespan",
    "overtime",
    "stability",
    "preference",
    "cost",
]
ResourceStrategy = Literal["EARLIEST_FINISH", "PREFERRED", "LEAST_SETUP", "LOWEST_COST"]
Direction = Literal["FORWARD", "BACKWARD", "HYBRID"]
BindingType = Literal[
    "HORIZON_START",
    "RELEASE",
    "FROZEN_ZONE",
    "PREDECESSOR",
    "MATERIAL",
    "RESOURCE",
    "SETUP",
    "LABOR",
    "TOOL",
    "CALENDAR",
    "FIXED",
    "NONE",
]


# --------------------------------------------------------------------------------------------
# Horizon and calendars
# --------------------------------------------------------------------------------------------


class Horizon(_Model):
    start: datetime
    end: datetime
    frozen_until: datetime | None = None
    flexible_until: datetime | None = None
    timezone: str = "UTC"
    overflow_days: int = Field(
        default=60,
        ge=0,
        le=730,
        description="Calendars are expanded this many days past `end` so that late work can still be "
        "placed (and reported as late / beyond horizon) instead of being dropped.",
    )

    @model_validator(mode="after")
    def _check(self) -> Horizon:
        if self.end <= self.start:
            raise ValueError("horizon.end must be after horizon.start")
        return self


class BreakSpec(_Model):
    start: time
    end: time


class ShiftSpec(_Model):
    weekday: int = Field(ge=0, le=6, description="0 = Monday … 6 = Sunday (plant local time)")
    start: time
    end: time = Field(description="If end <= start the shift crosses midnight")
    kind: Literal["REGULAR", "OVERTIME"] = "REGULAR"
    label: str | None = None
    breaks: list[BreakSpec] = Field(default_factory=list)


class CalendarExceptionSpec(_Model):
    start: datetime
    end: datetime
    kind: Literal["CLOSED", "WORKING", "OVERTIME"]
    reason: str | None = None


class CalendarSpec(_Model):
    id: str
    name: str | None = None
    timezone: str = "UTC"
    always_available: bool = False
    shifts: list[ShiftSpec] = Field(default_factory=list)
    exceptions: list[CalendarExceptionSpec] = Field(default_factory=list)
    parent_id: str | None = Field(
        default=None, description="Inherit the parent's shifts when this calendar defines none, plus its exceptions"
    )


# --------------------------------------------------------------------------------------------
# Resources
# --------------------------------------------------------------------------------------------


class UnavailabilitySpec(_Model):
    id: str | None = None
    start: datetime
    end: datetime
    kind: UnavailabilityKind = "BLOCKED"
    reason: str | None = None
    capacity_loss: int | None = Field(default=None, description="Units lost on a cumulative resource; None = all")


class CapacityChangeSpec(_Model):
    start: datetime
    end: datetime
    capacity: int = Field(ge=0)


class ResourceSpec(_Model):
    id: str
    code: str
    name: str | None = None
    kind: ResourceKind = "MACHINE"
    capacity: int = Field(default=1, ge=0, description="1 = unary (disjunctive) resource; >1 = cumulative")
    calendar_id: str | None = Field(default=None, description="None = available 24/7")
    efficiency: float = Field(default=1.0, gt=0, le=5)
    finite: bool = True
    groups: list[str] = Field(default_factory=list)
    area: str | None = None
    plant: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    unavailability: list[UnavailabilitySpec] = Field(default_factory=list)
    capacity_profile: list[CapacityChangeSpec] = Field(default_factory=list)
    initial_state: dict[str, Any] | None = Field(
        default=None, description="Setup attributes of the last job run before the horizon"
    )
    setup_matrix_ids: list[str] = Field(default_factory=list)
    setup_combine: Literal["MAX", "SUM"] = "MAX"
    detached_setup: bool = Field(
        default=False,
        description="Changeovers can be done before the job is ready (machine prepared in advance); precedences, "
        "release and material then constrain the run start instead of the setup start",
    )
    cost_per_hour: float = 0.0
    overtime_cost_per_hour: float = 0.0
    setup_cost_per_hour: float = 0.0
    energy_kw: float = 0.0
    co2_kg_per_kwh: float = 0.0
    available_from: datetime | None = None
    available_until: datetime | None = None


# --------------------------------------------------------------------------------------------
# Setup modelling
# --------------------------------------------------------------------------------------------


class SetupEntrySpec(_Model):
    from_value: str = Field(alias="from")
    to_value: str = Field(alias="to")
    minutes: float = Field(ge=0)


class SetupMatrixSpec(_Model):
    id: str
    name: str | None = None
    attribute: str = Field(description="Setup attribute compared between consecutive jobs (item, family, color, …)")
    same_minutes: float = Field(default=0, ge=0)
    default_minutes: float = Field(default=0, ge=0, description="Changeover not listed in entries / unknown state")
    entries: list[SetupEntrySpec] = Field(default_factory=list)


class SetupRuleSpec(_Model):
    id: str
    description: str | None = None
    resource_ids: list[str] | None = None
    when_prev: dict[str, Any] = Field(default_factory=dict)
    when_next: dict[str, Any] = Field(default_factory=dict)
    add_minutes: float = Field(ge=0)


# --------------------------------------------------------------------------------------------
# Materials
# --------------------------------------------------------------------------------------------


class SupplySpec(_Model):
    id: str
    time: datetime | None = Field(default=None, description="None = available now (on hand)")
    quantity: float = Field(gt=0)
    kind: SupplyKind = "PURCHASE"
    firm: bool = True
    ref: str | None = None
    supplier_id: str | None = None


class MaterialSpec(_Model):
    id: str
    code: str
    name: str | None = None
    uom: str = "unit"
    quantity_type: Literal["INTEGER", "DECIMAL"] = "DECIMAL"
    make_or_buy: Literal["MAKE", "BUY"] = "BUY"
    supplies: list[SupplySpec] = Field(default_factory=list)
    safety_stock: float = 0.0
    replenishment_lead_time_minutes: int | None = None


class MaterialUseSpec(_Model):
    material_id: str
    quantity: float = Field(gt=0)


# --------------------------------------------------------------------------------------------
# Orders and operations
# --------------------------------------------------------------------------------------------


class RunTierSpec(_Model):
    min_quantity: float = Field(ge=0)
    minutes_per_unit: float = Field(ge=0)


class DurationSpec(_Model):
    setup_minutes: float = Field(default=0, ge=0)
    run_minutes_per_unit: float = Field(default=0, ge=0)
    run_tiers: list[RunTierSpec] = Field(default_factory=list)
    fixed_minutes: float = Field(default=0, ge=0)
    batch_size: float | None = Field(default=None, gt=0)
    minutes_per_batch: float = Field(default=0, ge=0)
    teardown_minutes: float = Field(default=0, ge=0)
    queue_minutes: float = Field(default=0, ge=0, description="Waiting before the operation (no resource)")
    move_minutes: float = Field(default=0, ge=0, description="Transport after the operation (no resource)")
    wait_minutes: float = Field(default=0, ge=0, description="Waiting after the operation, e.g. drying")
    buffer_before_minutes: float = Field(default=0, ge=0)
    buffer_after_minutes: float = Field(default=0, ge=0)


class SecondaryRequirementSpec(_Model):
    resource_id: str
    units: int = Field(default=1, ge=1)


class SubcontractSpec(_Model):
    supplier_id: str | None = None
    lead_time_minutes: int = Field(ge=0, description="Elapsed time at the subcontractor")
    cost: float = 0.0


class ModeSpec(_Model):
    resource_id: str
    secondary: list[SecondaryRequirementSpec] = Field(default_factory=list)
    speed_factor: float = Field(default=1.0, gt=0)
    preference: int = Field(default=0, ge=0, description="0 = primary; higher = less preferred alternative")
    setup_minutes: float | None = Field(default=None, ge=0)
    run_minutes_per_unit: float | None = Field(default=None, ge=0)
    cost_per_hour: float | None = None
    subcontract: SubcontractSpec | None = None
    label: str | None = None


class FixedAssignmentSpec(_Model):
    resource_id: str
    start: datetime
    end: datetime | None = None
    reason: Literal["FROZEN", "LOCKED", "IN_PROGRESS", "MANUAL", "KEPT"] = "LOCKED"
    setup_minutes: float | None = None


class OperationSpec(_Model):
    id: str
    order_id: str
    seq: int
    code: str | None = None
    name: str | None = None
    quantity: float = Field(gt=0)
    duration: DurationSpec = Field(default_factory=DurationSpec)
    modes: list[ModeSpec] = Field(default_factory=list)
    materials: list[MaterialUseSpec] = Field(default_factory=list)
    setup_state: dict[str, Any] = Field(default_factory=dict)
    interruptible: bool = Field(default=True, description="False = must run inside one continuous working window")
    splittable: bool = False
    min_split_quantity: float | None = None
    transfer_batch: float | None = Field(default=None, gt=0, description="Overlap: successor may start after this qty")
    overlap_percent: float | None = Field(default=None, ge=0, le=100)
    status: OperationStatus = "PLANNED"
    remaining_quantity: float | None = None
    earliest_start: datetime | None = None
    fixed: FixedAssignmentSpec | None = None
    pinned_resource_id: str | None = None


class LotRulesSpec(_Model):
    min_lot: float | None = None
    max_lot: float | None = None
    multiple: float | None = None
    integer: bool = False


class OrderSpec(_Model):
    id: str
    number: str
    item_id: str
    item_code: str | None = None
    quantity: float = Field(gt=0)
    due: datetime
    release: datetime | None = None
    requested: datetime | None = None
    promised: datetime | None = None
    priority: int = Field(default=5, ge=0, le=10)
    customer_id: str | None = None
    customer_priority: int = Field(default=5, ge=0, le=10)
    strategic: bool = False
    expedite: bool = False
    planner_priority: int | None = Field(default=None, ge=0, le=10)
    weight: float | None = Field(default=None, gt=0, description="Explicit weight; otherwise derived (objectives.priority_weighting)")
    family: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    produces_material_id: str | None = Field(
        default=None, description="Material supplied (at the end of the last operation) for parent orders"
    )
    sales_order_ref: str | None = None
    revenue: float | None = None
    safety_time_minutes: float = 0.0
    lot: LotRulesSpec | None = None
    status: str | None = None


class PrecedenceSpec(_Model):
    pred: str
    succ: str
    type: PrecedenceType = "FS"
    lag_minutes: float = Field(default=0, description="Negative = lead")
    kind: Literal["ROUTING", "MATERIAL", "MANUAL", "SEQUENCE"] = "ROUTING"


class SequenceConstraintSpec(_Model):
    id: str
    type: Literal["BEFORE", "NOT_IMMEDIATELY_AFTER", "LOCKED_SEQUENCE"]
    op_a: str | None = None
    op_b: str | None = None
    resource_ids: list[str] | None = None
    prev_match: dict[str, Any] = Field(default_factory=dict)
    next_match: dict[str, Any] = Field(default_factory=dict)
    op_ids: list[str] = Field(default_factory=list)
    description: str | None = None


# --------------------------------------------------------------------------------------------
# Rules, constraints, objectives, solver settings
# --------------------------------------------------------------------------------------------


class RuleSpec(_Model):
    """Configurable planning rule: IF condition THEN actions (see rules.py for the DSL)."""

    id: str
    name: str
    condition: dict[str, Any] = Field(default_factory=dict)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    priority: int = 100
    active: bool = True


class ConstraintSettings(_Model):
    materials: Literal["HARD", "ALLOW_SHORTAGE", "IGNORE"] = "HARD"
    projected_receipts: bool = Field(default=True, description="Count non-firm / projected receipts as supply")
    allow_overtime: bool = False
    frozen: Literal["HARD", "ALLOW_CHANGES"] = "HARD"
    frozen_zone_blocks_new_work: bool = Field(
        default=True, description="Non-frozen work may not be placed before horizon.frozen_until"
    )
    labor: Literal["HARD", "IGNORE"] = "HARD"
    tools: Literal["HARD", "IGNORE"] = "HARD"
    sequence_rules: bool = True
    beyond_horizon: Literal["ALLOW", "UNSCHEDULE"] = "ALLOW"


DEFAULT_WEIGHTS: dict[str, float] = {
    "late_orders": 20.0,
    "tardiness": 15.0,
    "critical_tardiness": 10.0,
    "setup": 20.0,
    "makespan": 15.0,
    "inventory": 5.0,
    "wip": 5.0,
    "overtime": 5.0,
    "stability": 5.0,
}


class PriorityWeighting(_Model):
    """weight_j = base + Σ coefficient × feature (documented, configurable — no hidden logic)."""

    base: float = 1.0
    priority: float = 0.2
    customer_priority: float = 0.1
    planner_priority: float = 0.1
    expedite: float = 2.0
    strategic: float = 1.0


class ObjectiveSpec(_Model):
    mode: Literal["WEIGHTED", "LEXICOGRAPHIC"] = "WEIGHTED"
    preset: str | None = None
    weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    levels: list[list[str]] = Field(default_factory=list)
    tolerance: float = Field(default=0.0, ge=0, le=1)
    priority_weighting: PriorityWeighting = Field(default_factory=PriorityWeighting)
    stability_resource_change_minutes: int = 480

    @field_validator("weights")
    @classmethod
    def _known_weights(cls, v: dict[str, float]) -> dict[str, float]:
        allowed = set(ObjectiveComponent.__args__)  # type: ignore[attr-defined]
        unknown = set(v) - allowed
        if unknown:
            raise ValueError(f"unknown objective components: {sorted(unknown)}")
        return v

    @field_validator("levels")
    @classmethod
    def _known_levels(cls, v: list[list[str]]) -> list[list[str]]:
        allowed = set(ObjectiveComponent.__args__)  # type: ignore[attr-defined]
        for lvl in v:
            unknown = set(lvl) - allowed
            if unknown:
                raise ValueError(f"unknown objective components: {sorted(unknown)}")
        return v


class ModeSelection(_Model):
    """How the schedule builder picks among feasible modes (score in minutes, lower is better)."""

    strategy: ResourceStrategy = "EARLIEST_FINISH"
    preference_penalty_minutes: float = 30.0
    setup_weight: float = 0.5
    cost_weight_minutes_per_currency: float = 0.0


class SolverSettings(_Model):
    provider: ProviderName = "hybrid"
    profile: SolverProfile = "QUICK"
    time_limit_s: float | None = Field(default=None, gt=0, le=36000)
    seed: int = 42
    workers: int = Field(default=8, ge=1, le=64)
    reproducible: bool = True
    dispatch_rules: list[DispatchRule] = Field(default_factory=lambda: ["HYBRID_APS"])
    direction: Direction = "FORWARD"
    mode_selection: ModeSelection = Field(default_factory=ModeSelection)
    cpsat_max_ops: int = Field(default=250, ge=1, description="Largest problem solved as a single CP-SAT model")
    circuit_max_ops: int = Field(default=80, ge=2, description="Max ops per resource modelled with AddCircuit")
    lns_neighbourhood_ops: int = Field(default=90, ge=5)
    local_search: bool = True
    explain: bool = True
    multi_start: bool = True


class BaselineOpSpec(_Model):
    op_id: str
    resource_id: str
    start: datetime
    end: datetime
    setup_start: datetime | None = None


class Problem(_Model):
    schema_version: str = SCHEMA_VERSION
    scenario_id: str | None = None
    label: str | None = None
    as_of: datetime | None = Field(default=None, description="'Now'; defaults to horizon.start")
    horizon: Horizon
    calendars: list[CalendarSpec] = Field(default_factory=list)
    resources: list[ResourceSpec] = Field(default_factory=list)
    setup_matrices: list[SetupMatrixSpec] = Field(default_factory=list)
    setup_rules: list[SetupRuleSpec] = Field(default_factory=list)
    materials: list[MaterialSpec] = Field(default_factory=list)
    orders: list[OrderSpec] = Field(default_factory=list)
    operations: list[OperationSpec] = Field(default_factory=list)
    precedences: list[PrecedenceSpec] = Field(default_factory=list)
    sequence_constraints: list[SequenceConstraintSpec] = Field(default_factory=list)
    rules: list[RuleSpec] = Field(default_factory=list)
    constraints: ConstraintSettings = Field(default_factory=ConstraintSettings)
    objectives: ObjectiveSpec = Field(default_factory=ObjectiveSpec)
    solver: SolverSettings = Field(default_factory=SolverSettings)
    baseline: list[BaselineOpSpec] = Field(default_factory=list)


# --------------------------------------------------------------------------------------------
# Solution
# --------------------------------------------------------------------------------------------


class Binding(_Model):
    type: BindingType
    ref: str | None = None
    detail: str | None = None
    at: datetime | None = None
    wait_minutes: int = 0


class SecondaryAllocation(_Model):
    resource_id: str
    units: int


class ScheduledOperation(_Model):
    op_id: str
    order_id: str
    resource_id: str
    mode_index: int
    secondary: list[SecondaryAllocation] = Field(default_factory=list)
    setup_start: datetime
    start: datetime
    end: datetime
    setup_minutes: int
    run_minutes: int
    teardown_minutes: int = 0
    working_minutes: int
    overtime_minutes: int = 0
    quantity: float
    fixed: bool = False
    fixed_reason: str | None = None
    late: bool = False
    zone: Literal["FROZEN", "FLEXIBLE", "PLANNING"] = "PLANNING"
    prev_op_id: str | None = None
    material_ready: datetime | None = None
    binding: Binding
    subcontracted: bool = False
    cost: float = 0.0


class UnscheduledOperation(_Model):
    op_id: str
    order_id: str
    reason: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class Violation(_Model):
    severity: Severity
    hardness: Hardness
    type: str
    message: str
    order_id: str | None = None
    op_id: str | None = None
    resource_id: str | None = None
    material_id: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class OrderResult(_Model):
    order_id: str
    number: str
    status: Literal["ON_TIME", "LATE", "UNSCHEDULED", "PARTIAL", "COMPLETED"]
    start: datetime | None = None
    end: datetime | None = None
    due: datetime
    lateness_minutes: int = 0
    weight: float = 1.0
    earliest_possible_end: datetime | None = None
    limiting: Binding | None = None
    material_status: Literal["OK", "RISK", "SHORTAGE", "LATE_SUPPLY", "NONE"] = "NONE"
    rules_applied: list[str] = Field(default_factory=list)


class Reason(_Model):
    code: str
    text: str
    data: dict[str, Any] = Field(default_factory=dict)


class Alternative(_Model):
    mode_index: int
    resource_id: str
    feasible: bool
    chosen: bool = False
    start: datetime | None = None
    end: datetime | None = None
    delta_finish_minutes: int | None = None
    setup_minutes: int | None = None
    blocking: list[Reason] = Field(default_factory=list)


class Explanation(_Model):
    op_id: str
    order_id: str
    resource_id: str | None
    start: datetime | None = None
    end: datetime | None = None
    reasons: list[Reason] = Field(default_factory=list)
    alternatives: list[Alternative] = Field(default_factory=list)
    binding: Binding | None = None
    rules_applied: list[str] = Field(default_factory=list)


class BottleneckCause(_Model):
    kind: str
    ref: str | None = None
    label: str
    minutes: int
    share: float


class Bottleneck(_Model):
    resource_id: str | None
    kind: Literal[
        "OVERLOADED", "HIGH_UTILIZATION", "MATERIAL", "LABOR", "TOOL", "SEQUENCE", "CALENDAR"
    ]
    rank: int
    capacity_minutes: int = 0
    scheduled_minutes: int = 0
    requirement_minutes: int = 0
    overload_minutes: int = 0
    utilization: float = 0.0
    induced_wait_minutes: int = 0
    orders_affected: int = 0
    causes: list[BottleneckCause] = Field(default_factory=list)
    ref: str | None = None


class PegLink(_Model):
    material_id: str
    supply_id: str
    supply_kind: str
    supply_ref: str | None = None
    supply_order_id: str | None = None
    supply_time: datetime | None = None
    consumer_op_id: str
    consumer_order_id: str
    need_time: datetime | None = None
    quantity: float


class PhaseLog(_Model):
    name: str
    status: Literal["DONE", "SKIPPED", "FAILED"] = "DONE"
    runtime_s: float = 0.0
    detail: str | None = None


class SolverMetadata(_Model):
    provider: str
    status: Literal["OPTIMAL", "FEASIBLE", "HEURISTIC", "INFEASIBLE", "NO_SOLUTION", "VALIDATION_ONLY"]
    objective: float | None = None
    objective_breakdown: dict[str, float] = Field(default_factory=dict)
    objective_vector: list[float] = Field(default_factory=list)
    best_bound: float | None = None
    gap: float | None = None
    proven_optimal: bool = False
    runtime_s: float = 0.0
    time_limit_s: float | None = None
    iterations: int = 0
    seed: int | None = None
    input_hash: str | None = None
    engine_version: str | None = None
    phases: list[PhaseLog] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class Solution(_Model):
    schema_version: str = SCHEMA_VERSION
    scenario_id: str | None = None
    schedule: list[ScheduledOperation] = Field(default_factory=list)
    unscheduled: list[UnscheduledOperation] = Field(default_factory=list)
    violations: list[Violation] = Field(default_factory=list)
    feasible: bool = True
    orders: list[OrderResult] = Field(default_factory=list)
    kpis: dict[str, float | None] = Field(default_factory=dict)
    kpi_details: dict[str, Any] = Field(default_factory=dict)
    bottlenecks: list[Bottleneck] = Field(default_factory=list)
    explanations: dict[str, Explanation] = Field(default_factory=dict)
    pegging: list[PegLink] = Field(default_factory=list)
    solver_metadata: SolverMetadata
