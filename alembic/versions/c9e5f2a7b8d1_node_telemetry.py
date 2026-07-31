"""node telemetry (Phase 3) — nodelog + nodemetric tables: agent-shipped log lines
and metrics history samples, both per-node capped (rows/age) at ingest.

Revision ID: c9e5f2a7b8d1
Revises: a7c3d9e2f1b4
Create Date: 2026-07-30

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel              ##### Required when using sqlmodel and not use sqlalchemy
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'c9e5f2a7b8d1'
down_revision = 'a7c3d9e2f1b4'
branch_labels = None
depends_on = None


def upgrade(engine_name):
    globals()["upgrade_alltenants"]()


def downgrade(engine_name):
    globals()["downgrade_alltenants"]()


def upgrade_alltenants():
    # nullability mirrors the model metadata exactly (sa_column nullable flags in
    # models_node_telemetry; plain str fields with defaults are NOT NULL), so
    # `make check` stays drift-free.
    op.create_table(
        'nodelog',
        sa.Column('id',        sa.BigInteger(),                    nullable=False, autoincrement=True),
        sa.Column('node_id',   sa.String(),                        nullable=False),
        sa.Column('source',    sa.String(),                        nullable=False),
        sa.Column('ts',        sa.DateTime(),                      nullable=False),
        sa.Column('line',      sa.String(),                        nullable=False),
        sa.Column('ingest_ts', sa.DateTime(),                      nullable=False),
        sa.Column('tenant_id', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('site_id',   sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_nodelog_node_ts', 'nodelog', ['node_id', 'ts'])
    op.create_index('ix_nodelog_node_source', 'nodelog', ['node_id', 'source'])

    op.create_table(
        'nodemetric',
        sa.Column('id',              sa.BigInteger(),                    nullable=False, autoincrement=True),
        sa.Column('node_id',         sa.String(),                        nullable=False),
        sa.Column('ts',              sa.DateTime(),                      nullable=False),
        sa.Column('load1',           sa.Float(),                         nullable=True),
        sa.Column('cpu_count',       sa.Integer(),                       nullable=True),
        sa.Column('mem_used_bytes',  sa.BigInteger(),                    nullable=True),
        sa.Column('mem_total_bytes', sa.BigInteger(),                    nullable=True),
        sa.Column('root_disk_pct',   sa.Float(),                         nullable=True),
        sa.Column('docker_running',  sa.Integer(),                       nullable=True),
        sa.Column('docker_total',    sa.Integer(),                       nullable=True),
        sa.Column('k8s_running',     sa.Integer(),                       nullable=True),
        sa.Column('k8s_total',       sa.Integer(),                       nullable=True),
        sa.Column('tenant_id',       sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('site_id',         sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('node_id', 'ts', name='uq_nodemetric_node_ts'),
    )
    op.create_index('ix_nodemetric_node_ts', 'nodemetric', ['node_id', 'ts'])


def downgrade_alltenants():
    op.drop_index('ix_nodemetric_node_ts', table_name='nodemetric')
    op.drop_table('nodemetric')
    op.drop_index('ix_nodelog_node_source', table_name='nodelog')
    op.drop_index('ix_nodelog_node_ts', table_name='nodelog')
    op.drop_table('nodelog')
