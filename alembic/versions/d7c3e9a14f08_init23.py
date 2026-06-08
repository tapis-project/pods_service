"""init23 — stack templates: templatetag.kind/stack_definition + stack.secret_map/from_template

Revision ID: d7c3e9a14f08
Revises: 19b287a40aa6
Create Date: 2026-06-08 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel              ##### Required when using sqlmodel and not use sqlalchemy
from sqlalchemy.dialects import postgresql

revision     = 'd7c3e9a14f08'
down_revision = '19b287a40aa6'
branch_labels = None
depends_on    = None


def upgrade(engine_name):
    globals()["upgrade_alltenants"]()


def downgrade(engine_name):
    globals()["downgrade_alltenants"]()


def upgrade_alltenants():
    # Template tags can now describe a whole stack (kind='stack' + stack_definition).
    op.add_column('templatetag', sa.Column('kind', sqlmodel.sql.sqltypes.AutoString(), nullable=True, server_default='pod'))
    op.add_column('templatetag', sa.Column('stack_definition', sa.JSON(), nullable=True))
    # Stacks instantiated from a template carry shared secrets (single source for ${stack:secrets:KEY})
    # and provenance.
    op.add_column('stack', sa.Column('secret_map', sa.JSON(), nullable=True, server_default='{}'))
    op.add_column('stack', sa.Column('from_template', sqlmodel.sql.sqltypes.AutoString(), nullable=True, server_default=''))


def downgrade_alltenants():
    op.drop_column('stack', 'from_template')
    op.drop_column('stack', 'secret_map')
    op.drop_column('templatetag', 'stack_definition')
    op.drop_column('templatetag', 'kind')
