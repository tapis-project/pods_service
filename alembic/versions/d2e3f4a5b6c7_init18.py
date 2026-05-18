"""init18 — pod_log_runs table

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-05-17 00:01:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'd2e3f4a5b6c7'
down_revision = 'c1d2e3f4a5b6'
branch_labels = None
depends_on = None


def upgrade(engine_name):
    globals()["upgrade_alltenants"]()


def downgrade(engine_name):
    globals()["downgrade_alltenants"]()


def upgrade_alltenants():
    op.create_table(
        'pod_log_runs',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True, nullable=False),
        sa.Column('pod_id', sa.String(), nullable=False),
        sa.Column('tenant_id', sa.String(), nullable=False),
        sa.Column('site_id', sa.String(), nullable=False),
        sa.Column('run_index', sa.Integer(), nullable=False),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('stopped_at', sa.DateTime(), nullable=True),
        sa.Column('logs', sa.Text(), nullable=True),
        sa.Column('log_size_bytes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('is_archived', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('archive_path', sa.String(), nullable=True),
        sa.UniqueConstraint('pod_id', 'tenant_id', 'run_index', name='uq_pod_log_run'),
    )
    op.create_index('ix_pod_log_runs_id', 'pod_log_runs', ['id'], unique=True)
    op.create_index('ix_pod_log_runs_pod_id', 'pod_log_runs', ['pod_id'])
    op.create_index('ix_pod_log_runs_tenant_id', 'pod_log_runs', ['tenant_id'])
    op.create_index('ix_pod_log_runs_pod_tenant_idx', 'pod_log_runs', ['pod_id', 'tenant_id', 'run_index'])
    op.create_index('ix_pod_log_runs_pod_tenant_active', 'pod_log_runs', ['pod_id', 'tenant_id', 'is_active'])


def downgrade_alltenants():
    op.drop_index('ix_pod_log_runs_pod_tenant_active', table_name='pod_log_runs')
    op.drop_index('ix_pod_log_runs_pod_tenant_idx', table_name='pod_log_runs')
    op.drop_index('ix_pod_log_runs_tenant_id', table_name='pod_log_runs')
    op.drop_index('ix_pod_log_runs_pod_id', table_name='pod_log_runs')
    op.drop_index('ix_pod_log_runs_id', table_name='pod_log_runs')
    op.drop_table('pod_log_runs')
