"""init17 — traffic_logs table

Revision ID: c1d2e3f4a5b6
Revises: 89eebaf0ea78
Create Date: 2026-05-17 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'c1d2e3f4a5b6'
down_revision = '89eebaf0ea78'
branch_labels = None
depends_on = None


def upgrade(engine_name):
    globals()["upgrade_alltenants"]()


def downgrade(engine_name):
    globals()["downgrade_alltenants"]()


def upgrade_alltenants():
    op.create_table(
        'traffic_logs',
        sa.Column('id', postgresql.UUID(as_uuid=False), primary_key=True, nullable=False),
        sa.Column('pod_id', sa.String(), nullable=False),
        sa.Column('tenant_id', sa.String(), nullable=False),
        sa.Column('site_id', sa.String(), nullable=False),
        sa.Column('ts', sa.DateTime(), nullable=False),
        sa.Column('method', sa.String(), nullable=False, server_default=''),
        sa.Column('path', sa.String(), nullable=False, server_default=''),
        sa.Column('status_code', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('duration_ms', sa.Float(), nullable=False, server_default='0'),
        sa.Column('source_ip', sa.String(), nullable=False, server_default=''),
        sa.Column('username', sa.String(), nullable=True),
        sa.Column('entry_point', sa.String(), nullable=False, server_default=''),
        sa.Column('router_name', sa.String(), nullable=False, server_default=''),
        sa.Column('raw_headers', postgresql.JSON(), nullable=True),
    )
    op.create_index('ix_traffic_logs_id', 'traffic_logs', ['id'], unique=True)
    op.create_index('ix_traffic_logs_pod_id', 'traffic_logs', ['pod_id'])
    op.create_index('ix_traffic_logs_tenant_id', 'traffic_logs', ['tenant_id'])
    op.create_index('ix_traffic_logs_ts', 'traffic_logs', ['ts'])
    op.create_index('ix_traffic_logs_status_code', 'traffic_logs', ['status_code'])
    op.create_index('ix_traffic_logs_pod_tenant_ts', 'traffic_logs', ['pod_id', 'tenant_id', 'ts'])


def downgrade_alltenants():
    op.drop_index('ix_traffic_logs_pod_tenant_ts', table_name='traffic_logs')
    op.drop_index('ix_traffic_logs_status_code', table_name='traffic_logs')
    op.drop_index('ix_traffic_logs_ts', table_name='traffic_logs')
    op.drop_index('ix_traffic_logs_tenant_id', table_name='traffic_logs')
    op.drop_index('ix_traffic_logs_pod_id', table_name='traffic_logs')
    op.drop_index('ix_traffic_logs_id', table_name='traffic_logs')
    op.drop_table('traffic_logs')
