"""plan results tables and read models

Per-order results, pegging and unscheduled operations of a plan version move from the
``plan.analysis`` document to indexed tables, operation explanations move to compressed
per-resource documents (``plan_document``), and ``scheduled_operation`` loses the columns and
constraints that made 200 000-row plan versions slow to write. Existing plans are migrated.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-28 21:54:11.843493
"""

import gzip
import json
import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

JSONB = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
LATE_DETAIL_CAP = 500

plan_order = sa.table(
    "plan_order",
    *(sa.column(n, t) for n, t in (
        ("id", sa.Uuid()), ("tenant_id", sa.Uuid()), ("plan_id", sa.Uuid()), ("order_key", sa.String()), ("order_id", sa.Uuid()), ("number", sa.String()), ("status", sa.String()),
        ("start", sa.DateTime(timezone=True)), ("end", sa.DateTime(timezone=True)), ("due", sa.DateTime(timezone=True)), ("lateness_minutes", sa.Integer()),
        ("weight", sa.Float()), ("earliest_possible_end", sa.DateTime(timezone=True)), ("limiting", sa.JSON()), ("material_status", sa.String()),
        ("rules_applied", sa.JSON()), ("cause", sa.JSON()),
    )),
)
plan_peg = sa.table(
    "plan_peg",
    *(sa.column(n, t) for n, t in (
        ("id", sa.Uuid()), ("tenant_id", sa.Uuid()), ("plan_id", sa.Uuid()), ("material_id", sa.String()), ("supply_id", sa.String()), ("supply_kind", sa.String()),
        ("supply_ref", sa.String()), ("supply_order_id", sa.String()), ("supply_time", sa.DateTime(timezone=True)), ("consumer_op_id", sa.String()),
        ("consumer_order_id", sa.String()), ("need_time", sa.DateTime(timezone=True)), ("quantity", sa.Float()),
    )),
)
plan_unscheduled = sa.table(
    "plan_unscheduled",
    *(sa.column(n, t) for n, t in (
        ("id", sa.Uuid()), ("tenant_id", sa.Uuid()), ("plan_id", sa.Uuid()), ("op_key", sa.String()), ("order_key", sa.String()), ("reason", sa.String()),
        ("message", sa.Text()), ("details", sa.JSON()),
    )),
)
plan_document = sa.table(
    "plan_document",
    *(sa.column(n, t) for n, t in (
        ("id", sa.Uuid()), ("tenant_id", sa.Uuid()), ("plan_id", sa.Uuid()), ("kind", sa.String()), ("key", sa.String()), ("item_count", sa.Integer()), ("data", sa.LargeBinary()),
    )),
)
plan_table = sa.table("plan", sa.column("id", sa.Uuid()), sa.column("analysis", sa.JSON()), sa.column("kpi_details", sa.JSON()))


def _doc(v):
    if v is None:
        return {}
    return json.loads(v) if isinstance(v, str | bytes) else v


def _uuid(v):
    try:
        return uuid.UUID(str(v))
    except ValueError:
        return None


def _dt(v):
    if v is None:
        return None
    from datetime import UTC, datetime

    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _move_plan_results(bind) -> None:
    """analysis.orders / pegging / unscheduled → tables; explanations → per-resource documents."""
    plans = bind.execute(sa.text("SELECT id, tenant_id FROM plan")).all()
    for pid, tid in plans:
        pid = pid if isinstance(pid, uuid.UUID) else uuid.UUID(str(pid))
        tid = tid if isinstance(tid, uuid.UUID) else uuid.UUID(str(tid))
        row = bind.execute(sa.text("SELECT analysis, kpi_details FROM plan WHERE id = :p"), {"p": pid}).one()
        analysis, details = _doc(row[0]), _doc(row[1])
        if analysis.get("storage", 1) >= 2:
            continue
        causes = {x.get("order_id"): x.get("cause") for x in details.get("late_orders", [])}
        orders = analysis.pop("orders", []) or []
        pegs = analysis.pop("pegging", []) or []
        uns = analysis.pop("unscheduled", []) or []
        if orders:
            bind.execute(plan_order.insert(), [
                {
                    "id": uuid.uuid4(), "tenant_id": tid, "plan_id": pid, "order_key": o["order_id"], "order_id": _uuid(o["order_id"]), "number": o.get("number") or o["order_id"], "status": o.get("status") or "ON_TIME",
                    "start": _dt(o.get("start")), "end": _dt(o.get("end")), "due": _dt(o.get("due")), "lateness_minutes": int(o.get("lateness_minutes") or 0), "weight": float(o.get("weight") or 1.0),
                    "earliest_possible_end": _dt(o.get("earliest_possible_end")), "limiting": o.get("limiting"), "material_status": o.get("material_status") or "NONE",
                    "rules_applied": o.get("rules_applied") or [], "cause": causes.get(o["order_id"]),
                }
                for o in orders
            ])
        if pegs:
            bind.execute(plan_peg.insert(), [
                {
                    "id": uuid.uuid4(), "tenant_id": tid, "plan_id": pid, "material_id": p["material_id"], "supply_id": p["supply_id"], "supply_kind": p.get("supply_kind") or "SUPPLY",
                    "supply_ref": p.get("supply_ref"), "supply_order_id": p.get("supply_order_id"), "supply_time": _dt(p.get("supply_time")), "consumer_op_id": p["consumer_op_id"],
                    "consumer_order_id": p["consumer_order_id"], "need_time": _dt(p.get("need_time")), "quantity": float(p.get("quantity") or 0),
                }
                for p in pegs
            ])
        if uns:
            bind.execute(plan_unscheduled.insert(), [
                {"id": uuid.uuid4(), "tenant_id": tid, "plan_id": pid, "op_key": u["op_id"], "order_key": u["order_id"], "reason": u.get("reason") or "UNSCHEDULED", "message": u.get("message") or "", "details": u.get("details") or {}}
                for u in uns
            ])
        # explanations of the scheduled operations, one compressed document per resource (a resource
        # at a time: a large plan holds hundreds of megabytes of them)
        keys = [k for (k,) in bind.execute(sa.text("SELECT DISTINCT resource_key FROM scheduled_operation WHERE plan_id = :p AND explanation IS NOT NULL"), {"p": pid})]
        for rk in keys:
            ops = {op_key: _doc(ex) for op_key, ex in bind.execute(sa.text("SELECT op_key, explanation FROM scheduled_operation WHERE plan_id = :p AND resource_key = :r AND explanation IS NOT NULL"), {"p": pid, "r": rk})}
            bind.execute(plan_document.insert(), [{"id": uuid.uuid4(), "tenant_id": tid, "plan_id": pid, "kind": "EXPLANATIONS", "key": rk, "item_count": len(ops), "data": gzip.compress(json.dumps(ops, separators=(",", ":")).encode(), 3)}])
        n_ops = bind.execute(sa.text("SELECT count(*) FROM scheduled_operation WHERE plan_id = :p"), {"p": pid}).scalar() or 0
        analysis["storage"] = 2
        analysis["counts"] = {"operations": int(n_ops), "orders": len(orders), "unscheduled": len(uns), "pegging": len(pegs)}
        late = details.get("late_orders") or []
        details["late_orders_total"] = len(late)
        details["late_orders"] = late[:LATE_DETAIL_CAP]
        bind.execute(plan_table.update().where(plan_table.c.id == pid).values(analysis=analysis, kpi_details=details))


def upgrade() -> None:
    op.create_table('plan_document',
    sa.Column('plan_id', sa.Uuid(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('key', sa.String(length=120), nullable=False),
    sa.Column('item_count', sa.Integer(), nullable=False),
    sa.Column('data', sa.LargeBinary(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['plan_id'], ['plan.id'], name=op.f('fk_plan_document_plan_id_plan'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_plan_document')),
    sa.UniqueConstraint('plan_id', 'kind', 'key', name=op.f('uq_plan_document_plan_id_kind_key'))
    )
    op.create_table('plan_order',
    sa.Column('plan_id', sa.Uuid(), nullable=False),
    sa.Column('order_key', sa.String(length=120), nullable=False),
    sa.Column('order_id', sa.Uuid(), nullable=True),
    sa.Column('number', sa.String(length=120), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('start', sa.DateTime(timezone=True), nullable=True),
    sa.Column('end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('due', sa.DateTime(timezone=True), nullable=False),
    sa.Column('lateness_minutes', sa.Integer(), nullable=False),
    sa.Column('weight', sa.Float(), nullable=False),
    sa.Column('earliest_possible_end', sa.DateTime(timezone=True), nullable=True),
    sa.Column('limiting', JSONB, nullable=True),
    sa.Column('material_status', sa.String(length=20), nullable=False),
    sa.Column('rules_applied', JSONB, nullable=False),
    sa.Column('cause', JSONB, nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['plan_id'], ['plan.id'], name=op.f('fk_plan_order_plan_id_plan'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_plan_order')),
    sa.UniqueConstraint('plan_id', 'order_key', name=op.f('uq_plan_order_plan_id_order_key'))
    )
    op.create_index('ix_plan_order_status', 'plan_order', ['plan_id', 'status', 'lateness_minutes'], unique=False)
    op.create_index('ix_plan_order_order', 'plan_order', ['plan_id', 'order_id'], unique=False)
    op.create_table('plan_peg',
    sa.Column('plan_id', sa.Uuid(), nullable=False),
    sa.Column('material_id', sa.String(length=120), nullable=False),
    sa.Column('supply_id', sa.String(length=200), nullable=False),
    sa.Column('supply_kind', sa.String(length=30), nullable=False),
    sa.Column('supply_ref', sa.String(length=200), nullable=True),
    sa.Column('supply_order_id', sa.String(length=120), nullable=True),
    sa.Column('supply_time', sa.DateTime(timezone=True), nullable=True),
    sa.Column('consumer_op_id', sa.String(length=120), nullable=False),
    sa.Column('consumer_order_id', sa.String(length=120), nullable=False),
    sa.Column('need_time', sa.DateTime(timezone=True), nullable=True),
    sa.Column('quantity', sa.Float(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['plan_id'], ['plan.id'], name=op.f('fk_plan_peg_plan_id_plan'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_plan_peg'))
    )
    op.create_index('ix_plan_peg_consumer', 'plan_peg', ['plan_id', 'consumer_order_id'], unique=False)
    op.create_index('ix_plan_peg_material', 'plan_peg', ['plan_id', 'material_id'], unique=False)
    op.create_index('ix_plan_peg_supply_order', 'plan_peg', ['plan_id', 'supply_order_id'], unique=False)
    op.create_table('plan_unscheduled',
    sa.Column('plan_id', sa.Uuid(), nullable=False),
    sa.Column('op_key', sa.String(length=120), nullable=False),
    sa.Column('order_key', sa.String(length=120), nullable=False),
    sa.Column('reason', sa.String(length=60), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('details', JSONB, nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('tenant_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['plan_id'], ['plan.id'], name=op.f('fk_plan_unscheduled_plan_id_plan'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_plan_unscheduled'))
    )
    op.create_index('ix_plan_unsched_op', 'plan_unscheduled', ['plan_id', 'op_key'], unique=False)
    op.create_index('ix_plan_unsched_order', 'plan_unscheduled', ['plan_id', 'order_key'], unique=False)

    _move_plan_results(op.get_bind())

    op.drop_index(op.f('ix_constraint_violation_tenant_id'), table_name='constraint_violation')
    op.drop_index(op.f('ix_sched_op_order'), table_name='scheduled_operation')
    op.drop_index(op.f('ix_sched_op_window'), table_name='scheduled_operation')
    op.drop_index(op.f('ix_scheduled_operation_tenant_id'), table_name='scheduled_operation')
    op.create_index('ix_sched_op_lane', 'scheduled_operation', ['plan_id', 'resource_key', 'setup_start'], unique=False)
    op.create_index('ix_sched_op_order_key', 'scheduled_operation', ['plan_id', 'order_key'], unique=False)
    # SQLite cannot drop constraints or (portably) columns in place; there the unused column and the
    # unenforced-by-design references stay (the application no longer reads or writes them)
    if op.get_bind().dialect.name == "sqlite":
        return
    op.drop_constraint(op.f('fk_scheduled_operation_order_id_production_order'), 'scheduled_operation', type_='foreignkey')
    op.drop_constraint(op.f('fk_scheduled_operation_order_operation_id_production_or_cbcf'), 'scheduled_operation', type_='foreignkey')
    op.drop_constraint(op.f('fk_scheduled_operation_resource_id_resource'), 'scheduled_operation', type_='foreignkey')
    op.drop_column('scheduled_operation', 'explanation')


def downgrade() -> None:
    # results stay in the new tables' data only until they are dropped: plans written by version
    # 0003 keep their schedule rows but lose per-order results, pegging and explanations
    if op.get_bind().dialect.name != "sqlite":
        op.add_column('scheduled_operation', sa.Column('explanation', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=True))
        op.create_foreign_key(op.f('fk_scheduled_operation_resource_id_resource'), 'scheduled_operation', 'resource', ['resource_id'], ['id'], ondelete='SET NULL')
        op.create_foreign_key(op.f('fk_scheduled_operation_order_operation_id_production_or_cbcf'), 'scheduled_operation', 'production_order_operation', ['order_operation_id'], ['id'], ondelete='SET NULL')
        op.create_foreign_key(op.f('fk_scheduled_operation_order_id_production_order'), 'scheduled_operation', 'production_order', ['order_id'], ['id'], ondelete='SET NULL')
    op.drop_index('ix_sched_op_order_key', table_name='scheduled_operation')
    op.drop_index('ix_sched_op_lane', table_name='scheduled_operation')
    op.create_index(op.f('ix_scheduled_operation_tenant_id'), 'scheduled_operation', ['tenant_id'], unique=False)
    op.create_index(op.f('ix_sched_op_window'), 'scheduled_operation', ['plan_id', 'resource_id', 'start'], unique=False)
    op.create_index(op.f('ix_sched_op_order'), 'scheduled_operation', ['plan_id', 'order_id'], unique=False)
    op.create_index(op.f('ix_constraint_violation_tenant_id'), 'constraint_violation', ['tenant_id'], unique=False)
    op.drop_index('ix_plan_unsched_order', table_name='plan_unscheduled')
    op.drop_index('ix_plan_unsched_op', table_name='plan_unscheduled')
    op.drop_table('plan_unscheduled')
    op.drop_index('ix_plan_peg_supply_order', table_name='plan_peg')
    op.drop_index('ix_plan_peg_material', table_name='plan_peg')
    op.drop_index('ix_plan_peg_consumer', table_name='plan_peg')
    op.drop_table('plan_peg')
    op.drop_index('ix_plan_order_order', table_name='plan_order')
    op.drop_index('ix_plan_order_status', table_name='plan_order')
    op.drop_table('plan_order')
    op.drop_table('plan_document')
