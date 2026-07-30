"""node command dispatcher (v1) — nodecommand table: one-shot user-queued commands
delivered via the agent's GET /commands poll; first consumer is the bench suite.

Revision ID: d1f4a8c3e6b2
Revises: c9e5f2a7b8d1
Create Date: 2026-07-31

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel              ##### Required when using sqlmodel and not use sqlalchemy
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = 'd1f4a8c3e6b2'
down_revision = 'c9e5f2a7b8d1'
branch_labels = None
depends_on = None


def upgrade(engine_name):
    globals()["upgrade_alltenants"]()


def downgrade(engine_name):
    globals()["downgrade_alltenants"]()


def upgrade_alltenants():
    op.create_table(
        'nodecommand',
        sa.Column('command_id',   sa.String(),                        nullable=False),
        sa.Column('node_id',      sa.String(),                        nullable=False),
        sa.Column('type',         sa.String(),                        nullable=False),
        sa.Column('params',       sa.JSON(),                          nullable=True),
        sa.Column('status',       sa.String(),                        nullable=False),
        sa.Column('result',       sa.JSON(),                          nullable=True),
        sa.Column('requested_by', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('created_ts',   sa.DateTime(),                      nullable=True),
        sa.Column('delivered_ts', sa.DateTime(),                      nullable=True),
        sa.Column('completed_ts', sa.DateTime(),                      nullable=True),
        sa.Column('tenant_id',    sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('site_id',      sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint('command_id'),
    )
    op.create_index('ix_nodecommand_node_status', 'nodecommand', ['node_id', 'status'])


def downgrade_alltenants():
    op.drop_index('ix_nodecommand_node_status', table_name='nodecommand')
    op.drop_table('nodecommand')
