"""node agent settings channel — agent_settings JSON column on node: central-stored
sparse settings overlay, carried to agents in checkin responses; env vars win.

Revision ID: e2a7b5c9d4f1
Revises: d1f4a8c3e6b2
Create Date: 2026-07-31

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel              ##### Required when using sqlmodel and not use sqlalchemy
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'e2a7b5c9d4f1'
down_revision = 'd1f4a8c3e6b2'
branch_labels = None
depends_on = None


def upgrade(engine_name):
    globals()["upgrade_alltenants"]()


def downgrade(engine_name):
    globals()["downgrade_alltenants"]()


def upgrade_alltenants():
    op.add_column('node', sa.Column('agent_settings', sa.JSON(), nullable=True))
    # Backfill: pre-existing rows must present the sparse overlay as {}, not NULL
    op.execute("UPDATE node SET agent_settings = '{}'::json WHERE agent_settings IS NULL")


def downgrade_alltenants():
    op.drop_column('node', 'agent_settings')
