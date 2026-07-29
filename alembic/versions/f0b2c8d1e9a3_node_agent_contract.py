"""node agent contract — HTTP-only edges: drop direct pg/rabbit provisioning columns,
add claim/agent token hashes, capabilities, checkin + hash-gated inventory fields

Revision ID: f0b2c8d1e9a3
Revises: e5a1c7b2d3f4
Create Date: 2026-07-29

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel              ##### Required when using sqlmodel and not use sqlalchemy
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'f0b2c8d1e9a3'
down_revision = 'e5a1c7b2d3f4'
branch_labels = None
depends_on = None


def upgrade(engine_name):
    globals()["upgrade_alltenants"]()


def downgrade(engine_name):
    globals()["downgrade_alltenants"]()


def upgrade_alltenants():
    # Edges speak HTTP only — no direct Postgres/RabbitMQ access, so the per-node
    # db/rabbit provisioning metadata goes away entirely. k8config/k8username die with
    # the central-connects-out model (agents connect in). last_bootstrap*/last_connected
    # are superseded by claimed_at/last_checkin_ts.
    for col in ('db_username', 'db_secret_ref', 'rabbit_username', 'rabbit_vhost',
                'rabbit_secret_ref', 'vector_writer_role', 'ts_stats',
                'last_bootstrap', 'last_bootstrap_ip', 'last_connected',
                'k8config', 'k8username'):
        op.drop_column('node', col)

    op.add_column('node', sa.Column('login_server', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('node', sa.Column('capabilities', postgresql.ARRAY(sa.String()), nullable=True))
    op.add_column('node', sa.Column('agent_version', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('node', sa.Column('inventory', sa.JSON(), nullable=True))
    op.add_column('node', sa.Column('inventory_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('node', sa.Column('last_checkin_ts', sa.DateTime(), nullable=True))
    op.add_column('node', sa.Column('claimed_at', sa.DateTime(), nullable=True))
    op.add_column('node', sa.Column('claim_token_expires', sa.DateTime(), nullable=True))
    op.add_column('node', sa.Column('claim_token_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('node', sa.Column('agent_token_hash', sqlmodel.sql.sqltypes.AutoString(), nullable=True))


def downgrade_alltenants():
    for col in ('login_server', 'capabilities', 'agent_version', 'inventory',
                'inventory_hash', 'last_checkin_ts', 'claimed_at',
                'claim_token_expires', 'claim_token_hash', 'agent_token_hash'):
        op.drop_column('node', col)

    op.add_column('node', sa.Column('db_username', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('node', sa.Column('db_secret_ref', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('node', sa.Column('rabbit_username', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('node', sa.Column('rabbit_vhost', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('node', sa.Column('rabbit_secret_ref', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('node', sa.Column('vector_writer_role', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('node', sa.Column('ts_stats', postgresql.ARRAY(sa.String()), nullable=True))
    op.add_column('node', sa.Column('last_bootstrap', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('node', sa.Column('last_bootstrap_ip', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.add_column('node', sa.Column('last_connected', sa.DateTime(), nullable=True))
    op.add_column('node', sa.Column('k8config', sa.JSON(), nullable=True))
    op.add_column('node', sa.Column('k8username', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
