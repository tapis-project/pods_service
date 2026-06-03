"""init19 — templategallery table

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-05-21 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel

revision     = 'e3f4a5b6c7d8'
down_revision = 'd2e3f4a5b6c7'
branch_labels = None
depends_on    = None


def upgrade(engine_name):
    globals()["upgrade_alltenants"]()


def downgrade(engine_name):
    globals()["downgrade_alltenants"]()


def upgrade_alltenants():
    op.create_table(
        'templategallery',
        sa.Column('id',            sa.Integer(),    nullable=False, autoincrement=True),
        sa.Column('template_id',   sa.String(),     nullable=False),
        sa.Column('tenant_id',     sa.String(),     nullable=False),
        sa.Column('site_id',       sa.String(),     nullable=False),
        sa.Column('markdown_note', sa.Text(),        nullable=True),
        sa.Column('photo_1',       sa.LargeBinary(), nullable=True),
        sa.Column('photo_1_mime',  sa.String(),     nullable=True),
        sa.Column('photo_2',       sa.LargeBinary(), nullable=True),
        sa.Column('photo_2_mime',  sa.String(),     nullable=True),
        sa.Column('photo_3',       sa.LargeBinary(), nullable=True),
        sa.Column('photo_3_mime',  sa.String(),     nullable=True),
        sa.Column('updated_at',    sa.DateTime(),   nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_templategallery_id',          'templategallery', ['id'],          unique=True)
    op.create_index('ix_templategallery_template_id', 'templategallery', ['template_id'])
    op.create_index('ix_templategallery_tenant_id',   'templategallery', ['tenant_id'])
    op.create_index('ix_templategallery_tpl_tenant',  'templategallery', ['template_id', 'tenant_id'])


def downgrade_alltenants():
    op.drop_index('ix_templategallery_tpl_tenant',  table_name='templategallery')
    op.drop_index('ix_templategallery_tenant_id',   table_name='templategallery')
    op.drop_index('ix_templategallery_template_id', table_name='templategallery')
    op.drop_index('ix_templategallery_id',          table_name='templategallery')
    op.drop_table('templategallery')
