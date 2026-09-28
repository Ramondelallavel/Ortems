"""circular foreign keys

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28 11:26:08.668060
"""

from alembic import op
import sqlalchemy as sa
import monxuplan.core.db


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite cannot add constraints to existing tables; there these references stay unenforced
    # (the application keeps them consistent) — PostgreSQL enforces them.
    if op.get_bind().dialect.name == "sqlite":
        return
    op.create_foreign_key(op.f('fk_planning_run_plan_id_plan'), 'planning_run', 'plan', ['plan_id'], ['id'], ondelete='SET NULL', use_alter=True)
    op.create_foreign_key(op.f('fk_plant_published_plan_id_plan'), 'plant', 'plan', ['published_plan_id'], ['id'], ondelete='SET NULL', use_alter=True)
    op.create_foreign_key(op.f('fk_plant_default_calendar_id_calendar'), 'plant', 'calendar', ['default_calendar_id'], ['id'], ondelete='SET NULL', use_alter=True)
    op.create_foreign_key(op.f('fk_plant_live_scenario_id_scenario'), 'plant', 'scenario', ['live_scenario_id'], ['id'], ondelete='SET NULL', use_alter=True)
    op.create_foreign_key(op.f('fk_scenario_head_plan_id_plan'), 'scenario', 'plan', ['head_plan_id'], ['id'], ondelete='SET NULL', use_alter=True)
    op.create_foreign_key(op.f('fk_scenario_base_plan_id_plan'), 'scenario', 'plan', ['base_plan_id'], ['id'], ondelete='SET NULL', use_alter=True)


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        return
    op.drop_constraint(op.f('fk_scenario_base_plan_id_plan'), 'scenario', type_='foreignkey')
    op.drop_constraint(op.f('fk_scenario_head_plan_id_plan'), 'scenario', type_='foreignkey')
    op.drop_constraint(op.f('fk_plant_live_scenario_id_scenario'), 'plant', type_='foreignkey')
    op.drop_constraint(op.f('fk_plant_default_calendar_id_calendar'), 'plant', type_='foreignkey')
    op.drop_constraint(op.f('fk_plant_published_plan_id_plan'), 'plant', type_='foreignkey')
    op.drop_constraint(op.f('fk_planning_run_plan_id_plan'), 'planning_run', type_='foreignkey')
