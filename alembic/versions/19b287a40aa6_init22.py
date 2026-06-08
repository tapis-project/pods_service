"""init22 — stacks: stack table + pod.stack_id/depends_on/ready_condition/force_stop

Revision ID: 19b287a40aa6
Revises: a014bbaf8ee8
Create Date: 2026-06-08 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel              ##### Required when using sqlmodel and not use sqlalchemy
from sqlalchemy.dialects import postgresql

revision     = '19b287a40aa6'
down_revision = 'a014bbaf8ee8'
branch_labels = None
depends_on    = None


def upgrade(engine_name):
    globals()["upgrade_alltenants"]()


def downgrade(engine_name):
    globals()["downgrade_alltenants"]()


def upgrade_alltenants():
    op.create_table('stack',
        sa.Column('stack_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('description', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('restart_policy', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('status', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('creation_ts', sa.DateTime(), nullable=True),
        sa.Column('update_ts', sa.DateTime(), nullable=True),
        sa.Column('tenant_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('site_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('created_by', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('permissions', postgresql.ARRAY(sa.String(), dimensions=1), nullable=True),
        sa.Column('action_logs', postgresql.ARRAY(sa.String()), nullable=True),
        sa.PrimaryKeyConstraint('stack_id')
    )
    op.add_column('pod', sa.Column('stack_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True, server_default=''))
    op.add_column('pod', sa.Column('depends_on', postgresql.ARRAY(sa.String()), nullable=True))
    op.add_column('pod', sa.Column('ready_condition', sqlmodel.sql.sqltypes.AutoString(), nullable=True, server_default='available'))
    op.add_column('pod', sa.Column('force_stop', sa.Boolean(), nullable=True, server_default='false'))


def downgrade_alltenants():
    op.drop_column('pod', 'force_stop')
    op.drop_column('pod', 'ready_condition')
    op.drop_column('pod', 'depends_on')
    op.drop_column('pod', 'stack_id')
    op.drop_table('stack')
