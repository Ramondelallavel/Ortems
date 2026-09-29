"""Revision of a tenant's planning inputs.

A planning run takes minutes on a large plant. When it finishes, its result may only become the
scenario's current plan if the inputs it was computed from are still current: the revision counter
below is incremented in the same transaction as every ORM change to a planning input (master data,
orders, inventory, calendars, resources, scenario changes, rules…). A run records the revision when it
builds its problem and compares it when it promotes its result (``services.planning``).

Changes that are not inputs — plan versions and their rows, runs, audit, alerts, events (their effects
on inputs are counted), users and integrations — do not count. Pointer moves on a scenario or plant
(head plan, published plan, redo stack, edit lock) do not count either: they are checked separately.

The counter row is updated once per transaction and tenant; concurrent writers of one tenant serialise
on it until commit, which is the price of an exact answer to "did anything change?".
"""

from __future__ import annotations

import uuid
from itertools import chain

from sqlalchemy import Integer, Uuid, event, insert, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from ..core.db import Base, TenantMixin


class DataRevision(Base):
    __tablename__ = "data_revision"
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


NOT_INPUT_TABLES = frozenset(
    {
        "plan",
        "scheduled_operation",
        "plan_order",
        "plan_peg",
        "plan_unscheduled",
        "plan_document",
        "constraint_violation",
        "kpi_value",
        "planning_run",
        "problem_snapshot",
        "audit_log",
        "event",
        "alert",
        "webhook_subscription",
        "webhook_delivery",
        "import_job",
        "export_job",
        "integration",
        "api_key",
        "app_user",
        "role",
        "user_role",
        "permission",
        "saved_view",
        "tenant",
        "data_revision",
    }
)
# attributes whose change is a pointer move, not an input change
POINTER_ATTRS = {
    "scenario": frozenset({"head_plan_id", "base_plan_id", "redo_stack", "locked_by", "lock_expires_at", "version", "updated_at", "updated_by"}),
    "plant": frozenset({"published_plan_id", "live_scenario_id", "version", "updated_at", "updated_by"}),
    "resource": frozenset({"version", "updated_at", "updated_by"}),
}


def _changes_input(session: Session, obj) -> bool:
    table = getattr(obj, "__tablename__", None)
    if table is None or table in NOT_INPUT_TABLES or not isinstance(obj, TenantMixin):
        return False
    if obj in session.new or obj in session.deleted:
        return True
    from sqlalchemy import inspect

    ignored = POINTER_ATTRS.get(table, frozenset({"version", "updated_at", "updated_by"}))
    st = inspect(obj)
    return any(a.key not in ignored and a.history.has_changes() for a in st.attrs)


def bump(session: Session, tenant_id: uuid.UUID) -> None:
    """Increment a tenant's input revision inside the current transaction."""
    conn = session.connection()
    res = conn.execute(update(DataRevision).where(DataRevision.tenant_id == tenant_id).values(revision=DataRevision.revision + 1))
    if (res.rowcount or 0) == 0:
        dialect = conn.dialect.name
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = pg_insert(DataRevision).values(tenant_id=tenant_id, revision=1)
            conn.execute(stmt.on_conflict_do_update(index_elements=["tenant_id"], set_={"revision": DataRevision.revision + 1}))
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as sq_insert

            stmt = sq_insert(DataRevision).values(tenant_id=tenant_id, revision=1)
            conn.execute(stmt.on_conflict_do_update(index_elements=["tenant_id"], set_={"revision": DataRevision.revision + 1}))
        else:  # pragma: no cover
            conn.execute(insert(DataRevision).values(tenant_id=tenant_id, revision=1))


def current(session: Session, tenant_id: uuid.UUID) -> int:
    from sqlalchemy import select

    return session.connection().execute(select(DataRevision.revision).where(DataRevision.tenant_id == tenant_id)).scalar() or 0


@event.listens_for(Session, "before_flush")
def _count_input_changes(session: Session, _ctx, _instances) -> None:
    # once per flush (not once per transaction): a flush inside a savepoint that is rolled back later
    # (an import's dry run) must not hide the changes flushed after it
    fallback = session.info.get("tenant_id")
    tenants = set()
    for obj in chain(session.new, session.dirty, session.deleted):
        if _changes_input(session, obj):
            tenants.add(getattr(obj, "tenant_id", None) or fallback)
    for tid in tenants - {None}:
        bump(session, tid)
