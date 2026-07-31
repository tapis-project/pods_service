"""no-downtime token rotation — pending_agent_token_hash + pending_token_ts on
node: during a rotate BOTH the current and the pending token authenticate; the
agent confirms with the NEW one (proving it persisted), which promotes pending
to active and revokes the old. A rotate that never lands simply expires and the
running agent keeps working — no park, no re-join.

Revision ID: h6e3f0a4c2d5
Revises: g5d2e9f3b1c4
Create Date: 2026-07-31

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel              ##### Required when using sqlmodel and not use sqlalchemy
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'h6e3f0a4c2d5'
down_revision = 'g5d2e9f3b1c4'
branch_labels = None
depends_on = None


def upgrade(engine_name):
    globals()["upgrade_alltenants"]()


def downgrade(engine_name):
    globals()["downgrade_alltenants"]()


def upgrade_alltenants():
    op.add_column('node', sa.Column('pending_agent_token_hash', sa.String(), nullable=True))
    op.add_column('node', sa.Column('pending_token_ts', sa.DateTime(), nullable=True))


def downgrade_alltenants():
    op.drop_column('node', 'pending_token_ts')
    op.drop_column('node', 'pending_agent_token_hash')
