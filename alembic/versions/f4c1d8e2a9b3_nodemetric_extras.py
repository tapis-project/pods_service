"""storage watch — extras JSON column on nodemetric: open-ended numeric gauges
(disk:<path>:used/:total per watched path, agent self-metrics later) so new
series never need new columns. Nullable, no backfill needed — absent extras
simply contribute no series, and NodeMetric rows never ship raw in responses.

Revision ID: f4c1d8e2a9b3
Revises: e2a7b5c9d4f1
Create Date: 2026-07-31

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel              ##### Required when using sqlmodel and not use sqlalchemy
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'f4c1d8e2a9b3'
down_revision = 'e2a7b5c9d4f1'
branch_labels = None
depends_on = None


def upgrade(engine_name):
    globals()["upgrade_alltenants"]()


def downgrade(engine_name):
    globals()["downgrade_alltenants"]()


def upgrade_alltenants():
    op.add_column('nodemetric', sa.Column('extras', sa.JSON(), nullable=True))


def downgrade_alltenants():
    op.drop_column('nodemetric', 'extras')
