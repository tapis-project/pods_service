"""init27 — create volumeusagelog table

The VolumeUsageLog model (time-series volume/snapshot disk-usage measurements,
written by health_central's du sweep, read by the /pods/volumes/usage and
/pods/snapshots/usage endpoints) shipped without a migration — fresh schemas
get the table from model-metadata create, but every EXISTING schema lacks it:
psycopg2.errors.UndefinedTable on read, and the sweep's log writes fail.

Idempotent: skips creation when the table already exists (fresh schemas).
env.py sets the per-tenant search_path and calls upgrade_alltenants() once per
schema. Index names match SQLModel's defaults (ix_<table>_<col>) so migrated
and metadata-created schemas are identical.

Revision ID: c4d8f2a91e63
Revises: f6b2d8c4a1e7
Create Date: 2026-07-08 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel              ##### Required when using sqlmodel and not use sqlalchemy
from sqlalchemy import inspect

revision = 'c4d8f2a91e63'
down_revision = 'f6b2d8c4a1e7'
branch_labels = None
depends_on = None

_TABLE = 'volumeusagelog'


def upgrade(engine_name):
    globals()["upgrade_alltenants"]()


def downgrade(engine_name):
    globals()["downgrade_alltenants"]()


def upgrade_alltenants():
    bind = op.get_bind()
    if inspect(bind).has_table(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('object_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('object_type', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('tenant_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('site_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('size_mb', sa.Float(), nullable=False),
        sa.Column('size_limit_mb', sa.Float(), nullable=True),
        sa.Column('over_limit', sa.Boolean(), nullable=False),
        sa.Column('measured_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_volumeusagelog_object_id', _TABLE, ['object_id'])
    op.create_index('ix_volumeusagelog_tenant_id', _TABLE, ['tenant_id'])
    op.create_index('ix_volumeusagelog_measured_at', _TABLE, ['measured_at'])


def downgrade_alltenants():
    bind = op.get_bind()
    if inspect(bind).has_table(_TABLE):
        op.drop_table(_TABLE)
