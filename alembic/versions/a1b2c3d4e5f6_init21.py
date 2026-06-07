"""init21 — add healthchecks and networking_live to pod

Revision ID: a1b2c3d4e5f6
Revises: f4a5b6c7d8e9
Create Date: 2026-06-07 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision     = 'a1b2c3d4e5f6'
down_revision = 'f4a5b6c7d8e9'
branch_labels = None
depends_on    = None


def upgrade(engine_name):
    globals()["upgrade_alltenants"]()


def downgrade(engine_name):
    globals()["downgrade_alltenants"]()


def upgrade_alltenants():
    op.add_column('pod', sa.Column('healthchecks', JSONB(), nullable=True))
    op.add_column('pod', sa.Column('networking_live', sa.Boolean(), nullable=False, server_default='false'))


def downgrade_alltenants():
    op.drop_column('pod', 'networking_live')
    op.drop_column('pod', 'healthchecks')
