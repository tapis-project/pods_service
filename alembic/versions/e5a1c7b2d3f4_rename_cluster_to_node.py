"""rename cluster table to node (nodes terminology — registry entry maps 1:1 to a tailnet node)

Revision ID: e5a1c7b2d3f4
Revises: c4d8f2a91e63
Create Date: 2026-07-29

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'e5a1c7b2d3f4'
down_revision = 'c4d8f2a91e63'
branch_labels = None
depends_on = None


def upgrade(engine_name):
    globals()["upgrade_alltenants"]()


def downgrade(engine_name):
    globals()["downgrade_alltenants"]()


def upgrade_alltenants():
    op.rename_table('cluster', 'node')
    op.alter_column('node', 'cluster_id', new_column_name='node_id')


def downgrade_alltenants():
    op.alter_column('node', 'node_id', new_column_name='cluster_id')
    op.rename_table('node', 'cluster')
