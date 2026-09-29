"""concurrency, stale results, publication overrides, webhook outbox

* ``data_revision``: per-tenant revision of the planning inputs (stale run detection).
* ``planning_run``: ``input_revision``, ``baseline_plan_id`` and a partial unique index allowing at most
  one QUEUED/RUNNING run per scenario (race-free queueing).
* ``plan.publish_overrides``: publication checks overridden with ``force``.
* ``webhook_delivery``: lease and retry columns of the outbox (atomic claim).

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-29 10:00:00
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

JSONB = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
ACTIVE = sa.text("status IN ('QUEUED', 'RUNNING')")


def upgrade() -> None:
    op.create_table(
        'data_revision',
        sa.Column('tenant_id', sa.Uuid(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('tenant_id', name=op.f('pk_data_revision')),
    )
    with op.batch_alter_table('planning_run', schema=None) as batch_op:
        batch_op.add_column(sa.Column('input_revision', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('baseline_plan_id', sa.Uuid(), nullable=True))
    # older duplicates (created by the race this index prevents) would make the index fail: keep the
    # oldest active run of each scenario, cancel the others
    bind = op.get_bind()
    dups = bind.execute(sa.text(
        "SELECT id, scenario_id FROM planning_run WHERE status IN ('QUEUED', 'RUNNING') ORDER BY scenario_id, created_at"
    )).all()
    seen = set()
    for rid, sid in dups:
        if sid in seen:
            bind.execute(sa.text("UPDATE planning_run SET status = 'CANCELLED', error_code = 'DUPLICATE_RUN' WHERE id = :i"), {"i": rid})
        seen.add(sid)
    op.create_index('uq_planning_run_active_scenario', 'planning_run', ['scenario_id'], unique=True, postgresql_where=ACTIVE, sqlite_where=ACTIVE)
    with op.batch_alter_table('plan', schema=None) as batch_op:
        batch_op.add_column(sa.Column('publish_overrides', JSONB, nullable=True))
    with op.batch_alter_table('webhook_delivery', schema=None) as batch_op:
        batch_op.add_column(sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('locked_by', sa.String(length=120), nullable=True))
        batch_op.create_index('ix_webhook_delivery_due', ['status', 'next_attempt_at'], unique=False)
    op.execute("UPDATE webhook_delivery SET next_attempt_at = created_at WHERE status = 'PENDING'")


def downgrade() -> None:
    with op.batch_alter_table('webhook_delivery', schema=None) as batch_op:
        batch_op.drop_index('ix_webhook_delivery_due')
        batch_op.drop_column('locked_by')
        batch_op.drop_column('locked_until')
        batch_op.drop_column('last_attempt_at')
        batch_op.drop_column('next_attempt_at')
    with op.batch_alter_table('plan', schema=None) as batch_op:
        batch_op.drop_column('publish_overrides')
    op.drop_index('uq_planning_run_active_scenario', table_name='planning_run')
    with op.batch_alter_table('planning_run', schema=None) as batch_op:
        batch_op.drop_column('baseline_plan_id')
        batch_op.drop_column('input_revision')
    op.drop_table('data_revision')
