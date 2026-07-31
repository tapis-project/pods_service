"""decommission flow — decommission_ts column on node: non-null = a decommission
was requested and the row is waiting for the agent to confirm self-removal
(hard-delete on ack, or after NODES_DECOMMISSION_TIMEOUT_MINUTES via the
health-central sweep). Nullable, no backfill needed — the field is Optional in
the read model, so pre-existing NULL rows respond fine.

Revision ID: g5d2e9f3b1c4
Revises: f4c1d8e2a9b3
Create Date: 2026-07-31

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel              ##### Required when using sqlmodel and not use sqlalchemy
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'g5d2e9f3b1c4'
down_revision = 'f4c1d8e2a9b3'
branch_labels = None
depends_on = None


def upgrade(engine_name):
    globals()["upgrade_alltenants"]()


def downgrade(engine_name):
    globals()["downgrade_alltenants"]()


def upgrade_alltenants():
    op.add_column('node', sa.Column('decommission_ts', sa.DateTime(), nullable=True))


def downgrade_alltenants():
    op.drop_column('node', 'decommission_ts')
