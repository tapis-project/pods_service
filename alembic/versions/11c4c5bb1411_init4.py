"""init4

Revision ID: 11c4c5bb1411
Revises: 1249c846b8f9
Create Date: 2022-10-18 13:29:42.277802

"""
from alembic import op
import sqlalchemy as sa
import sqlmodel              ##### Required when using sqlmodel and not use sqlalchemy


# revision identifiers, used by Alembic.
revision = '11c4c5bb1411'
down_revision = '1249c846b8f9'
branch_labels = None
depends_on = None


def upgrade(engine_name):
    globals()["upgrade_alltenants"]()


def downgrade(engine_name):
    globals()["downgrade_alltenants"]()




def upgrade_alltenants():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('pod', sa.Column('command', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    # ### end Alembic commands ###


def downgrade_alltenants():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('pod', 'command')
    # ### end Alembic commands ###