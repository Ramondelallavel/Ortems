"""user token revocation point

* ``app_user.tokens_valid_after``: sessions and bearer tokens issued before this instant are refused
  (set on password change, deactivation and "sign out everywhere").

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-04 21:30:00
"""

import sqlalchemy as sa
from alembic import op

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('app_user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tokens_valid_after', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('app_user', schema=None) as batch_op:
        batch_op.drop_column('tokens_valid_after')
